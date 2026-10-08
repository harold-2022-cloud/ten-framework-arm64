package io.ten.meetingminutes

import io.ten.meetingminutes.recording.OggOpusWriter
import io.ten.meetingminutes.recording.Opus
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The recording file the app writes itself: Ogg pages around Opus packets,
 * as RFC 7845 lays them out, saying 16 kHz mono -- what the board's
 * uploader checks for. The packets are real: libsndfile encoded a made-up
 * three-second signal at 16 kHz mono, and its 151 packets were kept.
 */
class OggOpusTest {
    private val packets: List<ByteArray> = javaClass.getResourceAsStream("/opus_packets_16k_mono.bin")!!.use {
        val input = DataInputStream(it)
        buildList {
            while (input.available() > 0) {
                val n = input.readUnsignedShort()
                add(ByteArray(n).also(input::readFully))
            }
        }
    }

    // libsndfile's own page granules agree with these lengths (checked once
    // when the fixture was made); 151 packets of 10 and 20 ms.
    private val allSamples48k = 144_480L

    private class Page(
        val type: Int,
        val granule: Long,
        val serial: Int,
        val sequence: Int,
        val segments: Int,
        val packets: List<ByteArray>,
    )

    /** An Ogg reader of the test's own, checking each page's CRC. */
    private fun pages(bytes: ByteArray): List<Page> {
        val out = mutableListOf<Page>()
        var pos = 0
        var partial = ByteArray(0)
        while (pos < bytes.size) {
            assertEquals("OggS", String(bytes, pos, 4, Charsets.US_ASCII))
            assertEquals(0, bytes[pos + 4].toInt())
            val le = ByteBuffer.wrap(bytes, pos, 27).order(ByteOrder.LITTLE_ENDIAN)
            val type = bytes[pos + 5].toInt()
            val granule = le.getLong(pos + 6)
            val serial = le.getInt(pos + 14)
            val sequence = le.getInt(pos + 18)
            val crc = le.getInt(pos + 22)
            val segments = bytes[pos + 26].toInt() and 0xFF
            val lacing = (0 until segments).map { bytes[pos + 27 + it].toInt() and 0xFF }
            val end = pos + 27 + segments + lacing.sum()
            val page = bytes.copyOfRange(pos, end)
            for (i in 22..25) page[i] = 0
            assertEquals("CRC of page $sequence", crc, slowCrc(page))
            var body = pos + 27 + segments
            val done = mutableListOf<ByteArray>()
            for (l in lacing) {
                partial += bytes.copyOfRange(body, body + l)
                body += l
                if (l < 255) {
                    done += partial
                    partial = ByteArray(0)
                }
            }
            out += Page(type, granule, serial, sequence, segments, done)
            pos = end
        }
        return out
    }

    /** Ogg's CRC-32, bit by bit: polynomial 0x04C11DB7, no reflection. */
    private fun slowCrc(bytes: ByteArray): Int {
        var crc = 0
        for (b in bytes) {
            crc = crc xor ((b.toInt() and 0xFF) shl 24)
            repeat(8) {
                crc = if (crc and 0x80000000.toInt() != 0) (crc shl 1) xor 0x04C11DB7 else crc shl 1
            }
        }
        return crc
    }

    private fun written(preSkip: Int = 312): ByteArray {
        val out = ByteArrayOutputStream()
        OggOpusWriter(out, preSkip, serial = 1234).use { w -> packets.forEach(w::write) }
        return out.toByteArray()
    }

    @Test
    fun theFileOpensWithAnOpusHeadSaying16kMonoThenOpusTags() {
        val pages = pages(written())

        val head = pages[0].packets.single()
        val le = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("OpusHead", String(head, 0, 8, Charsets.US_ASCII))
        assertEquals(1, head[8].toInt())            // version
        assertEquals(1, head[9].toInt())            // channels
        assertEquals(312, le.getShort(10).toInt())  // pre-skip
        assertEquals(16_000, le.getInt(12))         // input sample rate
        assertEquals(0, le.getShort(16).toInt())    // output gain
        assertEquals(0, head[18].toInt())           // mapping family
        assertEquals(0x02, pages[0].type)
        assertEquals(0L, pages[0].granule)

        val tags = pages[1].packets.single()
        assertEquals("OpusTags", String(tags, 0, 8, Charsets.US_ASCII))
        assertEquals(0, pages[1].type)
        assertEquals(0L, pages[1].granule)
    }

