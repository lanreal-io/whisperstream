"""The shared development launchers carry only contributor options."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["run_dev.ps1", "run_dev.sh"])
def test_shared_launcher_has_no_repository_private_options(name: str) -> None:
    source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    for token in (
        "candidate-models.json",
        "WHISPERSTREAM_DEV_CANDIDATE_",
        "ft_probe_tools",
        "WHISPERSTREAM_DEV_TOOLS",
        "WHISPERSTREAM_FINETUNE_DIR",
        "local-finetune",
        ".worktrees",
        "private/",
        "private\\",
    ):
        assert token not in source, token


def test_windows_launcher_forwards_only_named_backend_variables() -> None:
    source = (ROOT / "scripts" / "run_dev.ps1").read_text(encoding="utf-8")
    assert "WHISPERSTREAM_DEV_LAUNCHER_ENV" in source
    # Wrapper-supplied names are interpolated into the Backend tab's script.
    assert "'^[A-Za-z_][A-Za-z0-9_]*$'" in source


def test_macos_launcher_selects_dev_plugin_and_refuses_installed_data() -> None:
    source = (ROOT / "scripts" / "run_dev.sh").read_text(encoding="utf-8")
    assert "export WHISPERSTREAM_AGENT_PLUGIN_VARIANT=dev" in source
    assert "installed in candidate.parents" in source
    assert 'Whisperstream-dev"' in source


def _run_windows_launcher(tmp_path: Path, arguments: str, data_dir: Path | None = None):
    """Run a copy of the shared launcher with wt and port probes stubbed."""
    import json
    import os
    import shutil
    import subprocess

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "run_dev.ps1", project / "scripts" / "run_dev.ps1")
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "python.exe").touch()
    capture = tmp_path / "wt.json"
    launcher = str(project / "scripts" / "run_dev.ps1").replace("'", "''")
    wrapper = f"""
$ErrorActionPreference = 'Stop'
function global:Get-NetTCPConnection {{ }}
function global:wt {{
    @($args | ForEach-Object {{ "$_" }}) | ConvertTo-Json | Set-Content -LiteralPath '{str(capture).replace("'", "''")}' -Encoding UTF8
}}
& '{launcher}' {arguments}
exit $LASTEXITCODE
"""
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    env["WHISPERSTREAM_DATA_DIR"] = str(data_dir or tmp_path / "dev-data")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    launched = json.loads(capture.read_text(encoding="utf-8-sig")) if capture.exists() else None
    return result, launched


windows_only = pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="Runs the native Windows launcher"
)


@windows_only
def test_check_reports_preflight_without_launching(tmp_path: Path) -> None:
    result, launched = _run_windows_launcher(tmp_path, "-Check -NoTauri")
    assert result.returncode == 0, result.stderr or result.stdout
    assert "Preflight passed." in result.stdout
    assert launched is None
    assert not (tmp_path / "project" / "frontend").exists()


@windows_only
def test_backend_only_opens_only_the_backend_tab(tmp_path: Path) -> None:
    result, launched = _run_windows_launcher(tmp_path, "-BackendOnly")
    assert result.returncode == 0, result.stderr or result.stdout
    joined = " ".join(launched)
    assert "Backend" in joined
    assert "Frontend" not in joined and "Tauri" not in joined
    # No frontend install or sidecar staging for a backend-only launch.
    assert not (tmp_path / "project" / "frontend").exists()
    assert not (tmp_path / "project" / "src-tauri").exists()


@windows_only
def test_data_dir_inside_installed_app_data_is_refused(tmp_path: Path) -> None:
    installed_child = tmp_path / "local-app-data" / "Whisperstream" / "nested"
    result, launched = _run_windows_launcher(tmp_path, "-NoTauri", data_dir=installed_child)
    assert result.returncode != 0
    assert "outside the installed Whisperstream data directory" in (result.stderr + result.stdout)
    assert launched is None
