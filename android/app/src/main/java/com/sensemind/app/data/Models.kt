package com.sensemind.app.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

enum class Role {
    WEARER, CAREGIVER, LEGACY;

    companion object {
        fun parse(value: String?): Role = when (value?.lowercase()) {
            "wearer" -> WEARER
            "caregiver" -> CAREGIVER
            else -> LEGACY
        }
    }

    val label: String
        get() = when (this) {
            WEARER -> "佩戴者"
            CAREGIVER -> "照护者"
            LEGACY -> "旧版凭证"
        }
}

enum class DemoMode(val wire: String) {
    REPLAY("replay"), SYNTHETIC("synthetic");

    val label: String get() = if (this == REPLAY) "REPLAY 回放" else "SYNTHETIC 合成"
}

data class Session(
    val token: String,
    val guardianId: String,
    val familyId: String,
    val name: String,
    val role: Role,
    val baseUrl: String,
    val demoMode: DemoMode? = null,
)

@Serializable
data class PairResponse(
    val token: String = "",
    val guardian_id: String = "",
    val family_id: String = "",
    val name: String = "",
    val role: String = "legacy",
)

@Serializable
data class DeviceDto(
    val id: String = "",
    val family_id: String = "",
    val local_id: String = "",
    val model: String = "",
    val firmware: String? = null,
    val protocol: String? = null,
    val created_at: String? = null,
)

@Serializable
data class MeResponse(
    val guardian_id: String = "",
    val family_id: String = "",
    val name: String = "",
    val role: String = "legacy",
    val devices: List<DeviceDto> = emptyList(),
    val consents: Map<String, Boolean> = emptyMap(),
)

@Serializable
data class LatestTelemetryDto(
    val timestamp: String? = null,
    val source: String? = null,
    @SerialName("data_mode") val data_mode: String? = null,
    val heart_rate: Double? = null,
    val spo2: Double? = null,
    val acc_x: Double? = null,
    val acc_y: Double? = null,
    val acc_z: Double? = null,
    val signal_quality: String? = null,
    val reading_valid: Boolean? = null,
    val dataset: String? = null,
    @SerialName("participant_id") val participant_id: String? = null,
)

@Serializable
data class InvestigationSummaryDto(
    val id: String? = null,
    val event_id: String? = null,
    val device_id: String? = null,
    val kind: String? = null,
    val status: String? = null,
    val ai_status: String? = null,
    val model: String? = null,
    val created_at: String? = null,
    val started_at: String? = null,
    val completed_at: String? = null,
    val error: String? = null,
    val evidence_count: Int? = null,
    val attempts: Int? = null,
    val max_attempts: Int? = null,
)

@Serializable
data class SubjectDto(
    val device_id: String = "",
    val model: String = "",
    val firmware_version: String? = null,
    val latest: LatestTelemetryDto? = null,
    val data_mode: String? = null,
    val stale: Boolean = true,
    val age_seconds: Double? = null,
    val model_output: JsonElement? = null,
    val active_event_count: Int = 0,
    val unacknowledged_event_count: Int = 0,
    val latest_investigation: InvestigationSummaryDto? = null,
)

@Serializable
data class SubjectsResponse(val data: List<SubjectDto> = emptyList())

@Serializable
data class EventDto(
    val id: String = "",
    val device_id: String = "",
    val kind: String = "",
    val status: String = "",
    val opened_at: String = "",
    val closed_at: String? = null,
    val acknowledged: Boolean = false,
    val acknowledged_at: String? = null,
    val acknowledged_by: String? = null,
    val note: String? = null,
    val investigation: InvestigationSummaryDto? = null,
)

@Serializable
data class EventsResponse(val data: List<EventDto> = emptyList())

@Serializable
data class EvidenceDto(
    val evidence_id: String = "",
    val event_id: String = "",
    val device_id: String = "",
    val source_type: String = "",
    val timestamp: String? = null,
    val value: JsonElement? = null,
    val unit: String? = null,
    val quality_status: String = "",
    val data_mode: String = "",
    val model_version: String? = null,
    val clock_uncertainty_ms: Long? = null,
    val time_sync_status: String = "unknown",
    val synthetic: Boolean = false,
    val source_ref: String = "",
    val metadata: JsonObject? = null,
)

@Serializable
data class TransitionDto(
    val id: String = "",
    val investigation_id: String = "",
    val from_status: String? = null,
    val to_status: String = "",
    val reason: String? = null,
    val created_at: String = "",
)

@Serializable
data class EventDetailDto(
    val event: EventDto = EventDto(),
    val device: JsonObject? = null,
    val investigation: JsonObject? = null,
    val evidence: List<EvidenceDto> = emptyList(),
    val transitions: List<TransitionDto> = emptyList(),
)

@Serializable
data class AckResponse(
    val acknowledged: Boolean = false,
    val event_id: String = "",
    val acknowledged_at: String? = null,
    val acknowledged_by: String? = null,
    val already_acknowledged: Boolean = false,
)

@Serializable
data class MeasurementDto(
    val sample_id: String = "",
    val device_id: String = "",
    val metric: String = "",
    val value: Double = 0.0,
    val unit: String = "",
    val observed_at: String = "",
    val phone_received_at: String? = null,
    val source: String = "",
    val quality: String = "",
)

@Serializable
data class MeasurementsResponse(
    val status: String = "",
    val data: List<MeasurementDto> = emptyList(),
    val next_cursor: String? = null,
)

@Serializable
data class AlertDto(
    val id: String = "",
    val device_id: String = "",
    val kind: String = "",
    val text: String = "",
    val created_at: String = "",
    val evidence: JsonElement? = null,
    val received_at: String? = null,
    val confirmed_at: String? = null,
)

@Serializable
data class AlertsResponse(val data: List<AlertDto> = emptyList())

@Serializable
data class HealthDto(
    val ok: Boolean = false,
    val version: String = "",
    val apk_contract: String = "",
    val assistant: JsonObject? = null,
)

class ApiException(
    val code: String,
    override val message: String,
    val status: Int,
) : Exception(message)
