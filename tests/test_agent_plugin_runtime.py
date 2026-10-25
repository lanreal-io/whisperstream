"""Dev/prod isolation contracts; never touch real provider homes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_materialized_dev_payload_has_distinct_identity_and_target(tmp_path, monkeypatch, provider):
    from whisperstream.agent_plugin_runtime import plugin_identity
    from whisperstream.claude_hud import install_claude_plugin
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin

    materialize = install_claude_plugin if provider == 'claude' else materialize_codex_plugin
    monkeypatch.delenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', raising=False)
    prod = materialize(tmp_path / 'prod')
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    dev = materialize(tmp_path / 'dev')
    assert plugin_identity().selector == 'whisperstream-dev@whisperstream-dev'
    for result, name, root in [(prod, 'whisperstream', 'prod'), (dev, 'whisperstream-dev', 'dev')]:
        plugin = Path(result['plugin_dir'])
        manifest = json.loads((plugin / f'.{provider}-plugin/plugin.json').read_text('utf-8'))
        assert manifest['name'] == name
        target = json.loads((plugin / 'runtime-target.json').read_text('utf-8'))
        assert target['data_dir'] == str((tmp_path / root).resolve())
    if provider == 'codex':
        assert 'name: speak-dev\n' in (Path(dev['plugin_dir']) / 'skills/speak-dev/SKILL.md').read_text('utf-8')


def test_packaged_build_ignores_dev_variant(monkeypatch):
    from whisperstream import _build_flags
    from whisperstream.agent_plugin_runtime import plugin_identity

    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    monkeypatch.setattr(_build_flags, 'DEV_TOOLS_AVAILABLE', False)
    assert plugin_identity().name == 'whisperstream'


def test_dev_activation_tokens_are_exact(monkeypatch):
    from whisperstream.codex_hud import _speak_skill_argument
    from whisperstream.claude_hud import _is_speech_skill_expansion
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    assert _speak_skill_argument('$speak-dev Ada') == (True, 'Ada')
    assert _speak_skill_argument('$speak Ada') == (False, None)
    assert _speak_skill_argument('$speak-development') == (False, None)
    expansion = {'expansion_type': 'slash_command', 'command_source': 'plugin'}
    assert _is_speech_skill_expansion({**expansion, 'prompt': '/whisperstream-dev:speak Ada'})
    assert not _is_speech_skill_expansion({**expansion, 'prompt': '/whisperstream:speak Ada'})
    assert not _is_speech_skill_expansion({**expansion, 'prompt': '/whisperstream-dev:speaking'})


@pytest.mark.usefixtures('accepted_eula')
@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_hook_token_rejects_absent_and_other_runtime_before_processing(provider):
    from tests.test_codex_hook_endpoint import _make_client
    client, server = _make_client()
    other_client, other = _make_client()
    handler = AsyncMock(return_value={'accepted': True})
    setattr(server, f'_handle_{provider}_hook', handler)
    client.headers.pop('x-whisperstream-hook-token')
    assert client.get('/api/agent-hook/verify').status_code == 403
    assert client.get('/api/agent-hook/verify', headers={'x-whisperstream-hook-token': server._agent_hook_token}).json() == {'ok': True}
    route = f'/api/{provider}-hook'
    assert client.post(route, content='not-json').json() == {}
    assert client.post(route, content='not-json', headers={'x-whisperstream-hook-token': other._agent_hook_token}).json() == {}
    handler.assert_not_called()
    from starlette.requests import Request
    malformed = Request({'type': 'http', 'headers': [(b'x-whisperstream-hook-token', b'\xff' * 64)]})
    assert not server._valid_agent_hook_token(malformed)
    assert client.post(route, json={}, headers={'x-whisperstream-hook-token': server._agent_hook_token}).json() == {'accepted': True}
    handler.assert_awaited_once()
    client.close()
    other_client.close()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_payload_refresh_receipt_is_durable(tmp_path, monkeypatch, provider):
    from whisperstream.agent_plugin_runtime import mark_payload_synced
    from whisperstream.claude_hud import install_claude_plugin
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin
    materialize = install_claude_plugin if provider == 'claude' else materialize_codex_plugin
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    first = materialize(tmp_path)
    assert first['refresh_required'] is True
    assert materialize(tmp_path)['refresh_required'] is True
    mark_payload_synced(first)
    assert materialize(tmp_path)['refresh_required'] is False
    (Path(first['plugin_dir']) / '.whisperstream-payload-synced').write_text('stale')
    assert materialize(tmp_path)['refresh_required'] is True


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_legacy_recognition_requires_dev_layout_and_exact_ownership(tmp_path, monkeypatch, provider):
    from whisperstream.agent_plugin_runtime import legacy_dev_marketplace_owned
    from whisperstream.claude_hud import install_claude_plugin
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin
    materialize = install_claude_plugin if provider == 'claude' else materialize_codex_plugin
    monkeypatch.delenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', raising=False)
    old = materialize(tmp_path / 'Whisperstream-dev')
    prod = materialize(tmp_path / 'Whisperstream')
    assert legacy_dev_marketplace_owned(old['marketplace_dir'], tmp_path / 'Whisperstream', provider)
    assert not legacy_dev_marketplace_owned(prod['marketplace_dir'], tmp_path / 'Whisperstream', provider)
    marker_root = Path(old['plugin_dir'] if provider == 'claude' else old['marketplace_dir'])
    (marker_root / '.whisperstream-owned').write_text('not ours')
    assert not legacy_dev_marketplace_owned(old['marketplace_dir'], tmp_path / 'Whisperstream', provider)


@pytest.mark.skipif(os.environ.get('WHISPERSTREAM_TEST_PLUGIN_CLI') != '1', reason='Opt-in isolated real CLI integration')
@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('order', [('prod', 'dev'), ('dev', 'prod')])
def test_real_cli_coexistence(tmp_path, monkeypatch, provider, order):
    from whisperstream import claude_hud, codex_plugin_lifecycle
    executable = shutil.which(provider)
    if executable is None:
        pytest.skip(f'{provider} CLI is unavailable')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    (tmp_path / 'codex-home').mkdir()
    (tmp_path / 'claude-home').mkdir()
    monkeypatch.setattr(claude_hud, '_claude_settings_path', lambda: tmp_path / 'claude-home/settings.json')
    monkeypatch.setattr(codex_plugin_lifecycle, '_codex_hooks_trusted', lambda *_: False)
    enable = claude_hud.enable_claude_hud if provider == 'claude' else codex_plugin_lifecycle.enable_codex_plugin
    for variant in order:
        root = tmp_path / variant
        monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(root))
        monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', variant)
        result = enable(reconcile_cli=True) if provider == 'claude' else enable(root)
        assert result['ok'], result.get('error')
        again = enable(reconcile_cli=True) if provider == 'claude' else enable(root)
        assert again['ok'], again.get('error')
        assert again['source_status'] == 'current'
    listed = subprocess.run([executable, 'plugin', 'list', '--json'], capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    records = json.loads(listed.stdout)
    records = records if provider == 'claude' else records['installed']
    ids = {record.get('id', record.get('pluginId')) for record in records}
    assert {'whisperstream@whisperstream', 'whisperstream-dev@whisperstream-dev'} <= ids

    # A same-version source change must refresh the actual provider-owned cache,
    # not merely report success after rewriting the marketplace source files.
    monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(tmp_path / 'dev'))
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    module = claude_hud if provider == 'claude' else codex_plugin_lifecycle
    reader_name = '_plugin_template_bytes' if provider == 'claude' else '_template_bytes'
    original_reader = getattr(module, reader_name)
    marker = b'\nIsolated cache refresh regression fixture.\n'
    def changed_reader(path):
        data = original_reader(path)
        return data + marker if path == Path('skills/speak/SKILL.md') else data
    monkeypatch.setattr(module, reader_name, changed_reader)
    result = enable(reconcile_cli=True) if provider == 'claude' else enable(tmp_path / 'dev')
    assert result['ok'], result.get('error')
    cache = tmp_path / f'{provider}-home' / 'plugins/cache'
    assert any(marker in path.read_bytes() for path in cache.rglob('SKILL.md'))
    result = claude_hud.disable_claude_hud() if provider == 'claude' else codex_plugin_lifecycle.disable_codex_plugin(tmp_path / 'dev')
    assert result['ok'], result.get('error')
    # Claude's disabled cache is retained by its existing supported settings path.
    listed = subprocess.run([executable, 'plugin', 'list', '--json'], capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    records = json.loads(listed.stdout)
    records = records if provider == 'claude' else records['installed']
    prod = next(record for record in records if record.get('id', record.get('pluginId')) == 'whisperstream@whisperstream')
    assert prod.get('enabled') is True
    assert all(record.get('enabled') is False for record in records if record.get('id', record.get('pluginId')) == 'whisperstream-dev@whisperstream-dev')


@pytest.mark.skipif(os.environ.get('WHISPERSTREAM_TEST_PLUGIN_CLI') != '1', reason='Opt-in isolated real CLI integration')
@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('first_variant', ['prod', 'dev'])
def test_real_cli_migrates_owned_legacy_dev(tmp_path, monkeypatch, provider, first_variant):
    from whisperstream import claude_hud, codex_plugin_lifecycle

    executable = shutil.which(provider)
    if executable is None:
        pytest.skip(f'{provider} CLI is unavailable')
    for name, env in [('claude', 'CLAUDE_CONFIG_DIR'), ('codex', 'CODEX_HOME')]:
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv(env, str(home))
    monkeypatch.setattr(claude_hud, '_claude_settings_path', lambda: tmp_path / 'claude/settings.json')
    monkeypatch.setattr(codex_plugin_lifecycle, '_codex_hooks_trusted', lambda *_: False)
    enable = claude_hud.enable_claude_hud if provider == 'claude' else codex_plugin_lifecycle.enable_codex_plugin

    # Reproduce the old dev install: production identity under the dev data root.
    dev_root = tmp_path / 'Whisperstream-dev'
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'prod')
    monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(dev_root))
    old = enable(reconcile_cli=True) if provider == 'claude' else enable(dev_root)
    assert old['ok'], old.get('error')
    for variant in [first_variant, 'dev' if first_variant == 'prod' else 'prod']:
        root = dev_root if variant == 'dev' else tmp_path / 'Whisperstream'
        monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', variant)
        monkeypatch.setenv('WHISPERSTREAM_DATA_DIR', str(root))
        result = enable(reconcile_cli=True) if provider == 'claude' else enable(root)
        assert result['ok'], result.get('error')
    listed = subprocess.run([executable, 'plugin', 'list', '--json'], capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    records = json.loads(listed.stdout)
    records = records if provider == 'claude' else records['installed']
    enabled = {record.get('id', record.get('pluginId')) for record in records if record.get('enabled')}
    assert {'whisperstream@whisperstream', 'whisperstream-dev@whisperstream-dev'} <= enabled
    assert Path(old['plugin_dir']).is_dir()  # Migration only removes CLI registrations.


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('scenario', ['matching', 'stale', 'old-server', 'missing-target', 'missing-discovery'])
def test_cached_forwarder_targets_exact_runtime(tmp_path, monkeypatch, provider, scenario):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from whisperstream.claude_hud import install_claude_plugin
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin

    executable = shutil.which('powershell')
    if executable is None:
        pytest.skip('Windows PowerShell is required')
    monkeypatch.setenv('WHISPERSTREAM_AGENT_PLUGIN_VARIANT', 'dev')
    materialize = install_claude_plugin if provider == 'claude' else materialize_codex_plugin
    root = tmp_path / 'target with spaces'
    plugin = Path(materialize(root)['plugin_dir'])
    received = []
    probes = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            probes.append((self.path, self.headers.get('x-whisperstream-hook-token')))
            valid = scenario != 'stale' and self.headers.get('x-whisperstream-hook-token') == 'a' * 64
            self.send_response(404 if scenario == 'old-server' else (200 if valid else 403))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if valid else b'{"ok":false}')

        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    discovery = root / f'{provider}-hud-port.json'
    discovery.write_text(json.dumps({'port': server.server_port, 'token': 'a' * 64}), encoding='utf-8')
    if scenario == 'missing-target':
        (plugin / 'runtime-target.json').unlink()
    if scenario == 'missing-discovery':
        discovery.unlink()
    env = os.environ.copy()
    env['WHISPERSTREAM_DATA_DIR'] = str(tmp_path / 'wrong-inherited-target')
    env.pop('WHISPERSTREAM_SESSION_TOKEN', None)
    try:
        run = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(plugin / 'scripts/forward-hook.ps1')], input=b'{"fixture":"synthetic"}', capture_output=True, env=env, timeout=30)
        assert run.returncode == 0
        assert json.loads(run.stdout) == {}
        assert received == ([(f'/api/{provider}-hook', b'{"fixture":"synthetic"}')] if scenario == 'matching' else [])
        assert probes == ([] if scenario.startswith('missing-') else [('/api/agent-hook/verify', 'a' * 64)])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dev_launcher_propagates_explicit_variant():
    launcher = (Path(__file__).parents[1] / 'scripts/run_dev.ps1').read_text('utf-8')
    assert '$env:WHISPERSTREAM_AGENT_PLUGIN_VARIANT = "dev"' in launcher
    assert '"WHISPERSTREAM_AGENT_PLUGIN_VARIANT"' in launcher


def test_darwin_claude_payload_keeps_native_mod_and_authenticated_forwarder(tmp_path):
    from whisperstream.claude_hud import install_claude_plugin
    result = install_claude_plugin(tmp_path, hook_platform='darwin')
    plugin = Path(result['plugin_dir'])
    assert (plugin / 'hooks/register.js').read_bytes() == (Path(__file__).parents[1] / 'src/whisperstream/claude_plugin/hooks/register.js').read_bytes()
    hooks = json.loads((plugin / 'hooks/hooks.json').read_text('utf-8'))
    for registrations in hooks['hooks'].values():
        for registration in registrations:
            for hook in registration['hooks']:
                assert hook['command'] == '/bin/zsh'
                assert hook['args'] == ['${CLAUDE_PLUGIN_ROOT}/scripts/forward-hook.sh']
    assert (plugin / 'runtime-target.json').is_file()


def test_darwin_desktop_only_setup_materializes_without_cli(tmp_path, monkeypatch):
    from whisperstream import claude_hud
    from whisperstream.macos_agent_integration import MacOSAgentPluginAdapter
    from whisperstream.agent_integration_contract import AgentLifecycleAction, AgentLifecycleRequest, AgentProvider
    monkeypatch.setattr('whisperstream.config_manager.get_app_dir', lambda: tmp_path)
    monkeypatch.setattr(claude_hud, '_claude_settings_path', lambda: tmp_path / 'settings.json')
    adapter = MacOSAgentPluginAdapter(app_dir=tmp_path, reconcile_cli=lambda: False,
        cli_runner=lambda *_: pytest.fail('Desktop-only setup must not invoke CLI'))
    result = adapter.apply(AgentLifecycleRequest('desktop', 1, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.RECONCILE))
    payload = adapter.take_payload('desktop', 1)
    assert payload['ok'] is True
    assert (Path(payload['plugin_dir']) / 'hooks/register.js').is_file()
    assert result.failure is None


def test_windows_adapter_observes_current_surface_policy():
    from whisperstream.windows_agent_integration import WindowsAgentPluginAdapter
    from whisperstream.agent_integration_contract import AgentLifecycleAction, AgentLifecycleRequest, AgentProvider
    calls = []
    selected = [False]
    adapter = WindowsAgentPluginAdapter(reconcile_cli=lambda: selected[0],
        enable=lambda **kwargs: calls.append(kwargs) or {'ok': True})
    adapter.apply(AgentLifecycleRequest('desktop', 1, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.RECONCILE))
    selected[0] = True
    adapter.apply(AgentLifecycleRequest('cli', 2, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.RECONCILE))
    assert calls == [{'reconcile_cli': False}, {'reconcile_cli': True}]


def test_windows_adapter_forwards_force_pause_and_reports_failures():
    from whisperstream.windows_agent_integration import WindowsAgentPluginAdapter
    from whisperstream.agent_integration_contract import (
        AgentLifecycleAction, AgentLifecycleRequest, AgentLifecycleState, AgentProvider,
    )
    calls = []
    paused = []
    adapter = WindowsAgentPluginAdapter(
        reconcile_cli=lambda: True,
        enable=lambda **kwargs: calls.append(kwargs) or {'ok': True, 'status': 'installed'},
        disable=lambda: {'ok': False, 'status': 'failed', 'error': 'plugin_remove_failed'},
        pause=lambda: paused.append(True) or {'ok': True, 'status': 'paused'},
    )
    adapter.apply(AgentLifecycleRequest('a', 1, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.RECONCILE, force=True))
    adapter.apply(AgentLifecycleRequest('b', 2, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.RECONCILE, keep_disabled=True))
    assert calls == [{'reconcile_cli': True, 'force': True}, {'reconcile_cli': True, 'keep_disabled': True}]
    paused_result = adapter.apply(AgentLifecycleRequest('c', 3, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.DISABLE))
    assert paused_result.state is AgentLifecycleState.DISABLED and paused == [True]
    removed = adapter.apply(AgentLifecycleRequest('d', 4, AgentProvider.CLAUDE_CODE, AgentLifecycleAction.REMOVE))
    assert removed.state is AgentLifecycleState.FAILED


def test_windows_disconnect_uninstalls_through_the_claude_cli(monkeypatch):
    from whisperstream import claude_hud
    order = []
    monkeypatch.setattr(claude_hud, '_claude_cli_path', lambda: 'claude')
    monkeypatch.setattr(claude_hud, 'remove_claude_plugin_via_cli',
                        lambda *_a, **_k: order.append('cli') or {'ok': True, 'status': 'removed'})
    monkeypatch.setattr(claude_hud, 'disable_claude_hud',
                        lambda: order.append('settings') or {'ok': True, 'status': 'not_installed'})
    assert claude_hud.disconnect_claude_plugin()['ok'] is True
    assert order == ['cli', 'settings']
    monkeypatch.setattr(claude_hud, 'remove_claude_plugin_via_cli',
                        lambda *_a, **_k: {'ok': False, 'status': 'failed'})
    order.clear()
    assert claude_hud.disconnect_claude_plugin()['ok'] is False
    assert order == []
