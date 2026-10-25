"""Content-free Unit 03 process, listener, temp, and privacy cleanup proof."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import psutil


REPO = Path(__file__).resolve().parents[1]
RESULTS_ROOT = (
    REPO / "plans" / "macos-port" / "windows-parity-prep-loop" / "results"
)
UNIT_TEMPS = tuple(
    RESULTS_ROOT / name
    for name in (
        "unit03-model-contract",
        "unit03-acquisition",
        "unit03-cloud",
        "unit03-ollama",
        "unit03-secrets",
        "unit03-candidates",
        "unit03-enhancement-regression",
        "unit03-post-edit",
    )
)
SCANNED_FILES = (
    REPO / "src" / "whisperstream" / "configured_enhancement_provider.py",
    REPO / "src" / "whisperstream" / "model_lifecycle.py",
    REPO / "src" / "whisperstream" / "windows_model_lifecycle.py",
    REPO / "tests" / "test_enhancement_provider_conformance.py",
    REPO / "tests" / "test_windows_model_lifecycle_conformance.py",
    REPO / "tests" / "windows_ollama_live_probe.py",
)


def _ancestors() -> set[int]:
    ancestors: set[int] = set()
    process = psutil.Process()
    while process.parent() is not None:
        process = process.parent()
        ancestors.add(process.pid)
    return ancestors


def _owned_survivors() -> list[int]:
    root_marker = str(REPO).casefold()
    markers = (
        "pytest",
        "windows_ollama_live_probe",
        "whisperstream.main",
        "llama-server",
        "stt_worker",
    )
    excluded = _ancestors() | {os.getpid()}
    survivors: list[int] = []
    for process in psutil.process_iter(("pid", "cmdline")):
        if process.pid in excluded:
            continue
        try:
            command = " ".join(process.info.get("cmdline") or []).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if root_marker in command and any(marker in command for marker in markers):
            survivors.append(process.pid)
    return survivors


def _privacy_scan() -> list[str]:
    violations: list[str] = []
    owner_path = str(Path.home()).casefold()
    email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    for path in SCANNED_FILES:
        text = path.read_text(encoding="utf-8")
        if owner_path in text.casefold():
            violations.append("owner_path")
        if email.search(text):
            violations.append("full_email")
    return violations


def main() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        print("FAIL platform_not_native_windows_x64")
        return 2

    expected_parent = RESULTS_ROOT.resolve()
    for unit_temp in UNIT_TEMPS:
        resolved_temp = unit_temp.resolve()
        if (
            resolved_temp.parent != expected_parent
            or not resolved_temp.name.startswith("unit03-")
        ):
            print("FAIL temp_scope_invalid")
            return 1
        if unit_temp.exists():
            shutil.rmtree(unit_temp)

    survivors = _owned_survivors()
    listeners = [
        connection
        for connection in psutil.Process().net_connections(kind="tcp")
        if connection.status == psutil.CONN_LISTEN
    ]
    extra_threads = [
        thread for thread in threading.enumerate() if thread is not threading.main_thread()
    ]
    privacy = _privacy_scan()
    tracked = subprocess.run(
        ["git", "diff", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    temp_survivors = sum(path.exists() for path in UNIT_TEMPS)
    if temp_survivors or survivors or listeners or extra_threads or privacy:
        print(
            "FAIL cleanup "
            f"temp={temp_survivors} processes={len(survivors)} "
            f"listeners={len(listeners)} threads={len(extra_threads)} "
            f"privacy={len(privacy)}"
        )
        return 1
    if tracked.returncode != 0:
        print("FAIL diff_check")
        return 1
    print("PASS cleanup processes=0 listeners=0 threads=0 privacy=0 temp=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
