"""Focused contracts for Whisperstream's owned Codex plugin lifecycle."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


REPO_ROOT = Path(__file__).parents[1]
PLUGIN_TEMPLATE = REPO_ROOT / "src" / "whisperstream" / "codex_plugin"
FORWARDER = PLUGIN_TEMPLATE / "scripts" / "forward-hook.ps1"
WINDOWS_WRAPPER = PLUGIN_TEMPLATE / "scripts" / "forward-hook.cmd"
WINDOWS_LAUNCHER_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "$ProgressPreference='SilentlyContinue'; "
    "$WarningPreference='SilentlyContinue'; "
    "$InformationPreference='SilentlyContinue'; "
    "& (Join-Path $env:PLUGIN_ROOT 'scripts\\forward-hook.ps1') 2>$null; "
    "if ($LASTEXITCODE -ne 0) { [Console]::Out.WriteLine('{}') }; exit 0"
)
WINDOWS_LAUNCHER_COMMAND = (
    "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
    "-EncodedCommand "
    + base64.b64encode(WINDOWS_LAUNCHER_SCRIPT.encode("utf-16le")).decode("ascii")
)


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No test in this module may inspect or mutate the user's real Codex state."""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    yield codex_home
    assert os.environ["CODEX_HOME"] == str(codex_home)


def _completed(payload: object, *, returncode: int = 0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload) if not isinstance(payload, str) else payload,
        stderr="" if returncode == 0 else "unsupported",
    )


def _marketplace(path: Path) -> dict:
    return {
        "name": "whisperstream",
        "source": {"source": "local", "path": str(path)},
    }


def _installed(version: str, *, enabled: bool = True) -> dict:
    return {
        "name": "whisperstream",
        "marketplace": "whisperstream",
        "version": version,
        "enabled": enabled,
    }


def _real_installed_shape(version: str) -> dict:
    """Codex CLI 0.147.0's exact ``plugin list --json`` record."""

    return {
        "pluginId": "whisperstream@whisperstream",
        "name": "whisperstream",
        "marketplaceName": "whisperstream",
        "version": version,
        "installed": True,
        "enabled": True,
    }


