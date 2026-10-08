package io.ten.meetingminutes.store

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Where the board is and how the record should read. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var host: String
        get() = prefs.getString("host", "") ?: ""
        set(v) = prefs.edit().putString("host", v.trim()).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(v) = prefs.edit().putString("token", v.trim()).apply()

    /** "traditional" or "simplified"; sent with every upload. */
    var script: String
        get() = prefs.getString("script", "traditional") ?: "traditional"
        set(v) = prefs.edit().putString("script", v).apply()
}

/** One meeting as the phone knows it. The recording stays on the phone
 *  until the board says the meeting is done: the board never resumes an
 *  upload, so a failed one is sent again from here. */
data class Meeting(
    val id: String,
    val title: String,
    val speakers: Int,
    val script: String,
    val recordedAtMs: Long,
    val durationS: Double,
    val file: String,
    val uploadedAtMs: Long = 0,
    val state: String = "local",
    val done: Int = 0,
    val total: Int = 0,
    val error: String? = null,
    /** The phone has all the board will give: a final state, and the
     *  record if there is one. Nothing more to ask. */
    val settled: Boolean = false,
    val names: Map<Int, String> = emptyMap(),
    /** While queued on the board: how many meetings go first. */
    val ahead: Int = 0,
    /** When the "should be ready" notification is set for. */
    val dueAtMs: Long = 0,
) {
    val finished get() = state in setOf("archived", "empty", "failed")

    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("title", title).put("speakers", speakers)
        .put("script", script).put("recordedAtMs", recordedAtMs)
        .put("durationS", durationS).put("file", file)
        .put("uploadedAtMs", uploadedAtMs).put("state", state)
        .put("done", done).put("total", total).put("error", error ?: JSONObject.NULL)
        .put("settled", settled)
        .put("names", JSONObject(names.mapKeys { it.key.toString() }))
        .put("ahead", ahead).put("dueAtMs", dueAtMs)

    companion object {
        fun fromJson(j: JSONObject): Meeting {
            val names = j.optJSONObject("names") ?: JSONObject()
            return Meeting(
                id = j.getString("id"),
                title = j.optString("title"),
                speakers = j.optInt("speakers"),
                script = j.optString("script", "traditional"),
                recordedAtMs = j.optLong("recordedAtMs"),
                durationS = j.optDouble("durationS", 0.0),
                file = j.optString("file"),
                uploadedAtMs = j.optLong("uploadedAtMs"),
                state = j.optString("state", "local"),
                done = j.optInt("done"),
                total = j.optInt("total"),
                error = if (j.isNull("error")) null else j.optString("error"),
                settled = j.optBoolean("settled"),
                names = names.keys().asSequence().associate { it.toInt() to names.getString(it) },
                ahead = j.optInt("ahead"),
                dueAtMs = j.optLong("dueAtMs"),
            )
        }
    }
}

/** The phone's own list of meetings, newest first, in one JSON file. */
class MeetingStore(dir: File) {
    constructor(context: Context) : this(context.filesDir)

    private val file = File(dir, "meetings.json")
    private val records = File(dir, "records").apply { mkdirs() }

    fun all(): List<Meeting> = synchronized(LOCK) {
        if (!file.exists()) return emptyList()
        val arr = JSONArray(file.readText())
        (0 until arr.length()).map { Meeting.fromJson(arr.getJSONObject(it)) }
            .sortedByDescending { it.recordedAtMs }
    }

    fun get(id: String): Meeting? = all().firstOrNull { it.id == id }

    fun put(m: Meeting): Unit = synchronized(LOCK) {
        val rest = all().filterNot { it.id == m.id }
        val arr = JSONArray()
        (listOf(m) + rest).forEach { arr.put(it.toJson()) }
        val tmp = File(file.path + ".tmp")
        tmp.writeText(arr.toString())
        tmp.renameTo(file)
    }

    /** The board's record.json, kept so the meeting reads offline. */
    fun saveRecord(id: String, json: String) = File(records, "$id.json").writeText(json)

    fun record(id: String): String? = File(records, "$id.json").takeIf { it.exists() }?.readText()

    /** The meeting gone from the list, and its record with it. */
    fun remove(id: String): Unit = synchronized(LOCK) {
        val rest = all().filterNot { it.id == id }
        val arr = JSONArray()
        rest.forEach { arr.put(it.toJson()) }
        val tmp = File(file.path + ".tmp")
        tmp.writeText(arr.toString())
        tmp.renameTo(file)
        dropRecord(id)
    }

    /** An earlier run's record, gone before the meeting is sent again. */
    fun dropRecord(id: String) {
        File(records, "$id.json").delete()
    }

    private companion object {
        // One lock for every instance: an upload outlives the screen that
        // started it, and a rotated activity makes a new store.
        val LOCK = Any()
    }
}
