package io.ten.meetingminutes

import io.ten.meetingminutes.domain.Estimate
import io.ten.meetingminutes.domain.Ids
import io.ten.meetingminutes.domain.Texts
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SmallLogicTest {
    // The uploader's rule (meeting_uploader/store.py valid_id).
    private val valid = Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

    @Test
    fun aMeetingIdIsOneTheBoardAcceptsAndSaysWhen() {
        val id = Ids.meetingId(nowMs = 1790663400000L, random = "a1b2")

        assertTrue(id, valid.matches(id))
        assertTrue(id, id.contains("a1b2"))
        assertTrue(Ids.channel(id).startsWith("meeting-"))
        assertTrue(valid.matches(Ids.channel(id)))
    }

    @Test
    fun theNotificationIsDueOneRecordingLengthAndTwoMinutesAfterTheUpload() {
        // Measured on the board: processing takes about 0.93 x the recording.
        assertEquals(10_000L + (600 + 120) * 1000L, Estimate.notifyAtMs(10_000L, 600.0))
    }

    @Test
    fun statesReadInPlainWords() {
        assertEquals("準備中", Texts.state("decoding", 0, 0, null))
        assertEquals("轉成文字 2 / 5", Texts.state("transcribing", 2, 5, null))
        assertEquals("整理說話人", Texts.state("linking", 5, 5, null))
        assertEquals("寫摘要 3 / 5", Texts.state("summarising", 3, 5, null))
        assertEquals("完成", Texts.state("archived", 5, 5, null))
        assertEquals("錄音裡沒有偵測到說話", Texts.state("empty", 0, 0, null))
        assertEquals("處理失敗：disk", Texts.state("failed", 0, 0, "disk"))
    }

    @Test
    fun boardErrorsReadAsWhatToDo() {
        assertTrue(Texts.error(0, "").contains("連不上板子"))
        assertTrue(Texts.error(401, "").contains("權杖"))
        assertTrue(Texts.error(409, "meeting m0 is still being processed").contains("m0"))
        assertTrue(Texts.error(413, "").contains("太大"))
        assertTrue(Texts.error(415, "48000 Hz").contains("錄音格式"))
        assertTrue(Texts.error(507, "").contains("空間"))
    }
}
