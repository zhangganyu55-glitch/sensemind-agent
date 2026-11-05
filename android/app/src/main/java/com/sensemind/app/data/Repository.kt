package com.sensemind.app.data

import java.time.Instant
import java.time.temporal.ChronoUnit
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

interface SenseMindRepository {
    val demoMode: DemoMode?
    val isDemo: Boolean get() = demoMode != null

    suspend fun health(): HealthDto
    suspend fun me(): MeResponse
    suspend fun subjects(): List<SubjectDto>
    suspend fun events(deviceId: String? = null): List<EventDto>
    suspend fun eventDetail(eventId: String): EventDetailDto
    suspend fun acknowledge(eventId: String): AckResponse
    suspend fun measurements(deviceId: String, hours: Long = 24): List<MeasurementDto>
    suspend fun alerts(): List<AlertDto>
    suspend fun revoke()
    suspend fun uploadHardwarePacket(packetJson: String): JsonElement
    suspend fun registerDevice(model: String, firmware: String): String
    suspend fun setConsent(scope: String, granted: Boolean)
}

class ApiRepository(
    private val api: SenseMindApi,
    private val baseUrl: String,
    private val token: String,
) : SenseMindRepository {
    override val demoMode: DemoMode? = null
    private val root = baseUrl.trimEnd('/')

    override suspend fun health(): HealthDto = api.health(root)
    override suspend fun me(): MeResponse = api.me(root, token)
    override suspend fun subjects(): List<SubjectDto> = api.subjects(root, token).data
    override suspend fun events(deviceId: String?): List<EventDto> =
        api.events(root, token, deviceId).data

    override suspend fun eventDetail(eventId: String): EventDetailDto =
        api.eventDetail(root, token, eventId)

    override suspend fun acknowledge(eventId: String): AckResponse =
        api.acknowledge(root, token, eventId)

    override suspend fun measurements(deviceId: String, hours: Long): List<MeasurementDto> {
        val to = Instant.now()
        val from = to.minus(hours, ChronoUnit.HOURS)
        return api.measurements(root, token, deviceId, from.toString(), to.toString()).data
    }

    override suspend fun alerts(): List<AlertDto> = api.alerts(root, token).data
    override suspend fun revoke() {
        api.revoke(root, token)
    }

    override suspend fun uploadHardwarePacket(packetJson: String): JsonElement =
        api.postHardwarePacket(root, token, packetJson)

    override suspend fun registerDevice(model: String, firmware: String): String {
        val localId = java.util.UUID.randomUUID().toString()
        return api.registerDevice(root, token, localId, model, firmware, "1")["device_id"]
            ?.let { it as? kotlinx.serialization.json.JsonPrimitive }
            ?.content ?: localId
    }

    override suspend fun setConsent(scope: String, granted: Boolean) {
        api.setConsent(root, token, scope, granted)
    }
}

/**
 * Explicitly non-live fixture repository for UI development. Every subject and
 * evidence record is marked REPLAY or SYNTHETIC; nothing returns Live.
 */
class DemoRepository(override val demoMode: DemoMode) : SenseMindRepository {
    private val deviceId = "demo-band-01"
    private val synthetic = true
    private val mode = "replay"

    override suspend fun health(): HealthDto =
        HealthDto(ok = true, version = "demo", apk_contract = "v1")

    override suspend fun me(): MeResponse = MeResponse(
        guardian_id = "demo-guardian", family_id = "demo-family",
        name = "演示用户", role = "caregiver",
        devices = listOf(DeviceDto(id = deviceId, model = "SenseMind Band demo",
            firmware = "v0.7.0-demo", protocol = "1")),
        consents = mapOf("upload" to true, "ai_analysis" to true,
            "remote_alerts" to true))

    override suspend fun subjects(): List<SubjectDto> {
        delay(120)
        return listOf(SubjectDto(
            device_id = deviceId, model = "SenseMind Band demo",
            firmware_version = "v0.7.0-demo",
            latest = LatestTelemetryDto(
                timestamp = Instant.now().toString(), source = "simulator",
                data_mode = mode, heart_rate = 82.0, spo2 = 97.0,
                acc_x = 0.04, acc_y = -0.02, acc_z = 1.01,
                signal_quality = "good", reading_valid = true,
                dataset = if (demoMode == DemoMode.REPLAY) "demo-replay" else null),
            data_mode = mode, stale = false, age_seconds = 2.0,
            active_event_count = 1, unacknowledged_event_count = 1,
            latest_investigation = demoInvestigationSummary(),
        ))
    }

    override suspend fun events(deviceId: String?): List<EventDto> {
        delay(100)
        val now = Instant.now()
        return listOf(
            EventDto(
                id = "demo-event-active", device_id = this.deviceId,
                kind = "high_heart_rate", status = "active",
                opened_at = now.minus(2, ChronoUnit.MINUTES).toString(),
                acknowledged = false, note = "演示事件：不是真实测量",
                investigation = demoInvestigationSummary()),
            EventDto(
                id = "demo-event-resolved", device_id = this.deviceId,
                kind = "data_quality", status = "resolved",
                opened_at = now.minus(3, ChronoUnit.HOURS).toString(),
                closed_at = now.minus(170, ChronoUnit.MINUTES).toString(),
                acknowledged = true, acknowledged_by = "演示照护者",
                acknowledged_at = now.minus(165, ChronoUnit.MINUTES).toString(),
                note = "演示事件：不是真实测量", investigation = null),
        )
    }

