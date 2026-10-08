package io.ten.meetingminutes

import io.ten.meetingminutes.board.ApiError
import io.ten.meetingminutes.board.BoardApi
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.ServerSocket

/**
 * The app's side of the board's API, against a stand-in for the board: two
 * mock servers playing the Go server (/start, /stop) and the uploader
 * (/meeting/...), answering as the API guide says they do.
 */
class BoardApiTest {
    private val server = MockWebServer()
    private val uploader = MockWebServer()
    private var startCode = "0"
    private var uploadStatus = 200
    private var uploadBody = """{"meeting_id": "m1", "bytes": 5, "status": "accepted"}"""
    private var meetingsReady = true

    private fun json(status: Int, body: String) =
        MockResponse().setResponseCode(status).setBody(body)

    @Before
    fun up() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest) = when (request.path) {
                "/start" -> json(200, """{"code": "$startCode", "msg": "", "data": null}""")
                else -> json(200, """{"code": "0", "msg": "success", "data": null}""")
            }
        }
        uploader.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest) = when (request.path) {
                "/meetings" ->
                    if (meetingsReady) json(200, """{"meetings": []}""") else json(503, "{}")
                "/meeting/upload" -> json(uploadStatus, uploadBody)
                "/meeting/m1/record.json" -> json(200, RecordTest.SAMPLE)
                "/meeting/q1" -> json(
                    200,
                    """{"meeting_id": "q1", "state": "queued",
                       "progress": {"topics_done": 0, "topics_total": 0},
                       "queue": {"ahead": 1, "wait_s": 420},
                       "record": null, "error": null}""",
                )
                "/meeting/odd" -> json(200, "<html>captive portal</html>")
                "/meeting/m1" -> json(
                    200,
                    """{"meeting_id": "m1", "state": "transcribing",
                       "progress": {"topics_done": 2, "topics_total": 5},
                       "record": null, "error": null}""",
                )
                else -> json(404, """{"error": "no such thing"}""")
            }
        }
        server.start()
        uploader.start()
    }

    @After
    fun down() {
        server.shutdown()
        uploader.shutdown()
    }

    private fun api(token: String? = null) = BoardApi(
        host = "127.0.0.1",
        token = token,
        serverPort = server.port,
        uploaderPort = uploader.port,
    )

    @Test
    fun startAsksForTheMeetingGraphWithTheChannelAndTimeout() {
        api().start("meeting-m1", timeoutS = 600)

        val request = server.takeRequest()
        val sent = JSONObject(request.body.readUtf8())
        assertEquals("/start", request.path)
        assertEquals("meeting_minutes", sent.getString("graph_name"))
        assertEquals("meeting-m1", sent.getString("channel_name"))
        assertEquals(600, sent.getInt("timeout"))
    }

    @Test
    fun aStartTheServerRefusesSaysItsCode() {
        startCode = "10003"
        try {
            api().start("meeting-m1")
            fail("expected ApiError")
        } catch (e: ApiError) {
            assertTrue(e.message!!.contains("10003"))
        }
    }

    @Test
    fun readinessIsTheUploaderAnsweringMeetings() {
        assertTrue(api().waitReady(maxS = 2, pause = {}))
        meetingsReady = false
        assertFalse(api().waitReady(maxS = 2, pause = {}))
    }

    @Test
    fun anUploadIsOneMultipartWithTheFileAndTheFields() {
        val file = File.createTempFile("meeting", ".ogg").apply {
            writeBytes(byteArrayOf(79, 103, 103, 83, 0)) // "OggS\0"
            deleteOnExit()
        }

        val result = api(token = "s3cret").upload(
            file, meetingId = "m1", speakers = 6, title = "週會",
            recordedAtS = 1790663400, script = "traditional",
        )

        assertEquals("m1", result.meetingId)
        val request = uploader.takeRequest()
        val body = request.body.readUtf8()
        assertEquals("/meeting/upload", request.path)
        assertTrue(request.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        for (field in listOf("meeting_id", "speakers", "title", "recorded_at", "script")) {
            assertTrue("$field missing", body.contains("name=\"$field\""))
        }
        assertTrue(body.contains("traditional"))
        assertTrue(body.contains("週會"))
        assertTrue(body.contains("OggS"))
        assertEquals("Bearer s3cret", request.getHeader("Authorization"))
    }

    @Test
    fun theGoServerNeverGetsTheUploadersToken() {
        api(token = "s3cret").start("meeting-m1")

        assertEquals(null, server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun aRefusedUploadCarriesTheBoardsStatusAndReason() {
        uploadStatus = 409
        uploadBody = """{"error": "meeting m0 is still being processed"}"""
        val file = File.createTempFile("meeting", ".ogg").apply { deleteOnExit() }

        try {
            api().upload(file, "m1", null, null, null, null)
            fail("expected ApiError")
        } catch (e: ApiError) {
            assertEquals(409, e.status)
            assertEquals("meeting m0 is still being processed", e.reason)
        }
    }

    @Test
    fun anUploadTheBoardQueuesSaysHowManyGoFirstAndHowLong() {
        uploadBody = """{"meeting_id": "m1", "bytes": 5, "status": "queued", "ahead": 2, "wait_s": 900}"""
        val file = File.createTempFile("meeting", ".ogg").apply { deleteOnExit() }

        val r = api().upload(file, "m1", 2, null, null, null)

        assertTrue(r.queued)
        assertEquals(2, r.ahead)
        assertEquals(900.0, r.waitS, 0.0)
    }

    @Test
    fun anUploadTakenAtOnceIsNotQueued() {
        val file = File.createTempFile("meeting", ".ogg").apply { deleteOnExit() }

        val r = api().upload(file, "m1", 2, null, null, null)

        assertFalse(r.queued)
        assertEquals(0, r.ahead)
    }

    @Test
    fun aQueuedMeetingsStatusSaysItsPlace() {
        val s = api().status("q1")

        assertEquals("queued", s.state)
        assertEquals(1, s.ahead)
        assertEquals(420.0, s.waitS!!, 0.0)
    }

    @Test
    fun statusReadsStateAndProgress() {
        val s = api().status("m1")

        assertEquals("transcribing", s.state)
        assertEquals(2, s.topicsDone)
        assertEquals(5, s.topicsTotal)
    }

    @Test
    fun theRecordIsFetchedAndParsed() {
        val r = api().record("m1")

        assertEquals(6, r.speakerCount)
        assertEquals(2, r.topics.size)
    }

    @Test
    fun aReplyThatIsNotTheBoardsJsonIsAnApiErrorNotACrash() {
        try {
            api().status("odd")
            fail("expected ApiError")
        } catch (e: ApiError) {
            assertEquals(200, e.status)
        }
    }

    @Test
    fun aBoardThatIsNotThereIsStatusZero() {
        val closed = ServerSocket(0).use { it.localPort }
        val gone = BoardApi(host = "127.0.0.1", token = null, serverPort = closed, uploaderPort = closed)
        try {
            gone.status("m1")
            fail("expected ApiError")
        } catch (e: ApiError) {
            assertEquals(0, e.status)
        }
    }
}
