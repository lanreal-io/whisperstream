"""Contract tests for the packaged Claude plugin and blocking forwarder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


PLUGIN_TEMPLATE = Path(__file__).parents[1] / "src" / "whisperstream" / "claude_plugin"
FORWARDER = PLUGIN_TEMPLATE / "scripts" / "forward-hook.ps1"
# Accommodate Windows hosted-runner startup and scheduling variance while still
# detecting hangs far before the production hook's 130-second deadline.
FORWARDER_SUBPROCESS_TIMEOUT_SECONDS = 30
SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS = 0.01


def _hook_manifest() -> dict:
    return json.loads((PLUGIN_TEMPLATE / "hooks" / "hooks.json").read_text("utf-8"))


def _inner_hook(event: str) -> dict:
    entries = _hook_manifest()["hooks"][event]
    assert len(entries) == 1
    hooks = entries[0]["hooks"]
    assert len(hooks) == 1
    return hooks[0]


class TestPluginManifest:
    def test_required_hooks_use_one_portable_forwarder(self):
        manifest = _hook_manifest()
        assert set(manifest["hooks"]) == {
            "UserPromptExpansion",
            "UserPromptSubmit",
            "Stop",
            "PreToolUse",
            "PermissionRequest",
            "Notification",
            "SubagentStop",
            "SessionStart",
            "SessionEnd",
        }
        assert manifest["hooks"]["PreToolUse"][0]["matcher"] == "AskUserQuestion"

        for event in manifest["hooks"]:
            inner = _inner_hook(event)
            assert inner["type"] == "command"
            assert inner["command"] == "powershell"
            assert "${CLAUDE_PLUGIN_ROOT}/scripts/forward-hook.ps1" in inner["args"]
            assert inner["timeout"] == 130

    def test_forwarder_never_decodes_stdin_with_the_console_codepage(self):
        # [Console]::In decodes with [Console]::InputEncoding, the console's OEM
        # codepage, NOT UTF-8. Claude writes UTF-8, so that read shreds every
        # multi-byte character before the script sees it (IBM437 turned U+1F50A
        # into four chars and U+2014 into three). Never reintroduce it.
        script = FORWARDER.read_text("utf-8")
        # The forwarder documents WHY that read is wrong, so only executable
        # lines are checked; a comment naming it is the intended state.
        code = "\n".join(
            line
            for line in script.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "[Console]::In.ReadToEnd()" not in code
        assert "[Console]::OpenStandardInput()" in code
        assert "System.Text.UTF8Encoding" in code

    def test_skill_is_manual_and_human_readable(self):
        skill = (PLUGIN_TEMPLATE / "skills" / "speak" / "SKILL.md").read_text("utf-8")
        assert "name: speak" in skill
        assert "disable-model-invocation: true" in skill
        assert "🔊 " in skill
        assert "whisperstream:speak" not in skill

    def test_forwarder_has_bounded_layers_and_reads_stdin_once(self):
        script = FORWARDER.read_text("utf-8")
        assert script.count("Read-Utf8StandardInput") == 2  # definition + call
        assert script.count("$Reader.ReadToEnd()") == 1
        assert "$RequestTimeoutSeconds = 125" in script
        assert "$MaxRequestCharacters = 1048576" in script
        assert "$MaxResponseCharacters = 65536" in script
        assert "/api/claude-hook" in script


class _JsonHandler(BaseHTTPRequestHandler):
    response_body = b"{}"
    status_code = 200
    seen_path = ""
    seen_body = b""
    seen_headers: dict = {}

    def do_GET(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen_path = self.path
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen_body = self.rfile.read(length)
        type(self).seen_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def powershell() -> str:
    executable = shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is required for forwarder subprocess tests")
    return executable


def _run_forwarder(
    powershell: str,
    data_dir: Path,
    body: str,
    *,
    timeout: float = FORWARDER_SUBPROCESS_TIMEOUT_SECONDS,
    extra_env: "dict[str, str] | None" = None,
) -> subprocess.CompletedProcess[bytes]:
    from whisperstream.claude_hud import install_claude_plugin
    script = Path(install_claude_plugin(data_dir)["plugin_dir"]) / "scripts/forward-hook.ps1"
    env = os.environ.copy()
    env["WHISPERSTREAM_DATA_DIR"] = str(data_dir)
    # The forwarder must never see a stale token from the calling shell.
    env.pop("WHISPERSTREAM_SESSION_TOKEN", None)
    # Nor the calling agent's own attendance marker.
    env.pop("CLAUDE_CODE_SESSION_ATTENDED", None)
    if extra_env:
        env.update(extra_env)
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
        timeout=timeout,
        check=False,
        env=env,
    )


def test_forwarder_skips_unattended_sessions_without_contacting_backend(
    powershell: str, tmp_path: Path
):
    _JsonHandler.response_body = b'{"decision":"block","reason":"x"}'
    _JsonHandler.status_code = 200
    _JsonHandler.seen_path = ""
    _JsonHandler.seen_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(
            powershell,
            tmp_path,
            '{"hook_event_name":"Stop","session_id":"private"}',
            extra_env={"CLAUDE_CODE_SESSION_ATTENDED": "0"},
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0
    assert result.stdout.strip() == b"{}"
    assert _JsonHandler.seen_path == ""
    assert _JsonHandler.seen_body == b""


def test_forwarder_round_trips_valid_object_json_only(
    powershell: str, tmp_path: Path
):
    response = {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "Continue from the Whisperstream HUD",
        }
    }
    _JsonHandler.response_body = json.dumps(response).encode()
    _JsonHandler.status_code = 200
    _JsonHandler.seen_path = ""
    _JsonHandler.seen_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        payload = '{"hook_event_name":"Stop","session_id":"private"}'
        result = _run_forwarder(powershell, tmp_path, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == response
    assert _JsonHandler.seen_path == "/api/claude-hook"
    assert _JsonHandler.seen_body.decode() == payload
    assert len(result.stdout.strip().splitlines()) == 1


def test_forwarder_delivers_inbound_unicode_to_the_backend_byte_exact(
    powershell: str, tmp_path: Path
):
    """The INBOUND half: what Claude wrote must reach the backend unchanged.

    Regression test for the OEM-codepage stdin decode. The pre-existing
    round-trip test asserted `seen_body` too, but its payload was pure ASCII,
    so a decoder that mangles only multi-byte characters passed it happily.
    Every character below is multi-byte in UTF-8.
    """
    payload_obj = {
        "hook_event_name": "Stop",
        "session_id": "private",
        "response": (
            "🔊 Done — the em dash, curly quotes “like these”, "
            "Résumé, mañana, Crème brûlée, 東京, and ✅ all survive."
        ),
    }
    payload = json.dumps(payload_obj, ensure_ascii=False)
    assert not payload.isascii(), "payload must exercise multi-byte characters"

    _JsonHandler.response_body = b"{}"
    _JsonHandler.status_code = 200
    _JsonHandler.seen_path = ""
    _JsonHandler.seen_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(powershell, tmp_path, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    # Byte-exact on the wire, and structurally identical once parsed.
    assert _JsonHandler.seen_body == payload.encode("utf-8")
    assert json.loads(_JsonHandler.seen_body.decode("utf-8")) == payload_obj
    # The two characters that made this bug visible to the owner.
    delivered = json.loads(_JsonHandler.seen_body.decode("utf-8"))["response"]
    assert "\U0001f50a" in delivered
    assert "—" in delivered


# ---------------------------------------------------------------------------
# Managed-session correlation token (Task 2.4)
# ---------------------------------------------------------------------------


def test_forwarder_echoes_the_session_token_as_a_header_when_present(
    powershell: str, tmp_path: Path
):
    """WHISPERSTREAM_SESSION_TOKEN travels as a header, never a body field."""
    _JsonHandler.response_body = b"{}"
    _JsonHandler.status_code = 200
    _JsonHandler.seen_path = ""
    _JsonHandler.seen_body = b""
    _JsonHandler.seen_headers = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        payload = '{"hook_event_name":"Stop","session_id":"private"}'
        result = _run_forwarder(
            powershell,
            tmp_path,
            payload,
            extra_env={"WHISPERSTREAM_SESSION_TOKEN": "wrapper-minted-token"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert (
        _JsonHandler.seen_headers.get("x-whisperstream-managed-session-token")
        == "wrapper-minted-token"
    )
    # Never leaked into the body: the allowlisted fields are unchanged.
    assert json.loads(_JsonHandler.seen_body.decode("utf-8")) == json.loads(payload)


@pytest.mark.parametrize("session_token", [None, "", "   "])
def test_forwarder_omits_the_header_when_no_session_token_is_set(
    powershell: str, tmp_path: Path, session_token
):
    _JsonHandler.response_body = b"{}"
    _JsonHandler.status_code = 200
    _JsonHandler.seen_path = ""
    _JsonHandler.seen_body = b""
    _JsonHandler.seen_headers = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    extra_env = {} if session_token is None else {"WHISPERSTREAM_SESSION_TOKEN": session_token}
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(
            powershell, tmp_path, "{}", extra_env=extra_env
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "x-whisperstream-managed-session-token" not in _JsonHandler.seen_headers


def test_forwarder_emits_ascii_bytes_and_reconstructs_all_unicode(
    powershell: str, tmp_path: Path
):
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "additionalContext": (
                "End with 🔊 Résumé complete. Reply with 東京 or mañana."
            ),
            "decision": {
                "behavior": "allow",
                "updatedInput": {
                    "answer": "Crème brûlée — 東京 ✅",
                },
            },
        }
    }
    _JsonHandler.response_body = json.dumps(
        response,
        ensure_ascii=False,
    ).encode("utf-8")
    _JsonHandler.status_code = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(powershell, tmp_path, "{}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    wire = result.stdout.strip()
    assert result.returncode == 0
    assert result.stderr == b""
    assert wire.isascii()
    assert b"\\ud83d\\udd0a" in wire.lower()
    assert json.loads(wire.decode("ascii")) == response


@pytest.mark.parametrize(
    "response_body",
    [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b'"text"',
        b"null",
    ],
)
def test_forwarder_fails_open_on_invalid_backend_output(
    powershell: str, tmp_path: Path, response_body: bytes
):
    _JsonHandler.response_body = response_body
    _JsonHandler.status_code = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(powershell, tmp_path, "{}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 0
    assert result.stdout.strip() == b"{}"
    assert result.stderr == b""


@pytest.mark.parametrize("status_code", [408, 500])
def test_forwarder_fails_open_on_backend_timeout_or_error_status(
    powershell: str, tmp_path: Path, status_code: int
):
    _JsonHandler.response_body = b'{"private":"must-not-forward"}'
    _JsonHandler.status_code = status_code
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
        daemon=True,
    )
    thread.start()
    try:
        (tmp_path / "claude-hud-port.json").write_text(
            json.dumps({"port": server.server_port, "token": "a" * 64}),
            encoding="utf-8",
        )
        result = _run_forwarder(powershell, tmp_path, "{}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        _JsonHandler.status_code = 200

    assert result.returncode == 0
    assert result.stdout.strip() == b"{}"
    assert result.stderr == b""


@pytest.mark.parametrize(
    "body",
    ["", "not-json", "x" * 1_048_577],
    ids=["empty", "malformed", "oversized"],
)
def test_forwarder_fails_open_on_bad_input_or_no_backend(
    powershell: str, tmp_path: Path, body: str
):
    result = _run_forwarder(powershell, tmp_path, body)

    assert result.returncode == 0
    assert result.stdout.strip() == b"{}"
    assert result.stderr == b""