def test_payload_has_codex_manifest_skill_and_four_hooks():
    manifest = json.loads(
        (PLUGIN_TEMPLATE / ".codex-plugin" / "plugin.json").read_text("utf-8")
    )
    assert manifest["name"] == "whisperstream"
    assert manifest["version"] == "1.1.1"
    assert manifest["skills"] == "./skills/"
    assert manifest["hooks"] == "./hooks/hooks.json"

    hooks = json.loads(
        (PLUGIN_TEMPLATE / "hooks" / "hooks.json").read_text("utf-8")
    )["hooks"]
    assert set(hooks) == {
        "SessionStart",
        "Stop",
        "PermissionRequest",
        "UserPromptSubmit",
    }
    for entries in hooks.values():
        handler = entries[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == '/bin/zsh "${PLUGIN_ROOT}/scripts/forward-hook.sh" codex'
        assert "commandWindows" in handler
        windows_command = handler["commandWindows"]
        assert windows_command == WINDOWS_LAUNCHER_COMMAND

    skill = (PLUGIN_TEMPLATE / "skills" / "speak" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "name: speak" in skill
    assert "Codex session" in skill
    assert "$speak" not in skill


def test_forwarder_is_utf8_bounded_fail_open_and_content_silent():
    script = (PLUGIN_TEMPLATE / "scripts" / "forward-hook.ps1").read_text("utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "[Console]::In.ReadToEnd()" not in code
    assert "[Console]::OpenStandardInput()" in code
    assert "System.Text.UTF8Encoding" in code
    assert script.count("$Reader.ReadToEnd()") == 1
    assert "$RequestTimeoutSeconds = 125" in script
    assert "$MaxRequestCharacters = 1048576" in script
    assert "$OutputJson = '{}'" in script
    assert "/api/codex-hook" in script
    assert "Write-Host" not in script
    assert "Write-Warning" not in script

    wrapper = WINDOWS_WRAPPER.read_text("utf-8")
    assert '"%~dp0forward-hook.ps1"' in wrapper
    assert "2>nul" in wrapper
    assert "if errorlevel 1 echo {}" in wrapper
    assert "exit /b 0" in wrapper


class _ForwarderHandler(BaseHTTPRequestHandler):
    response_body = b"{}"
    seen_body = b""
    seen_path = ""
    seen_session_token = None

    def do_GET(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).seen_path = self.path
        type(self).seen_session_token = self.headers.get(
            "x-whisperstream-managed-session-token"
        )
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen_body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _run_forwarder(
    data_dir: Path,
    body: str,
    *,
    session_token: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin
    script = Path(materialize_codex_plugin(data_dir)["plugin_dir"]) / "scripts/forward-hook.ps1"
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for forwarder tests")
    environment = os.environ.copy()
    environment["WHISPERSTREAM_DATA_DIR"] = str(data_dir)
    if session_token is None:
        environment.pop("WHISPERSTREAM_SESSION_TOKEN", None)
    else:
        environment["WHISPERSTREAM_SESSION_TOKEN"] = session_token
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
        env=environment,
    )


def _run_windows_wrapper(
    plugin_root: Path, data_dir: Path, body: str, *, outer_shell: str
) -> subprocess.CompletedProcess[bytes]:
    command_prompt = shutil.which("cmd.exe")
    powershell = shutil.which("powershell.exe")
    if command_prompt is None:
        pytest.skip("Windows Command Prompt is required for wrapper tests")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for wrapper tests")
    environment = os.environ.copy()
    environment["PLUGIN_ROOT"] = str(plugin_root)
    environment["WHISPERSTREAM_DATA_DIR"] = str(data_dir)
    manifest_command = WINDOWS_LAUNCHER_COMMAND
    command_line = (
        subprocess.list2cmdline([command_prompt, "/D", "/S", "/C"])
        + f' "{manifest_command}"'
        if outer_shell == "cmd"
        else [powershell, "-NoProfile", "-NonInteractive", "-Command", manifest_command]
    )
    return subprocess.run(
        command_line,
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
        env=environment,
    )


def test_forwarder_delivers_unicode_byte_exact_and_emits_one_json_object(
    tmp_path: Path,
):
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "🔊 Résumé complete. 東京 ✅",
        },
        ensure_ascii=False,
    )
    response = {"decision": "block", "reason": "mañana"}
    _ForwarderHandler.response_body = json.dumps(
        response, ensure_ascii=False
    ).encode("utf-8")
    _ForwarderHandler.seen_body = b""
    _ForwarderHandler.seen_path = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForwarderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "codex-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}), encoding="utf-8"
        )
        result = _run_forwarder(tmp_path, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert _ForwarderHandler.seen_path == "/api/codex-hook"
    assert _ForwarderHandler.seen_body == payload.encode("utf-8")
    assert result.stdout.strip().isascii()
    assert json.loads(result.stdout) == response
    assert len(result.stdout.strip().splitlines()) == 1


@pytest.mark.parametrize(
    ("session_token", "expected"),
    [("private-wrapper-token", "private-wrapper-token"), (None, None)],
)
def test_forwarder_sends_the_optional_correlation_header(
    tmp_path: Path,
    session_token: str | None,
    expected: str | None,
):
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "sid"})
    _ForwarderHandler.response_body = b"{}"
    _ForwarderHandler.seen_session_token = "not-reset"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForwarderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "codex-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}), encoding="utf-8"
        )
        result = _run_forwarder(
            tmp_path,
            payload,
            session_token=session_token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert _ForwarderHandler.seen_session_token == expected


@pytest.mark.parametrize("outer_shell", ["cmd", "powershell"])
def test_windows_wrapper_delivers_from_plugin_root_with_spaces(
    tmp_path: Path, outer_shell: str
):
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin

    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "🔊 Wrapper path complete. 東京 ✅",
        },
        ensure_ascii=False,
    )
    response = {"decision": "block", "reason": "continue"}
    _ForwarderHandler.response_body = json.dumps(response).encode("utf-8")
    _ForwarderHandler.seen_body = b""
    _ForwarderHandler.seen_path = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForwarderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data_dir = tmp_path / "data dir"
        data_dir.mkdir()
        (data_dir / "codex-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}), encoding="utf-8"
        )
        materialized = materialize_codex_plugin(data_dir)
        result = _run_windows_wrapper(
            Path(materialized["plugin_dir"]),
            data_dir,
            payload,
            outer_shell=outer_shell,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert _ForwarderHandler.seen_path == "/api/codex-hook"
    assert _ForwarderHandler.seen_body == payload.encode("utf-8")
    assert json.loads(result.stdout) == response


@pytest.mark.parametrize("body", ["", "not-json"])
def test_forwarder_fails_open_without_backend(tmp_path: Path, body: str):
    result = _run_forwarder(tmp_path, body)

    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.strip() == b"{}"


def test_materialize_is_idempotent_updates_owned_files_and_preserves_unrelated(
    tmp_path: Path,
):
    from whisperstream.codex_plugin_lifecycle import materialize_codex_plugin

    app_dir = tmp_path / "app-data"
    first = materialize_codex_plugin(app_dir)
    plugin_dir = Path(first["plugin_dir"])
    from whisperstream.codex_plugin_lifecycle import _template_bytes
    relative = Path("scripts") / ("forward-hook.sh" if __import__("sys").platform == "darwin" else "forward-hook.cmd")
    wrapper = plugin_dir / relative
    unrelated = plugin_dir / "user-note.txt"
    unrelated.write_text("keep", encoding="utf-8")
    (plugin_dir / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    wrapper.write_text("broken", encoding="utf-8")

    second = materialize_codex_plugin(app_dir)
    third = materialize_codex_plugin(app_dir)

    assert first["status"] == "installed"
    assert second["status"] == "updated"
    assert third["status"] == "current"
    assert wrapper.read_bytes() == _template_bytes(relative)
    assert unrelated.read_text("utf-8") == "keep"


def test_materialize_refuses_same_named_unowned_marketplace(tmp_path: Path):
    from whisperstream.codex_plugin_lifecycle import (
        CodexPluginConflictError,
        materialize_codex_plugin,
    )

    app_dir = tmp_path / "app-data"
    conflict = (
        app_dir
        / "codex-plugin-marketplace"
        / "plugins"
        / "whisperstream"
        / ".codex-plugin"
        / "plugin.json"
    )
    conflict.parent.mkdir(parents=True)
    conflict.write_text('{"name":"someone-else"}', encoding="utf-8")

    with pytest.raises(CodexPluginConflictError):
        materialize_codex_plugin(app_dir)
    assert json.loads(conflict.read_text("utf-8"))["name"] == "someone-else"


def test_status_reports_cli_absent_without_materializing(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: None)
    app_dir = tmp_path / "app-data"

    result = lifecycle.codex_plugin_status(app_dir)

    assert result == {
        "ok": False,
        "status": "failed",
        "codex_cli_available": False,
        "codex_cli_supported": False,
        "trust_required": False,
        "error": "codex_cli_unavailable",
    }
    assert not app_dir.exists()


def test_status_distinguishes_unsupported_plugin_cli(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(
        lifecycle,
        "_run_codex_plugin_cli",
        lambda *_args, **_kwargs: _completed("", returncode=2),
    )

    result = lifecycle.codex_plugin_status(tmp_path / "app-data")

    assert result["ok"] is False
    assert result["codex_cli_available"] is True
    assert result["codex_cli_supported"] is False
    assert result["error"] == "codex_cli_unsupported"


def test_enable_installs_then_reports_trust_required(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    calls: list[list[str]] = []
    marketplace_dir = tmp_path / "app-data" / "codex-plugin-marketplace"

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        if arguments == ["marketplace", "list", "--json"]:
            return _completed({"marketplaces": []})
        if arguments == ["list", "--json", "--marketplace", "whisperstream"]:
            return _completed({"installed": [], "available": []})
        return _completed({"ok": True})

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_args: False)

    result = lifecycle.enable_codex_plugin(tmp_path / "app-data")

    assert result["ok"] is True
    assert result["status"] == "installed"
    assert result["trust_required"] is True
    assert result["trust_message"] == (
        "Trust Whisperstream in Codex. In the app, open Settings > Hooks > "
        "Whisperstream, then Trust all. In the CLI, run /hooks."
    )
    assert calls == [
        ["marketplace", "list", "--json"],
        ["marketplace", "add", str(marketplace_dir), "--json"],
        ["list", "--json", "--marketplace", "whisperstream"],
        ["add", "whisperstream@whisperstream", "--json"],
    ]


def test_status_current_and_trusted_is_connected_without_mutation(
    tmp_path: Path, monkeypatch
):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    app_dir = tmp_path / "app-data"
    materialized = lifecycle.materialize_codex_plugin(app_dir)
    marketplace_dir = Path(materialized["marketplace_dir"])
    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        if arguments[0] == "marketplace":
            return _completed({"marketplaces": [_marketplace(marketplace_dir)]})
        return _completed(
            {"installed": [_installed(materialized["version"])], "available": []}
        )

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_args: True)

    result = lifecycle.codex_plugin_status(app_dir)

    assert result["ok"] is True
    assert result["status"] == "current"
    assert result["trust_required"] is False
    assert calls == [["marketplace", "list", "--json"], ["list", "--json", "--marketplace", "whisperstream"]]


def test_current_cli_plugin_record_shape_is_recognized(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    app_dir = tmp_path / "app-data"
    materialized = lifecycle.materialize_codex_plugin(app_dir)
    marketplace_dir = Path(materialized["marketplace_dir"])

    def run(_executable: str, arguments: list[str]):
        if arguments[0] == "marketplace":
            return _completed(
                {"marketplaces": [{"name": "whisperstream", "root": str(marketplace_dir)}]}
            )
        return _completed(
            {
                "installed": [_real_installed_shape(materialized["version"])],
                "available": [],
            }
        )

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_args: True)

    assert lifecycle.codex_plugin_status(app_dir)["status"] == "current"


def test_hooks_list_requires_every_enabled_plugin_hook_to_be_trusted():
    from whisperstream.codex_plugin_lifecycle import _hooks_list_reports_trust

    trusted = {
        "data": [
            {
                "hooks": [
                    {
                        "pluginId": "whisperstream@whisperstream",
                        "enabled": True,
                        "trustStatus": "trusted",
                    },
                    {
                        "pluginId": "whisperstream@whisperstream",
                        "enabled": True,
                        "trustStatus": "managed",
                    },
                    {
                        "pluginId": "someone-else@marketplace",
                        "enabled": True,
                        "trustStatus": "untrusted",
                    },
                ]
            }
        ]
    }
    assert _hooks_list_reports_trust(trusted) is True

    trusted["data"][0]["hooks"][0]["trustStatus"] = "modified"
    assert _hooks_list_reports_trust(trusted) is False


def test_trust_probe_keeps_stdin_open_until_async_hooks_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    trusted_result = {
        "data": [
            {
                "hooks": [
                    {
                        "pluginId": "whisperstream@whisperstream",
                        "enabled": True,
                        "trustStatus": "trusted",
                    }
                ]
            }
        ]
    }

    class FakeStdin:
        def __init__(self) -> None:
            self.closed = False
            self.text = ""

        def write(self, value: str) -> None:
            self.text += value

        def flush(self) -> None:
            return

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = self
            self.returncode: int | None = None

        def __iter__(self):
            assert not self.stdin.closed
            yield json.dumps({"id": 0, "result": {}}) + "\n"
            assert not self.stdin.closed
            yield json.dumps({"id": 1, "result": trusted_result}) + "\n"

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    fake_process = FakeProcess()
    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )

    assert lifecycle._codex_hooks_trusted(
        tmp_path / "codex-home", tmp_path / "plugin", None
    ) is True
    assert '"method":"hooks/list"' in fake_process.stdin.text
    assert fake_process.stdin.closed is True


def test_enable_updates_1_0_0_plugin_without_touching_unrelated(
    tmp_path: Path, monkeypatch
):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    app_dir = tmp_path / "app-data"
    materialized = lifecycle.materialize_codex_plugin(app_dir)
    marketplace_dir = Path(materialized["marketplace_dir"])
    unrelated = {
        "name": "another-plugin",
        "marketplace": "another-marketplace",
        "version": "9.0.0",
        "enabled": True,
    }
    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        if arguments == ["marketplace", "list", "--json"]:
            return _completed(
                {
                    "marketplaces": [
                        _marketplace(marketplace_dir),
                        {"name": "another-marketplace", "source": "remote"},
                    ]
                }
            )
        if arguments == ["list", "--json", "--marketplace", "whisperstream"]:
            return _completed(
                {"installed": [_installed("1.0.0"), unrelated], "available": []}
            )
        return _completed({"ok": True})

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_args: False)

    result = lifecycle.enable_codex_plugin(app_dir)

    assert result["status"] == "updated"
    assert result["version"] == "1.1.1"
    assert result["trust_required"] is True
    assert ["add", "whisperstream@whisperstream", "--json"] in calls
    assert all("another-plugin" not in call for call in calls)
    assert all("another-marketplace" not in call for call in calls)