    @Test
    fun everyPacketComesBackInOrder() {
        val back = pages(written()).drop(2).flatMap { it.packets }

        assertEquals(packets.size, back.size)
        packets.zip(back).forEach { (a, b) -> assertArrayEquals(a, b) }
    }

    @Test
    fun theLastPageEndsTheStreamAndCountsEverySample() {
        val pages = pages(written())

        assertEquals(0x04, pages.last().type)
        assertEquals(312 + allSamples48k, pages.last().granule)
        assertEquals(listOf(0x02), pages.map { it.type }.filter { it and 0x02 != 0 })
        assertEquals(1, pages.count { it.type and 0x04 != 0 })
    }

    @Test
    fun thePagesAreOneStreamInSequence() {
        val pages = pages(written())

        assertEquals(pages.indices.toList(), pages.map { it.sequence })
        assertEquals(setOf(1234), pages.map { it.serial }.toSet())
        assertTrue(pages.all { it.segments <= 255 })
        assertTrue(pages.zipWithNext().all { (a, b) -> b.granule >= a.granule })
        // About a second of audio a page, so a file is not mostly headers.
        assertTrue(pages.size - 2 in 3..5)
    }

    @Test
    fun pagesReachTheStreamAsTheyFillNotOnlyAtTheEnd() {
        // A recording the app is killed in the middle of keeps all but the
        // last second.
        val out = ByteArrayOutputStream()
        val w = OggOpusWriter(out, 312, serial = 1)
        packets.take(120).forEach(w::write)

        val pages = pages(out.toByteArray())
        assertTrue(pages.size >= 3)
        assertTrue(pages.none { it.type and 0x04 != 0 })
    }

    @Test
    fun aPacketsLengthComesFromItsToc() {
        assertEquals(480, Opus.samples48k(byteArrayOf((8 shl 3).toByte())))        // SILK 10 ms
        assertEquals(960, Opus.samples48k(byteArrayOf((9 shl 3).toByte())))        // SILK 20 ms
        assertEquals(2880, Opus.samples48k(byteArrayOf((11 shl 3).toByte())))      // SILK 60 ms
        assertEquals(960, Opus.samples48k(byteArrayOf((13 shl 3).toByte())))       // hybrid 20 ms
        assertEquals(120, Opus.samples48k(byteArrayOf((16 shl 3).toByte())))       // CELT 2.5 ms
        assertEquals(1920, Opus.samples48k(byteArrayOf(((9 shl 3) or 1).toByte()))) // two frames
        assertEquals(2880, Opus.samples48k(byteArrayOf(((9 shl 3) or 3).toByte(), 3))) // three
        assertEquals(allSamples48k, packets.sumOf { Opus.samples48k(it).toLong() })
    }

    @Test
    fun thePreSkipIsTheEncodersOwn() {
        val head = byteArrayOf(
            'O'.code.toByte(), 'p'.code.toByte(), 'u'.code.toByte(), 's'.code.toByte(),
            'H'.code.toByte(), 'e'.code.toByte(), 'a'.code.toByte(), 'd'.code.toByte(),
            1, 1, 0x38, 0x01, 0x80.toByte(), 0x3E, 0, 0, 0, 0, 0,
        )
        // Android's encoder hands its headers over behind markers.
        val csd = "AOPUSHDR".toByteArray() + ByteArray(8).also { it[0] = head.size.toByte() } + head

        assertEquals(312, Opus.preSkip(head))
        assertEquals(312, Opus.preSkip(csd))
        assertNull(Opus.preSkip(byteArrayOf(1, 2, 3)))
    }

    @Test
    fun aFileForTheBoardToRead() {
        // Kept for decoding with the board's own reader (soundfile): see
        // DEVELOPMENT.md. Not an assertion here -- the JVM has no Opus decoder.
        File("build/test-output").mkdirs()
        File("build/test-output/oggopus-16k-mono.ogg").writeBytes(written())
    }
}
