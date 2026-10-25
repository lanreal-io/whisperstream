"""Local test resource policy. Standard library only; never imported by the app."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import warnings

THREAD_ENV_NAMES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS",
)
CPU_CAP_MARKER = "_WHISPERSTREAM_TEST_CPU_CAPPED"
CPU_PERCENT_ENV = "WHISPERSTREAM_TEST_CPU_PERCENT"
DEFAULT_CPU_PERCENT = 60
MAX_WORKERS = 6
BELOW_NORMAL_PRIORITY = 0x4000


def local_limits_enabled() -> bool:
    return os.environ.get("CI", "").lower() not in {"1", "true"} and (
        os.environ.get("WHISPERSTREAM_TEST_LIMITS") != "0"
    )


def cpu_percent() -> int:
    raw = os.environ.get(CPU_PERCENT_ENV, str(DEFAULT_CPU_PERCENT))
    try:
        value = int(raw)
        if 1 <= value <= 100:
            return value
    except ValueError:
        pass
    warnings.warn(
        f"[tests] {CPU_PERCENT_ENV} must be 1..100; using {DEFAULT_CPU_PERCENT}%.", RuntimeWarning, stacklevel=2
    )
    return DEFAULT_CPU_PERCENT


def worker_count(cpus: int | None = None) -> int:
    """Parallel test workers: half the logical CPUs, at most MAX_WORKERS. Mirrors Vitest."""
    return max(1, min(MAX_WORKERS, (cpus or os.cpu_count() or 2) // 2))


def cap_thread_environment(env) -> None:
    for name in THREAD_ENV_NAMES:
        try:
            value = int(env.get(name, "2"))
        except ValueError:
            value = 2
        env[name] = str(min(value, 2)) if value > 0 else "2"


class CpuRateInformation(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "GetPriorityClass": ([wintypes.HANDLE], wintypes.DWORD),
        "SetPriorityClass": ([wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def _check(value, action):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error(), action)
    return value


def create_windows_cpu_job(percent: int, target_pid: int | None = None) -> int:
    """Cap this process or an explicitly supplied test runner and its future children.

    No kill-on-close flag: the job survives helper exit until its processes exit.
    The caller owns the returned handle when it needs to inspect the job.
    """
    if not 1 <= percent <= 100:
        raise ValueError("CPU percentage must be 1..100")
    api = _kernel32()
    process = api.GetCurrentProcess()
    if target_pid is not None:
        # AssignProcessToJobObject requires SET_QUOTA | TERMINATE.
        process = _check(api.OpenProcess(0x0101, False, target_pid), "OpenProcess")
    job = None
    try:
        job = _check(api.CreateJobObjectW(None, None), "CreateJobObjectW")
        rate = CpuRateInformation(0x1 | 0x4, percent * 100)
        _check(api.SetInformationJobObject(job, 15, ctypes.byref(rate), ctypes.sizeof(rate)), "SetInformationJobObject")
        _check(api.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")
        return int(job)
    except OSError:
        if job:
            api.CloseHandle(job)
        raise
    finally:
        if target_pid is not None:
            api.CloseHandle(process)


def lower_priority() -> None:
    if os.name == "nt":
        api = _kernel32()
        process = api.GetCurrentProcess()
        current = _check(api.GetPriorityClass(process), "GetPriorityClass")
        if current not in {0x40, BELOW_NORMAL_PRIORITY}:  # Preserve idle priority.
            _check(api.SetPriorityClass(process, BELOW_NORMAL_PRIORITY), "SetPriorityClass")
    elif os.getpriority(os.PRIO_PROCESS, 0) < 10:
        os.setpriority(os.PRIO_PROCESS, 0, 10)


def apply_local_test_limits() -> None:
    if not local_limits_enabled():
        return
    cap_thread_environment(os.environ)
    try:
        lower_priority()
    except OSError as exc:
        warnings.warn(f"[tests] Could not lower process priority: {exc}", RuntimeWarning, stacklevel=2)
    if os.name == "nt" and os.environ.get(CPU_CAP_MARKER) != "1":
        percent = cpu_percent()
        try:
            handle = create_windows_cpu_job(percent)
            _kernel32().CloseHandle(handle)
            os.environ[CPU_CAP_MARKER] = "1"
            print(f"[tests] Windows process-tree CPU cap active: {percent}%", file=sys.stderr)
        except OSError as exc:
            warnings.warn(f"[tests] CPU cap unavailable; retaining worker/thread/priority limits: {exc}", RuntimeWarning, stacklevel=2)


if __name__ == "__main__":
    # Called by the Vitest config with its own PID, before creating workers.
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attach", type=int, required=True)
    args = parser.parse_args()
    try:
        handle = create_windows_cpu_job(cpu_percent(), args.attach)
        _kernel32().CloseHandle(handle)
    except OSError as exc:
        print(f"[tests] Windows CPU cap unavailable: {exc}", file=sys.stderr)
        sys.exit(1)
