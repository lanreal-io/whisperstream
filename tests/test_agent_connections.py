"""Synthetic connection lifecycle and noninteractive hook regressions."""

import json
from types import SimpleNamespace

import pytest

from tests.test_claude_hook_endpoint import _make_client, _make_mock_config, STOP_CLAUDE
from whisperstream.agent_integration_contract import (
    AgentLifecycleAction, AgentLifecycleRequest, AgentLifecycleState, AgentProvider,
)
from whisperstream.windows_agent_integration import WindowsAgentPluginAdapter

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.mark.parametrize(("route", "patch"), [
    ("/api/claude-hook", {"agent_id": "synthetic-worker"}),
    ("/api/codex-hook", {"agent_id": "synthetic-worker"}),
    ("/api/codex-hook", {"transcript_path": None}),
    ("/api/claude-event", {"agent_id": "synthetic-worker"}),
    ("/api/agent-event?agent=codex", {"transcript_path": None}),
])
def test_noninteractive_hooks_create_no_state(route, patch):
    client, server = _make_client(config=_make_mock_config(codex_hud_enabled=True))
    broadcasts = []
    server.broadcast = broadcasts.append
    with client:
        response = client.post(route, json={**STOP_CLAUDE, **patch})
    assert response.status_code == 200
    assert response.json() == ({"ok": True} if "event" in route else {})
    assert broadcasts == []
    assert server._agent_interactions is None
    assert server._claude_interactions is None


def test_windows_adapter_forwards_force_pause_and_remove_failure():
    calls = []
    adapter = WindowsAgentPluginAdapter(
        reconcile_cli=lambda: True,
        enable=lambda **kwargs: calls.append(kwargs) or {"ok": True, "status": "installed"},
        disable=lambda: {"ok": False, "status": "failed"},
        pause=lambda: {"ok": True, "status": "paused"},
    )
    def request(action, generation, **kwargs):
        return AgentLifecycleRequest(str(generation), generation, AgentProvider.CLAUDE_CODE, action, **kwargs)
    adapter.apply(request(AgentLifecycleAction.RECONCILE, 1, force=True))
    adapter.apply(request(AgentLifecycleAction.RECONCILE, 2, keep_disabled=True))
    assert calls == [{"reconcile_cli": True, "force": True}, {"reconcile_cli": True, "keep_disabled": True}]
    assert adapter.apply(request(AgentLifecycleAction.DISABLE, 3)).state is AgentLifecycleState.DISABLED
    assert adapter.apply(request(AgentLifecycleAction.REMOVE, 4)).state is AgentLifecycleState.FAILED


@pytest.mark.parametrize("keep_disabled", [False, True])
def test_claude_resume_preserves_or_reenables_plugin(tmp_path, monkeypatch, keep_disabled):
    from whisperstream import claude_hud as hud
    materialized = hud.install_claude_plugin(tmp_path)
    from whisperstream.agent_plugin_runtime import mark_payload_synced
    mark_payload_synced(materialized)
    calls = []
    def run(_executable, arguments):
        calls.append(arguments)
        if arguments[0] == "marketplace":
            payload = [{"name": "whisperstream", "source": "directory", "path": materialized["marketplace_dir"]}]
        elif arguments[0] == "list":
            payload = [{"id": "whisperstream@whisperstream", "scope": "user", "version": materialized["version"], "enabled": False}]
        else:
            payload = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(hud, "_claude_cli_path", lambda: "synthetic-claude")
    result = hud.reconcile_claude_plugin(tmp_path, cli_runner=run, keep_disabled=keep_disabled)
    assert result["ok"] is True
    assert (["enable", "whisperstream@whisperstream", "--scope", "user"] in calls) is not keep_disabled


def test_disconnect_removes_cli_cache_before_settings(monkeypatch):
    from whisperstream import claude_hud as hud
    calls = []
    monkeypatch.setattr(hud, "_claude_cli_path", lambda: "synthetic-claude")
    monkeypatch.setattr(hud, "remove_claude_plugin_via_cli", lambda *_a, **_k: calls.append("cli") or {"ok": True})
    monkeypatch.setattr(hud, "disable_claude_hud", lambda: calls.append("settings") or {"ok": True})
    assert hud.disconnect_claude_plugin()["ok"] is True
    assert calls == ["cli", "settings"]


@pytest.mark.parametrize("force", [False, True])
def test_codex_force_readds_same_version(tmp_path, monkeypatch, force):
    from whisperstream import codex_plugin_lifecycle as lifecycle
    from whisperstream.agent_plugin_runtime import mark_payload_synced
    materialized = lifecycle.materialize_codex_plugin(tmp_path)
    mark_payload_synced(materialized)
    calls = []
    def run(_executable, arguments):
        calls.append(arguments)
        payload = {"marketplaces": [{"name": "whisperstream", "root": materialized["marketplace_dir"]}]} if arguments[0] == "marketplace" else {
            "installed": [{"id": "whisperstream@whisperstream", "version": materialized["version"], "enabled": True}], "available": [],
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "synthetic-codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_a: True)
    result = lifecycle.enable_codex_plugin(tmp_path, force=force)
    assert result["ok"] is True
    assert (["add", "whisperstream@whisperstream", "--json"] in calls) is force
