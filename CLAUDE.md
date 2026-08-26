# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here: progressive-disclosure docs

`AGENTS.md` is the checked-in agent contract. Follow it: read
`docs/ai/L0_repo_card.md`, then load **all 8 files** in `docs/ai/L1/` (they are
small), and pull a single `docs/ai/L1/L2/*.md` deep dive only when L1 is not
detailed enough.

Two stale links in `AGENTS.md` — don't waste time hunting for them:

- L2 deep dives live in `docs/ai/L1/L2/` (indexed by `docs/ai/L1/L2/_index.md`),
  not `docs/ai/L1/deep_dives/`.
- `docs/progressive-disclosure-standard.md` does not exist.

The L1 docs are written almost entirely about `ai_agents/`. For anything under
`core/`, `packages/`, `tests/`, or `build/`, they will not help — use the
"Framework core" section below.

## Git conventions override the default

`AGENTS.md` forbids **Co-Authored-By trailers and any mention of AI tool names**
in commits, branches, and PRs. That overrides any default instruction to add
attribution trailers. Commit messages are conventional-commits, enforced in CI
by `commitlint` (`.github/configs/commitlint.config.mjs`) with **no local
commit-msg hook** — a bad message is only caught after push. The rule that
actually trips people up is `body-max-line-length` (≤100 chars *per body line*),
so hard-wrap bodies and prefer `git commit -F msg.txt` over `-m`.

## The repo is two independent halves

Deciding which half a change lands in determines the toolchain, the test
commands, and the docs that apply.

| | Framework core | AI agents |
| --- | --- | --- |
| Paths | `core/`, `packages/`, `tests/`, `build/`, `third_party/`, `tools/` | `ai_agents/` |
| Languages | C/C++ runtime, Rust (`ten_rust`, `ten_manager`), Go/Python/Node bindings | Python extensions, Go API server, Next.js playground |
| Build | GN + Ninja via the `tgn` wrapper | `task` targets, run inside the Docker dev container |
| Entry Taskfile | `Taskfile.yml` (root) | `ai_agents/Taskfile.yml` (included as `ai_agents:`) |
| CI | `linux_ubuntu2204.yml`, `mac_*`, `win.yml`, `tman_full_*` | `ai_agents.yaml` |

The two CI sets use **mutually exclusive `paths-ignore` filters**: a change
under `ai_agents/` never triggers the core build, and a change under `core/` etc.
never triggers the agents job (which also ignores `ai_agents/playground/**` and
`ai_agents/esp32-client/**` entirely). `.coderabbit.yaml` mirrors the same
filter, so core changes get no automated review either.

## Framework core commands

`core/ten_gn` is a **git submodule and is not populated on a fresh clone** —
nothing builds until you initialize it, and `tgn` itself comes from there:

```bash
git submodule update --init --recursive --depth 1 core/ten_gn
export PATH=$(pwd)/core/ten_gn:$PATH
```

```bash
task gen            # tgn gen linux x64 debug   (defaults from root Taskfile vars)
task build          # tgn build linux x64 debug
task clean          # rm -rf out

task gen-tman       # generate with only ten_rust + ten_manager enabled (fast, no bindings)
task build-tman     # build just ten_manager_package + tests/ten_manager

# Override the target triple (root Taskfile vars default to linux / x64 / debug):
task gen OS=linux ARCH=x64 BUILD_TYPE=release
```

Note the arg-forwarding asymmetry: `gen-tman` already emits the `tgn`
`--` separator before GN args, but plain `gen` appends `CLI_ARGS` straight after
`BUILD_TYPE` with no separator. For a one-off GN-flag build, call `tgn` directly
rather than fighting the Taskfile:

```bash
tgn gen linux x64 release -- log_level=1 ten_enable_go_binding=false
tgn build linux x64 release
```

