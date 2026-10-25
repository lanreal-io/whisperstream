"""Measure Windows input-hook latency while an owned pytest subprocess runs.

Sends tagged, same-position mouse moves (no clicks or keys), and records only
their timing. The observer is installed BEFORE pytest, behind any hooks pytest
adds. No physical input, pointer coordinates, or application content is saved.
Run with a Python environment containing this repository's dev dependencies.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def run_tests(args):
    import pytest
    from pynput._util import AbstractListener

    listeners = []
    original = AbstractListener.start
    current_test = None
    with (args.output / "listeners.jsonl").open("w", encoding="utf-8") as trace:
        def start(listener):
            listeners.append(listener)
            trace.write(json.dumps({"event": "start", "test": current_test,
                "kind": type(listener).__module__, "time": time.perf_counter()}) + "\n")
            trace.flush()
            return original(listener)

        AbstractListener.start = start

        class Audit:
            def pytest_runtest_setup(self, item):
                nonlocal current_test
                current_test = item.nodeid

            def pytest_runtest_logreport(self, report):
                if report.when == "teardown":
                    trace.write(json.dumps({"event": "teardown", "test": report.nodeid,
                        "time": time.perf_counter(), "started": len(listeners),
                        "alive": sum(x.is_alive() for x in listeners)}) + "\n")
                    trace.flush()

        try:
            result = pytest.main(args.tests, plugins=[Audit()])
            print(json.dumps({"listeners_started": len(listeners),
                "listeners_alive_at_session_end": sum(x.is_alive() for x in listeners),
                "threads_at_session_end": threading.active_count()}), flush=True)
            end = time.monotonic() + args.tail_cpu_seconds
            while time.monotonic() < end:
                sum(range(10000))
            return result
        finally:
            AbstractListener.start = original
            for listener in listeners:
                listener.stop()
            for listener in listeners:
                listener.join(timeout=1)


def summarize(rows):
    values = sorted(row["latency_ms"] for row in rows if row["latency_ms"] is not None)
    return {"samples": len(rows), "missing": len(rows) - len(values),
        "p50_ms": round(statistics.median(values), 3) if values else None,
        "p95_ms": round(values[min(len(values)-1, int(len(values)*.95))], 3) if values else None,
        "p99_ms": round(values[min(len(values)-1, int(len(values)*.99))], 3) if values else None,
        "max_ms": round(max(values), 3) if values else None,
        "over_16ms": sum(x > 16.67 for x in values)}


def measure(args):
    from ctypes import wintypes
    from pynput.mouse import Listener
    from pynput._util.win32 import INPUT, INPUT_union, MOUSEINPUT, SendInput

    # Stay in 32 bits: desktop input paths can truncate application extra-info.
    marker = [0x57000000 | ((os.getpid() & 0xFF) << 16)]
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    received = threading.Event()
    arrival = [0.0]

    def observe(msg, data):
        if data.dwExtraInfo == marker[0]:
            arrival[0] = time.perf_counter()
            received.set()
        return False  # Skip pynput callbacks; Windows still forwards the event.

    listener = Listener(win32_event_filter=observe)
    listener.start()
    listener.wait()
    # pynput marks its message loop ready just before installing the OS hook.
    time.sleep(.1)
    child = None
    rows = []
    deadline = time.monotonic() + args.timeout
    stage = "before"
    stage_end = time.monotonic() + 3
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")])
    command = [sys.executable, str(Path(__file__).resolve()), "--child",
        "--output", str(args.output), "--tail-cpu-seconds", str(args.tail_cpu_seconds), "--", *args.tests]
    timed_out = False
    samples_file = (args.output / "latency.jsonl").open("w", encoding="utf-8", buffering=1)
    # Watchdog is independent of SendInput, which can itself wait on slow hooks.
    def watchdog():
        nonlocal timed_out
        while not finished.wait(.2):
            if time.monotonic() > deadline:
                timed_out = True
                if child is not None and child.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return

    finished = threading.Event()
    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    try:
        with (args.output / "pytest.log").open("w", encoding="utf-8") as log:
            while not timed_out:
                if stage == "before" and time.monotonic() >= stage_end:
                    child = subprocess.Popen(command, cwd=ROOT, env=env,
                        stdout=log, stderr=subprocess.STDOUT)
                    stage = "tests"
                elif stage == "tests" and child.poll() is not None:
                    stage, stage_end = "after", time.monotonic() + 3
                elif stage == "after" and time.monotonic() >= stage_end:
                    break
                received.clear()
                marker[0] += 1  # A delayed previous event cannot satisfy this sample.
                # Windows discards relative (0, 0) events before the hook chain.
                # Target the current physical pixel's center on the virtual desktop.
                point = wintypes.POINT()
                if not user32.GetCursorPos(ctypes.byref(point)):
                    raise ctypes.WinError(ctypes.get_last_error())
                left, top, width, height = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
                sent = time.perf_counter()
                event = INPUT(type=INPUT.MOUSE, value=INPUT_union(mi=MOUSEINPUT(
                    dx=int((point.x-left+.5)*65536/width),
                    dy=int((point.y-top+.5)*65536/height), mouseData=0,
                    dwFlags=MOUSEINPUT.MOVE | 0x8000 | 0x4000,
                    time=0, dwExtraInfo=marker[0])))
                if SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
                    raise RuntimeError("SendInput failed (possibly integrity-level mismatch)")
                ok = received.wait(1)
                if stage == "before" and not ok:
                    raise RuntimeError("Baseline probe was not delivered; refusing to start pytest")
                row = {"phase": stage, "time": sent,
                    "latency_ms": (arrival[0]-sent)*1000 if ok else None}
                rows.append(row)
                samples_file.write(json.dumps(row)+"\n")
                time.sleep(.05)
    finally:
        finished.set()
        if child is not None and child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        listener.stop()
        listener.join(timeout=2)
        samples_file.close()
    summary = {"command": args.tests, "timeout": timed_out,
        "pytest_exit": child.returncode if child else None,
        **{phase: summarize([row for row in rows if row["phase"] == phase])
            for phase in ("before", "tests", "after")}}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if timed_out else (child.returncode if child else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--tail-cpu-seconds", type=float, default=0)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("tests", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows only")
    if args.tests[:1] == ["--"]:
        args.tests = args.tests[1:]
    if not args.tests:
        parser.error("supply explicit pytest targets after --")
    if not 10 <= args.timeout <= 1800 or not 0 <= args.tail_cpu_seconds <= 30:
        parser.error("timeout must be 10..1800 seconds; tail CPU work must be 0..30 seconds")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    return run_tests(args) if args.child else measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
