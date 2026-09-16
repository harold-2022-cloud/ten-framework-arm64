# What went wrong in the board's first conversations

Every claim here is traced to a line of `/tmp/task_run.log` from the run of
2026-09-16 00:10–00:14 and to the code that produced it. Times are the log's
own, to the millisecond.

The run used `voice_assistant_sherpa_tts`: soniox ASR in the cloud, the board's
LLM on the Vector Processor, sherpa-onnx TTS on the CPU.

---

## P1 — The LLM answers in 15 to 47 seconds

**Log**

```
00:11:44.258  [Ambarella] first answer token after 47.0s (reset=True, query_len=103)
00:11:52.855  [Ambarella] turn done in 55.6s, first token at 47.0s, 77 chars
00:13:39.056  [Ambarella] first answer token after 14.9s (reset=False, query_len=11)
00:13:39.057  [Ambarella] turn done in 14.9s, first token at 14.9s, 168 chars
```

The queries were 103 and 11 characters. What the model spent the time on is in
the reasoning blocks: 498 characters on the first turn, 2992 on the third. The
model is `Deepseek-R1-Distill-Qwen`, which writes its reasoning out before
answering.

**Code** — `ambarella.py:351` reads the response with `resp.content.iter_any()`,
so chunks are processed as they arrive and nothing waits for a complete body.
`_ReasoningSplitter` does hold everything back until `</think>`, by design:
before that tag there is no way to tell reasoning from answer, and reasoning
must not reach the speaker.

**Verdict** — not a code defect. Releasing the reasoning earlier would not make
speech start sooner, because it is never spoken. The answer cannot begin before
the model stops thinking.

**Disproved on the way** — the first turn, which clears the board's history,
was the *slowest*. History is not the cause; an earlier note saying so was
wrong.

---

## P2 — The answer is cut off two seconds in

This is the failure that makes the assistant unusable, and it follows from P1.

**Log**

```
00:13:24.111  _queue_context: role='user' content=' 如果教小朋友微积分。'
00:13:39.056  the answer begins, 15 seconds later
00:13:39.0xx  16 sentences handed to TTS
00:13:41.385  send_asr_result: text=' 如果'      final=False
00:13:41.388  [tts] receive tts_flush
00:13:41.388  Cancelling current request tts-request-2 in state processing
00:13:41.389  tts_audio_end  request_total_audio_duration_ms: 5329, reason: 2
00:13:41.390  [MainControlExtension] Interrupt signal sent
00:13:41.750  send_asr_result: text=' 如果要'    → second interrupt
00:13:42.033  send_asr_result: text=' 如果用于'  → third interrupt
```

The user's own question began with 如果. Having heard nothing for fifteen
seconds, they asked again; the repeat arrived two seconds after the answer
finally started, and cut it off. Of 168 characters the TTS received 43.

**Code**

```
extension.py:89   if not event.text: return          → ' 如果' is not empty
extension.py:91   should_interrupt(' 如果', False)
  interrupt_gate.py:19  if final:                    → False, skipped
  interrupt_gate.py:23  if len(text) <= MIN_CHARS    → 3 > 2, not suppressed
  interrupt_gate.py:25  if text == self._last_text   → last was '', not suppressed
  interrupt_gate.py:28  return True
extension.py:92   await self._interrupt()
extension.py:211  _send_data("tts_flush", "tts")
```

**Verdict** — a defect in `InterruptGate`, which is ours. It suppresses a
partial that is *identical* to the one before it. ASR partials grow one
character at a time, so no two are ever identical and that check has never
once fired. The gate has no idea whether the assistant is currently speaking.

**An earlier reading of this was wrong.** The transcript 如果 matches the first
words of what the assistant had just said, and that looked like the microphone
hearing the speaker. It was not: the user's question at 00:13:24 began with the
same word. Acoustic echo is not involved.

---

## P3 — A misheard repeat becomes the next question

**Log**

```
00:14:09.028  _queue_context: role='user' content=' 如果用于。'
```

The repeat was transcribed as 如果用于 — not a question — and went to the LLM as
one, costing another fifteen-second turn.

**Code** — `extension.py:88-90` queues any final ASR result. There is no length,
confidence or sanity check.

**Verdict** — existing `main_control` behaviour, harmless until P1 and P2 make
the user repeat themselves.

