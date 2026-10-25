"""Windows subprocesses atomically assigned to a backend-owned kill-on-close job.

Only managed llama-server children use this launcher. No handles are inherited;
closing the backend's last job handle also terminates the child's descendants.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import subprocess
import threading
from collections.abc import Mapping, Sequence

try:
    import win32api
    import win32job
except ImportError:
    win32api = win32job = None


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


def _kernel_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "InitializeProcThreadAttributeList": (
            wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_size_t)]),
        "UpdateProcThreadAttribute": (
            wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                            ctypes.c_void_p]),
        "DeleteProcThreadAttributeList": (None, [ctypes.c_void_p]),
        "CreateProcessW": (
            wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.LPCWSTR,
                            ctypes.POINTER(_StartupInfoEx),
                            ctypes.POINTER(_ProcessInformation)]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "GetExitCodeProcess": (
            wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(api, name)
        function.restype = restype
        function.argtypes = argtypes
    return api


class WindowsJobProcess:
    """The small async Process interface needed by the llama supervisor."""

    def __init__(self, api, process_handle, job, pid: int):
        self._lock = threading.RLock()
        self._api = api
        self._handle = process_handle
        self._job = job
        self.pid = pid
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        with self._lock:
            if self._handle is not None:
                status = self._api.WaitForSingleObject(self._handle, 0)
                if status == 0:  # WAIT_OBJECT_0; exit code 259 is valid after exit.
                    code = wintypes.DWORD()
                    if not self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self._returncode = code.value
                    self.close()
                elif status != 258:  # WAIT_TIMEOUT
                    raise ctypes.WinError(ctypes.get_last_error())
            return self._returncode

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.05)
        return self._returncode

    def terminate(self) -> None:
        with self._lock:
            if self.returncode is None:
                win32job.TerminateJobObject(self._job, 1)

    kill = terminate

    def close(self) -> None:
        with self._lock:
            # Close the job first, killing descendants even if the direct child exited.
            if self._job is not None:
                job, self._job = self._job, None
                job.Close()
            if self._handle is not None:
                handle, self._handle = self._handle, None
                self._api.CloseHandle(handle)

    def __del__(self):
        self.close()


def spawn_windows_job_process(
    args: Sequence[str], *, cwd: str, env: Mapping[str, str]
) -> WindowsJobProcess:
    """Create one hidden child inside a new job, or raise without an unowned child.

    PROC_THREAD_ATTRIBUTE_JOB_LIST (Windows 10+) assigns the job before the
    initial thread can execute. The job handle is non-inheritable and stays in
    the backend, so abrupt backend termination needs no Python cleanup.
    """
    if win32job is None:
        raise RuntimeError("Windows child lifetime protection unavailable")
    api = _kernel_api()
    job = win32job.CreateJobObject(None, "")
    attribute_list = None
    initialized = False
    info = _ProcessInformation()
    try:
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )
        # CreateJobObject(NULL security attributes) produces a non-inheritable
        # handle. Explicitly clear inheritance as a defence against future changes.
        win32api.SetHandleInformation(job, 1, 0)  # HANDLE_FLAG_INHERIT
        size = ctypes.c_size_t()
        api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attribute_list = ctypes.create_string_buffer(size.value)
        if not api.InitializeProcThreadAttributeList(
            attribute_list, 1, 0, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        initialized = True
        jobs = (wintypes.HANDLE * 1)(int(job))
        if not api.UpdateProcThreadAttribute(
            attribute_list, 0, 0x0002000D, jobs, ctypes.sizeof(jobs), None, None
        ):  # PROC_THREAD_ATTRIBUTE_JOB_LIST
            raise ctypes.WinError(ctypes.get_last_error())
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(
                env.items(), key=lambda item: item[0].upper()
            )) + "\0\0"
        )
        if not api.CreateProcessW(
            args[0], command, None, None, False,
            0x00080000 | 0x08000000 | 0x00000400,
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
            environment, cwd, ctypes.byref(startup), ctypes.byref(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        child = WindowsJobProcess(api, info.hProcess, job, info.dwProcessId)
        job = None  # Ownership transferred; all other paths close it below.
        info.hProcess = None
        return child
    finally:
        if info.hThread:
            api.CloseHandle(info.hThread)
        if info.hProcess:
            api.CloseHandle(info.hProcess)
        if initialized:
            api.DeleteProcThreadAttributeList(attribute_list)
        if job is not None:
            job.Close()
