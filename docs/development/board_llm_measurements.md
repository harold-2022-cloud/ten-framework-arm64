# N1-655 on-board LLM: measured behaviour

Numbers for a conversation with Ambarella about the LLM demo. Everything here
was measured on one board; nothing is inferred from documentation. Where a
figure is uncertain it says so.

A Traditional Chinese edition is at
[`board_llm_measurements.zh-TW.md`](board_llm_measurements.zh-TW.md).

## What was measured

| | |
| --- | --- |
| Board | Ambarella N1-655, vendor Fedora, `gcc-14.3.1-5.lch2025` |
| Model | `Deepseek-R1-Distill-Qwen` 7B, `n1-655_deepseek_r1_distill_qwen_7B_1NVP` |
| Launcher | `run_llm_demo.sh --run_mode start --model_type 9 --ip 127.0.0.1 --max_user 1` |
| Client | `test_llm_client --port 9005 --model-type 9 --bsize 64 --user-num 1` |
| Interface | HTTP POST to `127.0.0.1:8080`, SSE response, one character per event |
| Dates | 2026-09-16 and 2026-09-17 |

Two independent methods, which agree:

1. **Live conversations** through a voice pipeline. The application logs the
   time of the POST, of the first answer character, and of the last, plus the
   answer's length. Counting the SSE events gives the total characters.
2. **A standalone probe** over a raw socket, with the board otherwise idle,
   timing the closing `</think>` as it goes past.

Reasoning length from method 1 is `SSE events − answer characters − 3`. The
three are constant across every turn measured and are the framing around the
tag. Where the reasoning text was also recovered from the transcript, the two
agree: 167 against 167, 172 against 172.

## Generation rate

The board emits **one character per SSE event** at a rate that does not depend
on whether the characters are reasoning or answer.

### Board otherwise idle (probe, 2026-09-17)

| Query | Total characters | Total seconds | Characters/second |
| --- | ---: | ---: | ---: |
| 你是谁？ | 134 | 11.3 | 11.9 |
| 你是谁？ | 88 | 8.0 | 11.0 |
| 你是谁？ | 88 | 8.1 | 10.9 |
| 你是谁？ | 134 | 11.4 | 11.8 |
| 讲一个笑话。 | 563 | 56.4 | 10.0 |
| 讲一个笑话。 | 237 | 28.1 | 8.4 |

**8.4 – 11.9 characters/second, median 11.0.**

### Board also running speech (live conversations, 2026-09-16)

| Run | Turn | Total characters | Total seconds | Characters/second |
| --- | --- | ---: | ---: | ---: |
| 10:20 | 1 | 186 | 19.0 | 9.8 |
| 10:20 | 2 | 240 | 24.7 | 9.7 |
| 11:41 | 1 | 186 | 37.4 | **5.0** |
| 11:41 | 2 | 223 | 25.4 | 8.8 |
| 11:41 | 3 | 560 | 55.1 | 10.2 |

The 5.0 is the first turn of a session, while the ASR was draining a 6.5 s
audio backlog and the TTS was loading its model — both ONNX on the CPU. By the
third turn the rate is back to 10.2. **This is a correlation on one sample, not
a demonstrated cause**, and it is one of the questions below: the speech
workloads are on the CPU, and the LLM's inference is on the VP.

## Where the time goes

The wait before the first answer character is the reasoning, and the reasoning
length depends on the question, not on the answer.

| Query | Reasoning chars | First answer character at | Answer chars | Total |
| --- | ---: | ---: | ---: | ---: |
| 你是谁？ | 44 – 67 | 3.9 – 5.6 s | 44 – 67 | 8.0 – 11.4 s |
| 讲一个笑话 | 167 | 17.0 s | 16 | 19.2 s |
| 再讲讲一个 | 172 | 17.9 s | 65 | 24.9 s |
| 讲一个笑话 | 167 | 33.3 s | 16 | 37.6 s |
| 在再讲一个笑话 | 151 | 17.1 s | 69 | 25.6 s |
| 你可以解释四负ITY转换吗 | 370 | 36.3 s | 187 | 55.3 s |

The last query is a speech-recognition error for 「你可以解释傅立叶转换吗」. It
produced the longest reasoning measured and an answer that repeats one sentence
verbatim twice.

**For an open-ended question the user waits 17–36 seconds before hearing
anything.** The answer itself is 2–19 seconds of that.

### Reasoning is not a preamble

Both jokes were complete inside the reasoning before the answer began. The
second turn's reasoning, in full, 172 characters:

