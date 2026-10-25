"""Synthetic integrity checks for the public native dependency path.

No external fixtures, recordings or model data are used.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sherpa_source', ROOT / 'scripts/check-sherpa-source.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture(params=[".dll", ".so", ".dylib"])
def native(tmp_path, request):
    path = tmp_path / ("library" + request.param)
    path.write_bytes(b'synthetic native payload')
    entry = {'distribution': 'sherpa-onnx-core', 'native_files': [{
        'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
    dist = SimpleNamespace(version='1.13.8', files=[Path(path.name)],
                           locate_file=lambda name: tmp_path / name)
    return path, entry, dist


def test_accepts_exact_payload(native):
    _, entry, dist = native
    assert checker.verify_distribution(dist, entry, '1.13.8') == 1


@pytest.mark.parametrize('change', ['modified', 'missing', 'extra', 'version', 'no-record'])
def test_rejects_unreviewed_payload(native, change):
    path, entry, dist = native
    if change == 'modified':
        path.write_bytes(b'changed')
    elif change == 'missing':
        path.unlink()
    elif change == 'extra':
        dist.files.append(Path('extra.pyd'))
    elif change == 'version':
        dist.version = '1.13.7'
    else:
        dist.files = None
    with pytest.raises(ValueError):
        checker.verify_distribution(dist, entry, '1.13.8')


def test_lock_pins_only_reviewed_public_wheels():
    manifest = json.loads(checker.MANIFEST.read_text(encoding='utf-8'))
    lock = tomllib.loads((ROOT / 'uv.lock').read_text(encoding='utf-8'))
    sherpa = [p for p in lock['package'] if p['name'] in ('sherpa-onnx', 'sherpa-onnx-core')]
    expected = {e['url']: e['sha256'] for e in manifest['wheels']}
    found = {}
    for package in sherpa:
        assert package['version'] == manifest['version']
        assert 'sdist' not in package
        for wheel in package['wheels']:
            url = wheel.get('url') or package['source']['url']
            found[url] = wheel['hash'].removeprefix('sha256:')
    assert found == expected


def test_rejects_native_files_not_listed_in_record(tmp_path):
    directory = tmp_path / 'sherpa_onnx/lib'
    directory.mkdir(parents=True)
    (directory / 'reviewed.dll').write_bytes(b'synthetic')
    binding = SimpleNamespace(locate_file=lambda name: tmp_path / name)
    entries = [{'native_files': [{'path': 'sherpa_onnx/lib/reviewed.dll'}]}]
    checker.verify_native_directory(binding, entries)
    (directory / 'stale.pyd').write_bytes(b'unrecorded synthetic payload')
    with pytest.raises(ValueError, match='native directory'):
        checker.verify_native_directory(binding, entries)


@pytest.mark.parametrize(('system', 'machine', 'wheel_platform'), [
    ('win32', 'AMD64', 'win_amd64'), ('darwin', 'arm64', 'macosx_11_0_arm64'),
])
@pytest.mark.parametrize('minor', [12, 13, 14])
def test_supported_platform_selects_binding_and_core_before_import(monkeypatch, system, machine, wheel_platform, minor):
    manifest = json.loads(checker.MANIFEST.read_text())
    selected = []
    monkeypatch.setattr(checker.sys, 'platform', system)
    monkeypatch.setattr(checker.sys, 'version_info', SimpleNamespace(major=3, minor=minor))
    monkeypatch.setattr(checker.platform, 'machine', lambda: machine)
    monkeypatch.setattr(checker.metadata, 'distribution', lambda name: name)
    def inspect_inventory(distribution, entry, version):
        selected.append(entry)
        assert version == '1.13.8'
        assert entry['filename'].endswith('-' + wheel_platform + '.whl')
        assert f'-cp3{minor}-cp3{minor}-' in entry['filename'] or '-py3-none-' in entry['filename']
        if len(selected) == 2:
            raise ValueError('synthetic stop before native import')
        return len(entry['native_files'])
    monkeypatch.setattr(checker, 'verify_distribution', inspect_inventory)
    with pytest.raises(ValueError, match='synthetic stop'):
        checker.verify()
    assert sorted(entry['distribution'] for entry in selected) == ['sherpa-onnx', 'sherpa-onnx-core']


def test_intel_mac_fails_before_loading_distributions(monkeypatch):
    monkeypatch.setattr(checker.sys, 'platform', 'darwin')
    monkeypatch.setattr(checker.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(checker.metadata, 'distribution', lambda _: pytest.fail('unsupported host must not load native distributions'))
    with pytest.raises(ValueError, match='Apple Silicon'):
        checker.verify()
