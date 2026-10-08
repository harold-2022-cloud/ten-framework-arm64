package io.ten.meetingminutes.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.ten.meetingminutes.board.MeetingRecord
import io.ten.meetingminutes.domain.Ids
import io.ten.meetingminutes.domain.Minutes
import io.ten.meetingminutes.domain.Texts
import io.ten.meetingminutes.domain.Work
import io.ten.meetingminutes.recording.LeftBehind
import io.ten.meetingminutes.recording.RecorderService
import io.ten.meetingminutes.recording.Recording
import io.ten.meetingminutes.store.Meeting
import io.ten.meetingminutes.store.MeetingStore
import io.ten.meetingminutes.store.Settings
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private sealed interface Screen {
    data object Home : Screen
    data object Setup : Screen
    data class Details(val id: String) : Screen
}

@Composable
fun MeetingApp(open: MutableState<String?>) {
    val context = LocalContext.current
    val settings = remember { Settings(context) }
    val store = remember { MeetingStore(context) }
    var screen by remember { mutableStateOf<Screen>(Screen.Home) }
    val active by Recording.active.collectAsState()
    val finished by Recording.finished.collectAsState()

    // A recording the app was killed away from comes back to its form.
    LaunchedEffect(Unit) {
        if (Recording.active.value == null && Recording.finished.value == null) {
            val known = store.all().map { it.file }.toSet()
            LeftBehind.find(Recording.folder(context), known)?.let { Recording.finished.value = it }
        }
    }
    LaunchedEffect(open.value) {
        open.value?.let { screen = Screen.Details(it); open.value = null }
    }
    BackHandler(enabled = screen != Screen.Home) { screen = Screen.Home }

    MaterialTheme {
        Surface(Modifier.fillMaxSize()) {
            Column(Modifier.safeDrawingPadding().padding(16.dp)) {
                val recording = active
                val done = finished
                when {
                    recording != null -> RecordingScreen(recording) { RecorderService.stop(context) }
                    done != null -> UploadForm(done, settings, store) { id ->
                        Recording.finished.value = null
                        screen = if (id != null) Screen.Details(id) else Screen.Home
                    }
                    else -> when (val s = screen) {
                        Screen.Home -> HomeScreen(settings, store, onSetup = { screen = Screen.Setup }) {
                            screen = Screen.Details(it)
                        }
                        Screen.Setup -> SetupScreen(settings) { screen = Screen.Home }
                        is Screen.Details -> DetailsScreen(s.id, settings, store) { screen = Screen.Home }
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------- home

@Composable
private fun HomeScreen(
    settings: Settings,
    store: MeetingStore,
    onSetup: () -> Unit,
    onOpen: (String) -> Unit,
) {
    val context = LocalContext.current
    val meetings by produceState(store.all()) {
        while (true) {
            value = store.all()
            delay(2_000)
        }
    }
    val ask = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        if (granted[Manifest.permission.RECORD_AUDIO] == true) startRecording(context)
    }

    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text("會議記錄", fontSize = 24.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
        TextButton(onClick = onSetup) { Text("設定") }
    }
    Spacer(Modifier.height(12.dp))
    val problem by Recording.problem.collectAsState()
    problem?.let {
        Text(it, color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(12.dp))
    }
    if (settings.host.isBlank()) {
        Text("請先到「設定」填入會議室板子的位址。", color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(12.dp))
    }
    Button(
        onClick = {
            val wanted = buildList {
                add(Manifest.permission.RECORD_AUDIO)
                if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
            }
            if (wanted.all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }) {
                startRecording(context)
            } else {
                ask.launch(wanted.toTypedArray())
            }
        },
        enabled = settings.host.isNotBlank(),
        modifier = Modifier.fillMaxWidth().height(56.dp),
    ) { Text("開始錄音", fontSize = 18.sp) }
    Spacer(Modifier.height(20.dp))
    if (meetings.isEmpty()) {
        Text("還沒有會議。", color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(meetings, key = { it.id }) { m ->
            Card(Modifier.fillMaxWidth().clickable { onOpen(m.id) }) {
                Column(Modifier.padding(12.dp)) {
                    Text(m.title.ifBlank { "（未命名的會議）" }, fontWeight = FontWeight.SemiBold)
                    Text(
                        "${when_(m.recordedAtMs)} · ${minutes(m.durationS)} · ${stateText(m)}",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontSize = 13.sp,
                    )
                }
            }
        }
    }
}

private fun startRecording(context: Context) {
    val dir = Recording.folder(context).apply { mkdirs() }
    RecorderService.start(context, File(dir, "${System.currentTimeMillis()}.ogg"))
}

// ----------------------------------------------------------- recording

@Composable
private fun RecordingScreen(active: Recording.Active, onStop: () -> Unit) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(active) {
        while (true) {
            now = System.currentTimeMillis()
            delay(1000)
        }
    }
    var confirm by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize(), horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(48.dp))
        Text("錄音中", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(16.dp))
        Text(clock((now - active.startedAtMs) / 1000), fontSize = 56.sp, fontWeight = FontWeight.Light)
        Spacer(Modifier.height(16.dp))
        Text(
            "整場連續錄成一個檔。螢幕關閉、切到別的 APP 都會繼續錄。",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(40.dp))
        Button(onClick = { confirm = true }, modifier = Modifier.fillMaxWidth().height(56.dp)) {
            Text("散會，結束錄音", fontSize = 18.sp)
        }
    }
    if (confirm) {
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text("結束錄音？") },
            text = { Text("結束後就不能再接著錄進同一個檔。") },
            confirmButton = { TextButton(onClick = { confirm = false; onStop() }) { Text("結束") } },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text("繼續錄") } },
        )
    }
}