Build output lands in `out/<os>/<arch>/` (git-ignored): `ten_packages/` for
built packages, `tests/` for test binaries, `ten_manager/` for `tman`.
GN feature flags are declared in `build/options.gni` and
`build/ten_runtime/options.gni`; CI passes them after `--` (see
`.github/workflows/linux_ubuntu2204.yml` for the canonical debug/release sets).

Tests are GN targets under `tests/` (`tests/ten_runtime/{unit,smoke,integration}`,
`tests/ten_manager/`) — they are built by `tgn build` and the resulting binaries
run from `out/<os>/<arch>/tests/`. Rust crates are `core/src/ten_rust` and
`core/src/ten_manager`; type-checking config (`pyrightconfig.json`) covers this
half only and explicitly excludes `ai_agents/`.

**Never hand-edit `version` fields in core/packages `manifest.json`.** They are
generated from the latest git tag by
`tools/version/update_version_in_ten_framework.py`, and CI enforces consistency
via `tools/version/check_version_in_ten_framework.py`. (Extension versions under
`ai_agents/` *are* hand-maintained — those are a different set of files.)

## AI agents commands

Everything runs inside the `ten_agent_dev` container
(`ghcr.io/ten-framework/ten_agent_build:0.7.14`, started by
`ai_agents/docker-compose.yml`). The host shell generally has **none** of
`task`, `tgn`, `tman`, `black`, `pylint`, or `bun` — check before assuming a
bare command works, and prefix with `sudo` if plain `docker ps` is denied.
Working dir inside the container is `/app` (= `ai_agents/`).

```bash
cd ai_agents && docker compose up -d
```

Per-example install/run (from `ai_agents/agents/examples/<example>/`), which
wires up tenapp deps, Python deps, the playground, and the Go server:

```bash
docker exec ten_agent_dev bash -c "cd /app/agents/examples/voice-assistant && task install"
docker exec -d ten_agent_dev bash -c "cd /app/agents/examples/voice-assistant && task run > /tmp/task_run.log 2>&1"
```

`task run` starts the Go API server (8080), the Next.js playground (3000), and
TMAN Designer (49483) together. Use it — never `./bin/api` or `./bin/worker`
directly. Use `task install`, never bare `tman install` (which can delete
`bin/worker`). All logs go to `/tmp/task_run.log`.

Checks and tests (from `/app`; each maps to a blocking CI job):

```bash
task format                                   # black --line-length 80
task check                                    # black --check
task lint                                     # pylint over all extensions; ANY warning is fatal
task lint-extension EXTENSION=<ext_dir_name>  # pylint one extension only

task test                                     # all extension standalone tests + go test ./... in server/
task test-extension EXTENSION=agents/ten_packages/extension/<ext>              # one extension (installs first)
task test-extension-no-install EXTENSION=agents/ten_packages/extension/<ext>   # same, skip install (fast iteration)
task tts-guarder-test EXTENSION=<ext> [CONFIG_DIR=tests/configs]               # TTS integration suite
task asr-guarder-test EXTENSION=<ext> [CONFIG_DIR=tests/configs]               # ASR integration suite
```

Anything after `--` is forwarded to pytest, which is how you run a **single
test**:

```bash
task test-extension-no-install EXTENSION=agents/ten_packages/extension/deepgram_tts -- -k test_flush -s -v
task tts-guarder-test EXTENSION=deepgram_tts -- -k test_flush
```

CI runs `task test -- -s -v`. Do not run the ASR and TTS guarders concurrently
in one container — their build scripts collide on shared temp paths.

Other toolchains: TypeScript/JSON formatting and linting is Biome from the repo
root (`npm run lint`, `npm run lint:fix`, `npm run format`, config `biome.json`,
80-col); the Go server is `ai_agents/server` (`go test ./...`, built to
`bin/api`); the playground is Next.js run with `bun`.

## Architecture worth knowing before editing

