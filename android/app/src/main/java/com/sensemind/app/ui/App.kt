package com.sensemind.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.sensemind.app.BuildConfig
import com.sensemind.app.data.DemoMode
import com.sensemind.app.data.EventDto
import com.sensemind.app.data.Role
import com.sensemind.app.data.SubjectDto
import kotlinx.coroutines.delay

@Composable
fun SenseMindApp(viewModel: AppViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    SenseMindTheme {
        ScreenBackground {
            if (state.session == null) {
                PairingScreen(state = state, onPair = viewModel::pair,
                    onDemo = viewModel::startDemo, onClearMessages = viewModel::clearMessages)
            } else {
                LaunchedEffect(state.session?.token) {
                    while (true) {
                        delay(if (state.isDemo) 10_000 else 5_000)
                        viewModel.refresh()
                    }
                }
                if (state.role == Role.WEARER) {
                    WearerShell(state, viewModel)
                } else {
                    CaregiverShell(state, viewModel)
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PairingScreen(
    state: AppUiState,
    onPair: (String, String) -> Unit,
    onDemo: (Role, DemoMode) -> Unit,
    onClearMessages: () -> Unit,
) {
    var baseUrl by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }
    var demoRole by rememberSaveable { mutableStateOf(Role.CAREGIVER) }
    var demoMode by rememberSaveable { mutableStateOf(DemoMode.REPLAY) }

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Spacer(Modifier.height(28.dp)) }
        item {
            Text("知微 SenseMind", style = androidx.compose.material3.MaterialTheme.typography.headlineLarge)
            Text("研究性监测客户端 · 非医疗器械 · 不提供诊断或急救",
                style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp))
        }
        item {
            SectionCard("连接后端", "配对码由 Mac 端本地生成；角色由后台凭证决定，App 不能自选权限") {
                OutlinedTextField(
                    value = baseUrl, onValueChange = { baseUrl = it },
                    label = { Text("后端地址") },
                    placeholder = { Text("http://192.168.1.10:8000 或 https://…") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = code, onValueChange = { code = it },
                    label = { Text("配对码") },
                    placeholder = { Text("12–100 字符，一次性") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(12.dp))
                PrimaryButton("配对并连接", enabled = !state.busy &&
                    baseUrl.isNotBlank() && code.isNotBlank()) {
                    onClearMessages(); onPair(baseUrl, code)
                }
                if (state.busy) {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }
        }
        if (state.error != null) {
            item { ErrorBanner(state.error, onRetry = onClearMessages) }
        }
        if (state.notice != null) {
            item { Text(state.notice, color = Teal) }
        }
        item {
            SectionCard("角色说明", "后端 Bearer 凭证携带角色；旧版凭证只用于兼容") {
                RoleInfo("佩戴者", "设备扫描、BLE 数据接收、数据上传、设备状态；不能查看照护者事件确认接口")
                Spacer(Modifier.height(8.dp))
                RoleInfo("照护者", "被授权监测对象、实时状态、事件通知、AI 调查报告、人工确认、历史记录；不能上传设备数据")
            }
        }
        if (BuildConfig.ALLOW_DEMO) {
            item {
                SectionCard("演示数据（仅开发版）", "明确标记 REPLAY / SYNTHETIC，绝不标记为 Live；不上传、不 ACK 固件") {
                    Text("选择演示角色", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Role.entries.filter { it != Role.LEGACY }.forEach { role ->
                            FilterChip(
                                selected = demoRole == role, onClick = { demoRole = role },
                                label = { Text(role.label) },
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = Teal.copy(alpha = 0.18f),
                                    selectedLabelColor = Teal))
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("选择演示数据模式", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        DemoMode.entries.forEach { mode ->
                            FilterChip(
                                selected = demoMode == mode, onClick = { demoMode = mode },
                                label = { Text(mode.label) },
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = Amber.copy(alpha = 0.16f),
                                    selectedLabelColor = Amber))
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                    PrimaryButton("进入演示 · ${demoRole.label} · ${demoMode.label}") {
                        onClearMessages(); onDemo(demoRole, demoMode)
                    }
                }
            }
        }
        item {
            SectionCard("安全边界", null) {
                Text("• 不把 DeepSeek API Key 写入 APK；只保存后端 Bearer 凭证\n" +
                    "• 开发版允许局域网 HTTP，正式版强制 HTTPS\n" +
                    "• 不把开发服务器直接暴露到公网\n" +
                    "• 客户端只读 AI 调查；人工确认由照护者明确操作\n" +
                    "• 研究性监测数据不展示为医学诊断",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            }
        }
        item { Spacer(Modifier.height(30.dp)) }
    }
}

@Composable
private fun RoleInfo(title: String, detail: String) {
    Column {
        Text(title, color = Cyan, fontWeight = FontWeight.SemiBold)
        Text(detail, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
    }
}

@Composable
fun CaregiverShell(state: AppUiState, viewModel: AppViewModel) {
    if (state.eventDetail != null) {
        EventDetailScreen(state, viewModel)
        return
    }
    if (state.selectedSubject != null) {
        SubjectDetailScreen(state, viewModel)
        return
    }
    var tab by rememberSaveable { mutableIntStateOf(0) }
    Column(Modifier.fillMaxSize()) {
        TopBar(
            title = "照护者 · 监测总览",
            subtitle = "${state.session?.name.orEmpty()} · ${state.role.label}" +
                if (state.isDemo) " · ${state.session?.demoMode?.label}" else "",
            trailing = { TextButton(onClick = viewModel::refresh) { Text("刷新", color = Cyan) } })
        Column(Modifier.weight(1f)) {
            if (state.isDemo) {
                Box(Modifier.padding(horizontal = 20.dp)) { DemoBanner(state.session?.demoMode) }
            }
            if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            when (tab) {
                0 -> CaregiverOverview(state, viewModel)
                1 -> CaregiverEvents(state, viewModel)
                else -> SettingsScreen(state, viewModel)
            }
        }
        TabBar(tab, listOf("总览", "事件", "设置")) { tab = it }
    }
}

@Composable
private fun CaregiverOverview(state: AppUiState, viewModel: AppViewModel) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
        item {
            SectionCard("系统状态", "服务端 / 模型 / 网络") {
                KeyValue("后端版本", state.health?.version ?: "—")
                KeyValue("APK 契约", state.health?.apk_contract ?: "—")
                KeyValue("数据来源", if (state.isDemo) "演示 ${state.session?.demoMode?.label}" else "后端 API")
                KeyValue("事件通知", when {
                    state.isDemo -> "${state.alerts.size} 条（演示数据）"
                    state.remoteAlertsGranted -> "${state.alerts.size} 条（已授权）"
                    else -> "未授权，未轮询 /v1/alerts"
                })
                KeyValue("连接", if (state.error == null) "正常" else "异常")
            }
        }
        item { Text("被授权监测对象", color = Ink, fontWeight = FontWeight.SemiBold,
            modifier = Modifier.padding(top = 6.dp)) }
        if (state.subjects.isEmpty()) {
            item {
                SectionCard("暂无监测对象",
                    "佩戴者需要先用 wearer 角色配对、注册设备并上传 live_device 数据。") {
                    EmptyState("没有被授权监测对象。")
                }
            }
        } else {
            items(state.subjects, key = { it.device_id }) { subject ->
                SubjectCard(subject) { viewModel.selectSubject(subject) }
            }
        }
        item {
            SectionCard("说明", null) {
                Text("设备在线不等于数据有效；REPLAY/模拟数据不会显示为 Live。" +
                    "呼吸与雷达数据在 H1 后端未接入前显示为缺失，不用 0 或推断值填充。",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
private fun SubjectCard(subject: SubjectDto, onClick: () -> Unit) {
    val latest = subject.latest
    Column(
        Modifier.fillMaxWidth()
            .background(CardBg, RoundedCornerShape(14.dp))
            .border(1.dp, Line, RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
            .padding(14.dp)
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically) {
            Text(subject.device_id, color = Ink, fontWeight = FontWeight.SemiBold)
            DataModeChip(subject.data_mode, synthetic = false)
        }
        Text("${subject.model.ifBlank { "设备" }} · ${subject.firmware_version ?: "固件未知"}",
            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            MetricTile("心率", latest?.heart_rate?.let { "%.0f".format(it) } ?: "—", "bpm",
                note = if (subject.stale) "数据过期" else "较新读数",
                accent = if (subject.stale) Coral else Teal, modifier = Modifier.weight(1f))
            MetricTile("SpO₂", latest?.spo2?.let { "%.0f".format(it) } ?: "—", "%",
                note = "原型估算", accent = Cyan, modifier = Modifier.weight(1f))
        }
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Chip(if (subject.stale) "数据过期" else "数据新鲜",
                if (subject.stale) Coral else Teal)
            Chip("活动事件 ${subject.active_event_count}", Amber)
            Chip("待确认 ${subject.unacknowledged_event_count}", Violet)
        }
        subject.latest_investigation?.let { investigation ->
            Text("最新调查：${investigation.status ?: "—"} / AI ${investigation.ai_status ?: "—"}",
                style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 6.dp))
        }
        Text("点击查看实时状态、趋势、历史与调查 →", color = Cyan,
            style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(top = 6.dp))
    }
}

@Composable
private fun CaregiverEvents(state: AppUiState, viewModel: AppViewModel) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
        item {
            SectionCard("事件通知", "告警通知来自 /v1/alerts；调查详情来自服务端 Agent 2.0") {
                if (state.alerts.isEmpty()) EmptyState(
                    when {
                        state.isDemo -> "演示模式暂无告警。"
                        !state.remoteAlertsGranted ->
                            "未授予 remote_alerts 同意，App 未请求 /v1/alerts。可在设置中授权。"
                        else -> "已授权，但当前凭证范围内没有告警。"
                    })
                else state.alerts.take(3).forEach { alert ->
                    Text("${alert.kind} · ${isoToLocal(alert.created_at)}",
                        color = Amber, fontWeight = FontWeight.SemiBold)
                    Text(alert.text, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                }
            }
        }
        item { Text("规则事件", color = Ink, fontWeight = FontWeight.SemiBold) }
        if (state.events.isEmpty()) {
            item { SectionCard("暂无事件", null) { EmptyState("当前没有被授权设备的事件。") } }
        } else {
            items(state.events, key = { it.id }) { event ->
                EventRow(event) { viewModel.openEvent(event.id) }
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
fun EventRow(event: EventDto, onClick: () -> Unit) {
    Column(
        Modifier.fillMaxWidth()
            .background(CardBg, RoundedCornerShape(14.dp))
            .border(1.dp, Line, RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
            .padding(14.dp)
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically) {
            Text(eventKindLabel(event.kind), color = Ink, fontWeight = FontWeight.SemiBold)
            Chip(if (event.status == "active") "活动" else "已解除",
                if (event.status == "active") Amber else Teal)
        }
        Text("${event.device_id} · ${isoToLocal(event.opened_at)}",
            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Chip(if (event.acknowledged) "已人工确认" else "未确认",
                if (event.acknowledged) Teal else Violet)
            event.investigation?.let {
                Chip("调查 ${it.status ?: "—"} / AI ${it.ai_status ?: "—"}",
                    if (it.ai_status == "completed") Cyan else Amber)
            } ?: Chip("无调查记录", Faint)
        }
    }
}

@Composable
fun TabBar(selected: Int, labels: List<String>, onSelect: (Int) -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .background(CardBg)
            .border(1.dp, Line)
            .padding(horizontal = 8.dp, vertical = 6.dp),
        horizontalArrangement = Arrangement.SpaceEvenly,
    ) {
        labels.forEachIndexed { index, label ->
            TextButton(onClick = { onSelect(index) }) {
                Text(label, color = if (selected == index) Teal else Muted,
                    fontWeight = if (selected == index) FontWeight.SemiBold else FontWeight.Normal)
            }
        }
    }
}

fun eventKindLabel(kind: String): String = when (kind) {
    "high_heart_rate" -> "心率偏高（实验规则）"
    "low_spo2" -> "血氧偏低（实验规则）"
    "concurrent_signals" -> "多信号并发（实验规则）"
    "unusual_motion" -> "明显运动（实验规则）"
    "data_quality" -> "数据质量不足（实验规则）"
    else -> kind
}

fun sourceModeLabel(mode: String?): String = when (mode) {
    "live" -> "Live · 硬件来源"
    "replay" -> "Replay · 历史/模拟"
    else -> "模式未知"
}
