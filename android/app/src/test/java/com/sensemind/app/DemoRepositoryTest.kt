package com.sensemind.app

import com.sensemind.app.data.DemoMode
import com.sensemind.app.data.DemoRepository
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DemoRepositoryTest {
    @Test
    fun demoDataIsNeverMarkedLive() = runBlocking {
        for (mode in DemoMode.entries) {
            val repository = DemoRepository(mode)
            val subjects = repository.subjects()
            assertTrue(subjects.isNotEmpty())
            subjects.forEach { subject ->
                assertNotEquals("live", subject.data_mode)
            }
            val events = repository.events()
            assertTrue(events.isNotEmpty())
            val detail = repository.eventDetail(events.first().id)
            assertTrue(detail.evidence.isNotEmpty())
            detail.evidence.forEach { evidence ->
                assertNotEquals("live", evidence.data_mode)
                assertTrue(evidence.synthetic)
            }
            val measurements = repository.measurements(subjects.first().device_id, 1)
            assertTrue(measurements.isNotEmpty())
            measurements.forEach { sample ->
                assertNotEquals("live_device", sample.source)
            }
        }
    }

    @Test
    fun demoModeIsExplicit() {
        assertEquals(DemoMode.REPLAY, DemoRepository(DemoMode.REPLAY).demoMode)
        assertEquals(DemoMode.SYNTHETIC, DemoRepository(DemoMode.SYNTHETIC).demoMode)
        assertNotEquals(null, DemoRepository(DemoMode.REPLAY).demoMode)
    }
}
