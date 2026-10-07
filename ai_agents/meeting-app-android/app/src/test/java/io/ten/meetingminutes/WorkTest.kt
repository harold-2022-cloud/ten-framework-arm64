package io.ten.meetingminutes

import io.ten.meetingminutes.board.BoardApi
import io.ten.meetingminutes.domain.Work
import io.ten.meetingminutes.store.Meeting
import io.ten.meetingminutes.store.MeetingStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.ServerSocket
import java.nio.file.Files

/**
 * When the app starts and stops the meeting's worker on the board. The
 * worker is the only thing that can answer about a meeting, and it is
 * reaped ten minutes after the meeting ends without anyone asking.
 */
class WorkTest {
    private val server = MockWebServer()

    // The uploader runs inside the meeting's worker: a reaped worker is a
    // port that refuses connections, until /start brings a new one up.
    private val uploaderPort = ServerSocket(0).use { it.localPort }
    @Volatile private var uploader: MockWebServer? = null
    private val dir: File = Files.createTempDirectory("work").toFile()
    private val store = MeetingStore(dir)

    @Volatile private var state = "archived"
    @Volatile private var recordStatus = 200
    @Volatile private var starts = 0
    @Volatile private var stops = 0
    private val cancelled = mutableListOf<String>()

    private val meeting = Meeting(
        id = "m1", title = "週會", speakers = 6, script = "traditional",
        recordedAtMs = 1_790_663_400_000L, durationS = 600.0, file = "",
        uploadedAtMs = 1_790_664_000_000L, state = "summarising",
    )

    private fun json(status: Int, body: String) =
        MockResponse().setResponseCode(status).setBody(body)

    @Before
    fun up() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                when (request.path) {
                    "/start" -> { starts++; if (uploader == null) workerUp() }
                    "/stop" -> stops++
                }
                return json(200, """{"code": "0", "msg": "success", "data": null}""")
            }
        }
        server.start()
        workerUp()
    }

    private fun workerUp() {
        uploader = MockWebServer().apply {
            dispatcher = uploaderAnswers
            start(uploaderPort)
        }
    }

    private fun reaped() {
        uploader?.shutdown()
        uploader = null
    }

    private val uploaderAnswers = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse =
            when (request.path) {
                "/meetings" -> json(200, """{"meetings": []}""")
                "/meeting/upload" -> json(200, """{"meeting_id": "m1", "bytes": 5}""")
                "/meeting/m1/record.json" ->
                    if (recordStatus == 200) json(200, RecordTest.SAMPLE)
                    else json(recordStatus, """{"error": "no record"}""")
                "/meeting/m1" -> json(
                    200,
                    """{"meeting_id": "m1", "state": "$state",
                       "progress": {"topics_done": 2, "topics_total": 2},
                       "record": null, "error": null}""",
                )
                else -> json(404, """{"error": "no such thing"}""")
            }
    }

    @After
    fun down() {
        server.shutdown()
        reaped()
        dir.deleteRecursively()
    }

    private val api get() = BoardApi(
        host = "127.0.0.1", token = null,
        serverPort = server.port, uploaderPort = uploaderPort,
    )

    private fun refresh(m: Meeting) = runBlocking {
        Work.refresh(api, store, m, cancel = { cancelled += it })
    }

    @Test
    fun aFinishedMeetingIsKeptOnThePhoneAndItsWorkerLetGoOnce() {
        val after = refresh(meeting)

        assertEquals("archived", after.state)
        assertTrue(after.stopped)
        assertNotNull(store.record("m1"))
        assertEquals(1, stops)
        assertEquals(listOf("m1"), cancelled)

        refresh(after)
        assertEquals(1, stops)
    }

    @Test
    fun aMeetingWhoseWorkerWasReapedIsReadByStartingOneAgain() {
        reaped()

        val after = refresh(meeting)

        assertEquals(1, starts)
        assertEquals("archived", after.state)
        assertNull(after.error)
        assertNotNull(store.record("m1"))
        assertTrue("the worker started to read it is stopped again", after.stopped)
        assertEquals(1, stops)
    }

    @Test
    fun anArchivedMeetingWhoseRecordDidNotArriveKeepsItsWorker() {
        recordStatus = 500

        val after = refresh(meeting)

        assertEquals("archived", after.state)
        assertFalse(after.stopped)
        assertNull(store.record("m1"))
        assertEquals(0, stops)
        assertTrue(cancelled.isEmpty())
    }

    @Test
    fun aFailureWithoutARecordStillLetsTheWorkerGo() {
        // The uploader's own failures (an interrupted run) write no record.
        state = "failed"
        recordStatus = 404

        val after = refresh(meeting)

        assertEquals("failed", after.state)
        assertTrue(after.stopped)
        assertEquals(1, stops)
    }

    @Test
    fun aStillRunningMeetingIsOnlyRead() {
        state = "summarising"

        val after = refresh(meeting.copy(state = "transcribing"))

        assertEquals("summarising", after.state)
        assertEquals(0, starts)
        assertEquals(0, stops)
    }

    @Test
    fun sendingAMeetingAgainStartsItOver() {
        val file = File(dir, "a.ogg").apply { writeBytes(byteArrayOf(79, 103, 103, 83, 0)) }
        store.saveRecord("m1", RecordTest.SAMPLE)
        val failed = meeting.copy(file = file.path, state = "failed", stopped = true, error = "x")
        var due = 0L

        val after = runBlocking {
            Work.upload(api, store, failed, schedule = { _, at -> due = at })
        }

        assertEquals("received", after.state)
        assertFalse("the new run's worker must be stopped when it ends", after.stopped)
        assertNull(after.error)
        assertNull("the last run's record is gone", store.record("m1"))
        assertTrue(after.uploadedAtMs > failed.uploadedAtMs)
        assertEquals(after.uploadedAtMs + (600 + 120) * 1000L, due)
        assertEquals(after, store.get("m1"))
    }
}