---

## P4 — The greeting takes 3.9 seconds to be heard

**Log**

```
00:10:38.147  SherpaOnnxTTSExtensionAddon on_create_instance
00:10:38.652  Sent to TTS: is_final=True, text=你好，我是在安霸…
00:10:40.656  sherpa-onnx voice loaded … 22050 Hz
00:10:42.591  tts_ttfb: 3936 of request_id: tts-request-0
```

Two seconds of model load, then 1.9 seconds of synthesis. The 1.9 matches the
measured real-time factor exactly: 5249 ms of audio at 0.37.

**Code** — `extension.py` starts `warm_up()` as a background task when the
client is created, which gave it 0.5 seconds before the greeting arrived; the
load takes 2.0. And the greeting is a single sentence, so the engine's
per-sentence callback fires once: the first audio waits for the whole sentence.

**Verdict** — ours. The design spec excluded long-text splitting because it buys
no throughput, which is true, and did not consider that a single long sentence
makes first-audio latency equal to its full synthesis time. Splitting on
secondary punctuation would fix it.

For comparison, the turns whose text arrives in fragments: `tts_ttfb: 210` and
`tts_ttfb: 139`.

---

## P5 — The prompt became the subject of the reply

**Log** — asked for a story, the model planned one in its reasoning (a rabbit, a
fruit tree, the cycle of seasons) and then answered:

```
好的，我會按照您的要求，用繁體中文回答，并且保持回答的简洁和自然。
當您需要听到故事時，我會用中文音色進行合成。
當您有具體的問題或需要幫助時，請告訴我。
```

音色 and 合成 are words from the prompt, not from the user.

**Code** — the graph's `prompt` property was 95 characters and explained its own
reasons: answer in Chinese, because the voice speaks only Chinese and English
would be read as noise. A reasoning model treats an explanation as something to
reason about, and then replies to it.

**Verdict** — ours. Reduced to 18 characters with the reasons removed. The
Traditional Chinese requirement was also ours and also wrong: the voice is
`zh_CN`, trained on Simplified.

---

## P6 — A pinned framing can discard a whole answer in silence

Not observed in this run. It is a path in the code that would produce exactly
the symptom being investigated, so it is listed.

**Code** — `response_format` is pinned to `sse` in the graph. In that mode:

```
ambarella.py:376-377   if not line.startswith(SSE_PREFIX): continue
ambarella.py:393-394   if not tail.startswith(SSE_PREFIX): return
```

Any response without `data:` framing is dropped line by line, and the turn ends
with no text, no error and no log line.

The one raw response captured from the board — through `tools/ambarella/probe_llm_wire.py`,
with `Stream-Off: 1` — was `Content-Type: text/event-stream` carrying a
`Content-Length` and a body with no `data:` prefix at all. The extension uses
`Stream-Off: 0`, whose framing has not been captured, so this is a plausible
path rather than a demonstrated one.

**Verdict** — ours, latent. A framing mismatch should be reported, not swallowed.

---

## P7 — Messages with nowhere to go

```
Failed to find destination of a 'data' message 'asr_results'
Failed to find destination of a 'data' message 'tts_flush_end'
Failed to find destination of a 'data' message 'metrics'            ×2
Failed to find destination of a 'data' message 'provide_features'   ×2
Failed to find destination of a 'cmd' message 'on_user_*'           ×5
```

Inherited from the graph these were copied from. Harmless, and one line per
transcript in the log.

---

## What is not wrong

**The TTS extension.** Every character it received became audio: 30, 77 and 43
characters produced 5249, 15109 and 5329 ms. `Received empty payload for TTS
response` appears zero times. Its steady-state time to first audio is 139–210
ms, against the 361 ms ElevenLabs measured on this board.

**The reasoning/answer split.** The story turn's answer was complete; the
boilerplate was what the model produced, not what was left after stripping.

---

## Order of work

| | Problem | Whose |
| --- | --- | --- |
| 1 | P2 — the gate does not know the assistant is speaking | ours |
| 2 | P4 — split a long sentence so the first audio comes early | ours |
| 3 | P6 — report a framing mismatch instead of swallowing it | ours |
| 4 | P1 — make the model think less | the model's |

P3 and P7 follow from the others or are cosmetic.
