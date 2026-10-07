package io.ten.meetingminutes.recording

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import io.ten.meetingminutes.MainActivity
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.File

/** The recording in progress, and the one just finished, for the UI. */
object Recording {
    data class Active(val file: File, val startedAtMs: Long)
    data class Finished(val file: File, val startedAtMs: Long, val durationS: Double)

    val active = MutableStateFlow<Active?>(null)
    val finished = MutableStateFlow<Finished?>(null)

    /** Where recordings go, each named for when it started: <ms>.ogg. */
    fun folder(context: Context) = File(context.filesDir, "recordings")
}

/** A recording no meeting holds: the app was killed after "stop" and before
 *  the upload form was sent, or while recording. Its length is read off the
 *  file -- named for its start, last written at its end. */
object LeftBehind {
    private val NAME = Regex("""(\d+)\.ogg""")

    fun find(dir: File, known: Set<String>): Recording.Finished? =
        (dir.listFiles() ?: emptyArray())
            .filter { it.path !in known && it.length() > 0 }
            .mapNotNull { f ->
                NAME.matchEntire(f.name)?.groupValues?.get(1)?.toLongOrNull()?.let { start ->
                    Recording.Finished(f, start, maxOf(0L, f.lastModified() - start) / 1000.0)
                }
            }
            .maxByOrNull { it.startedAtMs }
}

/**
 * Records the whole meeting into one Ogg-Opus file, 16 kHz mono -- the only
 * format the board takes. A foreground service, so the screen going off or
 * the app going to the background does not stop it.
 */
class RecorderService : Service() {
    private var recorder: MediaRecorder? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> begin(File(intent.getStringExtra(EXTRA_FILE)!!))
            ACTION_STOP -> end()
        }
        return START_NOT_STICKY
    }

    private fun begin(file: File) {
        if (recorder != null) return
        startForeground(
            NOTIFICATION_ID, notification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
        )
        @Suppress("DEPRECATION")
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else MediaRecorder()
        r.setAudioSource(MediaRecorder.AudioSource.MIC)
        r.setOutputFormat(MediaRecorder.OutputFormat.OGG)
        r.setAudioEncoder(MediaRecorder.AudioEncoder.OPUS)
        r.setAudioChannels(1)
        r.setAudioSamplingRate(16_000)
        r.setAudioEncodingBitRate(24_000)
        r.setOutputFile(file.path)
        r.prepare()
        r.start()
        recorder = r
        Recording.finished.value = null
        Recording.active.value = Recording.Active(file, System.currentTimeMillis())
    }

    private fun end() {
        val active = Recording.active.value
        recorder?.runCatching { stop() }
        recorder?.release()
        recorder = null
        if (active != null) {
            val durationS = (System.currentTimeMillis() - active.startedAtMs) / 1000.0
            Recording.finished.value =
                Recording.Finished(active.file, active.startedAtMs, durationS)
        }
        Recording.active.value = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "錄音中", NotificationManager.IMPORTANCE_LOW)
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("會議錄音中")
            .setContentText("點開回到 APP，散會時結束錄音")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val EXTRA_FILE = "file"
        private const val CHANNEL = "recording"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context, file: File) {
            context.startForegroundService(
                Intent(context, RecorderService::class.java)
                    .setAction(ACTION_START).putExtra(EXTRA_FILE, file.path)
            )
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, RecorderService::class.java).setAction(ACTION_STOP)
            )
        }
    }
}
