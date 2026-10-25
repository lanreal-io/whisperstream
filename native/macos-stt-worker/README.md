# macOS STT worker

This crate is the production-directed native home for Whisperstream's Apple
Silicon FluidAudio worker. With no arguments, the executable serves the frozen
`whisperstream-stt` protocol over stdin/stdout for the existing Python parent
supervisor. The Unit 01 fixture canary remains available through `--fixture` for
direct bridge checks.

The original Swift FFI in `swift/WhisperstreamAsrBridge.swift` depends directly
on Apache-2.0 FluidAudio `0.14.8`, revision
`56607d90b97de7d95a731200563bf49aa5beef20`. `Package.resolved` freezes that
revision. The former `fluidaudio-rs` dependency is removed; none of its wrapper
implementation is copied or linked. Distribution remains separately unreviewed
in `license-audit.toml`.

The bridge loads exact cached CoreML models directly, without download or
recovery helpers. Every final decode creates a fresh `TdtDecoderState`. It
returns native token strings, sample-relative start times, and durations derived
from native end times in owned JSON. These are decoder-derived timing estimates;
upstream can infer endpoints from the next token or encoder frame. The Python
adapter pins the emission-delay correction to one frame. Rust explicitly frees each returned buffer
and destroys its retained Swift handle on unload/shutdown. Native confidence is
not treated as a log probability. Final metadata is independently validated by
the Python parent; malformed or absent alignment preserves all text.

The macOS adapter accepts one `load` field:

```json
{"model_name":"parakeet-tdt-0.6b-v3"}
```

The model cache is selected only by the child process's isolated `HOME` and
`CFFIXED_USER_HOME`. Preview, hotwords, and language hints are absent from the
capability map and return an explicit `capability_unsupported` error if sent.

Build and test only with isolated application state:

```sh
MACOSX_DEPLOYMENT_TARGET=14.0 cargo build --release --locked \
  --manifest-path native/macos-stt-worker/Cargo.toml
HOME=/tmp/whisperstream-macos-stt-loop/home \
CFFIXED_USER_HOME=/tmp/whisperstream-macos-stt-loop/home \
WHISPERSTREAM_DATA_DIR=/tmp/whisperstream-macos-stt-loop/data \
  uv run pytest tests/test_macos_stt_worker.py -v -rs
HOME=/tmp/whisperstream-macos-stt-loop/home \
CFFIXED_USER_HOME=/tmp/whisperstream-macos-stt-loop/home \
WHISPERSTREAM_DATA_DIR=/tmp/whisperstream-macos-stt-loop/data \
  uv run pytest tests/test_macos_stt_recovery.py -v -rs

# Retained direct canary
HOME=/tmp/whisperstream-macos-stt-loop/home \
CFFIXED_USER_HOME=/tmp/whisperstream-macos-stt-loop/home \
WHISPERSTREAM_DATA_DIR=/tmp/whisperstream-macos-stt-loop/data \
  native/macos-stt-worker/target/release/whisperstream-macos-stt-worker \
    --fixture tests/fixtures/synthetic/speech.wav \
    --expected tests/fixtures/synthetic/speech.txt \
    --runs 2
```

The tests drive the native executable through the shared Python protocol and
supervisor seams. They cover negotiation, validation, lifecycle, the committed
fixture, explicit capability absence, bounded ordinary errors, hard exits,
recovery, EOF, expected shutdown, and exact-PID cleanup. The canary and probe
emit content-free JSON measurements. They never print transcript or audio
content. The recovery suite also covers start/protocol/timeout/native failures,
one retry, matching suppression, pre-reply and post-reply delivery boundaries,
backend survival, bounded redacted diagnostics, and repeated process/pipe
cleanup. The canary and worker require the preselected model already present in
their isolated FluidAudio cache. They never acquire missing model assets. Before
the first compile, run `swift package resolve --package-path native/macos-stt-worker`.
Subsequent Cargo builds disable automatic resolution and use the committed pin.
