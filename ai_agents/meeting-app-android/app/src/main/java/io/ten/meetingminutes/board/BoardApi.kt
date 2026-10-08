package io.ten.meetingminutes.board

import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * Why a call to the board failed: the HTTP status, 0 when nothing answered,
 * and the board's own reason ("error" in the uploader's JSON).
 */
class ApiError(val status: Int, val reason: String) :
    Exception(if (status == 0) reason else "$status $reason")

/** queued: the board took the meeting but is processing another; ahead
 *  meetings go first, and it should start in about waitS seconds. */
data class UploadResult(
    val meetingId: String,
    val bytes: Long,
    val queued: Boolean = false,
    val ahead: Int = 0,
    val waitS: Double = 0.0,
)

data class MeetingStatus(
    val state: String,
    val topicsDone: Int,
    val topicsTotal: Int,
    val error: String?,
    /** While queued: how many go first, and about how long until it starts. */
    val ahead: Int? = null,
    val waitS: Double? = null,
)

/**
 * The board's two HTTP ports, as the API guide describes them: the Go
 * server (8081) starts and stops the meeting worker; the uploader inside
 * that worker (8765) takes the recording and answers for it. Blocking calls:
 * run them off the main thread.
 */
class BoardApi(
    private val host: String,
    private val token: String?,
    private val serverPort: Int = 8081,
    private val uploaderPort: Int = 8765,
    private val timeoutMs: Int = 15_000,
) {
    private val server get() = "http://$host:$serverPort"
    private val uploader get() = "http://$host:$uploaderPort"

    /** Start the meeting worker. timeoutS only has to cover the upload and
     *  fetching the result: while it processes, the board keeps it alive. */
    fun start(channel: String, timeoutS: Int = 600) {
        serverCall(
            "/start",
            JSONObject()
                .put("request_id", UUID.randomUUID().toString())
                .put("channel_name", channel)
                .put("graph_name", GRAPH)
                .put("timeout", timeoutS),
        )
    }

    /** Restart a running worker's idle clock. */
    fun ping(channel: String) {
        serverCall(
            "/ping",
            JSONObject()
                .put("request_id", UUID.randomUUID().toString())
                .put("channel_name", channel),
        )
    }

    /** The uploader answers GET /meetings once the worker is up. */
    fun waitReady(maxS: Int = 30, pause: (Long) -> Unit = { Thread.sleep(it) }): Boolean {
        repeat(maxS) {
            val up = try {
                request("$uploader/meetings", "GET", null, null).first == 200
            } catch (e: ApiError) {
                false
            }
            if (up) return true
            pause(1000)
        }
        return false
    }

    fun upload(
        file: File,
        meetingId: String,
        speakers: Int?,
        title: String?,
        recordedAtS: Long?,
        script: String?,
    ): UploadResult {
        val boundary = "----meeting" + UUID.randomUUID().toString().replace("-", "")
        val fields = linkedMapOf<String, String>("meeting_id" to meetingId)
        speakers?.let { fields["speakers"] = it.toString() }
        title?.takeIf { it.isNotBlank() }?.let { fields["title"] = it }
        recordedAtS?.let { fields["recorded_at"] = it.toString() }
        script?.let { fields["script"] = it }

        val head = buildString {
            for ((k, v) in fields) {
                append("--$boundary\r\nContent-Disposition: form-data; name=\"$k\"\r\n\r\n$v\r\n")
            }
            append("--$boundary\r\nContent-Disposition: form-data; name=\"file\"; ")
            append("filename=\"meeting.ogg\"\r\nContent-Type: audio/ogg\r\n\r\n")
        }.toByteArray(Charsets.UTF_8)
        val tail = "\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8)

        val conn = open("$uploader/meeting/upload", "POST", auth = true)
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        // Streamed, never whole in memory: an hour is about 7 MB, but
        // nothing stops a longer one.
        conn.setFixedLengthStreamingMode(head.size + file.length() + tail.size)
        conn.doOutput = true
        val (status, text) = exchange(conn) { out ->
            out.write(head)
            file.inputStream().use { it.copyTo(out) }
            out.write(tail)
        }
        if (status != 200) throw ApiError(status, reason(text))
        val json = parse(status, text)
        return UploadResult(
            meetingId = json.optString("meeting_id", meetingId),
            bytes = json.optLong("bytes"),
            queued = json.optString("status") == "queued",
            ahead = json.optInt("ahead"),
            waitS = json.optDouble("wait_s", 0.0),
        )
    }

    fun status(meetingId: String): MeetingStatus {
        val json = parse(200, get("$uploader/meeting/$meetingId"))
        val progress = json.optJSONObject("progress") ?: JSONObject()
        val queue = json.optJSONObject("queue")
        return MeetingStatus(
            state = json.optString("state"),
            topicsDone = progress.optInt("topics_done"),
            topicsTotal = progress.optInt("topics_total"),
            error = if (json.isNull("error")) null else json.optString("error"),
            ahead = queue?.optInt("ahead"),
            waitS = queue?.optDouble("wait_s"),
        )
    }

    fun record(meetingId: String): MeetingRecord = MeetingRecord.parse(recordText(meetingId))

    /** record.json as the board wrote it, to keep on the phone. */
    fun recordText(meetingId: String): String = get("$uploader/meeting/$meetingId/record.json")

    // ------------------------------------------------------------------

    private fun serverCall(path: String, body: JSONObject) {
        val (status, text) = request(
            "$server$path", "POST", "application/json",
            body.toString().toByteArray(Charsets.UTF_8), auth = false,
        )
        // The Go server answers 200 even when it refuses; "0" is success.
        val code = runCatching { JSONObject(text).optString("code") }.getOrDefault("")
        if (status != 200 || code != "0") {
            throw ApiError(status, "code $code ${text.take(200)}")
        }
    }

    private fun get(url: String): String {
        val (status, text) = request(url, "GET", null, null)
        if (status != 200) throw ApiError(status, reason(text))
        return text
    }

    private fun request(
        url: String,
        method: String,
        contentType: String?,
        body: ByteArray?,
        auth: Boolean = true,
    ): Pair<Int, String> {
        val conn = open(url, method, auth)
        if (body != null) {
            conn.doOutput = true
            contentType?.let { conn.setRequestProperty("Content-Type", it) }
            conn.setFixedLengthStreamingMode(body.size)
        }
        return exchange(conn) { out -> body?.let { out.write(it) } }
    }

    private fun open(url: String, method: String, auth: Boolean): HttpURLConnection {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = timeoutMs
        conn.readTimeout = timeoutMs
        if (auth && !token.isNullOrBlank()) {
            conn.setRequestProperty("Authorization", "Bearer $token")
        }
        return conn
    }

    private fun exchange(
        conn: HttpURLConnection,
        write: (java.io.OutputStream) -> Unit,
    ): Pair<Int, String> {
        try {
            if (conn.doOutput) conn.outputStream.use(write)
            val status = conn.responseCode
            val stream = if (status >= 400) conn.errorStream else conn.inputStream
            val text = stream?.use { it.readBytes().toString(Charsets.UTF_8) } ?: ""
            return status to text
        } catch (e: IOException) {
            throw ApiError(0, e.message ?: e.javaClass.simpleName)
        } finally {
            conn.disconnect()
        }
    }

    /** The board's JSON, or an ApiError: whatever else answers on that
     *  address (a hotel Wi-Fi login page, another service) must not crash
     *  the app. */
    private fun parse(status: Int, text: String): JSONObject =
        try {
            JSONObject(text)
        } catch (e: JSONException) {
            throw ApiError(status, "not the board's reply: ${text.take(80)}")
        }

    private fun reason(text: String): String =
        runCatching { JSONObject(text).optString("error", text) }.getOrDefault(text)

    companion object {
        const val GRAPH = "meeting_minutes"
    }
}
