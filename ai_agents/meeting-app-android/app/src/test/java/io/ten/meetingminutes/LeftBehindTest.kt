package io.ten.meetingminutes

import io.ten.meetingminutes.recording.LeftBehind
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * A recording the app was killed away from -- after "stop" but before the
 * upload form was sent, or in the middle -- is offered again, never lost.
 */
class LeftBehindTest {
    private val dir: File = Files.createTempDirectory("recordings").toFile()

    @After
    fun down() {
        dir.deleteRecursively()
    }

    private fun recording(startMs: Long, endMs: Long, bytes: Int = 5) =
        File(dir, "$startMs.ogg").apply {
            writeBytes(ByteArray(bytes))
            setLastModified(endMs)
        }

    @Test
    fun aRecordingNoMeetingKnowsIsOfferedWithItsLength() {
        val f = recording(1_790_663_400_000L, 1_790_663_400_000L + 1_800_000L)

        val found = LeftBehind.find(dir, known = emptySet())!!

        assertEquals(f.path, found.file.path)
        assertEquals(1_790_663_400_000L, found.startedAtMs)
        assertEquals(1800.0, found.durationS, 1.0)
    }

    @Test
    fun aRecordingAMeetingAlreadyHoldsIsLeftAlone() {
        val f = recording(1_000_000L, 2_000_000L)

        assertNull(LeftBehind.find(dir, known = setOf(f.path)))
    }

    @Test
    fun anEmptyFileOrAnotherNameIsNotARecording() {
        recording(1_000_000L, 2_000_000L, bytes = 0)
        File(dir, "notes.txt").writeText("x")

        assertNull(LeftBehind.find(dir, known = emptySet()))
    }

    @Test
    fun theNewestIsOfferedFirst() {
        recording(1_000_000L, 2_000_000L)
        val newer = recording(3_000_000L, 4_000_000L)

        assertEquals(newer.path, LeftBehind.find(dir, known = emptySet())!!.file.path)
    }

    @Test
    fun noFolderIsNothing() {
        assertNull(LeftBehind.find(File(dir, "missing"), known = emptySet()))
    }
}
