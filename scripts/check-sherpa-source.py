"""Verify the official native wheels used by the GPL source edition.

This checks installed bytes and APIs without downloading models or building a
package. The installer build reuses this same inventory and API gate.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import platform
import sys
from pathlib import Path

MANIFEST = Path(__file__).with_name("sherpa-source-wheels.json")


def verify_distribution(distribution, entry: dict, version: str) -> int:
    """Check every recorded native file, including the core's Scripts copies."""
    if distribution.version != version:
        raise ValueError(f"{entry['distribution']}: expected version {version}")
    files = distribution.files
    if files is None:
        raise ValueError(f"{entry['distribution']}: installed RECORD is missing")
    actual = {str(p).replace('\\', '/') for p in files
              if str(p).lower().endswith(('.dll', '.pyd', '.so', '.dylib'))}
    expected = {record['path']: record['sha256'] for record in entry['native_files']}
    if len(expected) != len(entry['native_files']):
        raise ValueError('Reviewed native file inventory contains duplicate paths')
    if actual != set(expected):
        raise ValueError(f"{entry['distribution']}: native file inventory differs from reviewed wheel")
    for name, digest in expected.items():
        path = Path(distribution.locate_file(name))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"{entry['distribution']}: native file hash mismatch: {name}")
    return len(actual)


def verify_native_directory(binding, entries: list[dict]) -> None:
    """Reject stale native files not named by either distribution's RECORD."""
    native_dir = Path(binding.locate_file('sherpa_onnx/lib'))
    expected = {Path(binding.locate_file(item['path'])).resolve()
                for entry in entries for item in entry['native_files']
                if item['path'].startswith('sherpa_onnx/lib/')}
    actual = {path.resolve() for path in native_dir.rglob('*')
              if path.is_file() and path.suffix.lower() in ('.dll', '.pyd', '.so', '.dylib')}
    if actual != expected:
        raise ValueError('Installed sherpa native directory differs from the reviewed graph')


def verify() -> None:
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    machine = platform.machine().lower()
    if sys.platform == 'win32' and machine in ('amd64', 'x86_64'):
        wheel_platform = 'win_amd64'
    elif sys.platform == 'darwin' and machine == 'arm64':
        wheel_platform = 'macosx_11_0_arm64'
    else:
        raise ValueError('Source wheels require Windows x64 or Apple Silicon macOS')
    if sys.implementation.name != 'cpython' or abi not in ('cp312', 'cp313', 'cp314'):
        raise ValueError('Source wheels require CPython 3.12, 3.13 or 3.14')
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    entries = [e for e in manifest['wheels']
               if e['filename'].endswith(f'-{wheel_platform}.whl')
               and (f'-{abi}-{abi}-' in e['filename'] or '-py3-none-' in e['filename'])]
    if sorted(e['distribution'] for e in entries) != ['sherpa-onnx', 'sherpa-onnx-core']:
        raise ValueError('Expected one reviewed binding wheel and one reviewed core wheel')
    count = sum(verify_distribution(metadata.distribution(e['distribution']), e, manifest['version'])
                for e in entries)
    verify_native_directory(metadata.distribution('sherpa-onnx'), entries)
    # Import only after the binding and all core copies match their reviewed bytes.
    import sherpa_onnx as sherpa
    if sherpa.__version__ != manifest['version']:
        raise ValueError('Imported sherpa-onnx version differs from installed metadata')
    for method in ('from_transducer', 'from_qwen3_asr', 'from_cohere_transcribe'):
        if not callable(getattr(sherpa.OfflineRecognizer, method, None)):
            raise ValueError(f'Missing recognizer API: {method}')
    model = sherpa.OfflineTtsModelConfig()
    model.supertonic = sherpa.OfflineTtsSupertonicModelConfig()
    model.kokoro = sherpa.OfflineTtsKokoroModelConfig()
    if model.kokoro is None or not callable(getattr(sherpa.OfflineTts, "generate", None)):
        raise ValueError("Kokoro configuration/synthesis API is unavailable")
    config = sherpa.OfflineTtsConfig()
    config.model = model
    if config.model.supertonic is None:
        raise ValueError('Supertonic configuration API is unavailable')
    print(f"Official sherpa-onnx {manifest['version']} / {abi}: {count} native files and APIs verified.")


if __name__ == '__main__':
    try:
        verify()
    except (ValueError, OSError, ImportError, AttributeError) as exc:
        print(f'Sherpa source check failed: {exc}', file=sys.stderr)
        sys.exit(1)
