# sherpa-onnx downstream carve-out runbook

Use this runbook only after an official upstream release fails one or more
acceptance criteria in [`UPGRADE.md`](UPGRADE.md). It recreates
Whisperstream's espeak-free, Supertonic-capable Windows x64 wheel set for a new
upstream version without weakening provenance, ABI, symbol, dependency, or
packaged-runtime gates.

The retained patch and provenance in [README.md](README.md) describe the
`1.13.3+whisperstream.2` implementation. The current source edition uses the
official upstream wheel; this fallback runbook does not change that selection.

## Non-negotiable output contract

The downstream release is acceptable only when:

- source is a clean detached checkout of one recorded upstream commit;
- the tracked patch applies without fuzz;
- ASR and Supertonic remain compiled and importable;
- espeak-ng and piper-phonemize are not fetched, compiled, linked, exported,
  packaged, or shipped as data;
- cp312, cp313, and cp314 wheels are independently built and verified, unless
  Whisperstream's supported Python range has deliberately changed;
- wheel objects are immutable and the public index carries exact SHA-256
  fragments;
- `uv.lock` can resolve only the reviewed wheel set;
- the wheel, Nuitka standalone tree, and extracted packaged runtime contain
  the same native extension bytes; and
- packaged Parakeet STT and Supertonic TTS both pass model-backed tests.

Never reuse a downstream version after any wheel bytes have changed. Start
with `.1` for a new upstream version and increment the local suffix for every
replacement, for example `2.0.1+whisperstream.1`.

## 1. Prepare the upgrade branch

1. Create an isolated `app/` branch and worktree from current `main`.
2. Confirm `_version.py` contains the `0.0.0+dev` sentinel before Git
   operations or package builds.
3. Read:
   - this file and `vendor/sherpa-onnx/README.md`;
   - `scripts/build-sherpa-wheels.py`;
   - `scripts/verify-sherpa-binary.py`;
   - the wheel publisher for your reviewed package index (the source edition
     does not include one);
   - `scripts/build.py`'s sherpa preflight;
   - the exact upstream source and retained patch provenance; and
   - available native and packaged verification results for the selected inputs.
4. Record the candidate upstream tag, full commit SHA, upstream version,
   proposed downstream version, supported ABIs, and reason the official route
   failed in the PR or a feature plan.

Required local tools are Windows x64, Git, `uv`, Visual Studio Build Tools with
the MSVC toolchain, CMake, and `dumpbin`. The builder installs its pinned
Python build requirements in isolated environments. Publication credentials
are not needed until the live publication step.

## 2. Port the patch on exact upstream source

Use a clean detached checkout. Keep it on a short path because Visual Studio
FileTracker still has legacy path handling:

```powershell
$source = 'C:\ws-sherpa-port\source'
if (Test-Path -LiteralPath $source) {
  throw "Scratch source path must not exist: $source"
}
New-Item -ItemType Directory -Path $source -Force
git -C $source init
git -C $source config core.longpaths true
git -C $source remote add origin https://github.com/k2-fsa/sherpa-onnx.git
git -C $source fetch --depth 1 origin <FULL_UPSTREAM_COMMIT>
git -C $source checkout --detach FETCH_HEAD
git -C $source rev-parse HEAD
git -C $source apply --check `
  C:\path\to\whisperstream\vendor\sherpa-onnx\espeak-free-supertonic.patch
git -C $source apply `
  C:\path\to\whisperstream\vendor\sherpa-onnx\espeak-free-supertonic.patch
git -C $source diff --check
```

Replace `<FULL_UPSTREAM_COMMIT>` and the repository patch path before running
the commands.

Port the patch manually if upstream changed. Review every hunk instead of
forcing rejected hunks. The downstream CMake option must remain default-off so
the unmodified upstream build graph is preserved when the option is absent.

Inspect both graphs:

- the default graph should still match the selected upstream release;
- the Whisperstream graph must retain ASR, the Python module, and required
  Supertonic sources and bindings;
- the Whisperstream graph must omit espeak/piper fetches, sources, dispatcher
  branches, config bindings, tests, libraries, and link targets; and
- package metadata and Python exports must describe only APIs compiled into
  the downstream native module.

Do not use `SHERPA_ONNX_ENABLE_TTS=OFF`; it removes Supertonic as well as the
unwanted engines.

## 3. Update artifact-production identity

Keep the application dependency on the last published wheel while creating
the new artifact. Updating `pyproject.toml` before publication would make
locked development and package builds depend on a wheel that does not exist.

First update only the artifact-production and verification sources:

| Value or behavior | Files |
| --- | --- |
| Upstream commit and version | `scripts/build-sherpa-wheels.py` and your wheel publisher |
| New PEP 440 downstream version | builder, publisher, and verifier |
| Supported Python versions and ABI tags | builder, publisher, verifier, and their tests |
| CMake flags and patch path | builder |
| Expected filenames, object prefix, manifest, and public-index identity | publisher |
| Wheel metadata, native layout, forbidden distributions/markers/symbols, and runtime smoke | verifier |
| Patch behavior and upstream provenance | `vendor/sherpa-onnx/README.md` and the patch |
| Exact expectations | `tests/test_build_sherpa_wheels.py`, your publisher's tests, and `tests/test_verify_sherpa_binary.py` |

Search for every old identity before proceeding:

```powershell
rg -n "OLD_UPSTREAM_VERSION|OLD_DOWNSTREAM_VERSION|OLD_COMMIT" `
  scripts vendor tests pyproject.toml uv.lock README.md `
  THIRD_PARTY_LICENSES.txt src-tauri/resources/THIRD_PARTY_LICENSES.txt
```

At this stage, occurrences in the application pin, lock, active package
preflight, and generated licenses should still name the last published set.

Run the artifact-focused unit gates:

```powershell
uv run --locked pytest `
  tests/test_build_sherpa_wheels.py `
  tests/test_verify_sherpa_binary.py
git diff --check
```

## 4. Build the complete wheel set

The builder:

- creates a unique short scratch directory under `C:\ws-build`;
- checks out the exact commit and applies the tracked patch;
- injects the downstream version;
- builds each supported normal-ABI Windows x64 wheel;
- verifies metadata, dependencies, native markers, imports, and target ABI;
- copies verified wheels into an atomic staging directory;
- verifies the copies again; and
- promotes the complete set plus `sha256-manifest.json` into the requested
  output directory.

Build every supported ABI:

```powershell
uv run --locked python scripts/build-sherpa-wheels.py
```

If `C:\ws-build` is unavailable, use another short absolute root:

```powershell
$env:WHISPERSTREAM_SHERPA_WORK_ROOT = 'D:\ws-build'
uv run --locked python scripts/build-sherpa-wheels.py
Remove-Item Env:WHISPERSTREAM_SHERPA_WORK_ROOT
```

Do not point scratch at the repository or a deep worktree. Generated CMake
try-compile paths can trigger MSBuild FileTracker FTK1011 even when Windows
long paths are enabled.

The default output must contain exactly:

- one `win_amd64` wheel for every supported ABI; and
- `sha256-manifest.json`.

The manifest must record the distribution, downstream version, upstream URL,
full commit, upstream version, patch path/hash, and every wheel name, size, and
SHA-256.

## 5. Independently verify the built wheels

The builder's internal checks are necessary but not the independent acceptance
pass:

```powershell
Get-ChildItem dist/sherpa-onnx-wheels/*.whl | ForEach-Object {
  uv run --locked python scripts/verify-sherpa-binary.py $_.FullName
}
```

Then run your publisher's dry run against the complete wheel set.

For every wheel, confirm:

- normalized distribution name and exact downstream version;
- exact ABI and `win_amd64` platform tags;
- no `sherpa-onnx-core` or other unverified native dependency;
- exactly the expected ABI-matched `_sherpa_onnx` extension;
- no forbidden raw markers;
- no forbidden imported DLL or exported symbol;
- target-interpreter import and version smoke;
- retained recognizer and Supertonic config APIs; and
- absent piper-backed model config APIs.

The publisher dry run must accept exactly the manifest plus complete wheel set
and print the reviewed hashes without reading credentials or writing remotely.

## 6. Publish immutable wheels and the project index

Only after the dry run passes:

The live publisher must:

1. create missing versioned wheel objects with conditional writes;
2. reject an existing object whose recorded size or SHA-256 differs;
3. download every public object and verify its bytes;
4. build and validate the PEP 503 project index;
5. update the mutable index only after all immutable wheels pass; and
6. read the public index back and verify exact links, ABI tags, versions, and
   `#sha256` fragments.

Independently inspect the published `sherpa-onnx` project index page.

Do not overwrite or delete an object to fix a bad build. Increment the
downstream suffix, rebuild the complete set, and publish new immutable keys.
Old objects remain the rollback path.

## 7. Move the application to the published set

After public read-back succeeds, update the application trust identity:

| Concern | Files |
| --- | --- |
| Exact dependency version and explicit index binding | `pyproject.toml` |
| Locked wheel URLs and hashes | `uv.lock` generated by `uv lock` |
| Package-build expected version and native file layout | `scripts/build.py` |
| Native license version, commit, and patch provenance | `scripts/generate_licenses.py` |
| Developer commands and current version | developer setup documentation |
| Exact application expectations | `tests/test_build_command.py`, `tests/test_dependency_policy.py`, and `tests/test_licenses_lib.py` |