def test_enable_rejects_registered_marketplace_owned_by_someone_else(
    tmp_path: Path, monkeypatch
):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    def run(_executable: str, arguments: list[str]):
        assert arguments == ["marketplace", "list", "--json"]
        return _completed(
            {
                "marketplaces": [
                    _marketplace(tmp_path / "someone-elses-marketplace")
                ]
            }
        )

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)

    result = lifecycle.enable_codex_plugin(tmp_path / "app-data")

    assert result["ok"] is False
    assert result["error"] == "marketplace_conflict"


def test_disable_removes_only_owned_registration_and_files(tmp_path: Path, monkeypatch):
    import whisperstream.codex_plugin_lifecycle as lifecycle

    app_dir = tmp_path / "app-data"
    materialized = lifecycle.materialize_codex_plugin(app_dir)
    marketplace_dir = Path(materialized["marketplace_dir"])
    plugin_dir = Path(materialized["plugin_dir"])
    unrelated_file = plugin_dir / "keep.txt"
    unrelated_file.write_text("keep", encoding="utf-8")
    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        if arguments == ["marketplace", "list", "--json"]:
            return _completed(
                {
                    "marketplaces": [
                        _marketplace(marketplace_dir),
                        {"name": "another-marketplace", "source": "remote"},
                    ]
                }
            )
        if arguments == ["list", "--json", "--marketplace", "whisperstream"]:
            return _completed(
                {
                    "installed": [
                        _installed(materialized["version"]),
                        {
                            "name": "another-plugin",
                            "marketplace": "another-marketplace",
                            "version": "9.0.0",
                        },
                    ],
                    "available": [],
                }
            )
        return _completed({"ok": True})

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)

    result = lifecycle.disable_codex_plugin(app_dir)

    assert result["ok"] is True
    assert result["status"] == "removed"
    assert calls[-2:] == [
        ["remove", "whisperstream@whisperstream", "--json"],
        ["marketplace", "remove", "whisperstream", "--json"],
    ]
    assert unrelated_file.read_text("utf-8") == "keep"
    assert all("another-plugin" not in call for call in calls)
    assert all("another-marketplace" not in call for call in calls)


