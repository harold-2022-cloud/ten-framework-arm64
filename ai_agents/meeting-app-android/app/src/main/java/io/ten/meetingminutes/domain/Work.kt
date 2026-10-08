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
            val m = m0.copy(uploadedAtMs = 0, done = 0, total = 0, error = null, settled = false)
            store.dropRecord(m.id)
            try {
                store.put(m.copy(state = "starting"))
                begin(api)
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
     *  phone and the meeting settled. A meeting that ended without a
     *  record (the uploader's own failures write none) is settled too. */
    suspend fun refresh(context: Context, settings: Settings, store: MeetingStore, m: Meeting): Meeting =
        refresh(api(settings), store, m) { Due.cancel(context, it) }

    suspend fun refresh(api: BoardApi, store: MeetingStore, m: Meeting, cancel: (String) -> Unit): Meeting =
        withContext(Dispatchers.IO) {
            try {
                val s = try {
                    api.status(m.id)
                } catch (e: ApiError) {
                    // The worker is reaped ten minutes after its last meeting
                    // ends, but the meeting stays on the board's disk. Start
                    // it to read it: a fresh uploader leaves a finished
                    // meeting as it is.
                    if (e.status != 0) throw e
                    begin(api)
                    api.status(m.id)
                }
                var now = m.copy(state = s.state, done = s.topicsDone, total = s.topicsTotal, error = s.error)
                if (now.finished) {
                    val kept = runCatching { store.saveRecord(m.id, recordJson(api, m.id)) }.isSuccess
                    // An archived meeting whose record did not arrive is
                    // asked again next time.
                    if (kept || now.state != "archived") {
                        cancel(m.id)
                        now = now.copy(settled = true)
                    }
                }
                now.also(store::put)
            } catch (e: ApiError) {
                // Not an answer about the meeting -- the board is away, or
                // has never heard of it. Keep what we know.
                m.copy(error = Texts.error(e.status, e.reason)).also(store::put)
            }
        }

    /** The meeting off the phone: its recording, its record, its entry and
     *  a notification still due. Not while it is being sent. The board keeps
     *  its own copy of a meeting that reached it. */
    fun delete(context: Context, store: MeetingStore, m: Meeting): Boolean =
        delete(store, m) { Due.cancel(context, it) }

    fun delete(store: MeetingStore, m: Meeting, cancel: (String) -> Unit): Boolean {
        if (m.id in uploading.value) return false
        if (m.file.isNotEmpty()) File(m.file).delete()
        cancel(m.id)
        store.remove(m.id)
        return true
    }

    /** The meeting worker, with its uploader answering. */
    private fun begin(api: BoardApi) {
        try {
            api.start(Ids.CHANNEL)
        } catch (e: ApiError) {
            // 10003: the worker is already running -- another meeting's,
            // or an earlier attempt's. Use it, and restart its idle clock:
            // it may be a minute from being reaped.
            if (!e.reason.contains("10003")) throw e
            runCatching { api.ping(Ids.CHANNEL) }
        }
        if (!api.waitReady()) {
            throw ApiError(0, "板子的會議服務 30 秒內沒有回應")
        }
    }

    private fun recordJson(api: BoardApi, id: String): String = api.recordText(id)
}
