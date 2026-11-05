package com.sensemind.app.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.sensemind.app.BuildConfig
import com.sensemind.app.data.AlertDto
import com.sensemind.app.data.ApiException
import com.sensemind.app.data.ApiRepository
import com.sensemind.app.data.DemoMode
import com.sensemind.app.data.DemoRepository
import com.sensemind.app.data.EventDetailDto
import com.sensemind.app.data.EventDto
import com.sensemind.app.data.HealthDto
import com.sensemind.app.data.MeResponse
import com.sensemind.app.data.MeasurementDto
import com.sensemind.app.data.Role
import com.sensemind.app.data.SenseMindApi
import com.sensemind.app.data.SenseMindRepository
import com.sensemind.app.data.Session
import com.sensemind.app.data.SessionStore
import com.sensemind.app.data.SubjectDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class AppUiState(
    val session: Session? = null,
    val busy: Boolean = false,
    val error: String? = null,
    val notice: String? = null,
    val health: HealthDto? = null,
    val subjects: List<SubjectDto> = emptyList(),
    val events: List<EventDto> = emptyList(),
    val alerts: List<AlertDto> = emptyList(),
    val me: MeResponse? = null,
    val remoteAlertsGranted: Boolean = false,
    val wearerReadings: Map<String, List<MeasurementDto>> = emptyMap(),
    val selectedSubject: SubjectDto? = null,
    val subjectEvents: List<EventDto> = emptyList(),
    val measurements: List<MeasurementDto> = emptyList(),
    val eventDetail: EventDetailDto? = null,
) {
    val role: Role get() = session?.role ?: Role.LEGACY
    val isDemo: Boolean get() = session?.demoMode != null
}

class AppViewModel(application: Application) : AndroidViewModel(application) {
    private val store = SessionStore(application)
    private val api = SenseMindApi()
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _state = MutableStateFlow(AppUiState(session = store.load()))
    val state: StateFlow<AppUiState> = _state.asStateFlow()

    private var repository: SenseMindRepository? = null

    init {
        val session = store.load()
        if (session != null) {
            repository = buildRepository(session)
            refresh()
        }
    }

    private fun buildRepository(session: Session): SenseMindRepository =
        session.demoMode?.let { DemoRepository(it) }
            ?: ApiRepository(api, session.baseUrl, session.token)

