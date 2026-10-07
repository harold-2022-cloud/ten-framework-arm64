package io.ten.meetingminutes.domain

import io.ten.meetingminutes.board.MeetingRecord
import io.ten.meetingminutes.board.Topic
import io.ten.meetingminutes.board.Utterance

/** The record as the app shows and shares it, with the user's names. */
object Minutes {
    /** A name the user gave, else 說話人N counted from 1 -- the same
     *  number minutes.txt shows for speaker N - 1. */
    fun speaker(n: Int, names: Map<Int, String>, script: String): String =
        names[n]?.takeIf { it.isNotBlank() } ?: (label(script) + (n + 1))

    /** Where in the recording the line was said. */
    fun time(topic: Topic, u: Utterance): String = mmss(topic.startS + u.startS)

    fun mmss(seconds: Double): String {
        val s = seconds.toInt()
        return "%02d:%02d".format(s / 60, s % 60)
    }

    fun share(record: MeetingRecord, names: Map<Int, String>, script: String): String {
        val simplified = script == "simplified"
        val out = StringBuilder()
        out.append(record.title ?: record.meetingId).append("\n\n")
        out.append(if (simplified) "结论" else "結論").append("\n")
        out.append(record.summary.trim()).append("\n")
        record.topics.forEachIndexed { i, t ->
            out.append("\n").append(if (simplified) "话题 " else "話題 ").append(i + 1)
                .append(" · ").append(mmss(t.startS)).append("–").append(mmss(t.endS)).append("\n")
            if (t.summary.isNotBlank()) out.append(t.summary.trim()).append("\n\n")
            if (t.error != null) {
                val failed = if (simplified) "这一段未能处理" else "這一段未能處理"
                out.append("（${mmss(t.startS)}–${mmss(t.endS)} $failed：${t.error}）\n")
            }
            for (u in t.utterances) {
                out.append("[").append(time(t, u)).append("] ")
                    .append(speaker(u.speaker, names, script)).append("：").append(u.text).append("\n")
            }
        }
        return out.toString()
    }

    private fun label(script: String) = if (script == "simplified") "说话人" else "說話人"
}
