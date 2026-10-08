package io.ten.meetingminutes.domain

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

object Ids {
    /** Unique and acceptable to the uploader: letters, digits, - and _. */
    fun meetingId(nowMs: Long, random: String = UUID.randomUUID().toString().take(4)): String =
        SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date(nowMs)) + "-" + random

    /** The one channel every meeting's worker runs on. One worker, so one
     *  uploader: a second worker could not get the port, and stopping a
     *  per-meeting one could kill whichever meeting the uploader was on.
     *  The board reaps it ten minutes after its last meeting; nothing here
     *  stops it. */
    const val CHANNEL = "meeting-room"
}

object Estimate {
    /** When to tell the user the minutes should be ready. Measured on the
     *  board, processing takes about 0.93 x the recording; two minutes on
     *  top covers the start and the summaries. */
    fun notifyAtMs(uploadedAtMs: Long, durationS: Double): Long =
        uploadedAtMs + ((durationS + 120.0) * 1000).toLong()
}

/** The board's states and errors in plain words (app requirements Part 3, 5). */
object Texts {
    fun state(state: String, done: Int, total: Int, error: String?): String = when (state) {
        "received", "decoding" -> "準備中"
        "transcribing" -> "轉成文字 $done / $total"
        "linking" -> "整理說話人"
        "summarising" -> "寫摘要 $done / $total"
        "concluding" -> "寫結論"
        "archived" -> "完成"
        "empty" -> "錄音裡沒有偵測到說話"
        "failed" -> "處理失敗：${error ?: "原因不明"}"
        else -> state
    }

    fun error(status: Int, reason: String): String = when (status) {
        0 -> "連不上板子：請確認手機和板子在同一個網路、位址正確，而且板子已開機"
        400 -> "上傳的資料有誤：$reason"
        401 -> "板子要求權杖：請到設定填入正確的權杖"
        409 -> "板子正在處理另一場會議（$reason），請稍後再傳"
        413 -> "錄音檔太大（上限 64 MB）"
        415 -> "錄音格式不對（$reason）：這是 APP 錄音設定的問題，請回報"
        503 -> "板子收下了檔案，但處理端沒有接上（$reason），請回報管理者"
        507 -> "板子的儲存空間不夠，請稍後再試或回報管理者"
        else -> "板子回應 $status：$reason"
    }
}
