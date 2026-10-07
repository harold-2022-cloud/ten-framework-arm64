package io.ten.meetingminutes.domain

import android.content.Context
import io.ten.meetingminutes.board.ApiError
import io.ten.meetingminutes.board.BoardApi
import io.ten.meetingminutes.notify.Due
import io.ten.meetingminutes.store.Meeting
import io.ten.meetingminutes.store.MeetingStore
import io.ten.meetingminutes.store.Settings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** What the app does with the board, in the order the API guide gives. */
object Work {
    private fun api(settings: Settings) = BoardApi(settings.host, settings.token)

    // Uploads belong to the process, not to a screen: leaving the form or
    // turning the phone must not cancel one halfway.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Meetings being sent right now. One left "starting" or "uploading"
     *  but not in here was cut off (the app was killed) and can be sent
     *  again. */
    val uploading = MutableStateFlow<Set<String>>(emptySet())

    fun startUpload(context: Context, settings: Settings, store: MeetingStore, m: Meeting) {
        if (m.id in uploading.value) return
        uploading.update { it + m.id }
        val app = context.applicationContext
        scope.launch {
            try {
                upload(app, settings, store, m)
            } finally {
                uploading.update { it - m.id }
            }
        }
    }

    /** /start, wait for the uploader, upload, and set the notification.
     *  Sending a meeting again starts it over: the board clears that id's
     *  last run, and so does the phone. */
    suspend fun upload(context: Context, settings: Settings, store: MeetingStore, m0: Meeting): Meeting =
        upload(api(settings), store, m0) { m, atMs -> Due.schedule(context, m.id, m.title, atMs) }

    suspend fun upload(
        api: BoardApi,
        store: MeetingStore,
        m0: Meeting,
        schedule: (Meeting, Long) -> Unit,
    ): Meeting =
        withContext(Dispatchers.IO) {
            val m = m0.copy(uploadedAtMs = 0, done = 0, total = 0, error = null, stopped = false)
            store.dropRecord(m.id)
            try {
                store.put(m.copy(state = "starting"))
                begin(api, m.id)
                store.put(m.copy(state = "uploading"))
                api.upload(
                    File(m.file), m.id, m.speakers, m.title,
                    m.recordedAtMs / 1000, m.script,
                )
                val now = System.currentTimeMillis()
                schedule(m, Estimate.notifyAtMs(now, m.durationS))
                m.copy(state = "received", uploadedAtMs = now).also(store::put)
            } catch (e: ApiError) {
                m.copy(state = "upload_failed", error = Texts.error(e.status, e.reason))
                    .also(store::put)
            }
        }

    /** The board's state; once it is final, the record is kept on the
     *  phone and then the worker stopped. A meeting that ended without
     *  a record (the uploader's own failures write none) is stopped too. */
    suspend fun refresh(context: Context, settings: Settings, store: MeetingStore, m: Meeting): Meeting =
        refresh(api(settings), store, m) { Due.cancel(context, it) }

    suspend fun refresh(api: BoardApi, store: MeetingStore, m: Meeting, cancel: (String) -> Unit): Meeting =
        withContext(Dispatchers.IO) {
            try {
                var stopped = m.stopped
                val s = try {
                    api.status(m.id)
                } catch (e: ApiError) {
                    // The worker that held the meeting is reaped ten minutes
                    // after it ends, but the meeting stays on the board's
                    // disk. Start one to read it (a fresh uploader leaves a
                    // finished meeting as it is), and stop it again below.
                    if (e.status != 0) throw e
                    begin(api, m.id)
                    stopped = false
                    api.status(m.id)
                }
                var now = m.copy(
                    state = s.state, done = s.topicsDone, total = s.topicsTotal,
                    error = s.error, stopped = stopped,
                )
                if (now.finished) {
                    val kept = runCatching { store.saveRecord(m.id, recordJson(api, m.id)) }.isSuccess
                    // An archived meeting without its record on the phone
                    // keeps its worker, so the next look can fetch it.
                    if (!kept && now.state == "archived") return@withContext now.also(store::put)
                    cancel(m.id)
                    if (!now.stopped) {
                        runCatching { api.stop(Ids.channel(m.id)) }
                        now = now.copy(stopped = true)
                    }
                }
                now.also(store::put)
            } catch (e: ApiError) {
                // Not an answer about the meeting -- the board is away, or
                // has never heard of it. Keep what we know.
                m.copy(error = Texts.error(e.status, e.reason)).also(store::put)
            }
        }

    /** A worker for the meeting's channel, with its uploader answering. */
    private fun begin(api: BoardApi, id: String) {
        try {
            api.start(Ids.channel(id))
        } catch (e: ApiError) {
            // 10003: this meeting's worker is already running -- an
            // earlier attempt got that far. Carry on with it.
            if (!e.reason.contains("10003")) throw e
        }
        if (!api.waitReady()) {
            throw ApiError(0, "板子的會議服務 30 秒內沒有回應")
        }
    }

    private fun recordJson(api: BoardApi, id: String): String = api.recordText(id)
}
