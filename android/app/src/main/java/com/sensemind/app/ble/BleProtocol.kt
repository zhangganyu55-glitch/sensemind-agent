package com.sensemind.app.ble

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Source-derived BLE transport for the nRF52840 firmware WIP branch
 * (firmware/nrf52840/src/main.c, wire.c). These UUIDs and the 16-byte fragment
 * header / 8-byte ACK are NOT hardware-verified. Do not present a successful
 * compile as a real-device result.
 */
object BleProtocol {
    const val SERVICE_UUID = "5f9b3c10-1d2a-4f6e-9a7b-1c2d3e4f5a60"
    const val DATA_UUID = "5f9b3c11-1d2a-4f6e-9a7b-1c2d3e4f5a60"
    const val ACK_UUID = "5f9b3c12-1d2a-4f6e-9a7b-1c2d3e4f5a60"
    const val HEADER_SIZE = 16
    const val MAX_MESSAGE = 4096
    const val MAX_FRAGMENTS = 1024

    fun crc16CcittFalse(data: ByteArray): Int {
        var crc = 0xffff
        for (byte in data) {
            crc = crc xor ((byte.toInt() and 0xff) shl 8)
            repeat(8) {
                crc = if (crc and 0x8000 != 0) {
                    ((crc shl 1) xor 0x1021) and 0xffff
                } else {
                    (crc shl 1) and 0xffff
                }
            }
        }
        return crc
    }

    fun acknowledgement(messageId: Long, retry: Boolean = false): ByteArray {
        val status = if (retry) 1 else 0
        return byteArrayOf(
            'S'.code.toByte(), 'A'.code.toByte(), 1, status.toByte(),
            (messageId and 0xff).toByte(),
            ((messageId shr 8) and 0xff).toByte(),
            ((messageId shr 16) and 0xff).toByte(),
            ((messageId shr 24) and 0xff).toByte(),
        )
    }

    fun u16(bytes: ByteArray, offset: Int): Int =
        (bytes[offset].toInt() and 0xff) or ((bytes[offset + 1].toInt() and 0xff) shl 8)

    fun u32(bytes: ByteArray, offset: Int): Long =
        (bytes[offset].toLong() and 0xff) or
            ((bytes[offset + 1].toLong() and 0xff) shl 8) or
            ((bytes[offset + 2].toLong() and 0xff) shl 16) or
            ((bytes[offset + 3].toLong() and 0xff) shl 24)
}

data class CompleteMessage(val messageId: Long, val payload: ByteArray)

/**
 * Bounded reassembler mirroring gateways/ble/transport.py. Duplicate fragments
 * with different bytes invalidate the whole message; stale assemblies expire.
 */
class FragmentAssembler(
    private val timeoutMs: Long = 10_000,
    private val maxActive: Int = 4,
    private val clock: () -> Long = { System.nanoTime() / 1_000_000 },
) {
    private data class Assembly(
        val created: Long,
        val count: Int,
        val total: Int,
        val crc: Int,
        val parts: MutableMap<Int, ByteArray> = mutableMapOf(),
        var size: Int = 0,
    )

    private val pending = linkedMapOf<Long, Assembly>()

    fun feed(frame: ByteArray): CompleteMessage? {
        expire()
        require(frame.size > BleProtocol.HEADER_SIZE) { "short fragment" }
        val magic = frame.copyOfRange(0, 2)
        val version = frame[2].toInt() and 0xff
        val reserved = frame[3].toInt() and 0xff
        val id = BleProtocol.u32(frame, 4)
        val index = BleProtocol.u16(frame, 8)
        val count = BleProtocol.u16(frame, 10)
        val total = BleProtocol.u16(frame, 12)
        val crc = BleProtocol.u16(frame, 14)
        val payload = frame.copyOfRange(BleProtocol.HEADER_SIZE, frame.size)
        require(magic.contentEquals(byteArrayOf('S'.code.toByte(), 'M'.code.toByte())) &&
            version == 1 && reserved == 0 && count in 1..BleProtocol.MAX_FRAGMENTS &&
            index < count && total in 1..BleProtocol.MAX_MESSAGE &&
            payload.size <= total) { "invalid fragment header" }

        val assembly = pending[id] ?: run {
            require(pending.size < maxActive) { "assembly capacity exceeded" }
            Assembly(clock(), count, total, crc).also { pending[id] = it }
        }
        if (assembly.count != count || assembly.total != total || assembly.crc != crc) {
            pending.remove(id)
            throw IllegalArgumentException("conflicting fragment header")
        }
        val previous = assembly.parts[index]
        if (previous == null) {
            assembly.parts[index] = payload
            assembly.size += payload.size
            if (assembly.size > total) {
                pending.remove(id)
                throw IllegalArgumentException("oversized assembly")
            }
        } else if (!previous.contentEquals(payload)) {
            pending.remove(id)
            throw IllegalArgumentException("conflicting duplicate fragment")
        }
        if (assembly.parts.size == count) {
            pending.remove(id)
            val message = ByteArray(total)
            var offset = 0
            for (partIndex in 0 until count) {
                val part = assembly.parts[partIndex]
                    ?: throw IllegalArgumentException("missing fragment")
                part.copyInto(message, offset)
                offset += part.size
            }
            require(message.size == total &&
                BleProtocol.crc16CcittFalse(message) == crc) { "length or CRC mismatch" }
            return CompleteMessage(id, message)
        }
        return null
    }

    private fun expire() {
        val now = clock()
        val stale = pending.filterValues { now - it.created > timeoutMs }.keys
        stale.forEach(pending::remove)
    }
}

data class HardwarePacketSummary(
    val deviceId: String,
    val streamId: String,
    val sensorType: String,
    val sourceType: String,
    val packetKind: String,
    val sequence: Long,
    val sampleChannels: List<String>,
    val sampleCount: Int,
    val sensorPresent: Boolean,
    val firmwareVersion: String,
)

object HardwarePacketParser {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    fun summarize(raw: String): HardwarePacketSummary {
        val obj = json.parseToJsonElement(raw).jsonObject
        val status = obj["device_status"] as? JsonObject
        val measurements = obj["measurements"] as? JsonObject
        val channels = measurements?.keys?.toList().orEmpty()
        val samples = measurements?.values?.sumOf { value ->
            val channel = value as? JsonObject
            (channel?.get("samples") as? kotlinx.serialization.json.JsonArray)?.size ?: 0
        } ?: 0
        fun text(key: String, fallback: String = "—") =
            obj[key]?.jsonPrimitive?.content ?: fallback
        return HardwarePacketSummary(
            deviceId = text("device_id"),
            streamId = text("stream_id"),
            sensorType = text("sensor_type"),
            sourceType = text("source_type"),
            packetKind = text("packet_kind"),
            sequence = obj["sequence_number"]?.jsonPrimitive?.content?.toLongOrNull() ?: 0L,
            sampleChannels = channels,
            sampleCount = samples,
            sensorPresent = status?.get("sensor_present")?.jsonPrimitive?.content == "true",
            firmwareVersion = status?.get("firmware_version")?.jsonPrimitive?.content ?: "unknown",
        )
    }
}