**Layer stack.** A vendor extension is Python-only and sits at the top of:
C runtime (`core/src/ten_runtime`) → language binding
(`core/src/ten_runtime/binding/{python,go,nodejs}`) → the `ten_ai_base` base
classes (`AsyncASRBaseExtension`, `AsyncTTS2BaseExtension`,
`AsyncLLMBaseExtension`, `AsyncLLMToolBaseExtension`, …) → the extension.
Adding a vendor touches only the top layer. Changing a *message type, schema,
or lifecycle contract* reaches into `core/` and puts you in the other half of
the repo, with a different build and different CI.

**`ten_ai_base` is not source in this repo.** `ai_agents/agents/ten_packages/`
contains only `extension/`; `ten_packages/system/ten_ai_base` is a *versioned
registry dependency* (`{"type": "system", "name": "ten_ai_base", "version": "0.7"}`
in each example's `tenapp/manifest.json`) materialized by `tman install`. So the
base-class and `api/*-interface.json` paths quoted throughout `docs/ai/` only
exist inside an installed tenapp (i.e. in the container after `task install`) —
`find`ing them in a fresh checkout will come up empty. Local extensions, by
contrast, are `{"path": "../../../ten_packages/extension/<ext>"}` deps and are
editable source.

**Three names must match exactly**, or the extension fails silently at graph
load: the `@register_addon_as_extension("x")` decorator argument in `addon.py`,
the `name` field in the extension's `manifest.json`, and the `addon` field of
the graph node in the example's `tenapp/property.json`.

**Graphs, not code, define an agent.** `ai_agents/agents/examples/<ex>/tenapp/property.json`
holds `predefined_graphs[]` — `nodes` (which extensions instantiate) plus
`connections` (typed message routing: `cmd`, `data`, `audio_frame`,
`video_frame`). Wiring a new extension into a pipeline means editing that JSON
plus adding a path dependency in the same directory's `manifest.json` — not
writing glue code. Cross-extension message names (`asr_result`,
`tts_text_input`, `pcm_frame`, `tool_register`, `flush`, …) are contracts
declared in `ten_ai_base/api/*-interface.json` and imported by each extension's
`manifest.json`.

**Server–worker split.** The Go server (`ai_agents/server/internal/http_server.go`)
is stateless routing: `POST /start` spawns one worker process per session
(`tman run start`), `/stop` kills it, `/ping` keeps it alive. The server
injects dynamic values into the graph at start time — `channel_name` into every
node that declares a `channel` property, plus `startPropMap` entries
(`remote_stream_id`, `bot_stream_id`, `token`) and per-extension overrides from
`req.Properties`. A new extension picks up the channel automatically by
declaring the property; no server change needed.

**Adding a graph is not hot-reloadable.** Editing values inside an existing
graph takes effect per session, but adding or removing a graph requires a full
restart of server *and* playground (the frontend caches `/graphs`). `.env` is
read only at container start, and Python deps do not survive a container
restart. See `docs/ai/L1/01_setup.md` and `docs/ai/L1/L2/operations_restarts.md`
for the exact sequences.

## Gotchas that cost the most time

- `ten_env.get_property_*()` returns a **`(value, error)` tuple**, not the
  value. Always take `[0]`.
- **No signal handlers or `atexit`** in extensions — they run off the main
  thread. Clean up in `on_stop()`.
- Import from `ten_runtime`, not `ten` (the pre-0.11 module name).
- A stale `.ten/` directory left in an extension dir makes `task check` report
  phantom reformatting and breaks the next standalone install — `rm -rf <ext>/.ten`.
- Never edit generated/ignored artifacts: `out/`, `manifest-lock.json`, `.ten/`,
  `bin/`.

`docs/ai/L1/07_gotchas.md` has the full list (zombie workers, `next-server`
holding port 3000, guarder symlink drift, vendor "PCM" that isn't PCM16, audio
routing splits) — read it before debugging runtime behavior.
