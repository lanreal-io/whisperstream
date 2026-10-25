"""Exercise session-end messages on owned Tauri/WebView2 windows, never the OS."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("This native probe requires Windows")
    env = os.environ.copy()
    env["TAURI_CONFIG"] = json.dumps({"bundle": {"externalBin": [], "resources": []}})

    def build_executable(command: list[str]) -> Path:
        result = subprocess.run([
            "cargo", *command, "--locked", "--manifest-path", str(ROOT / "src-tauri/Cargo.toml"),
            "--message-format=json",
        ], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, check=True)
        artifacts = [json.loads(line) for line in result.stdout.splitlines()]
        executable = Path(next(record["executable"] for record in artifacts if record.get("executable")))
        # Match production; bare Cargo examples/tests don't get Tauri's manifest.
        manifest = executable.with_suffix(".exe.manifest")
        manifest.write_text('''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<dependency><dependentAssembly><assemblyIdentity type="win32"
name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*"
publicKeyToken="6595b64144ccf1df" language="*" /></dependentAssembly></dependency></assembly>
''', encoding="utf-8")
        kits = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10/bin"
        manifest_tools = sorted(kits.glob("*/x64/mt.exe"))
        if not manifest_tools:
            raise SystemExit("Windows SDK x64 mt.exe is required to embed the test manifest")
        mt = manifest_tools[-1]
        subprocess.run([str(mt), "-nologo", "-manifest", str(manifest),
                        f"-outputresource:{executable};#1"], check=True)
        return executable

    unit_tests = build_executable(["test", "--lib", "--no-run"])
    subprocess.run([str(unit_tests), "windows_shutdown::tests", "--test-threads=1"],
                   cwd=ROOT, env=env, check=True, timeout=60)
    executable = build_executable(["build", "--example", "windows_shutdown_probe"])
    for mode in ["shutdown", "logoff", "restart-manager", "cancel", "ordinary"]:
        started = time.monotonic()
        result = subprocess.run([str(executable), mode], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, f"{mode}: {result.returncode}\n{result.stderr}"
        assert "probe timed out" not in result.stderr
        if mode != "ordinary":
            assert "query/cancellation preserved shell" in result.stderr
        confirmed = mode in {"shutdown", "logoff", "restart-manager"}
        assert f"Exit callback ending={str(confirmed).lower()}" in result.stderr
        assert ("Exit callback returning" not in result.stderr) == confirmed
        print(f"PASS {mode}: process exited in {time.monotonic() - started:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
