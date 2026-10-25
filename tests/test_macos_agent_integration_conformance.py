"""Deterministic/native Darwin conformance for D-056 Agent Speak ownership."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.agent_integration import create_agent_integration_adapters
from whisperstream.agent_integration_contract import (
    AgentFailure,
    AgentHookDisposition,
    AgentHookEvent,
    AgentHookRequest,
    AgentInteractionKind,
    AgentLifecycleAction,
    AgentLifecycleRequest,
    AgentLifecycleState,
    AgentProvider,
    AgentSessionRef,
)
from whisperstream.claude_hud import _plugin_version, install_claude_plugin
from whisperstream.macos_agent_integration import (
    MacOSAgentHookAdapter,
    MacOSAgentPluginAdapter,
)


ROOT = Path(__file__).resolve().parents[1]
FORWARDER = (
    ROOT
    / "src"
    / "whisperstream"
    / "claude_plugin"
    / "scripts"
    / "forward-hook.sh"
)
RUST_HUD_OWNER = ROOT / "src-tauri" / "src" / "macos_window_lifecycle.rs"

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine().lower() != "arm64",
    reason="native Apple Silicon conformance only",
)


class _CliState:
    def __init__(self, marketplace: Path) -> None:
        self.marketplace = marketplace
        self.marketplace_added = False
        self.installed_version: str | None = None
        self.calls: list[tuple[str, ...]] = []

    def run(self, _executable: str, arguments: list[str]):
        self.calls.append(tuple(arguments))
        if arguments == ["marketplace", "list", "--json"]:
            payload = [
                {"name": "unrelated", "source": "git", "url": "synthetic"}
            ]
            if self.marketplace_added:
                payload.append(
                    {
                        "name": "whisperstream",
                        "source": "directory",
                        "path": str(self.marketplace),
                    }
                )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if arguments[:2] == ["marketplace", "add"]:
            self.marketplace_added = True
        elif arguments == ["list", "--json"]:
            payload = []
            if self.installed_version is not None:
                payload.append(
                    {
                        "id": "whisperstream@whisperstream",
                        "scope": "user",
                        "version": self.installed_version,
                    }
                )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        elif arguments[0] in {"install", "update"}:
            self.installed_version = _plugin_version()
        elif arguments[0] == "uninstall":
            self.installed_version = None
        elif arguments == [
            "marketplace",
            "remove",
            "whisperstream",
            "--scope",
            "user",
        ]:
            self.marketplace_added = False
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")


def _lifecycle(
    action: AgentLifecycleAction, generation: int
) -> AgentLifecycleRequest:
    return AgentLifecycleRequest(
        f"plugin-{generation}",
        generation,
        AgentProvider.CLAUDE_CODE,
        action,
    )


def _hook(event: AgentHookEvent, generation: int = 1) -> AgentHookRequest:
    return AgentHookRequest(
        f"hook-{event.value}-{generation}",
        generation,
        AgentSessionRef(AgentProvider.CLAUDE_CODE, "synthetic-private-session"),
        event,
        0.25,
        json.dumps({"hook_event_name": event.value, "synthetic": True}),
    )


def test_factory_selects_darwin_owners_lazily() -> None:
    plugin, hook = create_agent_integration_adapters(
        system="Darwin", machine="arm64"
    )
    assert isinstance(plugin, MacOSAgentPluginAdapter)
    assert isinstance(hook, MacOSAgentHookAdapter)


def test_cli_only_reconcile_update_remove_preserves_unrelated_settings(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    marketplace = app_dir / "claude-plugin"
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    sentinel = {
        "enabledPlugins": {"unrelated@example": True},
        "hooks": {"Stop": [{"command": "unrelated"}]},
    }
    settings.write_text(json.dumps(sentinel), encoding="utf-8")
    cli = _CliState(marketplace)
    from whisperstream import claude_hud

    adapter = MacOSAgentPluginAdapter(app_dir=app_dir, cli_runner=cli.run)
    with (
        patch.object(claude_hud, "_claude_cli_path", return_value="synthetic-claude"),
    ):
        first = adapter.apply(_lifecycle(AgentLifecycleAction.RECONCILE, 1))
        assert first.state is AgentLifecycleState.UPDATED
        cli.installed_version = "1.3.0"
        updated = adapter.apply(_lifecycle(AgentLifecycleAction.RECONCILE, 2))
        assert updated.state is AgentLifecycleState.UPDATED
        removed = adapter.apply(_lifecycle(AgentLifecycleAction.REMOVE, 3))

    assert removed.state is AgentLifecycleState.REMOVED
    assert (
        "marketplace",
        "add",
        str(marketplace),
        "--scope",
        "user",
    ) in cli.calls
    assert ("install", "whisperstream@whisperstream", "--scope", "user") in cli.calls
    assert ("update", "whisperstream@whisperstream", "--scope", "user") in cli.calls
    assert ("uninstall", "whisperstream@whisperstream", "--scope", "user") in cli.calls
    assert (
        "marketplace",
        "remove",
        "whisperstream",
        "--scope",
        "user",
    ) in cli.calls
    assert json.loads(settings.read_text(encoding="utf-8")) == sentinel
    assert not marketplace.exists()
    assert adapter.take_payload("plugin-3", 3) is not None
    adapter.close()
    assert adapter.take_payload("plugin-3", 3) is None


def test_cli_failure_is_typed_and_never_falls_back_to_settings(tmp_path: Path) -> None:
    adapter = MacOSAgentPluginAdapter(
        app_dir=tmp_path,
        reconcile=lambda _root: {
            "ok": False,
            "status": "failed",
            "error": "claude_cli_unavailable",
        },
    )
    result = adapter.apply(_lifecycle(AgentLifecycleAction.RECONCILE, 1))
    assert result.state is AgentLifecycleState.FAILED
    assert result.failure is AgentFailure.CLI_UNAVAILABLE


def test_close_terminates_exact_owned_cli_process(tmp_path: Path) -> None:
    executable = tmp_path / "synthetic-claude"
    executable.write_text("#!/bin/zsh\n/bin/sleep 20\n", encoding="utf-8")
    executable.chmod(0o700)
    adapter = MacOSAgentPluginAdapter(app_dir=tmp_path / "app")
    output: list[object] = []
    from whisperstream import claude_hud

    with patch.object(claude_hud, "_claude_cli_path", return_value=str(executable)):
        worker = threading.Thread(
            target=lambda: output.append(
                adapter.apply(_lifecycle(AgentLifecycleAction.RECONCILE, 1))
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        while not adapter._cli_processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter._cli_processes
        process = next(iter(adapter._cli_processes))
        adapter.close()
        worker.join(2)
    assert not worker.is_alive()
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    assert output[0].state is AgentLifecycleState.FAILED


def test_darwin_plugin_tree_has_native_forwarder_and_six_documented_events(
    tmp_path: Path,
) -> None:
    installed = install_claude_plugin(tmp_path, hook_platform="darwin")
    plugin = Path(installed["plugin_dir"])
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())
    documented = {
        "Notification",
        "PermissionRequest",
        "PreToolUse",
        "Stop",
        "SubagentStop",
        "UserPromptSubmit",
    }
    assert documented <= set(hooks["hooks"])
    for event in documented:
        for registration in hooks["hooks"][event]:
            for hook in registration["hooks"]:
                assert hook["command"] == "/bin/zsh"
                assert hook["args"] == [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/forward-hook.sh"
                ]
    assert (plugin / "scripts" / "forward-hook.sh").is_file()
    assert not (plugin / "scripts" / "forward-hook.ps1").exists()


@pytest.mark.parametrize(
    ("event", "interaction"),
    [
        (AgentHookEvent.NOTIFICATION, AgentInteractionKind.ACTIVITY),
        (AgentHookEvent.PERMISSION_REQUEST, AgentInteractionKind.PERMISSION),
        (AgentHookEvent.PRE_TOOL_USE, AgentInteractionKind.QUESTION),
        (AgentHookEvent.STOP, AgentInteractionKind.RESPONSE),
        (AgentHookEvent.SUBAGENT_STOP, AgentInteractionKind.RESPONSE),
        (AgentHookEvent.USER_PROMPT_SUBMIT, AgentInteractionKind.COMPOSE),
    ],
)
def test_all_six_hook_events_are_typed(
    event: AgentHookEvent, interaction: AgentInteractionKind
) -> None:
    adapter = MacOSAgentHookAdapter(transport=lambda *_args: {})
    result = adapter.forward(_hook(event))
    assert result.disposition is AgentHookDisposition.FORWARDED
    assert result.interaction is interaction
    assert result.response_json == "{}"


def test_notification_cannot_gain_decision_authority() -> None:
    adapter = MacOSAgentHookAdapter(
        transport=lambda *_args: {"decision": "allow"}
    )
    result = adapter.forward(_hook(AgentHookEvent.NOTIFICATION))
    assert result.disposition is AgentHookDisposition.FORWARDED
    assert result.response_json == "{}"


@pytest.mark.parametrize(
    ("transport", "failure"),
    [
        (None, AgentFailure.ROUTE_UNAVAILABLE),
        (MagicMock(side_effect=TimeoutError()), AgentFailure.TIMEOUT),
        (lambda *_args: [], AgentFailure.INVALID_EVENT),
        (lambda *_args: "not-json", AgentFailure.INVALID_EVENT),
    ],
)
def test_unavailable_timeout_and_malformed_transport_fail_open_exactly(
    transport, failure: AgentFailure
) -> None:
    result = MacOSAgentHookAdapter(transport=transport).forward(
        _hook(AgentHookEvent.PERMISSION_REQUEST)
    )
    assert result.disposition is AgentHookDisposition.FAILED_OPEN
    assert result.failure is failure
    assert result.response_json == "{}"


def test_cancel_provider_exit_and_close_release_generations() -> None:
    entered = threading.Event()
    release = threading.Event()

    def transport(*_args):
        entered.set()
        release.wait(1)
        return {"decision": "allow"}

    adapter = MacOSAgentHookAdapter(transport=transport)
    output: list[object] = []
    request = _hook(AgentHookEvent.PERMISSION_REQUEST)
    worker = threading.Thread(target=lambda: output.append(adapter.forward(request)))
    worker.start()
    assert entered.wait(1)
    adapter.provider_exited()
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert output[0].disposition is AgentHookDisposition.EXPIRED
    adapter.close()
    closed = adapter.forward(_hook(AgentHookEvent.STOP, 2))
    assert closed.disposition is AgentHookDisposition.FAILED_OPEN
    assert closed.response_json == "{}"


class _JsonHandler(BaseHTTPRequestHandler):
    response = b'{"hookSpecificOutput":{"hookEventName":"Stop"}}'

    def do_GET(self) -> None:
        assert self.headers.get("x-whisperstream-hook-token") == "a" * 64
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        assert self.headers.get("x-whisperstream-hook-token") == "a" * 64
        length = int(self.headers["Content-Length"])
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(self.response)))
        self.end_headers()
        self.wfile.write(self.response)

    def log_message(self, *_args) -> None:
        return None


def test_native_forwarder_success_fail_open_and_exact_temp_cleanup(
    tmp_path: Path,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "claude-hud-port.json").write_text(
        json.dumps({"port": server.server_port, "token": "a" * 64}), encoding="utf-8"
    )
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    forwarder = plugin / "scripts" / "forward-hook.sh"
    forwarder.write_bytes(FORWARDER.read_bytes())
    (plugin / "runtime-target.json").write_text(json.dumps({"data_dir": str(data_dir)}))
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(tmp_path),
        "WHISPERSTREAM_DATA_DIR": str(data_dir),
    }
    try:
        success = subprocess.run(
            ["/bin/zsh", str(forwarder)],
            input='{"hook_event_name":"Stop","synthetic":true}',
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
    malformed = subprocess.run(
        ["/bin/zsh", str(forwarder)],
        input="[]",
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=3,
    )
    assert success.returncode == 0
    assert success.stdout.strip() == _JsonHandler.response.decode()
    assert malformed.returncode == 0
    assert malformed.stdout == "{}\n"
    assert list(tmp_path.glob("whisperstream-claude-hook.*")) == []


def test_mac_native_hud_owner_focuses_and_releases_without_new_state_machine() -> None:
    source = RUST_HUD_OWNER.read_text(encoding="utf-8")
    parent = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "src" / "pages" / "HUD.tsx").read_text(
        encoding="utf-8"
    )
    reconciler = (
        ROOT / "frontend" / "src" / "pages" / "hud" / "useHudNativeReconciler.ts"
    ).read_text(encoding="utf-8")
    assert "set_agent_hud_focusable" in source
    assert "NSWorkspace::sharedWorkspace" in source
    assert "NSRunningApplication::runningApplicationWithProcessIdentifier" in source
    assert "macos_window_lifecycle::set_agent_hud_focusable" in parent
    assert 'invoke("set_hud_window_focusable"' in frontend
    # HUD visibility stays on the shared window-lifecycle contract, now owned
    # by the reconciler HUD.tsx delegates to rather than a second path.
    assert "useHudNativeReconciler({" in frontend
    assert 'applyWindowLifecycle("hud", action)' in reconciler
    assert "hudWindow.show()" not in frontend


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("verified", [True, False])
def test_native_hook_verifies_target_before_body_or_managed_credential(tmp_path, provider, verified):
    seen = []
    payload = '{"hook_event_name":"Stop","synthetic":"café"}'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("x-whisperstream-managed-session-token"), b""))
            body = json.dumps({"ok": verified}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("POST", self.path, self.headers.get("x-whisperstream-managed-session-token"), body))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *_args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    data = tmp_path / "data"
    data.mkdir()
    (data / f"{provider}-hud-port.json").write_text(json.dumps({"port": server.server_port, "token": "a" * 64}))
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    script = plugin / "scripts/forward-hook.sh"
    script.write_bytes(FORWARDER.read_bytes())
    (plugin / "runtime-target.json").write_text(json.dumps({"data_dir": str(data)}))
    try:
        result = subprocess.run(["/bin/zsh", str(script), provider], input=payload, text=True, capture_output=True, timeout=5, env={
            "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "WHISPERSTREAM_DATA_DIR": str(tmp_path / "wrong-inherited-channel"),
            "WHISPERSTREAM_SESSION_TOKEN": "synthetic-managed-token",
        })
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
    assert result.returncode == 0 and result.stdout == "{}\n"
    assert seen[0] == ("GET", "/api/agent-hook/verify", None, b"")
    assert len(seen) == (2 if verified else 1)
    if verified:
        assert seen[1] == ("POST", f"/api/{provider}-hook", "synthetic-managed-token", payload.encode())
    assert not list(tmp_path.glob("whisperstream-claude-hook.*"))