    fun pair(baseUrl: String, code: String) {
        val normalized = api.normalizeBaseUrl(baseUrl)
        if (normalized.isBlank()) {
            _state.update { it.copy(error = "请填写后端地址") }
            return
        }
        if (!BuildConfig.DEBUG && !normalized.startsWith("https://")) {
            _state.update { it.copy(error = "正式版只允许 HTTPS 后端；开发版可用局域网 HTTP") }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, notice = null) }
            try {
                val pair = withContext(Dispatchers.IO) { api.exchange(normalized, code.trim()) }
                val session = Session(
                    token = pair.token,
                    guardianId = pair.guardian_id,
                    familyId = pair.family_id,
                    name = pair.name.ifBlank { "SenseMind 用户" },
                    role = Role.parse(pair.role),
                    baseUrl = normalized,
                    demoMode = null,
                )
                repository = buildRepository(session)
                store.save(session)
                _state.update { it.copy(session = session, busy = false) }
                refresh()
            } catch (error: ApiException) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            } catch (error: Exception) {
                _state.update { it.copy(busy = false, error = "网络错误：${error.message ?: "无法连接后端"}") }
            }
        }
    }

    fun startDemo(role: Role, mode: DemoMode) {
        if (!BuildConfig.ALLOW_DEMO) {
            _state.update { it.copy(error = "正式版不启用演示数据") }
            return
        }
        val session = Session(
            token = "demo-token",
            guardianId = "demo-guardian",
            familyId = "demo-family",
            name = "演示${role.label}",
            role = role,
            baseUrl = "demo://local",
            demoMode = mode,
        )
        repository = DemoRepository(mode)
        session.let(store::save)
        _state.update { it.copy(session = session, error = null, notice = null) }
        refresh()
    }

    fun refresh() {
        val session = _state.value.session ?: return
        val repo = repository ?: return
        viewModelScope.launch {
            try {
                val health = withContext(Dispatchers.IO) { repo.health() }
                val me = withContext(Dispatchers.IO) { repo.me() }
                // The server is the source of truth. Do not call /v1/alerts
                // before this credential has explicitly granted remote_alerts.
                val remoteAlerts = session.demoMode != null ||
                    me.consents["remote_alerts"] == true
                if (session.role == Role.WEARER) {
                    val readings = me.devices.take(4).associate { device ->
                        device.id to runCatching {
                            withContext(Dispatchers.IO) { repo.measurements(device.id, 1) }
                        }.getOrDefault(emptyList())
                    }
                    val alerts = if (remoteAlerts) {
                        runCatching { withContext(Dispatchers.IO) { repo.alerts() } }
                            .getOrDefault(emptyList())
                    } else emptyList()
                    _state.update {
                        it.copy(health = health, me = me, wearerReadings = readings,
                            alerts = alerts, remoteAlertsGranted = remoteAlerts,
                            error = null)
                    }
                } else {
                    val subjects = withContext(Dispatchers.IO) { repo.subjects() }
                    val events = withContext(Dispatchers.IO) { repo.events() }
                    val alerts = if (remoteAlerts) {
                        runCatching { withContext(Dispatchers.IO) { repo.alerts() } }
                            .getOrDefault(emptyList())
                    } else emptyList()
                    val selected = _state.value.selectedSubject?.let { current ->
                        subjects.firstOrNull { it.device_id == current.device_id }
                    }
                    _state.update {
                        it.copy(health = health, me = me, subjects = subjects, events = events,
                            alerts = alerts, remoteAlertsGranted = remoteAlerts,
                            selectedSubject = selected,
                            subjectEvents = if (selected == null) emptyList()
                                else events.filter { event -> event.device_id == selected.device_id },
                            error = null)
                    }
                }
            } catch (error: ApiException) {
                _state.update { it.copy(error = friendly(error)) }
            } catch (error: Exception) {
                _state.update {
                    it.copy(error = "网络中断或设备离线：${error.message ?: "无法刷新"}")
                }
            }
        }
    }

    fun selectSubject(subject: SubjectDto) {
        _state.update { it.copy(selectedSubject = subject, eventDetail = null) }
        val repo = repository ?: return
        viewModelScope.launch {
            val measurements = withContext(Dispatchers.IO) {
                runCatching { repo.measurements(subject.device_id, 24) }.getOrDefault(emptyList())
            }
            _state.update {
                it.copy(measurements = measurements,
                    subjectEvents = it.events.filter { event ->
                        event.device_id == subject.device_id
                    })
            }
        }
    }

    fun clearSubject() {
        _state.update { it.copy(selectedSubject = null, measurements = emptyList(),
            subjectEvents = emptyList(), eventDetail = null) }
    }

    fun openEvent(eventId: String) {
        val repo = repository ?: return
        viewModelScope.launch {
            _state.update { it.copy(busy = true) }
            try {
                val detail = withContext(Dispatchers.IO) { repo.eventDetail(eventId) }
                _state.update { it.copy(eventDetail = detail, busy = false) }
            } catch (error: ApiException) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            } catch (error: Exception) {
                _state.update { it.copy(busy = false,
                    error = "事件详情加载失败：${error.message ?: "网络中断"}") }
            }
        }
    }

    fun closeEvent() {
        _state.update { it.copy(eventDetail = null) }
    }

    fun acknowledge(eventId: String) {
        val repo = repository ?: return
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            try {
                val result = withContext(Dispatchers.IO) { repo.acknowledge(eventId) }
                _state.update {
                    it.copy(busy = false,
                        notice = if (result.already_acknowledged) "该事件已由人工确认"
                        else "已由 ${result.acknowledged_by ?: "人工"} 确认")
                }
                openEvent(eventId)
                refresh()
            } catch (error: ApiException) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            } catch (error: Exception) {
                _state.update { it.copy(busy = false, error = "确认失败：网络中断") }
            }
        }
    }

    /**
     * Upload an H1 hardware packet. Only a server response with accepted=true
     * may cause the BLE layer to ACK the firmware frame.
     */
    suspend fun uploadHardwarePacket(packetJson: String): Boolean {
        val repo = repository ?: return false
        if (repo.isDemo) {
            _state.update { it.copy(error = "演示模式不会上传硬件包，也不会 ACK 固件") }
            return false
        }
        return try {
            val result = withContext(Dispatchers.IO) { repo.uploadHardwarePacket(packetJson) }
            val accepted = result.jsonObject["accepted"]?.jsonPrimitive?.booleanOrNull == true
            if (!accepted) {
                _state.update { it.copy(error = "后端未确认持久化，未发送 ACK") }
            }
            accepted
        } catch (error: ApiException) {
            val message = if (error.status == 404) {
                "后端未部署 H1 /v1/hardware/packets；原始数据未上传，未发送 ACK"
            } else friendly(error)
            _state.update { it.copy(error = message) }
            false
        } catch (error: Exception) {
            _state.update { it.copy(error = "上传失败：${error.message ?: "网络中断"}") }
            false
        }
    }

    fun registerWearerDevice() {
        val repo = repository ?: return
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, notice = null) }
            try {
                withContext(Dispatchers.IO) { repo.setConsent("upload", true) }
                val id = withContext(Dispatchers.IO) {
                    repo.registerDevice("SenseMind Band", "0.7.0")
                }
                _state.update {
                    it.copy(busy = false, notice = "已注册设备 $id；等待 live_device 数据上传")
                }
                refresh()
            } catch (error: ApiException) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            } catch (error: Exception) {
                _state.update { it.copy(busy = false,
                    error = "注册设备失败：${error.message ?: "网络中断"}") }
            }
        }
    }

    /**
     * Explicit caregiver consent for push/notification data. Revocation uses
     * the same endpoint with granted=false and takes effect immediately: the
     * next refresh stops calling /v1/alerts and clears any cached alerts.
     */
    fun setRemoteAlerts(granted: Boolean) {
        val session = _state.value.session ?: return
        val repo = repository ?: return
        if (session.demoMode != null) {
            _state.update {
                it.copy(notice = "演示模式使用本地数据，不写入后端授权")
            }
            return
        }
        if (session.role == Role.WEARER) {
            _state.update {
                it.copy(error = "佩戴者角色不能授予 remote_alerts；请由照护者凭证操作")
            }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, notice = null) }
            try {
                withContext(Dispatchers.IO) { repo.setConsent("remote_alerts", granted) }
                _state.update {
                    it.copy(
                        busy = false,
                        remoteAlertsGranted = granted,
                        alerts = if (granted) it.alerts else emptyList(),
                        notice = if (granted) "已授予 remote_alerts 同意；开始读取被授权告警"
                        else "已撤销 remote_alerts 同意；立即停止告警轮询")
                }
                refresh()
            } catch (error: ApiException) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            } catch (error: Exception) {
                _state.update {
                    it.copy(busy = false,
                        error = "授权操作失败：${error.message ?: "网络中断"}")
                }
            }
        }
    }

    fun logout() {
        val session = _state.value.session
        val repo = repository
        viewModelScope.launch {
            if (session != null && session.demoMode == null && repo != null) {
                runCatching { withContext(Dispatchers.IO) { repo.revoke() } }
            }
            store.clear()
            repository = null
            _state.value = AppUiState()
        }
    }

    fun clearMessages() {
        _state.update { it.copy(error = null, notice = null) }
    }

    private fun friendly(error: ApiException): String = when (error.code) {
        "authentication_required", "invalid_credential" -> "凭证无效或已过期，请重新配对"
        "role_forbidden" -> "当前角色无权执行此操作：${error.message}"
        "consent_required" -> "需要在设置中授予明确同意后才能继续"
        "device_not_found" -> "该设备不属于当前家庭或已被移除"
        "event_not_found" -> "事件不存在或已不可访问"
        "invalid_pairing_code" -> "配对码无效、已使用或已过期"
        "too_many_attempts" -> "配对尝试过多，请稍后再试"
        else -> "${error.status} ${error.message}"
    }
}
