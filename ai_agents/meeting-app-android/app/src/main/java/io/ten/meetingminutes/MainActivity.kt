package io.ten.meetingminutes

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import io.ten.meetingminutes.notify.Due
import io.ten.meetingminutes.ui.MeetingApp

class MainActivity : ComponentActivity() {
    /** A meeting to open, from the "minutes should be ready" notification. */
    private val open = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        open.value = intent?.getStringExtra(Due.EXTRA_MEETING)
        setContent { MeetingApp(open) }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(Due.EXTRA_MEETING)?.let { open.value = it }
    }
}