> 好，用户又让我讲笑话，我得再想一个简短有趣的。这次，我想到一个关于小明和数学的笑话。小明问数学老师：「为什么我们要学数学？」老师回答：「因为数学是科学的皇后，而科学是国家的基石。」小明听后，说：「那我以后当总统，数学就不是我的皇后了！」这个笑话既有趣又带点幽默，适合口语表达。再检查一下，有没有更合适的，但这个挺好的，应该可以满足用户的需求了。

The answer that followed was the same joke in dialogue form. The user waited
17.9 seconds for text the model had finished composing at about second 10.

The last 42 characters are identical in both turns:

> ，适合口语表达。再检查一下，有没有更合适的，但这个挺好的，应该可以满足用户的需求了。

4.3 seconds per turn spent writing the same sentence.

## Prompting does not shorten it

| Prompt form | Query | Reasoning chars | vs baseline |
| --- | --- | ---: | --- |
| baseline | 你是谁？ | 67 | |
| `/no_think ` prefix | 你是谁？ | 44 | shorter |
| 「直接回答，不要思考过程。」 prefix | 你是谁？ | 44 | shorter |
| 「直接回答，不要思考过程。」 prefix | 讲一个笑话。 | **523** | **3× longer than the 167 baseline** |

Telling the model in Chinese not to show its reasoning made it reason three
times as long on the open-ended question, and took 51.2 seconds.

A fourth form appended `\n<think>\n</think>` to the query and reported zero
reasoning on 讲一个笑话. **That measurement is not trusted**: the probe detects
the end of reasoning by finding `</think>` in the response, and that form puts
`</think>` into the request, so an echo would be indistinguishable from the
model having skipped the phase. It is listed only so it is not mistaken for a
result.

## Concurrency and sessions

`--max_user 1`, and a session is held for **180 seconds after the reply**:

```
[INFO] 10:26:xx  From user: fd: 10, ... content: 讲一个笑话。
[ERR]  current user num (2) > max_user_num (1), please wait 180s to see if
       there are any timeout session.
[ERR]  handle_request_from_post fail
[INFO] 10:29:00  Dev session free: Dev VA: ..., fd_dev: 7, net_type: 9, sid: 2
```

A client that sends each request under a new `Session-Id` therefore gets one
request per 180 seconds. Reusing one `Session-Id` with `Reset-En: 1` works, and
is what the application does.

The board was also observed running **two `test_llm_client` processes** where
the guide shows one. Both had the same arguments and the same working
directory. Whether the second holds a user slot was not determined.

## What the interface offers

Four headers, from the guide: `Session-Id` (a non-zero decimal integer, parsed
numerically — a non-numeric value is refused by closing the connection with no
HTTP response at all), `Model-Type`, `Stream-Off`, `Reset-En`. **None of them
affects generation.** There is no setting for the reasoning phase, for a length
limit, or for a stop condition.

The response body is `text/event-stream`, one character per `data:` event, with
whitespace escaped as `<SP>` and `<NL>`. The reasoning is terminated by
`</think>` but is **not preceded by an opening `<think>`**.

## Answered by Ambarella, 2026-09-17

**The reasoning phase can be bypassed**, in the model's own prompt template
rather than per request. In `prompt_config.json` beside the weights, the
assistant marker ends with an already-closed think block:

```json
"Symbol1": "<｜Assistant｜><think>\n\n</think>"
```

The daemon reads it at load, so it takes a restart. `tools/ambarella/set_llm_thinking.sh`
makes the change, backs up first, and restores with `--on`.

This is the same idea as the fourth prompt form above, done at the layer where
it is unambiguous: the template is applied server-side before generation, so
nothing about it can be echoed back into the response and mistaken for a
result.

**8–11 token/s is expected** for this model on an N1-655, which is what was
measured here.

## Questions still open

1. **Why does the rate fall to 5.0 characters/second while the host CPU is
   busy**, if inference runs on the VP? What host-side work is on the critical
   path — tokenisation, the HTTP/SSE layer, feeding the VP?

2. **Can the 180-second session hold be shortened**, or a session released
   explicitly when a reply completes? At `--max_user 1` this is what stops a
   second client connecting.

3. **Is `--max_user` greater than 1 supported**, and what does each additional
   user cost in memory and in rate?

4. **Should there ever be two `test_llm_client` processes?** If not, what
   leaves one behind, and how should it be cleared safely?

5. **Is the missing opening `<think>` intentional?** Every reply carries a
   closing `</think>` with no opening tag, which every off-the-shelf parser for
   this model family gets wrong.

## How to reproduce

In this repository:

```bash
tools/ambarella/check_llm_board.sh          # compare the board against the guide
python3 tools/ambarella/probe_llm_thinking.py --only "as deployed" --repeat 3
```

The probe sends over a raw socket rather than through an HTTP client library,
because the board's error page is not parseable by one, and reports think time,
answer time and both character counts per request.