// --------------------------------------------------------------- upload

@Composable
private fun UploadForm(
    done: Recording.Finished,
    settings: Settings,
    store: MeetingStore,
    onClose: (String?) -> Unit,
) {
    val context = LocalContext.current
    var title by remember { mutableStateOf("") }
    var people by remember { mutableStateOf("") }
    var script by remember { mutableStateOf(settings.script) }
    var discard by remember { mutableStateOf(false) }

    Column(Modifier.verticalScroll(rememberScrollState())) {
        Text("錄好了", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text("長度 ${minutes(done.durationS)}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(16.dp))
        OutlinedTextField(title, { title = it }, label = { Text("會議標題（選填）") }, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            people, { people = it.filter(Char::isDigit).take(2) },
            label = { Text("與會人數") },
            supportingText = { Text("寧多勿少：多填一兩人幾乎沒影響，少填會把兩個人當成同一個。") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        ScriptChoice(script) { script = it }
        Spacer(Modifier.height(16.dp))
        Button(
            onClick = {
                val m = Meeting(
                    id = Ids.meetingId(done.startedAtMs),
                    title = title.trim(),
                    speakers = people.toIntOrNull() ?: 0,
                    script = script,
                    recordedAtMs = done.startedAtMs,
                    durationS = done.durationS,
                    file = done.file.path,
                )
                store.put(m)
                Work.startUpload(context, settings, store, m)
                onClose(m.id)
            },
            enabled = (people.toIntOrNull() ?: 0) >= 1,
            modifier = Modifier.fillMaxWidth().height(56.dp),
        ) { Text("上傳到會議室的板子", fontSize = 18.sp) }
        Spacer(Modifier.height(8.dp))
        TextButton(onClick = { discard = true }) { Text("捨棄這段錄音") }
    }
    if (discard) {
        AlertDialog(
            onDismissRequest = { discard = false },
            title = { Text("捨棄錄音？") },
            text = { Text("錄音檔會從手機刪除，無法復原。") },
            confirmButton = {
                TextButton(onClick = { done.file.delete(); discard = false; onClose(null) }) { Text("捨棄") }
            },
            dismissButton = { TextButton(onClick = { discard = false }) { Text("保留") } },
        )
    }
}

@Composable
private fun ScriptChoice(script: String, onChange: (String) -> Unit) {
    Text("記錄文字", fontWeight = FontWeight.SemiBold)
    Row(verticalAlignment = Alignment.CenterVertically) {
        RadioButton(script == "traditional", { onChange("traditional") })
        Text("繁體", Modifier.clickable { onChange("traditional") })
        Spacer(Modifier.padding(8.dp))
        RadioButton(script == "simplified", { onChange("simplified") })
        Text("简体", Modifier.clickable { onChange("simplified") })
    }
}

// -------------------------------------------------------------- details

@Composable
private fun DetailsScreen(id: String, settings: Settings, store: MeetingStore, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var m by remember { mutableStateOf(store.get(id)) }
    var busy by remember { mutableStateOf(false) }
    val sending by Work.uploading.collectAsState()

    // The phone's copy is read every 2 s, which shows an upload moving
    // along. The board is asked every 30 s, and only while it knows more
    // than the phone. Nothing is asked once this screen is gone (F6, F7).
    LaunchedEffect(id) {
        var asked = 0L
        while (true) {
            val cur = store.get(id) ?: break
            val now = System.currentTimeMillis()
            m = if (askBoard(cur) && id !in Work.uploading.value && now - asked >= 30_000) {
                asked = now
                Work.refresh(context, settings, store, cur)
            } else {
                cur
            }
            delay(2_000)
        }
    }
    val meeting = m ?: run { Text("找不到這場會議。"); return }
    val record = remember(meeting.state, meeting.uploadedAtMs, meeting.settled) {
        store.record(id)?.let { runCatching { MeetingRecord.parse(it) }.getOrNull() }
    }

    var deleting by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = onBack) { Text("‹ 會議") }
        Spacer(Modifier.weight(1f))
        if (id !in sending) {
            TextButton(onClick = { deleting = true }) {
                Text("刪除", color = MaterialTheme.colorScheme.error)
            }
        }
    }
    if (deleting) {
        AlertDialog(
            onDismissRequest = { deleting = false },
            title = { Text("刪除這場會議？") },
            text = {
                Text(
                    if (meeting.uploadedAtMs > 0) "錄音和手機上的記錄都會刪除，無法復原。板子上的記錄不受影響。"
                    else "錄音檔會從手機刪除，無法復原。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    deleting = false
                    if (Work.delete(context, store, meeting)) onBack()
                }) { Text("刪除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { deleting = false }) { Text("保留") } },
        )
    }
    Text(meeting.title.ifBlank { "（未命名的會議）" }, fontSize = 22.sp, fontWeight = FontWeight.Bold)
    Text(
        "${when_(meeting.recordedAtMs)} · ${minutes(meeting.durationS)} · ${meeting.speakers} 人",
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    Spacer(Modifier.height(12.dp))

    if (meeting.state != "archived" || record == null) {
        Text(stateText(meeting), fontSize = 18.sp)
        if (meeting.uploadedAtMs > 0 && !meeting.finished) {
            val eta = meeting.dueAtMs.takeIf { it > 0 }
                ?: (meeting.uploadedAtMs + ((meeting.durationS + 120) * 1000).toLong())
            Text(
                "預計 ${SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(eta))} 左右完成；可以先離開，到時會通知。",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        meeting.error?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, color = MaterialTheme.colorScheme.error)
        }
        Spacer(Modifier.height(12.dp))
        val here = id in sending
        // Left "starting" or "uploading" with nothing sending it: the app
        // was killed halfway.
        val cutOff = meeting.state in setOf("starting", "uploading") && !here
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (!here && (cutOff || meeting.state in setOf("local", "upload_failed", "failed"))) {
                Button(onClick = { Work.startUpload(context, settings, store, meeting) }) {
                    Text(if (meeting.state == "local") "上傳" else "重新上傳")
                }
            }
            if (askBoard(meeting) && !here) {
                OutlinedButton(enabled = !busy, onClick = {
                    busy = true
                    scope.launch { m = Work.refresh(context, settings, store, meeting); busy = false }
                }) { Text(if (busy) "詢問板子中…" else "重新整理") }
            }
        }
        Spacer(Modifier.height(12.dp))
    }
    if (record != null) RecordView(record, meeting, store) { m = it }
}

/** Whether the board knows more about the meeting than the phone: it was
 *  sent and is not settled. */
private fun askBoard(m: Meeting) = m.uploadedAtMs > 0 && !m.settled

@Composable
private fun RecordView(record: MeetingRecord, meeting: Meeting, store: MeetingStore, onChange: (Meeting) -> Unit) {
    val context = LocalContext.current
    var names by remember { mutableStateOf(meeting.names) }
    var naming by remember { mutableStateOf(false) }
    val simplified = meeting.script == "simplified"
    val speakers = (record.topics.flatMap { t -> t.utterances.map { it.speaker } }.maxOrNull() ?: -1) + 1

    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { naming = !naming }) { Text(if (naming) "完成命名" else "說話人命名") }
        Button(onClick = {
            val text = Minutes.share(record, names, meeting.script)
            context.startActivity(
                Intent.createChooser(
                    Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text),
                    "分享會議記錄",
                )
            )
        }) { Text("分享") }
    }
    Spacer(Modifier.height(8.dp))
    LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (naming) {
            items((0 until speakers).toList(), key = { "name$it" }) { n ->
                OutlinedTextField(
                    names[n] ?: "",
                    { v ->
                        names = names + (n to v)
                        val next = meeting.copy(names = names.filterValues { it.isNotBlank() })
                        store.put(next)
                        onChange(next)
                    },
                    label = { Text(Minutes.speaker(n, emptyMap(), meeting.script)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }
        item(key = "summary") {
            Spacer(Modifier.height(8.dp))
            Text(if (simplified) "结论" else "結論", fontWeight = FontWeight.Bold, fontSize = 18.sp)
            Text(record.summary.ifBlank { "（沒有產出結論）" })
            Spacer(Modifier.height(12.dp))
        }
        record.topics.forEachIndexed { i, t ->
            item(key = "topic${t.id}") {
                HorizontalDivider()
                Spacer(Modifier.height(8.dp))
                Text(
                    "${if (simplified) "话题" else "話題"} ${i + 1} · ${Minutes.mmss(t.startS)}–${Minutes.mmss(t.endS)}",
                    fontWeight = FontWeight.Bold,
                )
                if (t.summary.isNotBlank()) Text(t.summary, color = MaterialTheme.colorScheme.primary)
                t.error?.let { Text("這一段未能處理：$it", color = MaterialTheme.colorScheme.error) }
                Spacer(Modifier.height(6.dp))
                for (u in t.utterances) {
                    Text("[${Minutes.time(t, u)}] ${Minutes.speaker(u.speaker, names, meeting.script)}：${u.text}", fontSize = 14.sp)
                }
            }
        }
    }
}

// --------------------------------------------------------------- setup

@Composable
private fun SetupScreen(settings: Settings, onDone: () -> Unit) {
    var host by remember { mutableStateOf(settings.host) }
    var token by remember { mutableStateOf(settings.token) }
    var script by remember { mutableStateOf(settings.script) }
    Column(Modifier.verticalScroll(rememberScrollState())) {
        Text("設定", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            host, { host = it }, label = { Text("板子位址") },
            supportingText = { Text("會議室板子的 IP，例如 192.168.1.50；手機要連同一個網路。") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            token, { token = it }, label = { Text("權杖（選填）") },
            supportingText = { Text("板子有設定權杖時才需要。") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        ScriptChoice(script) { script = it }
        Spacer(Modifier.height(16.dp))
        Button(onClick = {
            settings.host = host
            settings.token = token
            settings.script = script
            onDone()
        }, modifier = Modifier.fillMaxWidth()) { Text("儲存") }
    }
}

// --------------------------------------------------------------- bits

private fun stateText(m: Meeting): String = when (m.state) {
    "local" -> "還沒上傳"
    "starting" -> "連線到板子…"
    "uploading" -> "上傳中…"
    "upload_failed" -> "上傳失敗"
    else -> Texts.state(m.state, m.done, m.total, m.error, m.ahead)
}

private fun when_(ms: Long) = SimpleDateFormat("M/d HH:mm", Locale.getDefault()).format(Date(ms))

private fun minutes(s: Double) = if (s < 60) "${s.toInt()} 秒" else "${(s / 60).toInt()} 分鐘"

private fun clock(s: Long) = "%02d:%02d:%02d".format(s / 3600, (s % 3600) / 60, s % 60)
