package io.ten.meetingminutes.recording

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaRecorder
import java.io.File
import java.io.FileOutputStream

/**
 * The microphone at 16 kHz mono, compressed by Android's own Opus encoder
 * (MediaCodec, there since Android 10), written by OggOpusWriter. The app
 * writes the file itself because MediaRecorder's Ogg muxer decides the
 * header on its own, and the board's uploader takes 16 kHz mono Ogg-Opus
 * and nothing else.
 *
 * Not covered by the unit tests (the JVM has neither the microphone nor the
 * encoder): OggOpusTest covers the file, a phone covers this.
 */
class OpusRecorder(private val file: File) {
    @Volatile private var running = false
    private var thread: Thread? = null
    private var record: AudioRecord? = null
    private var codec: MediaCodec? = null

    /** Throws, having let everything go, when the microphone or the encoder
     *  cannot be had -- the microphone in use by a call, say. */
    @SuppressLint("MissingPermission") // the app asks for RECORD_AUDIO before starting
    fun start() {
        try {
            val minBuffer = AudioRecord.getMinBufferSize(RATE, MONO, PCM_16)
            check(minBuffer > 0) { "這支手機無法以 16 kHz 單聲道錄音" }
            val r = AudioRecord(MediaRecorder.AudioSource.MIC, RATE, MONO, PCM_16, maxOf(minBuffer, RATE))
            record = r
            check(r.state == AudioRecord.STATE_INITIALIZED) { "麥克風無法開啟" }
            val c = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
            codec = c
            c.configure(
                MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, RATE, 1).apply {
                    setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
                },
                null, null, MediaCodec.CONFIGURE_FLAG_ENCODE,
            )
            c.start()
            r.startRecording()
            check(r.recordingState == AudioRecord.RECORDSTATE_RECORDING) { "麥克風正被其他程式使用" }
        } catch (e: Exception) {
            release()
            throw e
        }
        running = true
        thread = Thread(::loop, "opus-recorder").also { it.start() }
    }

    /** Stop, and finish the file: the encoder's last packets, the
     *  end-of-stream page. */
    fun stop() {
        running = false
        thread?.join(5_000)
        thread = null
    }

    private fun loop() {
        val r = record!!
        val c = codec!!
        val pcm = ByteArray(RATE / 50 * 2) // 20 ms of 16-bit samples
        val info = MediaCodec.BufferInfo()
        var samples = 0L
        var preSkip: Int? = null
        var writer: OggOpusWriter? = null
        fun openWriter() = writer ?: OggOpusWriter(FileOutputStream(file), preSkip ?: Opus.DEFAULT_PRE_SKIP)
            .also { writer = it }

        /** Takes what the encoder has ready; true at the end of the stream. */
        fun drain(timeoutUs: Long): Boolean {
            while (true) {
                val i = c.dequeueOutputBuffer(info, timeoutUs)
                when {
                    i == MediaCodec.INFO_TRY_AGAIN_LATER -> return false
                    i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        c.outputFormat.getByteBuffer("csd-0")?.let { csd ->
                            preSkip = Opus.preSkip(ByteArray(csd.remaining()).also { csd.get(it) }) ?: preSkip
                        }
                    }
                    i >= 0 -> {
                        val out = c.getOutputBuffer(i)!!
                        out.position(info.offset)
                        val bytes = ByteArray(info.size).also { out.get(it) }
                        c.releaseOutputBuffer(i, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                            preSkip = Opus.preSkip(bytes) ?: preSkip
                        } else if (bytes.isNotEmpty()) {
                            openWriter().write(bytes)
                        }
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return true
                    }
                }
            }
        }

        /** Hands n bytes of pcm to the encoder; with end, the last of them
         *  (n may be 0) carries the end-of-stream flag. */
        fun feed(n: Int, end: Boolean) {
            var off = 0
            val until = System.currentTimeMillis() + 2_000
            while (true) {
                val i = c.dequeueInputBuffer(10_000)
                if (i < 0) {
                    check(System.currentTimeMillis() < until) { "the encoder takes no input" }
                    drain(0) // an output taken frees an input
                    continue
                }
                val buf = c.getInputBuffer(i)!!
                buf.clear()
                val len = minOf(n - off, buf.remaining())
                buf.put(pcm, off, len)
                val last = end && off + len >= n
                c.queueInputBuffer(
                    i, 0, len, samples * 1_000_000L / RATE,
                    if (last) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0,
                )
                off += len
                samples += len / 2
                if (off >= n) return
            }
        }

        try {
            while (running) {
                val n = r.read(pcm, 0, pcm.size)
                if (n < 0) break
                if (n > 0) feed(n, end = false)
                drain(0)
            }
            feed(0, end = true)
            val until = System.currentTimeMillis() + 3_000
            while (!drain(10_000) && System.currentTimeMillis() < until) Unit
        } catch (_: Exception) {
            // Whatever was encoded is on disk already, a page a second.
        } finally {
            runCatching { openWriter().close() }
            release()
        }
    }

    private fun release() {
        record?.let { r ->
            runCatching { r.stop() }
            r.release()
        }
        codec?.let { c ->
            runCatching { c.stop() }
            c.release()
        }
        record = null
        codec = null
    }

    private companion object {
        const val RATE = 16_000
        const val BIT_RATE = 24_000
        const val MONO = AudioFormat.CHANNEL_IN_MONO
        const val PCM_16 = AudioFormat.ENCODING_PCM_16BIT
    }
}
