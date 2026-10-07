package io.ten.meetingminutes

import io.ten.meetingminutes.board.MeetingRecord
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** record.json as the board writes it (meeting_control_python, record.py). */
class RecordTest {
    companion object {
        val SAMPLE = """
        {
          "meeting_id": "m1", "title": "週會", "recorded_at": 1790663400.0,
          "duration_s": 600.0, "audio": "audio.ogg", "speaker_count": 6,
          "summary": "會議決定下週出版本。",
          "topics": [
            {"id": "t01", "start_s": 0.0, "end_s": 300.0, "summary": "開場。",
             "error": null,
             "utterances": [
               {"start_s": 4.0, "end_s": 6.0, "speaker": 0, "text": "大家好。"},
               {"start_s": 12.5, "end_s": 15.0, "speaker": 2, "text": "我先說。"}
             ]},
            {"id": "t02", "start_s": 300.0, "end_s": 600.0, "summary": "",
             "error": "transcribe failed: status ERROR", "utterances": []}
          ],
          "actions": [], "actions_error": "no JSON object in the answer", "error": null
        }
        """.trimIndent()
    }

    @Test
    fun everyFieldTheAppShowsIsRead() {
        val r = MeetingRecord.parse(SAMPLE)

        assertEquals("m1", r.meetingId)
        assertEquals("週會", r.title)
        assertEquals(1790663400L, r.recordedAtS)
        assertEquals(6, r.speakerCount)
        assertEquals("會議決定下週出版本。", r.summary)
        assertEquals(listOf("t01", "t02"), r.topics.map { it.id })
        val first = r.topics[0].utterances[1]
        assertEquals(12.5, first.startS, 0.0)
        assertEquals(2, first.speaker)
        assertEquals("我先說。", first.text)
    }

    @Test
    fun aFailedTopicKeepsItsReasonAndNoLines() {
        val t = MeetingRecord.parse(SAMPLE).topics[1]

        assertEquals("transcribe failed: status ERROR", t.error)
        assertEquals(0, t.utterances.size)
    }

    @Test
    fun missingOptionalFieldsAreNull() {
        val r = MeetingRecord.parse(
            """{"meeting_id": "m2", "title": null, "recorded_at": null,
               "duration_s": 0.0, "speaker_count": 0, "summary": "", "topics": []}"""
        )

        assertNull(r.title)
        assertNull(r.recordedAtS)
    }
}
