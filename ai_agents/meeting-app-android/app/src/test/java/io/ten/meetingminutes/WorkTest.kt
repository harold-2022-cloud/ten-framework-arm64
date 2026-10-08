package io.ten.meetingminutes

import io.ten.meetingminutes.board.BoardApi
import io.ten.meetingminutes.domain.Ids
import io.ten.meetingminutes.domain.Work
import io.ten.meetingminutes.recording.LeftBehind
import io.ten.meetingminutes.store.Meeting
import io.ten.meetingminutes.store.MeetingStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
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
 * How the app uses the board's meeting worker. There is one, on one
 * channel, for every meeting: the uploader inside it is the only thing that
 * answers about a meeting, and the board reaps it ten minutes after its last
 * meeting ends. The app starts it when it is not there and never stops it --
 * stopping it could kill someone else's meeting halfway.
 */
class WorkTest {
    private val server = MockWebServer()

    // The uploader runs inside the worker: a reaped worker is a port that
    // refuses connections, until /start brings a new one up.
    private val uploaderPort = ServerSocket(0).use { it.localPort }
    @Volatile private var uploader: MockWebServer? = null
    private val dir: File = Files.createTempDirectory("work").toFile()
    private val store = MeetingStore(dir)

    @Volatile private var state = "archived"
    @Volatile private var recordStatus = 200
    private val starts = mutableListOf<String>()
    @Volatile private var pings = 0
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
                val ok = json(200, """{"code": "0", "msg": "success", "data": null}""")
                return when (request.path) {
                    "/start" -> {
                        synchronized(starts) {
                            starts += JSONObject(request.body.readUtf8()).getString("channel_name")
                        }
                        if (uploader != null) {
                            // What the Go server says of a channel already running.
                            json(400, """{"code": "10003", "msg": "channel existed", "data": null}""")
                        } else {
                            workerUp()
                            ok
                        }
                    }
                    "/ping" -> { pings++; ok }
                    "/stop" -> { stops++; ok }
                    else -> ok
                }
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

    private fun send(m: Meeting) = runBlocking {
        Work.upload(api, store, m, schedule = { _, _ -> })
    }

    private fun recording() =
        File(dir, "a.ogg").apply { writeBytes(byteArrayOf(79, 103, 103, 83, 0)) }

    @Test
    fun aFinishedMeetingIsKeptOnThePhoneAndTheWorkerLeftToTheBoard() {
        val after = refresh(meeting)

        assertEquals("archived", after.state)
        assertTrue(after.settled)
        assertNotNull(store.record("m1"))
        assertEquals(listOf("m1"), cancelled)
        assertEquals(0, stops)
    }

    @Test
    fun aMeetingWhoseWorkerWasReapedIsReadByStartingTheWorkerAgain() {
        reaped()

        val after = refresh(meeting)

        assertEquals(listOf(Ids.CHANNEL), starts)
        assertEquals("archived", after.state)
        assertNull(after.error)
        assertNotNull(store.record("m1"))
        assertTrue(after.settled)
        assertEquals(0, stops)
    }

    @Test
    fun anArchivedMeetingWhoseRecordDidNotArriveIsAskedAgain() {
        recordStatus = 500

        val after = refresh(meeting)

        assertEquals("archived", after.state)
        assertFalse(after.settled)
        assertNull(store.record("m1"))
        assertTrue(cancelled.isEmpty())
    }

    @Test
    fun aFailureWithoutARecordIsSettled() {
        // The uploader's own failures (an interrupted run) write no record.
        state = "failed"
        recordStatus = 404

        val after = refresh(meeting)

        assertEquals("failed", after.state)
        assertTrue(after.settled)
    }

    @Test
    fun aStillRunningMeetingIsOnlyRead() {
        state = "summarising"

        val after = refresh(meeting.copy(state = "transcribing"))

        assertEquals("summarising", after.state)
        assertFalse(after.settled)
        assertTrue(starts.isEmpty())
    }

    @Test
    fun everyMeetingGoesToTheOneWorkerAndNoneIsStopped() {
        reaped()
        val file = recording()

        send(meeting.copy(id = "m1", file = file.path, state = "local", uploadedAtMs = 0))
        send(meeting.copy(id = "m2", file = file.path, state = "local", uploadedAtMs = 0))

        assertEquals(listOf(Ids.CHANNEL, Ids.CHANNEL), starts)
        assertEquals(0, stops)
    }

    @Test
    fun aWorkerAlreadyRunningIsKeptAliveAndUsed() {
        // It may be minutes from being reaped: the ping restarts its clock.
        val after = send(meeting.copy(file = recording().path, state = "local", uploadedAtMs = 0))

        assertEquals("received", after.state)
        assertEquals(1, starts.size)
        assertEquals(1, pings)
    }

    @Test
    fun sendingAMeetingAgainStartsItOver() {
        store.saveRecord("m1", RecordTest.SAMPLE)
        val failed = meeting.copy(file = recording().path, state = "failed", settled = true, error = "x")
        var due = 0L

        val after = runBlocking {
            Work.upload(api, store, failed, schedule = { _, at -> due = at })
        }

        assertEquals("received", after.state)
        assertFalse("the new run is read until it ends", after.settled)
        assertNull(after.error)
        assertNull("the last run's record is gone", store.record("m1"))
        assertTrue(after.uploadedAtMs > failed.uploadedAtMs)
        assertEquals(after.uploadedAtMs + (600 + 120) * 1000L, due)
        assertEquals(after, store.get("m1"))
    }

    @Test
    fun deletingAMeetingTakesItsRecordingAndRecordOffThePhone() {
        // Named as the app names recordings, so a file left behind would be
        // offered again at the next launch.
        val file = File(dir, "1790663400000.ogg").apply { writeBytes(byteArrayOf(79, 103, 103, 83, 0)) }
        store.put(meeting.copy(file = file.path, state = "upload_failed"))
        store.saveRecord("m1", RecordTest.SAMPLE)

        assertTrue(Work.delete(store, store.get("m1")!!, cancel = { cancelled += it }))

        assertFalse(file.exists())
        assertNull(store.get("m1"))
        assertNull(store.record("m1"))
        assertEquals(listOf("m1"), cancelled)
        assertNull(LeftBehind.find(dir, emptySet()))
    }

    @Test
    fun aMeetingBeingSentIsNotDeleted() {
        val file = recording()
        store.put(meeting.copy(file = file.path, state = "uploading"))
        Work.uploading.value = setOf("m1")
        try {
            assertFalse(Work.delete(store, store.get("m1")!!, cancel = { cancelled += it }))
        } finally {
            Work.uploading.value = emptySet()
        }

        assertTrue(file.exists())
        assertNotNull(store.get("m1"))
    }
}