    override suspend fun eventDetail(eventId: String): EventDetailDto {
        delay(120)
        val events = events(null)
        val event = events.firstOrNull { it.id == eventId } ?: events.first()
        return EventDetailDto(
            event = event,
            device = buildJsonObject {
                put("id", deviceId)
                put("model", "SenseMind Band demo")
            },
            investigation = buildJsonObject {
                put("id", "demo-investigation")
                put("status", "completed")
                put("ai_status", "completed")
                put("model", "deepseek-flash-demo")
                put("evidence_count", 5)
                put("attempts", 1)
                put("max_attempts", 2)
                put("structured", buildJsonObject {
                    put("investigation_status", "inconclusive")
                    put("conclusion", "演示数据不足以得出结论；需要人工复核。")
                })
            },
            evidence = demoEvidence(),
            transitions = listOf(
                TransitionDto(from_status = "queued", to_status = "running",
                    reason = "worker_started", created_at = Instant.now().minus(110, ChronoUnit.SECONDS).toString()),
                TransitionDto(from_status = "running", to_status = "completed",
                    reason = "worker_finished", created_at = Instant.now().minus(100, ChronoUnit.SECONDS).toString()),
            ),
        )
    }

    override suspend fun acknowledge(eventId: String): AckResponse = AckResponse(
        acknowledged = true, event_id = eventId,
        acknowledged_at = Instant.now().toString(),
        acknowledged_by = "演示照护者", already_acknowledged = false)

    override suspend fun measurements(deviceId: String, hours: Long): List<MeasurementDto> {
        delay(100)
        val now = Instant.now()
        return (0 until 24).flatMap { index ->
            val at = now.minus((23 - index) * 5L, ChronoUnit.MINUTES)
            listOf(
                MeasurementDto(sample_id = "demo-hr-$index", device_id = this.deviceId,
                    metric = "hr", value = (72 + (index % 7) * 2).toDouble(), unit = "bpm",
                    observed_at = at.toString(), source = "replay", quality = "good"),
                MeasurementDto(sample_id = "demo-spo2-$index", device_id = this.deviceId,
                    metric = "spo2", value = (96 + (index % 3)).toDouble(), unit = "%",
                    observed_at = at.toString(), source = "replay", quality = "good"),
            )
        }
    }

    override suspend fun alerts(): List<AlertDto> = listOf(
        AlertDto(id = "demo-alert", device_id = deviceId, kind = "high_heart_rate",
            text = "演示告警：心率偏高（合成数据）",
            created_at = Instant.now().minus(2, ChronoUnit.MINUTES).toString(),
            evidence = buildJsonObject { put("demo", true); put("synthetic", true) }))

    override suspend fun revoke() = Unit

    override suspend fun uploadHardwarePacket(packetJson: String): JsonElement {
        throw ApiException("demo_mode", "演示模式不会上传硬件包", 400)
    }

    override suspend fun registerDevice(model: String, firmware: String): String = deviceId

    override suspend fun setConsent(scope: String, granted: Boolean) = Unit

    private fun demoInvestigationSummary() = InvestigationSummaryDto(
        id = "demo-investigation", event_id = "demo-event-active",
        device_id = deviceId, kind = "high_heart_rate", status = "completed",
        ai_status = "completed", model = "deepseek-flash-demo",
        evidence_count = 5, attempts = 1, max_attempts = 2)

    private fun demoEvidence(): List<EvidenceDto> = listOf(
        EvidenceDto(evidence_id = "demo-ev-hr", device_id = deviceId,
            source_type = "heart_rate_estimate", value = kotlinx.serialization.json.JsonPrimitive(82.0),
            unit = "bpm", quality_status = "good", data_mode = mode, synthetic = synthetic,
            timestamp = Instant.now().toString(), time_sync_status = "gateway_estimated",
            clock_uncertainty_ms = 1000),
        EvidenceDto(evidence_id = "demo-ev-quality", device_id = deviceId,
            source_type = "signal_quality", value = kotlinx.serialization.json.JsonPrimitive("good"),
            quality_status = "good", data_mode = mode, synthetic = synthetic,
            timestamp = Instant.now().toString()),
        EvidenceDto(evidence_id = "demo-ev-activity", device_id = deviceId,
            source_type = "activity", value = kotlinx.serialization.json.JsonPrimitive("standing"),
            quality_status = "good", data_mode = mode, synthetic = synthetic,
            timestamp = Instant.now().toString(), model_version = "pamap2-rf-demo"),
        EvidenceDto(evidence_id = "demo-ev-imu", device_id = deviceId,
            source_type = "imu_raw", value = buildJsonArray {
                add(kotlinx.serialization.json.JsonPrimitive(0.04))
                add(kotlinx.serialization.json.JsonPrimitive(-0.02))
                add(kotlinx.serialization.json.JsonPrimitive(1.01))
            }, unit = "g", quality_status = "good", data_mode = mode, synthetic = synthetic,
            timestamp = Instant.now().toString()),
        EvidenceDto(evidence_id = "demo-ev-online", device_id = deviceId,
            source_type = "device_online", value = kotlinx.serialization.json.JsonPrimitive(true),
            quality_status = "good", data_mode = mode, synthetic = synthetic,
            timestamp = Instant.now().toString()),
    )
}
