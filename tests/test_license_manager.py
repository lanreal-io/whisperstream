"""Unrestricted grants are local and cannot mutate historical evidence."""
import json
import socket
import sys

import pytest
from whisperstream.license_manager import LicenseManager


@pytest.mark.parametrize('historical', [None, b'corrupt', json.dumps({'activated': True}).encode(), json.dumps({'activated': False, 'trial_started_at': '2020-01-01', 'status': 'refunded'}).encode()])
def test_final_edition_is_free_without_network_or_history_writes(tmp_path, monkeypatch, historical):
    monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(tmp_path))
    if sys.platform == 'win32':
        import winreg
        for name in ('OpenKey', 'CreateKey', 'QueryValueEx', 'SetValueEx', 'DeleteValue'):
            monkeypatch.setattr(winreg, name, lambda *a, **kw: pytest.fail('unexpected registry access'))
    trial_record = tmp_path / 'trial-policy-refresh.json'
    trial_record.write_bytes(b'old-refresh-state')
    record = tmp_path / 'license.json'
    if historical is not None:
        record.write_bytes(historical)
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('unexpected network'))
    for _ in range(2):
        manager = LicenseManager()
        assert manager.is_pro and manager.can_start_recording()
        assert not manager.trial_expired
        manager.mark_trial_started()
        manager.record_usage(999999999)
        assert manager.status()['is_pro'] is True
        assert manager.status()['license_key'] == ''
    assert (record.read_bytes() if record.exists() else None) == historical
    assert trial_record.read_bytes() == b'old-refresh-state'


@pytest.mark.asyncio
async def test_retired_license_actions_do_not_disable_free_edition(tmp_path, monkeypatch):
    monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(tmp_path))
    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: pytest.fail('unexpected network'))
    manager = LicenseManager()
    assert (await manager.activate('old-key'))['is_pro']
    assert (await manager.deactivate())['is_pro']
    assert await manager.validate()
    await manager.maybe_revalidate()
    assert not list(tmp_path.iterdir())
