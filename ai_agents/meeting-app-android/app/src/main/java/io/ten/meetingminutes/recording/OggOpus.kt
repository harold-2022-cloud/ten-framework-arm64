package io.ten.meetingminutes.recording

import java.io.Closeable
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.random.Random

/** What the app needs to know about an Opus packet (RFC 6716). */
object Opus {
    /** libopus's look-ahead, 6.5 ms at 48 kHz: the pre-skip when the
     *  encoder does not say. */
    const val DEFAULT_PRE_SKIP = 312

    /** How long a packet plays, in samples at 48 kHz -- from its TOC byte:
     *  the configuration gives a frame's length, the code how many frames. */
    fun samples48k(packet: ByteArray): Int {
        val toc = packet[0].toInt() and 0xFF
        val config = toc shr 3
        val tenthsOfMs = when {
            config < 12 -> intArrayOf(100, 200, 400, 600)[config % 4] // SILK
            config < 16 -> intArrayOf(100, 200)[config % 2]           // hybrid
            else -> intArrayOf(25, 50, 100, 200)[config % 4]          // CELT
        }
        val frames = when (toc and 3) {
            0 -> 1
            1, 2 -> 2
            else -> if (packet.size > 1) packet[1].toInt() and 0x3F else 0
        }
        return frames * tenthsOfMs * 48 / 10
    }

    /** The pre-skip in the encoder's own OpusHead, wherever it sits in its
     *  codec-config bytes (Android puts it behind an "AOPUSHDR" marker). */
    fun preSkip(csd: ByteArray): Int? {
        val tag = "OpusHead".toByteArray(Charsets.US_ASCII)
        for (i in 0..csd.size - 19) {
            if ((tag.indices).all { csd[i + it] == tag[it] }) {
                return (csd[i + 10].toInt() and 0xFF) or ((csd[i + 11].toInt() and 0xFF) shl 8)
            }
        }
        return null
    }
}

/**
 * Ogg pages around Opus packets, as RFC 7845 lays them out: an OpusHead
 * page, an OpusTags page, then the audio about a second a page, the last
 * one marked end-of-stream. The app writes this itself, rather than
 * MediaRecorder's own Ogg muxer, so that every header field -- 16 kHz,
 * mono -- is what it says, whatever the phone: the board's uploader turns
 * anything else away (415).
 *
 * A page goes out once it holds a second of audio, so a recording the app
 * is killed in the middle of loses at most that second.
 */
class OggOpusWriter(
    private val out: OutputStream,
    preSkip: Int,
    inputRate: Int = 16_000,
    private val serial: Int = Random.nextInt(),
    private val pageSamples48k: Int = 48_000,
) : Closeable {
    private var sequence = 0
    private var granule = preSkip.toLong()
    private val pending = mutableListOf<ByteArray>()
    private var pendingSamples = 0

    init {
        val head = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN)
            .put("OpusHead".toByteArray(Charsets.US_ASCII))
            .put(1)                              // version
            .put(1)                              // channels
            .putShort(preSkip.toShort())
            .putInt(inputRate)
            .putShort(0)                         // output gain
            .put(0)                              // mapping family: mono/stereo
            .array()
        page(listOf(head), 0L, BOS)
        val vendor = "TEN meeting app".toByteArray(Charsets.UTF_8)
        val tags = ByteBuffer.allocate(8 + 4 + vendor.size + 4).order(ByteOrder.LITTLE_ENDIAN)
            .put("OpusTags".toByteArray(Charsets.US_ASCII))
            .putInt(vendor.size)
            .put(vendor)
            .putInt(0)                           // no user comments
            .array()
        page(listOf(tags), 0L, 0)
    }

    fun write(packet: ByteArray) {
        val segments = pending.sumOf { it.size / 255 + 1 } + packet.size / 255 + 1
        if (pending.isNotEmpty() && (pendingSamples >= pageSamples48k || segments > 255)) {
            flush(0)
        }
        pending += packet
        pendingSamples += Opus.samples48k(packet)
    }

    /** The last page, marked end-of-stream. */
    override fun close() {
        flush(EOS)
        out.close()
    }

    private fun flush(type: Int) {
        granule += pendingSamples
        page(pending.toList(), granule, type)
        pending.clear()
        pendingSamples = 0
    }

    private fun page(packets: List<ByteArray>, granule: Long, type: Int) {
        val lacing = mutableListOf<Int>()
        for (p in packets) {
            repeat(p.size / 255) { lacing += 255 }
            lacing += p.size % 255
        }
        val bodySize = packets.sumOf { it.size }
        val page = ByteBuffer.allocate(27 + lacing.size + bodySize).order(ByteOrder.LITTLE_ENDIAN)
            .put("OggS".toByteArray(Charsets.US_ASCII))
            .put(0)                              // version
            .put(type.toByte())
            .putLong(granule)
            .putInt(serial)
            .putInt(sequence++)
            .putInt(0)                           // CRC, filled in below
            .put(lacing.size.toByte())
        lacing.forEach { page.put(it.toByte()) }
        packets.forEach { page.put(it) }
        val bytes = page.array()
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).putInt(22, crc(bytes))
        out.write(bytes)
        out.flush()
    }

    private companion object {
        const val BOS = 0x02
        const val EOS = 0x04

        /** Ogg's CRC-32: polynomial 0x04C11DB7, no reflection, from 0. */
        val TABLE = IntArray(256) { n ->
            var r = n shl 24
            repeat(8) { r = if (r and 0x80000000.toInt() != 0) (r shl 1) xor 0x04C11DB7 else r shl 1 }
            r
        }

        fun crc(bytes: ByteArray): Int {
            var crc = 0
            for (b in bytes) {
                crc = (crc shl 8) xor TABLE[((crc ushr 24) xor (b.toInt() and 0xFF)) and 0xFF]
            }
            return crc
        }
    }
}
