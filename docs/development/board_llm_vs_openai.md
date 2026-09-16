# What the board's LLM interface does not have

`ambarella_llm2_python` talks to the N1-655's own model server. `openai_llm2_python`
talks to an SDK. The difference is not quality: it is that seven things the SDK
provides have no equivalent on the board, and each absence shows up as a
symptom in the voice pipeline.

Every row below is from the code, with line numbers, not from memory.

## The two interfaces

| | OpenAI | Board |
| --- | --- | --- |
| Reasoning content | `delta.reasoning_content`, its own field (`openai.py:277`) | mixed into the text, split on `</think>` |
| Opening tag | `<think>` and `</think>` both handled (`think_parser.py`) | **only `</think>`** — generation starts already inside the block |
| System prompt | `{"role": "system", ...}` as its own message (`openai.py:216`) | none; folded into the first user turn (`ambarella.py:421-424`) |
| Generation cap | `max_tokens` / `max_completion_tokens` (`openai.py:226-228`) | none |
| Temperature | `temperature` (`openai.py:229`) | none |
| Conversation history | stateless; we send `messages` and can trim them | kept server-side under `Session-Id`; can be cleared, never trimmed |
| Errors | typed exceptions from the SDK | a refusal closes the connection with no HTTP response at all |
| Concurrency | many | `--max_user 1` |

The four headers the board's guide documents are the whole interface:
`Session-Id`, `Model-Type`, `Stream-Off`, `Reset-En`.

## What each absence costs

**No `reasoning_content` field.** The model is `Deepseek-R1-Distill-Qwen`, which
writes its reasoning before answering. With no separate field the only way to
tell reasoning from answer is the tag, and the tag arrives at the end of the
thinking. Measured on 2026-09-16: 26.2 s, 45.4 s and 43.0 s to the first answer
character, against answers of 67, 8 and 496 characters. The time is not in the
answer.

**No generation cap.** Nothing limits how long the model thinks. `max_tokens`
would; there is no equivalent header.

**No system role.** The prompt goes into the first user turn, so the model reads
it as something the user said and reasons about it. Asked for a story on
2026-09-16, it planned one in its reasoning and then answered
`好的，我會按照您的要求，用繁體中文回答…當您需要听到故事時，我會用中文音色進行合成。` —
quoting the prompt's own words back. Shortening the prompt from 95 characters to
18, with the reasons removed, is the only lever available.

**Server-side history.** `Reset-En` clears it; nothing trims it. Sending less is
not possible, only sending nothing.

**No typed errors.** A refusal is a closed connection. `tools/ambarella/probe_llm_wire.py`
exists because curl will not hand over a response it cannot parse, and the board's
own 404 page carries a header with no colon.

## Why the first answer took forty-five seconds to appear on screen

Not the board. Ours.

`_ReasoningSplitter` held every character until `</think>` arrived, on the
grounds that until the tag appears the text cannot be classified. That is true
in general and false here: generation starts inside the block, measured on
2026-09-14, so text before the tag is reasoning by construction.

The repo's own reference adapter does not hold it. `think_parser.py` emits
`reasoning_delta` inside the loop, before the closing tag is found. Ours was the
outlier.

The cost was not audio — reasoning is never spoken. It was that the transcript
stayed empty for the whole thinking phase. A user watching nothing happen for
forty-five seconds concludes they were not heard and speaks again, and that
cancels the turn. Three questions on 2026-09-16, no answers.

Reasoning now goes out as it arrives.

### The one thing this costs

A stream that ends without ever sending the tag was the answer all along. It has
already gone out as reasoning, which `main_control` renders but never speaks
(`extension.py:98,105`), so `flush()` repeats it as answer. The text appears in
both transcript channels — a thought and then a reply — rather than not being
spoken at all.

Tested both ways: a stream with the tag emits reasoning then answer once each; a
stream without it emits the text as reasoning and again as answer.

## What is still open

**Making the model think less.** No header controls it. The one untried lever is
the prompt: R1 distills often respond to an explicit instruction not to reason.
Unverified on this board, and only measurable there — the log's
`first answer token after Ns` line is the measurement.

**Everything else in the table above.** They are properties of the interface,
not defects to fix.
