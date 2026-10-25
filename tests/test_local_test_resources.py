"""Resource policy contracts and real Windows child-process verification."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import local_test_resources as resources


@pytest.mark.parametrize("raw, expected", [("1", "1"), ("99", "2"), ("bad", "2"), ("0", "2"), ("-1", "2")])
def test_native_threads_preserve_stricter_caps(raw, expected):
    env = {name: raw for name in resources.THREAD_ENV_NAMES}
    env["UNRELATED"] = "retained"
    resources.cap_thread_environment(env)
    assert all(env[name] == expected for name in resources.THREAD_ENV_NAMES)
    assert env["UNRELATED"] == "retained"


@pytest.mark.parametrize("ci, opt_out", [("true", "1"), ("1", "1"), ("TRUE", "1"), ("", "0")])
def test_ci_and_explicit_opt_out_leave_process_untouched(monkeypatch, ci, opt_out):
    monkeypatch.setenv("CI", ci)
    monkeypatch.setenv("WHISPERSTREAM_TEST_LIMITS", opt_out)
    monkeypatch.setenv("OMP_NUM_THREADS", "17")
    monkeypatch.setattr(resources, "lower_priority", lambda: pytest.fail("priority changed"))
    monkeypatch.setattr(resources, "create_windows_cpu_job", lambda _: pytest.fail("job created"))
    resources.apply_local_test_limits()
    assert os.environ["OMP_NUM_THREADS"] == "17"


def test_inherited_cap_is_not_compounded(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("WHISPERSTREAM_TEST_LIMITS", raising=False)
    monkeypatch.setenv(resources.CPU_CAP_MARKER, "1")
    monkeypatch.setattr(resources, "lower_priority", lambda: None)
    monkeypatch.setattr(resources, "create_windows_cpu_job", lambda _: pytest.fail("nested job created"))
    resources.apply_local_test_limits()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_rejected_job_keeps_soft_limits_and_warns(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("WHISPERSTREAM_TEST_LIMITS", raising=False)
    monkeypatch.delenv(resources.CPU_CAP_MARKER, raising=False)
    monkeypatch.setattr(resources, "lower_priority", lambda: None)
    def reject(_):
        raise OSError("parent job policy")
    monkeypatch.setattr(resources, "create_windows_cpu_job", reject)
    with pytest.warns(RuntimeWarning, match="CPU cap unavailable"):
        resources.apply_local_test_limits()
    assert os.environ["OMP_NUM_THREADS"] in {"1", "2"}
    assert resources.CPU_CAP_MARKER not in os.environ


@pytest.mark.parametrize("raw, expected", [("20", 20), ("100", 100), ("0", 60), ("101", 60), ("bad", 60)])
def test_cpu_override_validation(monkeypatch, raw, expected):
    monkeypatch.setenv(resources.CPU_PERCENT_ENV, raw)
    if raw in {"0", "101", "bad"}:
        with pytest.warns(RuntimeWarning, match="must be 1..100"):
            assert resources.cpu_percent() == expected
    else:
        assert resources.cpu_percent() == expected


@pytest.mark.parametrize("cpus, expected", [(1, 1), (2, 1), (4, 2), (12, 6), (32, 6)])
def test_worker_count_uses_half_the_cpus_up_to_six(cpus, expected):
    assert resources.worker_count(cpus) == expected


def test_local_auto_workers_follow_policy_and_ci_keeps_xdist_default(monkeypatch):
    from tests import conftest

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("WHISPERSTREAM_TEST_LIMITS", raising=False)
    assert conftest.pytest_xdist_auto_num_workers(None) == resources.worker_count()
    monkeypatch.setenv("CI", "true")
    assert conftest.pytest_xdist_auto_num_workers(None) is None


# Query the actual OS state in a descendant, rather than just mocking Win32 calls.
QUERY = """
import ctypes, json, os
from scripts.local_test_resources import _kernel32, CpuRateInformation
api = _kernel32()
rate = CpuRateInformation()
query = api.QueryInformationJobObject
query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
query.restype = ctypes.c_int
assert query(None, 15, ctypes.byref(rate), ctypes.sizeof(rate), None)
print(json.dumps([rate.ControlFlags, rate.CpuRate,
    api.GetPriorityClass(api.GetCurrentProcess()), os.environ['OMP_NUM_THREADS']]))
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("attach", [False, True])
def test_real_child_inherits_cap_and_priority_after_helper_exit(attach):
    root = Path(__file__).resolve().parents[1]
    script = """
import os, subprocess, sys
from scripts import local_test_resources as r
# This subprocess owns its environment and job; do not alter pytest's own job.
os.environ.pop(r.CPU_CAP_MARKER, None)
os.environ.pop('CI', None)
os.environ.pop('WHISPERSTREAM_TEST_LIMITS', None)
os.environ[r.CPU_PERCENT_ENV] = '20'
"""
    if attach:
        script += """
r.cap_thread_environment(os.environ)
r.lower_priority()
subprocess.run([sys.executable, 'scripts/local_test_resources.py', '--attach', str(os.getpid())], check=True)
"""
    else:
        script += "r.apply_local_test_limits()\nr.apply_local_test_limits()\n"
    # The venv redirector adds an inner uncapped job. Query the inherited job
    # directly by launching the base interpreter, without that extra layer.
    script += f"subprocess.run([sys._base_executable, '-c', {QUERY!r}], check=True)\n"
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True)
    flags, rate, priority, threads = json.loads(result.stdout.strip())
    assert flags & 0x5 == 0x5
    assert rate == 2000
    assert priority in {resources.BELOW_NORMAL_PRIORITY, 0x40}
    assert threads in {"1", "2"}
