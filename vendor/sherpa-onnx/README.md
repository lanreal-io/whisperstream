# Whisperstream sherpa-onnx dependency

## Current GPL source edition

The source edition uses official PyPI `sherpa-onnx==1.13.8` and its
`sherpa-onnx-core==1.13.8` native dependency. Upstream v1.13.8 source is pinned to
commit [`11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf`](https://github.com/k2-fsa/sherpa-onnx/tree/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf).
Windows x64 wheels are available for CPython 3.12, 3.13 and 3.14; the core
distribution provides a separate `py3` Windows x64 wheel. The project lockfile
records the selected distribution versions and artifact identities. The
[source-wheel manifest](../../scripts/sherpa-source-wheels.json) records the
reviewed native file inventory and hashes for each wheel.
The [source/build mapping](SOURCE-MATERIALS.md) identifies pinned native source
inputs, the upstream Windows recipe and the release evidence still to assemble.

From the repository root:

```powershell
uv sync --locked --extra dev --python 3.13
uv run --locked python scripts/check-sherpa-source.py
```

This path does not restore private wheels, apply the downstream patch, or build
sherpa locally. The official payload includes espeak-ng/piper native code.
Preserve all applicable component
licenses and notices; upstream's Apache-2.0 package metadata does not replace
the terms of every bundled component.

Source setup and installer packaging share `check-sherpa-source.py` and
its reviewed binding/core native hash inventory. `scripts/verify-sherpa-binary.py` retains
its historical no-espeak contract; it is not used to certify the official graph.
Package creation and publication still need their separate authorization,
redistribution materials and actual frozen-runtime verification. See
[UPGRADE.md](UPGRADE.md).

## Historical downstream patch

The previous custom dependency pinned sherpa-onnx v1.13.3 at upstream commit
`330609dab49be6ee8b30702918ca7abbbad1286a`:

<https://github.com/k2-fsa/sherpa-onnx/commit/330609dab49be6ee8b30702918ca7abbbad1286a>

`espeak-free-supertonic.patch` adds the default-off CMake option
`SHERPA_ONNX_ENABLE_WHISPERSTREAM_SUPERTONIC_ONLY`. With the option enabled,
the build retains ASR and the Supertonic TTS implementation and Python
bindings, but does not fetch, compile, or link espeak-ng or piper-phonemize.
Other TTS implementations and their Python model-config bindings are also
excluded. The downstream wheel metadata uses a short description of that clean
graph instead of embedding upstream's broad engine catalog. Python exports for
optional TTS model configs follow the native module, so the clean graph imports
without stale names while the default graph retains its upstream exports.
Leaving the option off preserves the upstream v1.13.3 build graph and package
description.

### Apply and configure the historical patch

Start from a clean checkout of the pinned commit:

```powershell
git clone https://github.com/k2-fsa/sherpa-onnx.git
cd sherpa-onnx
git checkout --detach 330609dab49be6ee8b30702918ca7abbbad1286a
git apply --check C:\path\to\vendor\sherpa-onnx\espeak-free-supertonic.patch
git apply C:\path\to\vendor\sherpa-onnx\espeak-free-supertonic.patch
```

Configure upstream-compatible and Whisperstream graphs in separate build
directories. The wheel builder supplies the remaining Python and platform
arguments.

```powershell
cmake -S . -B build-upstream
cmake -S . -B build-whisperstream `
  -DSHERPA_ONNX_ENABLE_PYTHON=ON `
  -DSHERPA_ONNX_ENABLE_TTS=ON `
  -DSHERPA_ONNX_ENABLE_WHISPERSTREAM_SUPERTONIC_ONLY=ON
```

The upstream-compatible graph is expected to contain `piper_phonemize` and
espeak-ng targets. The Whisperstream graph must contain neither target and must
retain `_sherpa_onnx`, `sherpa-onnx-core`, the offline ASR sources, and the four
Supertonic sources.

### Historical carve-out procedure

[`CARVEOUT.md`](CARVEOUT.md) preserves the former patch-port, short-path build,
immutable publication, dependency-lock, attribution, packaged-runtime and
rollback procedure. It is not the current source setup path and does not
authorize wheel publication or packaged builds. Read [`UPGRADE.md`](UPGRADE.md)
before selecting a dependency or distribution workflow.

Do not carry generated build directories, downloaded dependencies, wheels, or
native binaries in this vendor directory.

### Historical patch license provenance

sherpa-onnx is distributed under the Apache License 2.0; its upstream `LICENSE`
file remains authoritative. Whisperstream's downstream changes in
`espeak-free-supertonic.patch` are provided under the same Apache-2.0 terms.
The Whisperstream-only graph does not download or link the separately licensed
espeak-ng or piper-phonemize projects.

### Historical Task 1.1 validation record

On 2026-07-26, the tracked patch applied without fuzz to a clean detached
checkout of commit `330609dab49be6ee8b30702918ca7abbbad1286a`, and
`git diff --check` passed. Static CMake graph inspection confirmed that:

- the default-off option leaves the upstream graph unchanged;
- the Whisperstream graph retains the shared ASR sources and Python module;
- all four Supertonic implementation sources and its Python model binding
  remain in the Whisperstream graph; and
- espeak-ng fetches, `piper_phonemize` links, piper-backed sources and tests,
  and non-Supertonic TTS bindings are all guarded out of that graph.

Both Visual Studio configure attempts reached C/C++ compiler identification,
then hung in the compiler-ID probe on this machine. They were terminated
without producing a usable generated graph. Task 1.2's clean Windows wheel
builds are therefore the compile/configure proof; Task 1.1 is limited to the
clean-apply, diff, and static graph checks recorded above.

## Source Kokoro runtime

The source edition also uses the official `OfflineTtsKokoroModelConfig` and
`OfflineTts.generate` APIs, checked by `check-sherpa-source.py`. Native
synthesis runs in an owned persistent process to keep upstream text/phoneme
diagnostics out of backend output. The pinned upstream v1.0 English file
inventory, retained legacy-cache compatibility and release requirements are
recorded with the Agent Speak feature.
Windows model-backed source synthesis does not establish Mac or frozen-runtime
behavior. Matching compiled eSpeak data source/build materials must accompany
any distribution that requires them.
