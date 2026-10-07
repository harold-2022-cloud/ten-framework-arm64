package io.ten.meetingminutes

import io.ten.meetingminutes.board.MeetingRecord
import io.ten.meetingminutes.domain.Minutes
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** What the app shows and shares, from record.json and the user's names. */
class MinutesTest {
    private val record = MeetingRecord.parse(RecordTest.SAMPLE)

    @Test
    fun speakersAreNumberedFromOneLikeMinutesTxt() {
        // record.json counts from 0; minutes.txt says 說話人1 for speaker 0.
        assertEquals("說話人1", Minutes.speaker(0, emptyMap(), "traditional"))
        assertEquals("说话人3", Minutes.speaker(2, emptyMap(), "simplified"))
    }

    @Test
    fun aNameTheUserGaveReplacesTheNumber() {
        assertEquals("王經理", Minutes.speaker(2, mapOf(2 to "王經理"), "traditional"))
    }

    @Test
    fun aLinesTimeIsItsTopicsStartPlusItsOwn() {
        val topic = record.topics[1].copy(
            utterances = listOf(record.topics[0].utterances[1])
        )
        assertEquals("05:12", Minutes.time(topic, topic.utterances[0]))
    }

    @Test
    fun theSharedTextHasConclusionTopicsAndNamedLines() {
        val text = Minutes.share(record, mapOf(0 to "Harold"), "traditional")

        assertTrue(text.startsWith("週會"))
        assertTrue(text.contains("會議決定下週出版本。"))
        assertTrue(text.contains("[00:04] Harold：大家好。"))
        assertTrue(text.contains("[00:12] 說話人3：我先說。"))
        assertTrue(text.contains("這一段未能處理"))
    }
}
