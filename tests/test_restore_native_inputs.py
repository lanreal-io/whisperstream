"""Native recovery fails closed before copying unverified wheel inputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

MODULE_PATH=Path(__file__).resolve().parents[1]/'scripts/restore-native-inputs.py'
spec=importlib.util.spec_from_file_location('restore_native_inputs',MODULE_PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@pytest.fixture
def inputs(tmp_path,monkeypatch):
    (tmp_path/'scripts').mkdir();archive=tmp_path/'archive';archive.mkdir()
    rows=[]
    for index in range(3):
        name=f'input-{index}.whl';data=f'payload{index}'.encode();(archive/name).write_bytes(data)
        rows.append({'filename':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    (tmp_path/'scripts/sherpa-wheel-inputs.json').write_text(json.dumps(rows))
    monkeypatch.setattr(module,'ROOT',tmp_path)
    return archive,tmp_path/'restored',rows

def test_restore_exact_set(inputs):
    archive,destination,rows=inputs;module.restore(archive,destination)
    assert {p.name for p in destination.iterdir()}=={r['filename'] for r in rows}
    assert all((destination/r['filename']).read_bytes()==(archive/r['filename']).read_bytes() for r in rows)

@pytest.mark.parametrize('missing',[False,True])
def test_invalid_archive_never_partially_copies(inputs,missing):
    archive,destination,rows=inputs;bad=archive/rows[-1]['filename']
    if missing:bad.unlink()
    else:bad.write_bytes(b'tampered')
    with pytest.raises(ValueError):module.restore(archive,destination)
    assert not destination.exists()
