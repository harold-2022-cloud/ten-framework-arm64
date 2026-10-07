package io.ten.meetingminutes.notify

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import io.ten.meetingminutes.MainActivity

/**
 * "The minutes should be ready": one local notification at the estimated
 * finish. Not polling in the background -- iOS forbids it and Android's
 * background work runs at most every 15 minutes -- the app checks for real
 * when the user opens it (app requirements F6, F7).
 */
object Due {
    private const val CHANNEL = "meeting_due"
    const val EXTRA_MEETING = "meeting_id"
    const val EXTRA_TITLE = "title"

    fun schedule(context: Context, meetingId: String, title: String, atMs: Long) {
        val alarms = context.getSystemService(AlarmManager::class.java)
        alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, intent(context, meetingId, title))
    }

    fun cancel(context: Context, meetingId: String) {
        context.getSystemService(AlarmManager::class.java)
            .cancel(intent(context, meetingId, ""))
    }

    private fun intent(context: Context, meetingId: String, title: String) =
        PendingIntent.getBroadcast(
            context, meetingId.hashCode(),
            Intent(context, DueReceiver::class.java)
                .putExtra(EXTRA_MEETING, meetingId).putExtra(EXTRA_TITLE, title),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    fun post(context: Context, meetingId: String, title: String) {
        if (context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) return
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "會議記錄完成", NotificationManager.IMPORTANCE_DEFAULT)
        )
        val open = PendingIntent.getActivity(
            context, meetingId.hashCode(),
            Intent(context, MainActivity::class.java).putExtra(EXTRA_MEETING, meetingId),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        nm.notify(
            meetingId.hashCode(),
            Notification.Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("會議記錄應該好了")
                .setContentText(title.ifBlank { "點開查看" })
                .setContentIntent(open)
                .setAutoCancel(true)
                .build(),
        )
    }
}

class DueReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Due.post(
            context,
            intent.getStringExtra(Due.EXTRA_MEETING) ?: return,
            intent.getStringExtra(Due.EXTRA_TITLE) ?: "",
        )
    }
}
