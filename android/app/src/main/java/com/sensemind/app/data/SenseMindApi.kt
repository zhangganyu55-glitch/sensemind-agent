package com.sensemind.app.data

import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

class SenseMindApi {
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        explicitNulls = false
    }
    private val mediaType = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .callTimeout(30, TimeUnit.SECONDS)
        .build()

    fun normalizeBaseUrl(value: String): String {
        val trimmed = value.trim().trimEnd('/')
        if (trimmed.isEmpty()) return ""
        return if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) trimmed
        else "http://$trimmed"
    }

    private fun request(
        url: String,
        token: String? = null,
        method: String = "GET",
        body: String? = null,
    ): Request {
        val builder = Request.Builder().url(url)
            .header("Accept", "application/json")
            .header("User-Agent", "SenseMind-Android/0.1")
        if (token != null) builder.header("Authorization", "Bearer $token")
        if (method == "POST") {
            builder.post((body ?: "{}").toRequestBody(mediaType))
        }
        return builder.build()
    }

    private fun execute(request: Request): String {
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                var code = "http_${response.code}"
                var message = text.take(300).ifBlank { "HTTP ${response.code}" }
                try {
                    val obj = json.parseToJsonElement(text).jsonObject
                    val detail = obj["detail"]
                    if (detail is JsonObject) {
                        code = detail["code"]?.jsonPrimitive?.content ?: code
                        message = detail["message"]?.jsonPrimitive?.content ?: message
                    } else if (detail is JsonPrimitive) {
                        message = detail.content
                    } else {
                        code = obj["code"]?.jsonPrimitive?.content ?: code
                        message = obj["message"]?.jsonPrimitive?.content ?: message
                    }
                } catch (_: Exception) {
                    // Keep a bounded, non-secret error message.
                }
                throw ApiException(code, message, response.code)
            }
            return text
        }
    }

    fun health(baseUrl: String): HealthDto =
        json.decodeFromString(execute(request("${baseUrl.trimEnd('/')}/health")))

    fun exchange(baseUrl: String, code: String): PairResponse {
        val payload = buildJsonObject { put("code", code) }.toString()
        return json.decodeFromString(
            execute(request("${baseUrl.trimEnd('/')}/v1/pairings/exchange",
                method = "POST", body = payload)))
    }

    fun me(baseUrl: String, token: String): MeResponse =
        json.decodeFromString(execute(request("${baseUrl.trimEnd('/')}/v1/me", token)))

    fun subjects(baseUrl: String, token: String): SubjectsResponse =
        json.decodeFromString(execute(request("${baseUrl.trimEnd('/')}/v1/subjects", token)))

    fun events(baseUrl: String, token: String, deviceId: String? = null): EventsResponse {
        val suffix = if (deviceId == null) "" else "?device_id=${enc(deviceId)}"
        return json.decodeFromString(
            execute(request("${baseUrl.trimEnd('/')}/v1/caregiver/events$suffix", token)))
    }

    fun eventDetail(baseUrl: String, token: String, eventId: String): EventDetailDto =
        json.decodeFromString(execute(request(
            "${baseUrl.trimEnd('/')}/v1/caregiver/events/${enc(eventId)}", token)))

    fun acknowledge(baseUrl: String, token: String, eventId: String): AckResponse {
        val payload = buildJsonObject { put("confirmed_by", "caregiver-app") }.toString()
        return json.decodeFromString(execute(request(
            "${baseUrl.trimEnd('/')}/v1/caregiver/events/${enc(eventId)}/ack",
            token, "POST", payload)))
    }

    fun measurements(
        baseUrl: String,
        token: String,
        deviceId: String,
        from: String,
        to: String,
        limit: Int = 200,
    ): MeasurementsResponse = json.decodeFromString(execute(request(
        "${baseUrl.trimEnd('/')}/v1/measurements?device_id=${enc(deviceId)}" +
            "&from=${enc(from)}&to=${enc(to)}&limit=$limit", token)))

    fun alerts(baseUrl: String, token: String): AlertsResponse =
        json.decodeFromString(execute(request("${baseUrl.trimEnd('/')}/v1/alerts", token)))

    fun revoke(baseUrl: String, token: String): JsonElement =
        json.parseToJsonElement(execute(request(
            "${baseUrl.trimEnd('/')}/v1/credentials/revoke", token, "POST", "{}")))

    fun setConsent(
        baseUrl: String,
        token: String,
        scope: String,
        granted: Boolean,
    ): JsonElement {
        val payload = buildJsonObject {
            put("scope", scope)
            put("granted", granted)
            put("policy_version", "demo-1")
        }.toString()
        return json.parseToJsonElement(execute(request(
            "${baseUrl.trimEnd('/')}/v1/consents", token, "POST", payload)))
    }

    fun registerDevice(
        baseUrl: String,
        token: String,
        localId: String,
        model: String,
        firmware: String,
        protocol: String = "1",
    ): JsonObject {
        val payload = buildJsonObject {
            put("local_id", localId)
            put("model", model)
            put("firmware_version", firmware)
            put("protocol_version", protocol)
        }.toString()
        return json.parseToJsonElement(execute(request(
            "${baseUrl.trimEnd('/')}/v1/devices", token, "POST", payload))).jsonObject
    }

    /**
     * Frozen H1 hardware packet upload. The main backend does not currently
     * expose this route; callers must surface a 404 as "H1 receiver not deployed"
     * and must not acknowledge the BLE frame as uploaded.
     */
    fun postHardwarePacket(baseUrl: String, token: String, packetJson: String): JsonElement =
        json.parseToJsonElement(execute(request(
            "${baseUrl.trimEnd('/')}/v1/hardware/packets", token, "POST", packetJson)))

    private fun enc(value: String): String =
        java.net.URLEncoder.encode(value, Charsets.UTF_8.name())
}
