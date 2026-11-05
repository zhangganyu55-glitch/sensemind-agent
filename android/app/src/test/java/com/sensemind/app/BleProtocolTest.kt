package com.sensemind.app

import com.sensemind.app.ble.BleProtocol
import com.sensemind.app.ble.FragmentAssembler
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BleProtocolTest {
    private fun fragment(
        messageId: Long,
        index: Int,
        count: Int,
        total: Int,
        crc: Int,
        payload: ByteArray,
    ): ByteArray {
        val header = ByteArray(16)
        header[0] = 'S'.code.toByte()
        header[1] = 'M'.code.toByte()
        header[2] = 1
        header[3] = 0
        header[4] = (messageId and 0xff).toByte()
        header[5] = ((messageId shr 8) and 0xff).toByte()
        header[6] = ((messageId shr 16) and 0xff).toByte()
        header[7] = ((messageId shr 24) and 0xff).toByte()
        header[8] = (index and 0xff).toByte()
        header[9] = ((index shr 8) and 0xff).toByte()
        header[10] = (count and 0xff).toByte()
        header[11] = ((count shr 8) and 0xff).toByte()
        header[12] = (total and 0xff).toByte()
        header[13] = ((total shr 8) and 0xff).toByte()
        header[14] = (crc and 0xff).toByte()
        header[15] = ((crc shr 8) and 0xff).toByte()
        return header + payload
    }

    @Test
    fun reassemblesSingleAndMultipleFragments() {
        val message = "{\"protocol_version\":\"1.0\",\"source_type\":\"test\"}".toByteArray()
        val crc = BleProtocol.crc16CcittFalse(message)
        val single = fragment(7, 0, 1, message.size, crc, message)
        val complete = FragmentAssembler().feed(single)
        assertNotNull(complete)
        assertEquals(7L, complete!!.messageId)
        assertArrayEquals(message, complete.payload)

        val first = message.copyOfRange(0, 20)
        val second = message.copyOfRange(20, message.size)
        val assembler = FragmentAssembler()
        assertNull(assembler.feed(fragment(9, 0, 2, message.size, crc, first)))
        val joined = assembler.feed(fragment(9, 1, 2, message.size, crc, second))
        assertNotNull(joined)
        assertArrayEquals(message, joined!!.payload)
    }

    @Test
    fun rejectsConflictingDuplicateFragment() {
        val message = "abcdefgh".toByteArray()
        val crc = BleProtocol.crc16CcittFalse(message)
        val assembler = FragmentAssembler()
        assembler.feed(fragment(1, 0, 2, message.size, crc, message.copyOfRange(0, 4)))
        try {
            assembler.feed(fragment(1, 0, 2, message.size, crc, byteArrayOf(1, 2, 3, 4)))
            throw AssertionError("expected conflicting duplicate to fail")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message.orEmpty().contains("conflicting"))
        }
    }

    @Test
    fun acknowledgementMatchesFrozenEightByteLayout() {
        assertArrayEquals(
            byteArrayOf('S'.code.toByte(), 'A'.code.toByte(), 1, 0, 0x78, 0x56, 0x34, 0x12),
            BleProtocol.acknowledgement(0x12345678),
        )
        assertArrayEquals(
            byteArrayOf('S'.code.toByte(), 'A'.code.toByte(), 1, 1, 1, 0, 0, 0),
            BleProtocol.acknowledgement(1, retry = true),
        )
    }
}
