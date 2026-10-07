package io.ten.meetingminutes.board

import org.json.JSONObject

data class Utterance(
    val startS: Double,
    val endS: Double,
    /** Meeting-wide, from 0. Shown as 說話人 speaker + 1, like minutes.txt. */
    val speaker: Int,
    val text: String,
)

data class Topic(
    val id: String,
    /** Position in the recording; an utterance's startS is from here. */
    val startS: Double,
    val endS: Double,
    val summary: String,
    val error: String?,
    val utterances: List<Utterance>,
)

/** record.json, as meeting_control_python writes it. */
data class MeetingRecord(
    val meetingId: String,
    val title: String?,
    val recordedAtS: Long?,
    val durationS: Double,
    val speakerCount: Int,
    val summary: String,
    val topics: List<Topic>,
) {
    companion object {
        fun parse(text: String): MeetingRecord {
            val json = JSONObject(text)
            val topics = json.optJSONArray("topics")
            return MeetingRecord(
                meetingId = json.getString("meeting_id"),
                title = json.str("title"),
                recordedAtS = if (json.isNull("recorded_at") || !json.has("recorded_at")) null
                else json.getDouble("recorded_at").toLong(),
                durationS = json.optDouble("duration_s", 0.0),
                speakerCount = json.optInt("speaker_count"),
                summary = json.str("summary") ?: "",
                topics = (0 until (topics?.length() ?: 0)).map { i ->
                    val t = topics!!.getJSONObject(i)
                    val us = t.optJSONArray("utterances")
                    Topic(
                        id = t.getString("id"),
                        startS = t.optDouble("start_s", 0.0),
                        endS = t.optDouble("end_s", 0.0),
                        summary = t.str("summary") ?: "",
                        error = t.str("error"),
                        utterances = (0 until (us?.length() ?: 0)).map { j ->
                            val u = us!!.getJSONObject(j)
                            Utterance(
                                startS = u.optDouble("start_s", 0.0),
                                endS = u.optDouble("end_s", 0.0),
                                speaker = u.optInt("speaker"),
                                text = u.optString("text"),
                            )
                        },
                    )
                },
            )
        }

        private fun JSONObject.str(key: String): String? =
            if (!has(key) || isNull(key)) null else optString(key)
    }
}
