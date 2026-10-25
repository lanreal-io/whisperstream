#!/usr/bin/env python3
"""Prove disposable Claude integration and TTS owners clean up exactly."""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from whisperstream.agent_integration_contract import (
    AgentHookDisposition,
    AgentHookEvent,
    AgentHookRequest,
    AgentLifecycleAction,
    AgentLifecycleRequest,
    AgentLifecycleState,
    AgentProvider,
    AgentSessionRef,
)
from whisperstream.windows_agent_integration import (
    WindowsAgentHookAdapter,
    WindowsAgentPluginAdapter,
)


class _CliResult:
    def __init__(self, payload: object) -> None:
        self.returncode = 0
        self.stdout = json.dumps(payload)
        self.stderr = ""


def main() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        print(json.dumps({"status": "invalid-host"}, sort_keys=True))
        return 2

    with tempfile.TemporaryDirectory(prefix="whisperstream-unit09-") as raw_root:
        root = Path(raw_root)
        app_dir = root / "app-data"
        settings_path = root / "claude" / "settings.json"
        installed = False

        def fake_cli(_executable: str, arguments: list[str]) -> _CliResult:
            nonlocal installed
            if arguments[:2] == ["marketplace", "list"]:
                return _CliResult([])
            if arguments[:2] == ["marketplace", "add"]:
                return _CliResult({})
            if arguments[:2] == ["list", "--json"]:
                return _CliResult(
                    [{"id": "whisperstream@whisperstream", "version": "1.0.0"}]
                    if installed
                    else []
                )
            if arguments and arguments[0] in {"install", "update"}:
                installed = True
                return _CliResult({})
            return _CliResult({})

        from whisperstream import claude_hud
        from whisperstream import config_manager

        def enable(**kwargs: object):
            return claude_hud.enable_claude_hud(**kwargs)

        def disable():
            return claude_hud.disable_claude_hud()

        plugin = WindowsAgentPluginAdapter(enable=enable, disable=disable)
        with (
            patch.object(config_manager, "get_app_dir", return_value=app_dir),
            patch.object(claude_hud, "_claude_settings_path", return_value=settings_path),
            patch.object(claude_hud, "_claude_cli_path", return_value="claude.exe"),
            patch.object(claude_hud, "_run_claude_plugin_cli", side_effect=fake_cli),
        ):
            reconciled = plugin.apply(
                AgentLifecycleRequest(
                    "plugin-reconcile",
                    1,
                    AgentProvider.CLAUDE_CODE,
                    AgentLifecycleAction.RECONCILE,
                )
            )
            removed = plugin.apply(
                AgentLifecycleRequest(
                    "plugin-remove",
                    2,
                    AgentProvider.CLAUDE_CODE,
                    AgentLifecycleAction.REMOVE,
                )
            )
        plugin.close()

        entered = threading.Event()
        release = threading.Event()

        def transport(_body: str, _event: AgentHookEvent, _deadline: float):
            entered.set()
            release.wait(2.0)
            return {}

        hook = WindowsAgentHookAdapter(transport=transport)
        request = AgentHookRequest(
            "hook-forward",
            1,
            AgentSessionRef(AgentProvider.CLAUDE_CODE, "synthetic-session"),
            AgentHookEvent.STOP,
            1.0,
            json.dumps({"hook_event_name": "Stop", "synthetic": True}),
        )
        results: list[object] = []
        worker = threading.Thread(
            target=lambda: results.append(hook.forward(request)),
            name="unit09-hook-forward",
            daemon=True,
        )
        worker.start()
        if not entered.wait(2.0):
            return 1
        cancelled = hook.cancel(request.request_id, request.generation)
        release.set()
        worker.join(2.0)
        hook.close()

        plugin_dir = app_dir / "claude-plugin" / "plugins" / "whisperstream"
        checks = {
            "plugin_reconciled": reconciled.state
            in {AgentLifecycleState.CURRENT, AgentLifecycleState.UPDATED},
            "plugin_removed": removed.state is AgentLifecycleState.REMOVED,
            "owned_plugin_absent": not plugin_dir.exists(),
            "hook_cancelled": cancelled.disposition is AgentHookDisposition.EXPIRED,
            "hook_worker_exited": not worker.is_alive(),
            "hook_expired": bool(results)
            and results[0].disposition is AgentHookDisposition.EXPIRED,
            "plugin_payload_scrubbed": plugin.payload_for("plugin-remove", 2) is None,
        }
        if not all(checks.values()):
            print(json.dumps({"status": "failed", "checks": checks}, sort_keys=True))
            return 1
        print(json.dumps({"status": "pass", "checks": checks}, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