Regenerate and inspect the lock:

```powershell
uv lock
uv lock --check
rg -n "sherpa-onnx|sherpa-onnx-core|pythonhosted" uv.lock
```

The downstream lock must contain only the reviewed direct wheel URLs and
hashes. Reject:

- a sherpa sdist;
- a `pythonhosted.org` sherpa artifact;
- a PyPI fallback;
- an unverified `sherpa-onnx-core`; or
- a missing supported ABI.

## 8. Cold-sync every supported ABI

Use a fresh cache for each full project sync:

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

For each interpreter, confirm the installed version, extension ABI, native
scan, import smoke, recognizer API, and Supertonic configs. Then prove the lock
fails closed for an unsupported Python version and a non-Windows target.

Return the worktree to its normal development interpreter before subsequent
tests:

```powershell
uv sync --locked
```

## 9. Regenerate attribution and run regression gates

```powershell
uv run --locked python scripts/generate_licenses.py

uv run --locked pytest `
  tests/test_build_sherpa_wheels.py `
  tests/test_verify_sherpa_binary.py `
  tests/test_build_command.py `
  tests/test_dependency_policy.py `
  tests/test_licenses_lib.py

uv lock --check
git diff --check
```

Confirm:

- root and Tauri `THIRD_PARTY_LICENSES.txt` are byte-identical;
- the license audit names the exact downstream version;
- native attribution records the exact upstream commit and patch;
- no espeak-ng or piper-phonemize package attribution was introduced; and
- unrelated packages with `piper` in their name were not accidentally removed.

Run the normal backend, frontend, Rust, and package-policy gates before the PR
is considered release-capable.

## 10. Build and prove the packaged sidecar

This dependency enters the frozen sidecar. The task is not complete after
wheel tests.

Follow the normal app-version and changelog procedure, then build at least an
unsigned experimental installer:

```powershell
uv run --locked python scripts/build.py --unsigned --experimental
dist/whisperstream-engine.exe --check
```

Require all of these:

1. Sherpa preflight passes before Nuitka starts and reports the exact version,
   ABI, and complete native member count.
2. The reviewed wheel extension, Nuitka standalone extension, and freshly
   extracted packaged-runtime extension are byte-identical by SHA-256.
3. Every native file in the wheel, standalone tree, and runtime passes the raw
   marker scan.
4. Recursive payload inventories find no espeak-ng/piper source, object,
   library, DLL, license, notice, or data path.
5. `dumpbin /dependents` and `/exports` find no forbidden dependency or
   symbol.
6. The packaged sidecar transcribes the Parakeet fixture correctly.
7. The packaged sidecar downloads Supertonic through the real endpoint and
   emits the expected ready/status transitions.
8. Supertonic produces finite, non-empty float32 44.1 kHz audio through the
   packaged sherpa extension and packaged ONNX Runtime DLL.
9. Parakeet correctly transcribes the synthesized samples in the model-backed
   roundtrip suite.
10. The final backend, frontend, Rust, license, and dependency-policy gates
    pass.

Record exact input/output hashes, tool versions, commands and results in the PR.
Keep native payload and packaged runtime verification distinct, and retain
machine-readable receipts outside the curated source export.
Physical speaker audibility can remain an explicitly documented
device-specific check; it does not replace the model-backed audio proof.

## 11. Release and rollback rules

- Keep any feature/release blocker open until packaged STT and TTS proof is
  complete.
- Do not publish a signed installer or change a production feature gate merely
  because the wheels were published.
- Preserve old immutable wheel objects and their lockfile hashes.
- To roll back, restore the previous `pyproject.toml` pin, `uv.lock`,
  `scripts/build.py` expected version, verifier identity, and attribution.
- Never fix an already-published version in place.
- Never accept a stock wheel, sdist fallback, unverified split package, or
  verifier exception as a temporary bridge.

## Completion checklist

- [ ] Official route failure is recorded.
- [ ] Exact upstream commit and new downstream version are recorded.
- [ ] Patch applies cleanly and both build graphs were reviewed.
- [ ] All supported wheels build from short scratch.
- [ ] Independent wheel and runtime verification passes.
- [ ] Dry-run publication passes.
- [ ] Immutable objects and public index are published and read back.
- [ ] Application pin and lock contain only reviewed URLs/hashes.
- [ ] Cold full-project sync passes for every supported ABI.
- [ ] Unsupported platforms/ABIs fail closed.
- [ ] License artifacts and audit are regenerated and checked.
- [ ] Full regression gates pass.
- [ ] Packaged wheel/standalone/runtime native hashes match.
- [ ] Packaged Parakeet and Supertonic model-backed tests pass.
- [ ] PR records wheel, extension, sidecar, and installer hashes.
- [ ] Rollback remains possible using the prior immutable set.
