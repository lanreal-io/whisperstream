# sherpa-onnx upgrade runbook

## GPL source-edition upgrades

The source edition now uses official `sherpa-onnx` and `sherpa-onnx-core` 1.13.8
wheels from PyPI. The upstream release source is
[`11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf`](https://github.com/k2-fsa/sherpa-onnx/tree/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf).
The official native payload includes espeak-ng/piper code. The owner's GPL
source-edition decision supersedes the no-espeak prerequisite for this source
dependency only. Packaging now reuses the reviewed source-wheel inventory. The
historical no-espeak verifier remains separate.

For a later source dependency upgrade:

1. Select a final upstream release and record its full source commit. Inspect
   release notes, Python APIs, native layout and component license changes.
2. Inventory official wheels for every supported Windows x64 Python ABI,
   including each split native distribution, and record artifact hashes.
3. Update exact dependency pins, the lockfile and
   `scripts/check-sherpa-source.py` together. Preserve the platform, version and
   native dependency checks; do not substitute the historical packaging verifier.
4. Install from public sources in a fresh environment and cache for every
   supported interpreter. Run the source check and affected regression checks.
   An import on one Python version does not establish another ABI's behavior.
5. Verify supported speech-recognition and Supertonic inference with suitable
   fixtures. Record any missing model, hardware or native-desktop verification.
6. Refresh component notices and dependency documentation. Review the complete
   branch diff and record exactly which source revision was verified.

Normal source setup is:

```powershell
uv sync --locked --extra dev --python 3.13
uv run --locked python scripts/check-sherpa-source.py
```

The previous private archive restore, downstream patch and wheel builder are
historical tools. They are not required by this setup. Keep them and their
provenance separate from the current official dependency identity.

## Historical packaged-binary workflow

The remainder of this document preserves the former no-espeak package acceptance
workflow. It is **not an instruction to package the source edition**.
`scripts/verify-sherpa-binary.py` retains that historical contract. Current
`build.py` instead verifies the reviewed official binding/core inventory through
`check-sherpa-source.py`. The old commands below are historical reference only;
use the installer profile guide for current source-edition packages. Actual
frozen-payload verification and recipient materials remain release gates.

The objective is not to preserve a custom build forever. The objective is to
package only an exact, reviewed Windows x64 payload that:

- retains the Parakeet STT and Supertonic TTS APIs Whisperstream uses;
- contains no espeak-ng or piper-phonemize code or data;
- has an explicit version, source, ABI, and SHA-256 identity; and
- is the same native payload verified in the wheel, Nuitka standalone tree,
  and packaged sidecar runtime.

If any official-wheel acceptance criterion below fails, stop this path and use
the separate [`CARVEOUT.md`](CARVEOUT.md) runbook. That document covers the
complete downstream patch, wheel build, immutable index publication, pinning,
packaged proof, and rollback procedure.

### Historical upstream assessment (July 27, 2026)

As of 2026-07-27, upstream
[issue #3731](https://github.com/k2-fsa/sherpa-onnx/issues/3731) is still open
and the `master` CMake graph still fetches espeak-ng and piper-phonemize when
TTS is enabled. Upstream intends to remove both in the breaking 2.0.0 release.
This dated status is not a permanent reason to keep the custom build. Recheck
the release artifacts and source graph for each candidate.

### Choose the historical package route

Use unmodified official upstream wheels only when all of these are true:

1. A final upstream release and immutable Windows x64 wheels exist for every
   CPython ABI supported by Whisperstream.
2. The wheel and every native dependency it installs pass the raw-marker,
   imported-DLL, and exported-symbol checks in
   `scripts/verify-sherpa-binary.py`.
3. No dependency can reintroduce espeak-ng or piper-phonemize. A future clean
   `sherpa-onnx-core` split is acceptable only after the verifier and build
   preflight verify that distribution's exact native payload.
4. The target-interpreter smoke retains `OfflineRecognizer`,
   `OfflineTtsSupertonicModelConfig`, `OfflineTtsModelConfig`, and
   `OfflineTtsConfig`, and does not expose removed piper-backed configs.
5. Real Parakeet STT and Supertonic TTS tests pass from the official payload.
6. The official payload can be pinned without an sdist or another artifact
   source silently satisfying an unsupported platform or ABI.

One failed criterion is enough to reject the official route. Do not add a
verifier exception or accept a split package without extending verification
to its native files.

### 1. Pin and inspect the official candidate

1. Select a final upstream tag and record its full commit SHA. Do not evaluate
   a moving branch as a release artifact.
2. Read the upstream release notes and diff the current Whisperstream pin to
   the candidate, concentrating on:
   - root and Python CMake graphs;
   - Python package layout and split-package metadata;
   - offline recognizer and Supertonic bindings;
   - ONNX Runtime version and DLL layout; and
   - Windows build and supported Python ABI changes.
3. Inspect the upstream TTS graph for any remaining espeak-ng or
   piper-phonemize fetch, source, library, data, or link path.
4. Inventory the official wheel filenames, hashes, tags, `Requires-Python`,
   and dependencies for every supported ABI.

Do not remove the current downstream pin yet. Candidate inspection happens on
an isolated branch while production remains locked to the last reviewed
artifact.

### 2. Adapt the fail-closed verifier

Update the acceptance identity and layout for the official candidate:

| Concern | Files |
| --- | --- |
| Exact accepted version and wheel layout | `scripts/verify-sherpa-binary.py` |
| Package-build version, ABI, and native-distribution preflight | `scripts/build.py` |
| Exact expectations | `tests/test_verify_sherpa_binary.py` and `tests/test_build_command.py` |

The verifier must continue to reject:

- all required espeak symbols and espeak/piper markers;
- wrong versions and wrong ABI tags;
- unexpected native members or missing native members;
- piper-backed Python config exports; and
- any unverified native split distribution.

If the official package uses `sherpa-onnx-core`, extend both the verifier and
the build preflight to enumerate and verify its recorded native files. Remove
the current blanket rejection only after clean wheel, dependency, export,
import, and packaged-runtime proof exists.

Run the candidate wheel through the updated verifier under its target
interpreter. Repeat independently for every supported ABI. A successful import
under the host Python is not cross-ABI proof.

### 3. Switch the dependency source

After every official wheel passes local native and import verification:

1. Pin the exact official version in `pyproject.toml`.
2. Remove the `sherpa-onnx` binding from `[tool.uv.sources]`.
3. Remove the explicit `whisperstream-models` index if nothing else uses it.
4. Regenerate and inspect the lock:

```powershell
uv lock
uv lock --check
rg -n "sherpa-onnx|sherpa-onnx-core|pythonhosted|whisperstream-models" uv.lock
```

The lock must name only the official artifacts that were reviewed. It must not
contain an sdist fallback or an unverified split native package.

Cold-sync the full project under every supported interpreter. Use a different
empty `UV_CACHE_DIR` for each run so a prior wheel cannot satisfy the test:

```powershell
$env:UV_CACHE_DIR = 'C:\ws-sherpa-cache-cp312'
uv sync --locked --python 3.12
uv run --locked python scripts/build.py --verify-sherpa-only

$env:UV_CACHE_DIR = 'C:\ws-sherpa-cache-cp313'
uv sync --locked --python 3.13
uv run --locked python scripts/build.py --verify-sherpa-only

$env:UV_CACHE_DIR = 'C:\ws-sherpa-cache-cp314'
uv sync --locked --python 3.14
uv run --locked python scripts/build.py --verify-sherpa-only
Remove-Item Env:UV_CACHE_DIR
```

Also prove that unsupported Python versions and non-Windows targets fail
resolution instead of falling back to another package source.

### 4. Retire active downstream package assumptions

An official-wheel migration must not relabel an upstream artifact as a
Whisperstream build.

Update the active package and attribution assumptions in:

- `pyproject.toml`, `uv.lock`, and `scripts/build.py`;
- `scripts/generate_licenses.py`;
- `vendor/sherpa-onnx/README.md`;
- developer setup documentation; and
- the build-preflight, dependency-policy, and license tests.

Keep `scripts/build-sherpa-wheels.py`, the verifier's reusable native checks,
the tracked patch, and [`CARVEOUT.md`](CARVEOUT.md) as maintained fallback
tooling. Their last downstream constants can remain a reproducible historical
pin until the next carve-out is built. They must not be called by the normal
official package path or described as the active dependency.

Keep prior manifests, PRs, and provenance in Git history. Remove the active
downstream patch attribution from generated license artifacts and attribute
the exact official version instead.

Regenerate and check attribution:

```powershell
uv run --locked python scripts/generate_licenses.py
uv run --locked pytest `
  tests/test_verify_sherpa_binary.py `
  tests/test_build_command.py `
  tests/test_dependency_policy.py `
  tests/test_licenses_lib.py
uv lock --check
git diff --check
```

Confirm the root and Tauri `THIRD_PARTY_LICENSES.txt` files are byte-identical.

### 5. Prove the packaged official artifact

This dependency enters the frozen sidecar, so wheel verification is not the
release gate. Follow the normal app-version and changelog procedure, then
build at least an unsigned experimental installer:

```powershell
uv run --locked python scripts/build.py --unsigned --experimental
dist/whisperstream-engine.exe --check
```

Before shipping, require:

1. The build preflight accepts the exact official distribution, version, ABI,
   and complete native file set before Nuitka starts.
2. The reviewed wheel extension, Nuitka standalone extension, and freshly
   extracted packaged-runtime extension have identical SHA-256 hashes.
3. Recursive wheel, standalone, and runtime scans find no espeak-ng/piper
   source, object, library, DLL, license, notice, data path, raw marker,
   dependency, or exported symbol.
4. The packaged sidecar transcribes the Parakeet fixture correctly.
5. The packaged sidecar downloads Supertonic, synthesizes finite non-empty
   44.1 kHz audio, and passes the Parakeet model-backed roundtrip.
6. Backend tests, frontend tests/build, `cargo check`, license tests, and
   dependency-policy tests pass.

Record exact wheel, native extension, sidecar, and installer hashes with the
verification evidence for the authorized distribution change.

### Historical package rollback

This workflow requires access to the exact prior downstream wheel objects.
Their public availability is not established. With those objects available,
reverting the dependency pin, source binding, verifier/preflight identity, and
`uv.lock` returns builds to the previous reviewed set.

Do not ship the official upgrade if rollback would require weakening a native
gate or reconstructing an overwritten artifact.
