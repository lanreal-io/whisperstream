"""Tests for graceful-exit teardown of a managed session (Task 3.4).

Scope decision 8: Whisperstream never ``TerminateProcess``es the CLI wrapper.
It requests graceful exit and waits. This file covers:

* ``ManagedSessionStore.request_shutdown`` sets a flag only, on a known
  session, and is a no-op for an unknown handle;
* the flag does not itself remove the record -- reaping still only happens on
  delivery-poll expiry or a dead PID (:meth:`ManagedSessionStore.reap`), matching
  "the backend waits and reaps only after the wrapper is gone or the
  delivery poll expires";
* a static guard, extended across every ``managed_session*.py`` module (not
  just the store), that no kill/terminate/signal call is even reachable from
  this feature's Python code paths.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import psutil
import pytest

from whisperstream import (
    managed_session_auth,
    managed_session_paste_guard,
    managed_sessions,
)
from whisperstream.managed_sessions import ManagedSessionStore


def _handle_factory():
    counter = itertools.count(1)
    return lambda: f"handle-{next(counter)}"


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_store(**kwargs) -> tuple[ManagedSessionStore, _FakeClock]:
    clock = _FakeClock()
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("handle_factory", _handle_factory())
    kwargs.setdefault("pid_is_alive", lambda pid: True)
    return ManagedSessionStore(**kwargs), clock


# ---------------------------------------------------------------------------
# request_shutdown: sets a flag, never touches the process
# ---------------------------------------------------------------------------


def test_request_shutdown_sets_the_flag_on_the_known_session():
    store, _clock = _make_store()
    session = store.register("tok-a", pid=111, cwd="/repo/one")

    assert session.shutdown_requested is False
    assert store.request_shutdown(session.session_handle) is True
    assert store.get("tok-a").shutdown_requested is True


def test_request_shutdown_returns_false_for_an_unknown_handle():
    store, _clock = _make_store()
    assert store.request_shutdown("no-such-handle") is False


def test_shutdown_request_can_be_read_by_handle():
    store, _clock = _make_store()
    session = store.register("tok-a", pid=111, cwd="/repo/one")

    assert store.is_shutdown_requested(session.session_handle) is False
    assert store.request_shutdown(session.session_handle) is True
    assert store.is_shutdown_requested(session.session_handle) is True
    assert store.is_shutdown_requested("no-such-handle") is False


def test_request_shutdown_does_not_probe_or_signal_the_process():
    """The request touches bookkeeping only -- never the PID."""
    probed: list[int] = []
    store, _clock = _make_store(pid_is_alive=lambda pid: probed.append(pid) or True)
    session = store.register("tok-a", pid=424242, cwd="/repo/one")

    assert store.request_shutdown(session.session_handle) is True
    assert probed == []


def test_request_shutdown_never_constructs_a_killable_psutil_process(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("request_shutdown must never construct psutil.Process")

    monkeypatch.setattr(psutil, "Process", explode)
    store, _clock = _make_store()
    session = store.register("tok-a", pid=99, cwd="/repo/one")

    assert store.request_shutdown(session.session_handle) is True


# ---------------------------------------------------------------------------
# A shutdown request does not itself remove the record: the backend waits
# and reaps only after the wrapper is gone or its delivery poll expires.
# ---------------------------------------------------------------------------


def test_shutdown_requested_session_survives_until_delivery_poll_expiry():
    store, clock = _make_store(session_ttl_seconds=90.0)
    session = store.register("tok-a", pid=111, cwd="/repo/one")
    store.request_shutdown(session.session_handle)

    # A fresh delivery poll still finds the session: the flag alone never reaps.
    clock.advance(10.0)
    assert store.reap() == ()
    assert store.get("tok-a") is not None
    assert store.get("tok-a").shutdown_requested is True

    # Only delivery-poll expiry (the wrapper going quiet, e.g. because it drained
    # itself in response to the request) actually removes the record.
    clock.advance(200.0)
    removed = store.reap()
    assert removed == (session.session_handle,)
    assert store.get("tok-a") is None


def test_shutdown_requested_session_is_removed_by_explicit_deregister():
    """The normal path: the wrapper observes the request, drains itself
    through its own exit, and deregisters on its own -- exactly like any
    other graceful exit."""
    store, _clock = _make_store()
    session = store.register("tok-a", pid=111, cwd="/repo/one")
    store.request_shutdown(session.session_handle)

    assert store.deregister("tok-a") is True
    assert store.get("tok-a") is None


# ---------------------------------------------------------------------------
# Static guard: no kill/terminate/signal call is reachable from any of the
# managed-session Python modules, extended beyond the store alone.
# ---------------------------------------------------------------------------

_FORBIDDEN_PROCESS_CALLS = frozenset(
    {
        "kill",
        "killpg",
        "terminate",
        "send_signal",
        "TerminateProcess",
        "taskkill",
        "SIGKILL",
        "SIGTERM",
    }
)


def _referenced_names(module) -> set[str]:
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Import):
            referenced.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            referenced.add((node.module or "").split(".")[0])
            referenced.update(alias.name for alias in node.names)
    return referenced


@pytest.mark.parametrize(
    "module",
    [managed_sessions, managed_session_auth, managed_session_paste_guard],
    ids=lambda m: m.__name__,
)
def test_no_kill_signal_or_terminate_call_is_reachable_in_managed_session_modules(module):
    """Scope decision 8, extended across every managed-session code path, not
    just the store: this must hold for the auth gate and the paste guard too,
    including whatever this task adds to the store for shutdown requests."""
    referenced = _referenced_names(module)

    assert not (referenced & _FORBIDDEN_PROCESS_CALLS)
    assert "signal" not in referenced
    assert "subprocess" not in referenced