def test_build_and_wheel_configs_include_codex_payload():
    build_text = (REPO_ROOT / "scripts" / "build.py").read_text("utf-8")
    assert (
        "--include-data-dir=src/whisperstream/codex_plugin="
        "whisperstream/codex_plugin"
    ) in build_text

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
    for path in (
        ".codex-plugin/plugin.json",
        "skills/speak/SKILL.md",
        "hooks/hooks.json",
        "scripts/forward-hook.cmd",
        "scripts/forward-hook.ps1",
    ):
        assert path in pyproject_text


@pytest.mark.parametrize(("force", "expect_add"), [(False, False), (True, True)])
def test_enable_force_re_adds_an_identical_version(tmp_path: Path, monkeypatch, force, expect_add):
    import whisperstream.codex_plugin_lifecycle as lifecycle
    from whisperstream.agent_plugin_runtime import mark_payload_synced

    app_dir = tmp_path / "app-data"
    materialized = lifecycle.materialize_codex_plugin(app_dir)
    mark_payload_synced(materialized)
    marketplace_dir = Path(materialized["marketplace_dir"])
    calls: list[list[str]] = []

    def run(_executable: str, arguments: list[str]):
        calls.append(arguments)
        if arguments[0] == "marketplace":
            return _completed(
                {"marketplaces": [{"name": "whisperstream", "root": str(marketplace_dir)}]}
            )
        if arguments == ["list", "--json", "--marketplace", "whisperstream"]:
            return _completed(
                {"installed": [_real_installed_shape(materialized["version"])], "available": []}
            )
        return _completed({"ok": True})

    monkeypatch.setattr(lifecycle, "_codex_cli_path", lambda: "codex")
    monkeypatch.setattr(lifecycle, "_run_codex_plugin_cli", run)
    monkeypatch.setattr(lifecycle, "_codex_hooks_trusted", lambda *_args: True)

    result = lifecycle.enable_codex_plugin(app_dir, force=force)

    assert result["ok"] is True
    assert (["add", "whisperstream@whisperstream", "--json"] in calls) is expect_add
