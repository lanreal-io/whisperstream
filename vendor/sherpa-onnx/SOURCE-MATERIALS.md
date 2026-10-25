# Official Windows native source and build mapping

This mapping covers the official sherpa-onnx/core 1.13.8 Windows x64 graph used
by the GPL source edition. It records inspected upstream inputs, not an executed
rebuild or final installer inventory. No custom eSpeak-free patch is applied.

The [wheel inventory](../../scripts/sherpa-source-wheels.json) records exact
download and native-file hashes for Python 3.12, 3.13 and 3.14. The Windows
packaging recipe currently selects Python 3.13. Preserve its binding wheel and
the shared core wheel as separate artifacts; the core package includes ONNX
Runtime and sherpa C/C++ API DLLs.

## Pinned inputs

| Input | Exact source identity | Build and notice evidence |
| --- | --- | --- |
| sherpa-onnx/core 1.13.8 | `k2-fsa/sherpa-onnx` commit `11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf` | [Windows wheel workflow](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/.github/workflows/build-wheels-win-x64.yaml), `setup.py`, `cmake/`, `scripts/wheel/sherpa-onnx-core/` and Apache-2.0 `LICENSE` |
| eSpeak NG and bundled ucd-tools | `csukuangfj/espeak-ng` commit `ed530aa113046142eb5115cf2fc9157854d0ffe1` | [Fetch/configuration recipe](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/cmake/espeak-ng-for-piper.cmake); GPL-3.0-or-later plus retained BSD/Unicode notices |
| piper-phonemize and bundled uni-algo | `csukuangfj/piper-phonemize` commit `f3ff95afc03640bc1399e113e83361192a2fafb4` | [Fetch/configuration recipe](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/cmake/piper-phonemize.cmake); MIT notices; linked eSpeak retains its GPL terms |

Upstream FetchContent pins the two source archives explicitly:

- [eSpeak source ZIP](https://github.com/csukuangfj/espeak-ng/archive/ed530aa113046142eb5115cf2fc9157854d0ffe1.zip), SHA-256 `e4e262cbe34f7fe21f91f1ba3397f2728e1f30eafbae7853f2b753a9ed13f0dd`.
- [Piper source ZIP](https://github.com/csukuangfj/piper-phonemize/archive/f3ff95afc03640bc1399e113e83361192a2fafb4.zip), SHA-256 `d9cca4e2bdc7d6dd8dffb96a4668283dbd3f77a9c194a3e530c1e8eba9406a5d`.

These are upstream-declared archive digests, not a record that this preparation
downloaded those archives. The [notice provenance](../../scripts/license-texts/sherpa-upstream-provenance.json)
records exact source URLs and hashes of the retained license texts. Preserve
the complete source trees, including bundled component notices and build files.

Both FetchContent recipes force their libraries to static linkage inside the
shared native graph. The core wheel split does not remove eSpeak. Its build
disables eSpeak tests, asynchronous audio, MBROLA, libsonic, libpcaudio, Klatt and
speechplayer and enables `EXTRA_cmn`/`EXTRA_ru`. Those native compiler flags do
not identify the separate Kokoro language-data build.

The pinned [Windows ONNX Runtime recipe](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/cmake/onnxruntime-win-x64.cmake)
selects 1.28.2, with distinct archive hashes for CRT and configuration variants.
Retain the chosen archive, its notices and actual CMake configuration rather than
guessing the variant from a package name. Other transitive FetchContent inputs
are described in the same pinned `cmake/` tree; preserve the actual resolved
source closure and hashes in the final materials inventory. The table above
identifies the eSpeak/Piper chain and is not a complete installer bill of materials.

## Rebuild recipe to verify

Use the pinned source in a fresh Windows x64 checkout with the Visual Studio
C++ toolchain. Upstream's workflow uses Windows Server 2022. Record the precise
local compiler, SDK, Python, CMake and packaging-tool versions during verification;
the upstream workflow does not pin every build tool version.

The core configure/build steps, translated from its workflow to PowerShell:

```powershell
git clone https://github.com/k2-fsa/sherpa-onnx.git sherpa-source
Set-Location sherpa-source
git checkout --detach 11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf
$nativeInstall = Join-Path (Get-Location) 'build/install'
cmake -S . -B build -A x64 -DSHERPA_ONNX_ENABLE_TTS=ON -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DSHERPA_ONNX_BUILD_C_API_EXAMPLES=OFF "-DCMAKE_INSTALL_PREFIX=$nativeInstall"
cmake --build build --config Release -- -m:2
cmake --build build --config Release --target install -- -m:2
```

Upstream invokes configure from inside `build`. The explicit absolute install
prefix above preserves its `build/install` output when using `-S`/`-B` from the
source root. Retain `CMakeCache.txt`, configure/build logs and fetched
source trees. Follow the workflow's core-wheel copy step for the DLLs, import
libraries and C API headers; `scripts/wheel/sherpa-onnx-core/setup.py` owns that
wheel's layout.

For the separate Python binding, the same upstream workflow sets:

```powershell
$env:SHERPA_ONNX_SPLIT_PYTHON_PACKAGE = 'ON'
$env:SHERPA_ONNX_CMAKE_ARGS = '-DSHERPA_ONNX_ENABLE_BINARY=OFF -DSHERPA_ONNX_BUILD_C_API_EXAMPLES=OFF -DSHERPA_ONNX_ENABLE_C_API=OFF -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF'
python setup.py bdist_wheel
```

Run that step with the selected Python ABI and public setuptools/wheel build
dependencies. Use a disposable shell to contain these build settings. The
recipe omits upstream publishing and rolling version-update steps; retain
version 1.13.8. These commands have been checked against the pinned source but
have not been executed in this preparation. No bit-for-bit rebuild is claimed.

## Release handoff

Follow the source-material assembly guide to bind the actual installer, public application source, exact dependency source
and build inputs, notices and functional modification/recombination evidence.
Keep [Kokoro's compiled-data mapping](../kokoro/SOURCE-MATERIALS.md) separate:
those files are downloaded directly upstream and are not staged by the current
installer recipe. Their HF artifact history is not determined by this native
CMake pin. The native code is conveyed and still needs matching source/build
materials, independent of whether the user installs any Kokoro data. Do not
replace the official native inventory with locally rebuilt hashes without a
separately reviewed dependency update.
