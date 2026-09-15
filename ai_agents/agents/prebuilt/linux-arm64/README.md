# agora_rtc, prebuilt for linux/arm64

The TEN registry publishes `agora_rtc` and `agora_rtc_sdk` for linux/x64 only,
so an arm64 board otherwise has to build the wrapper from source and repackage
the SDK before it can join an Agora channel. These are the results of doing
that once, on an Ambarella N1-655 (aarch64 Fedora), so that nobody else has to.

## Installing

```bash
ai_agents/agents/scripts/install_prebuilt_agora_rtc_arm64.sh <example>
```

The example defaults to `voice-assistant`. Run `task install` for that example
first: the script checks that every symbol resolves, which needs `ten_runtime`
already present in the tenapp.

To build from source instead — a different SDK version, a patched wrapper —
use `setup_agora_rtc_arm64.sh`, and `export_agora_rtc_arm64.sh` to refresh
this directory from what it produced.

## What is here

| | |
| --- | --- |
| `agora_rtc/` | the extension: `lib/libagora_rtc.so` plus its manifest and property |
| `agora_rtc_sdk/` | the five SDK libraries the wrapper links against |

Runtime files only. The SDK's 154 headers are needed to compile the wrapper,
never to run it, and they were most of the size.

The libraries are stripped. They load and resolve exactly as before — the
dynamic symbol tables are untouched, only the debug symbols are gone — but a
backtrace from a crash inside them will show addresses rather than function
names. Rebuild with `setup_agora_rtc_arm64.sh` if you need to debug one.

## Versions

| Package | Version |
| --- | --- |
| `agora_rtc` | `0.23.9-t1` |
| `agora_rtc_sdk` | `4.4.32-141` |

`agora_rtc`'s manifest declares both x64 and arm64 under `supports`, because
that is the upstream manifest with arm64 added. The binaries here are aarch64
only, which is why the install script refuses to run anywhere else.

Both packages are pinned to each other: `0.26` calls `setTotalExtraSendMs` and
needs SDK `4.4.32-175`, so they cannot be upgraded independently. See
[`docs/development/arm64_build.md`](../../../../docs/development/arm64_build.md).
