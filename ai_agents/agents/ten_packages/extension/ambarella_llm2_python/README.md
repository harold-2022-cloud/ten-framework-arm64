# ambarella_llm2_python

LLM provider backed by the on-board LLM demo server of an **Ambarella AI
Developer Kit** (N1-655 / Cooper Pro), reached over its plain HTTP interface.

Modelled on `dify_llm2_python`, because the two providers share the same
shape: a non-OpenAI HTTP endpoint, conversation history held server-side, no
system role, and no tool calling.

## Features

- Streams assistant text as `LLMResponseMessageDelta` / `...Done`.
- Keeps the board's conversation history via the `Session-Id` header, so only
  the newest user turn is sent per request.
- Tolerates either response framing the board might use (SSE `data:` lines or
  unframed text) — see **Response framing** below.
- Folds the system prompt into the first query, as the interface has no
  system role.

## Prerequisites

On the board, start the demo server (see section 4.2.1 of the *Cooper
Development Kit User Guide*):

```bash
mkdir -p ~/demo_resources/llm_demo/deepseek_7B
tar xvf ~/demo_resources/models/Deepseek-R1-Distill-Qwen/\
n1-655_deepseek_r1_distill_qwen_7B_1NVP.tar \
    -C ~/demo_resources/llm_demo/deepseek_7B
cd /usr/share/ambarella/llm_demo/
./run_llm_demo.sh --run_mode start --model_type 9 \
    --model_path ~/demo_resources/llm_demo --ip 127.0.0.1 --max_user 1
```

The first model load after boot can take up to 80 s; the board is ready once
`/tmp/log.txt` shows `Device ENABLE`.

To reach it from off-board, open the port in the board's firewall:

```bash
sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload
```

Two board-side constraints worth knowing:

- The LLM demo and the LLaVA demo **cannot run at the same time** — they
  share a library that does not support it.
- `--max_user 1` means one turn at a time. This extension serialises requests
  with a lock to match; drop it if you raise `--max_user`.

## Response framing

**This is the one part that is not verified.** The user guide documents the
request (a `curl` call plus its four headers) but never states what the
streaming response looks like. `response_format` therefore defaults to
`auto`, which sniffs the first chunk: a leading `data:` selects SSE parsing,
anything else is treated as unframed UTF-8 text. For an SSE payload the
assistant text is looked up under `delta`, `answer`, `text`, `content`,
`response` then `token`; a non-JSON payload is used verbatim.

Once you have seen the real framing, capture it and pin the setting:

```bash
curl -N -X POST --url http://<board>:8080/ \
  -H "Session-Id: probe" -H "Model-Type: 9" \
  -H "Stream-Off: 0" -H "Reset-En: 1" \
  --data "Count to five" | tee /tmp/amba_stream.raw
```

Then set `response_format` to `sse` or `raw`, and if the payload keys differ
from the guesses above, adjust `TEXT_KEYS` in `ambarella.py`.

One failure mode to check for specifically: this extension treats each
payload as an **increment** and accumulates the full answer itself. If the
board instead sends a **cumulative** snapshot per event — `"He"`, `"Hel"`,
`"Hell"` — the output will come out garbled as `HeHelHell`. If you see that,
the fix is to assign rather than append in `get_chat_completions`.

## Known limitations

| Limitation | Why |
| --- | --- |
| No tool calling | The endpoint takes a plain-text body. Registered tools are ignored; a warning is logged once. |
| History not owned by TEN | The board keys history off `Session-Id`. TEN's `messages` list is not replayed, so the two can drift after an abort. |
| Barge-in leaves a partial turn | The board cannot rewind an answer it already appended. Set `reset_after_abort` to clear the whole context instead, at the cost of losing all history. |
| No published token rate | The guide gives no tokens/s for 7B W4A16 on CVflow and states the demo is not fully optimised. Measure before relying on it for voice. |

## API

Refer to `api` definition in [manifest.json] and default values in
[property.json](property.json).

| Property | Default | Notes |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8080` | Board's demo server. |
| `model_type` | `9` | `run_llm_demo.sh` model type; 9 is `deepseek_7B`. Must match the server's running model. |
| `session_id` | `""` | Random per instance when empty. |
| `prompt` | `""` | Folded into the first query. |
| `response_format` | `auto` | `auto`, `sse` or `raw`. |
| `streaming` | `true` | Ceiling on the per-request setting; maps to `Stream-Off`. |
| `reset_on_first_request` | `true` | Sends `Reset-En: 1` once, so a previous worker's session cannot leak in. |
| `reset_after_abort` | `false` | See the limitations table. |
| `connect_timeout_s` | `15.0` | |
| `total_timeout_s` | `120.0` | Generous: first load can take 80 s. |

## Development

### Wiring it into a graph

Add a path dependency in the example's `tenapp/manifest.json`:

```json
{ "path": "../../../ten_packages/extension/ambarella_llm2_python" }
```

and point the graph's `llm` node at it in `tenapp/property.json`:

```json
{ "name": "llm", "addon": "ambarella_llm2_python", "property": { ... } }
```

The node must stay named `llm` — `main_control` addresses the
`chat_completion` and `abort` commands to that name.

### Unit test

`tests/test_regressions.py` — six regressions, each one pinned to a defect
found reviewing this extension, none needing a network or a board:

```bash
task test-extension EXTENSION=agents/ten_packages/extension/ambarella_llm2_python
```

They cover the multi-byte decode across chunk boundaries, SSE whitespace
preservation, the response-format sniff on a short first chunk, the system
prompt surviving a post-abort reset, and the error an uninitialised client
reports. Each was confirmed to fail against the code as it stood before its
fix.

## Misc

The board runs Lychee OS (Fedora 41 base, aarch64, glibc 2.41), so the agent
can also run natively on the board rather than talking to it over the
network.
