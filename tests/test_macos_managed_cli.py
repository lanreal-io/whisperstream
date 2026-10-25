"""Isolated shell setup and real Darwin terminal restoration checks."""
from __future__ import annotations
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

import pytest
from whisperstream import macos_managed_cli as cli

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mac shell profile ownership and PTY tests")


def test_command_install_and_remove_preserve_unrelated_profile_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    binaries = tmp_path / "App Resources" / "bin"
    binaries.mkdir(parents=True)
    command = binaries / "whisperstream"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    profile = tmp_path / ".zprofile"
    original = 'export MY_OTHER_TOOL="unchanged"\n'
    profile.write_text(original)
    assert cli.install(binaries)["installed"] is True
    installed = profile.read_text()
    assert cli.install(binaries)["installed"] is True
    assert profile.read_text() == installed
    assert cli.uninstall(binaries)["installed"] is False
    assert profile.read_text() == original
    assert not (tmp_path / ".local/bin/whisperstream").exists()


@pytest.mark.parametrize("profile_name", [".profile", ".bash_login", ".bash_profile"])
def test_install_preserves_bash_login_profile_precedence(tmp_path, monkeypatch, profile_name):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    binaries = tmp_path / "bundle-bin"
    binaries.mkdir()
    command = binaries / "whisperstream"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    profile = tmp_path / profile_name
    original = 'export WS_PROFILE_PROOF="retained"\n'
    profile.write_text(original)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    def read_login_value():
        return subprocess.check_output(
            ["/bin/bash", "--login", "-c", 'printf "%s" "${WS_PROFILE_PROOF:-missing}"'],
            env=env, text=True,
        )
    assert read_login_value() == "retained"
    assert cli.install(binaries)["installed"] is True
    assert cli._BLOCK in profile.read_text()
    assert read_login_value() == "retained"
    if profile_name != ".bash_profile":
        assert not (tmp_path / ".bash_profile").exists()
    # A new higher-precedence file must not hide the block from uninstall.
    if profile_name == ".profile":
        (tmp_path / ".bash_profile").write_text("# later user configuration\n")
    assert cli.uninstall(binaries)["installed"] is False
    assert profile.read_text() == original


def test_uninstall_preserves_unrelated_profile_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    (tmp_path / ".bash_profile").write_text(cli._BLOCK)
    other = tmp_path / "unrelated-profile"
    other.write_text("# unrelated\n")
    (tmp_path / ".profile").symlink_to(other)
    assert cli.uninstall(tmp_path / "bin")["ok"] is True
    assert (tmp_path / ".profile").is_symlink()
    assert other.read_text() == "# unrelated\n"


def test_install_refuses_an_unowned_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "whisperstream").write_text("tool")
    (binaries / "whisperstream").chmod(0o700)
    target = tmp_path / ".local/bin/whisperstream"
    target.parent.mkdir(parents=True)
    target.write_text("unrelated")
    assert cli.install(binaries)["ok"] is False
    assert target.read_text() == "unrelated"
    assert not (tmp_path / ".zprofile").exists()


def test_uninstall_refuses_an_edited_owned_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    profile = tmp_path / ".zprofile"
    original = cli._START + "\nuser changes\n" + cli._END
    profile.write_text(original)
    assert cli.uninstall(tmp_path / "bin")["ok"] is False
    assert profile.read_text() == original


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires a Darwin PTY")
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("termination", ["exit", "signal"])
def test_native_relay_restores_termios_and_preserves_io(tmp_path, provider, termination):
    import fcntl
    import termios
    binary = Path(__file__).resolve().parents[1] / "src-tauri/target/debug/whisperstream-cli"
    if not binary.is_file():
        pytest.skip("Build the standalone managed CLI before the native PTY suite")
    (tmp_path / "home").mkdir()
    target = tmp_path / provider
    target.write_text(f'#!{sys.executable}\nimport sys, os\nassert os.getcwd() == os.environ["EXPECTED_CWD"]\nprint("SYNTHETIC_READY", flush=True)\nline = input()\nprint("SYNTHETIC_REPLY:" + line, flush=True)\n')
    target.chmod(0o700)
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    before = termios.tcgetattr(slave)
    env = dict(os.environ, PATH=str(tmp_path), WHISPERSTREAM_DATA_DIR=str(tmp_path / "data"), HOME=str(tmp_path / "home"), EXPECTED_CWD=str(tmp_path))
    process = subprocess.Popen([str(binary), provider], stdin=slave, stdout=slave, stderr=slave, env=env, cwd=tmp_path, start_new_session=True)
    output = bytearray()
    try:
        deadline = time.monotonic() + 8
        while b"SYNTHETIC_READY" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                output.extend(os.read(master, 65536))
        assert b"SYNTHETIC_READY" in output
        if termination == "signal":
            process.send_signal(signal.SIGTERM)
        else:
            os.write(master, "hello café\n".encode())
        process.wait(timeout=8)
        while select.select([master], [], [], .1)[0]:
            output.extend(os.read(master, 65536))
        assert termios.tcgetattr(slave) == before
        if termination == "exit":
            assert process.returncode == 0
            assert "SYNTHETIC_REPLY:hello café".encode() in output
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
        os.close(slave)
