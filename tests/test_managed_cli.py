from pathlib import Path

from whisperstream import managed_cli as managed_cli_module
from whisperstream.managed_cli import (
    add_path_entry,
    cli_bin_dir,
    path_contains,
    remove_path_entry,
)


ROOT = Path(__file__).resolve().parents[1]


def test_path_install_is_exact_and_idempotent():
    target = Path(r"C:\Program Files\Whisperstream\bin")
    original = r"C:\Tools;C:\Program Files\Whisperstream\bin2"

    installed = add_path_entry(original, target)

    assert installed == original + ";" + str(target)
    assert add_path_entry(installed, target) == installed
    assert path_contains(installed, target) is True


def test_path_status_is_case_insensitive_on_windows_style_paths():
    target = Path(r"C:\Program Files\Whisperstream\bin")

    assert path_contains(r"C:\PROGRAM FILES\WHISPERSTREAM\BIN", target) is True


def test_path_uninstall_removes_only_the_exact_cli_segment():
    target = Path(r"C:\Program Files\Whisperstream\bin")
    original = (
        r"C:\Tools;C:\Program Files\Whisperstream\bin;"
        r"C:\Program Files\Whisperstream\bin2;C:\Other"
    )

    assert remove_path_entry(original, target) == (
        r"C:\Tools;C:\Program Files\Whisperstream\bin2;C:\Other"
    )


def test_installer_does_not_expose_cli_until_agent_speak_install():
    hooks = (ROOT / "src-tauri" / "installer" / "hooks.nsh").read_text(
        encoding="utf-8"
    )

    assert "NSIS_HOOK_POSTINSTALL" not in hooks
    assert "!insertmacro WHISPERSTREAM_PATH_REMOVE" in hooks


def test_cli_bin_dir_uses_nuitka_original_executable_directory(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(managed_cli_module.sys, "platform", "win32")
    install_dir = tmp_path / "installed"
    cli_dir = install_dir / "bin"
    cli_dir.mkdir(parents=True)
    (cli_dir / "whisperstream.exe").write_bytes(b"test-cli")
    runtime_executable = tmp_path / "runtime" / "whisperstream-engine.exe"
    runtime_executable.parent.mkdir()
    runtime_executable.write_bytes(b"test-engine")

    compiled = type("Compiled", (), {"containing_dir": str(install_dir)})()
    monkeypatch.delenv("WHISPERSTREAM_CLI_BIN_DIR", raising=False)
    monkeypatch.setitem(vars(managed_cli_module), "__compiled__", compiled)
    monkeypatch.setattr(managed_cli_module.sys, "executable", str(runtime_executable))

    assert cli_bin_dir() == cli_dir.resolve()
