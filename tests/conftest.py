"""Shared pytest fixtures for Whisperstream e2e tests."""

from contextlib import contextmanager
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
import socket

from scripts.local_test_resources import apply_local_test_limits, local_limits_enabled, worker_count

# Apply before importing app/native libraries or collecting test modules.
apply_local_test_limits()

import anyio
import pytest
from starlette.testclient import TestClient


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """Local ``-n auto`` uses the shared worker policy; CI keeps xdist's default."""
    return worker_count() if local_limits_enabled() else None


@pytest.fixture(autouse=True)
def _isolate_acceptance_session(monkeypatch):
    """Each test represents a fresh backend process, not a prior test's grant."""
    from whisperstream import eula

    monkeypatch.setattr(eula, "_acceptance_session", None)


@pytest.fixture(autouse=True)
def _isolate_agent_plugin_variant(monkeypatch):
    """Plugin fixtures default to production grammar even from a dev terminal.

    Tests of development identity select it explicitly after this fixture.
    Never let a caller's launcher setting rewrite synthetic plugin commands.
    """
    monkeypatch.delenv("WHISPERSTREAM_AGENT_PLUGIN_VARIANT", raising=False)


@pytest.fixture(autouse=True)
def _prevent_unit_test_global_input_hooks(request, monkeypatch):
    """A background pytest process must never sit in the desktop input chain.

    Low-level Windows hooks and active macOS event taps run synchronously even
    for unhandled input. Retaining them while pytest holds the GIL can stall the
    whole desktop. Real input belongs in the explicit e2e suite; unit tests use
    listener doubles.
    """
    if sys.platform not in ("win32", "darwin") or request.node.path.is_relative_to(
        Path(__file__).parent / "e2e"
    ):
        return

    from pynput._util import AbstractListener
    from whisperstream import macos_shortcuts

    def reject_start(self):
        raise RuntimeError(
            "Unit tests must not start global input listeners. "
            "Use mock_shortcut_listeners, a fake listener_factory, "
            "or move real-input coverage out of the unit tests."
        )

    monkeypatch.setattr(AbstractListener, "start", reject_start)
    monkeypatch.setattr(macos_shortcuts._CoreGraphicsEventListener, "start", reject_start)


# Background-thread owners and their idempotent teardown. Tests often build one
# without closing it, leaving a worker or long idle timer alive for the rest of
# the session (and in every xdist worker). Each instance a test creates is
# closed after the test; instances from broader-scoped fixtures are left alone.
def _close_shortcut_adapter(adapter, worker_queue) -> None:
    # Tests may swap _queue for a mock; the worker still reads the original.
    adapter._queue = worker_queue
    adapter.close()


def _cancel_question_timers(coordinator, _) -> None:
    # No public close: in production the real clock lets the TTL timer prune
    # and stop. A frozen test clock makes it re-arm every 15 s forever.
    with coordinator._lock:
        coordinator._cancel_terminal_timer_locked()
        coordinator._cancel_early_timer_locked()


_BACKGROUND_OWNERS = (
    ("whisperstream.claude_interactions", "ClaudeQuestionCoordinator", None, _cancel_question_timers),
    ("whisperstream.speech.selection", "EngineManager", None, lambda owner, _: owner.shutdown(timeout=0.2)),
    ("whisperstream.speech.controller", "SpeechController", None, lambda owner, _: owner.shutdown(timeout=0.2)),
    ("whisperstream.background_work", "DeadlineScheduler", None, lambda owner, _: owner.close()),
    ("whisperstream.keyboard_shortcuts", "WindowsShortcutAdapter", lambda owner: owner._queue, _close_shortcut_adapter),
)
_owned_by_test: list | None = None
_broader_fixture_depth = 0
_tracked_classes: set = set()


def _track_background_owners(*, import_modules: bool = False) -> None:
    """Wrap each owner's constructor once its module has been imported."""
    import functools
    import importlib

    for module_name, class_name, capture, close in _BACKGROUND_OWNERS:
        module = sys.modules.get(module_name)
        if module is None and import_modules:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 -- platform-specific modules may not import
                continue
        cls = getattr(module, class_name, None)
        if cls is None or cls in _tracked_classes:
            continue
        original = cls.__init__

        @functools.wraps(original)
        def init(self, *args, __original=original, __capture=capture, __close=close, **kwargs):
            __original(self, *args, **kwargs)
            if _owned_by_test is not None and _broader_fixture_depth == 0:
                _owned_by_test.append((self, __capture(self) if __capture else None, __close))

        cls.__init__ = init
        _tracked_classes.add(cls)


def pytest_sessionstart(session):
    # Wrap before the first test: a module first imported by a test or its
    # fixture would otherwise let that test's instances escape cleanup.
    _track_background_owners(import_modules=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    global _broader_fixture_depth
    broader = fixturedef.scope != "function"
    _broader_fixture_depth += broader
    try:
        yield
    finally:
        _broader_fixture_depth -= broader


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _owned_by_test
    _track_background_owners()
    owned = _owned_by_test = []
    try:
        yield
    finally:
        _owned_by_test = None
        for owner, captured, close in reversed(owned):
            try:
                close(owner, captured)
            except Exception:  # noqa: BLE001 -- best-effort cleanup of test doubles
                pass
        _track_background_owners()


@pytest.fixture
def mock_shortcut_listeners(monkeypatch):
    """Keep shortcut registration/routing real while replacing native listeners."""
    from unittest.mock import MagicMock
    from whisperstream import keyboard_shortcuts

    created = []

    def create_listener(**callbacks):
        listener = MagicMock(spec=["start", "stop", "suppress_event"])
        created.append(listener)
        return listener

    monkeypatch.setattr(keyboard_shortcuts, "Listener", create_listener)
    if keyboard_shortcuts.MouseListener is not None:
        monkeypatch.setattr(keyboard_shortcuts, "MouseListener", create_listener)
    return created


def _make_windows_udp_self_pipe(loop) -> None:
    """Create a Proactor wakeup pair without consuming TCP socket capacity."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        server.bind(("127.0.0.1", 0))
        client.bind(("127.0.0.1", 0))
        server.connect(client.getsockname())
        client.connect(server.getsockname())
        server.setblocking(False)
        client.setblocking(False)
    except BaseException:
        server.close()
        client.close()
        raise

    loop._ssock = server
    loop._csock = client
    loop._internal_fds += 1


@pytest.fixture(scope="session", autouse=True)
def _avoid_windows_asyncio_tcp_self_pipes():
    """Use UDP wakeup pairs for Windows event loops created during tests.

    CPython's Proactor loop normally calls ``socket.socketpair()``, whose
    Windows implementation creates a TCP loopback connection. The suite makes
    many short-lived loops through pytest-asyncio, ``asyncio.run``, AnyIO, and
    explicit TestClient contexts. Their correctly closed TCP pairs can still
    exhaust the host's socket capacity while Windows retains connection state.

    A connected UDP pair has the same wakeup semantics for the Proactor loop
    without allocating TCP state. Scope the override to pytest and restore the
    interpreter implementation when the session ends.
    """
    if os.name != "nt":
        yield
        return

    from asyncio import proactor_events

    original = proactor_events.BaseProactorEventLoop._make_self_pipe
    proactor_events.BaseProactorEventLoop._make_self_pipe = (
        _make_windows_udp_self_pipe
    )
    try:
        yield
    finally:
        proactor_events.BaseProactorEventLoop._make_self_pipe = original


@pytest.fixture(scope="session", autouse=True)
def _reuse_windows_testclient_portal():
    """Keep the full Windows suite off the finite loopback socket pool.

    An unentered Starlette ``TestClient`` creates and tears down an AnyIO
    blocking portal for every request. On Windows each asyncio portal owns a
    TCP-backed socket pair, so the complete source collection can hit
    ``WSAENOBUFS`` even though every individual test closes its request. One
    session-owned portal preserves the real ASGI path while giving the suite a
    single, bounded loopback owner that is closed before pytest exits.
    """

    if sys.platform != "win32":
        yield
        return

    import anyio

    original = TestClient._portal_factory
    with anyio.from_thread.start_blocking_portal(backend="asyncio") as portal:

        @contextmanager
        def shared_portal(client):
            if client.portal is not None:
                yield client.portal
            else:
                yield portal

        TestClient._portal_factory = shared_portal
        try:
            yield
        finally:
            TestClient._portal_factory = original


@pytest.fixture(scope="session", autouse=True)
def _isolate_whisperstream_data_dir(tmp_path_factory):
    """Redirect the app data dir to a throwaway temp dir for the whole test run.

    Several tests construct a real ``WhisperstreamApp`` (or ``TranscriptStore``)
    and exercise the recording/finalize path, which now persists each dictation
    to the encrypted ``TranscriptStore`` (the PR #183 persistence hook). The
    older of those tests predate the hook and never isolated the data dir, so
    without this fixture the hook writes test transcripts (e.g. "single shot
    result", "My secret password is hunter2") straight into the developer's REAL
    ``%LOCALAPPDATA%\\Whisperstream`` store, where they then surface in the
    installed app's Transcripts tab.

    Pinning ``WHISPERSTREAM_DATA_DIR`` to a session temp dir guarantees no test
    can touch real config / license / EULA / transcript state. Per-test
    ``monkeypatch.setenv`` overrides still win for their own duration.
    """
    data_dir = tmp_path_factory.mktemp("whisperstream-data")
    previous = os.environ.get("WHISPERSTREAM_DATA_DIR")
    os.environ["WHISPERSTREAM_DATA_DIR"] = str(data_dir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WHISPERSTREAM_DATA_DIR", None)
        else:
            os.environ["WHISPERSTREAM_DATA_DIR"] = previous


@pytest.fixture(scope="session", autouse=True)
def _reuse_testclient_portal_on_windows(
    _avoid_windows_asyncio_tcp_self_pipes,
):
    """Avoid exhausting Windows sockets through per-request event-loop churn.

    Starlette starts a new AnyIO portal for every request made by a TestClient
    that was not entered as a context manager. Each portal creates an asyncio
    loop, whose Windows self-pipe consumes a loopback socket pair. A full test
    run can churn through the available socket capacity even though every loop
    is closed correctly.

    Keep one portal for those context-free clients for the pytest session.
    Explicit ``with TestClient(...)`` blocks still use their own portal, so
    their lifespan startup and shutdown behavior remains unchanged.
    """
    if os.name != "nt":
        yield
        return

    original_portal_factory = TestClient._portal_factory
    with anyio.from_thread.start_blocking_portal() as shared_portal:

        @contextmanager
        def portal_factory(client):
            if client.portal is not None:
                yield client.portal
            else:
                yield shared_portal

        TestClient._portal_factory = portal_factory
        try:
            yield
        finally:
            TestClient._portal_factory = original_portal_factory


@pytest.fixture
def real_agent_plugin_refresh():
    """Opt a test into the real startup plugin refresh methods."""
    yield


@pytest.fixture(autouse=True)
def _inert_agent_plugin_startup_refresh(request, monkeypatch):
    """Keep server startup from running the user's real ``claude``/``codex`` CLIs.

    Startup refreshes every installed Whisperstream plugin through the
    provider CLIs, which act on the developer's real user-scope provider
    config. Server lifespans in tests make that refresh inert unless a test
    requests ``real_agent_plugin_refresh`` and fakes the CLI itself.
    """
    if "real_agent_plugin_refresh" in request.fixturenames:
        return
    from whisperstream.server import WhisperstreamServer

    async def _inert(self) -> None:
        return None

    monkeypatch.setattr(WhisperstreamServer, "_refresh_claude_plugin", _inert)
    monkeypatch.setattr(WhisperstreamServer, "_refresh_codex_plugin", _inert)


_AGENT_CLI_HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def _inside_temp(path, temp_root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(temp_root)
    except (OSError, TypeError, ValueError):
        return False


@pytest.fixture(autouse=True)
def _block_real_agent_cli(monkeypatch):
    """No test may run the developer's real ``claude`` or ``codex`` CLI.

    Plugin install and reconcile paths shell out to the provider CLI, which
    writes the real user-scope provider config. A leaked run once registered a
    ``whisperstream`` marketplace pointing at a pytest temp dir, and the
    installed app then refused to connect with ``marketplace_conflict``. Login
    shell PATH merging (``ensure_user_path``) makes such CLIs discoverable even
    when pytest's own PATH lacks them, so the guard sits at process creation.

    Stub executables under the temp dir are allowed, as are real CLIs whose
    provider home (``CLAUDE_CONFIG_DIR``/``CODEX_HOME``) points into it, which
    is how the opt-in ``WHISPERSTREAM_TEST_PLUGIN_CLI`` tests isolate them.
    Blocked spawns raise ``PermissionError``, an ``OSError`` the plugin code
    already reports as a CLI failure.
    """
    import subprocess
    import tempfile

    temp_root = Path(tempfile.gettempdir()).resolve()
    original_init = subprocess.Popen.__init__

    def guarded_init(self, *args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        argv = [command] if isinstance(command, (str, bytes, os.PathLike)) else list(command or ())
        program = kwargs.get("executable") or (argv[0] if argv else None)
        if program is not None and not kwargs.get("shell"):
            program = os.fsdecode(program)
            home_var = _AGENT_CLI_HOME_VARS.get(Path(program).stem.lower())
            if home_var and not _inside_temp(program, temp_root):
                home = (kwargs.get("env") or os.environ).get(home_var)
                if not (home and _inside_temp(home, temp_root)):
                    raise PermissionError(
                        f"Tests must not run the real {Path(program).name} CLI; fake the "
                        f"plugin CLI runner or point {home_var} into tmp_path."
                    )
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)


@pytest.fixture(autouse=True)
def _restore_whisperstream_log_propagation():
    """Let pytest's caplog see records emitted on the 'whisperstream' logger tree.

    Production sets ``propagate = False`` on the ``whisperstream`` logger
    (``whisperstream.logger.get_logger``) to avoid double-logging once its own
    handlers are attached. That flag is a process-wide side effect: as soon as
    any earlier test instantiates the app/server, it persists for the rest of
    the run. pytest's ``caplog`` handler lives on the root logger and only sees
    a record if it propagates up to root, so once propagation is off every
    ``caplog`` assertion on a ``whisperstream`` (or child) log line silently
    fails in full-suite order while passing in isolation.

    Restore propagation for the duration of each test, then put the original
    value back so production logging behavior is never changed.
    """
    wlogger = logging.getLogger("whisperstream")
    previous = wlogger.propagate
    wlogger.propagate = True
    try:
        yield
    finally:
        wlogger.propagate = previous


@pytest.fixture(autouse=True)
def _isolate_macos_protected_secret_items(monkeypatch):
    """Keep every implicit Darwin secret operation out of Security.framework.

    Redirecting ``HOME`` is not sufficient Keychain isolation: a missing
    redirected default Keychain can still cause macOS to present UI from
    ``SecItemAdd``. Pytest therefore uses a per-test memory store and replaces
    the native bridge with a fail-closed constructor. Fake-bridge adapter tests
    remain active; real Keychain validation belongs to the separately invoked,
    explicitly isolated protected-storage probe.
    """

    if sys.platform != "darwin":
        yield
        return

    import whisperstream.key_encryption as key_encryption
    import whisperstream.macos_protected_secret as macos_protected_secret
    import whisperstream.transcript_crypto as transcript_crypto
    from whisperstream.protected_secret_contract import (
        MACOS_KEYCHAIN_PREFIX,
        ProtectionAvailability,
        ProtectedSecretError,
        ProtectedSecretFailure,
        ProtectedSecretOperation,
    )

    class _BlockedSecurityFrameworkBridge:
        def __init__(self):
            raise RuntimeError("native Keychain access is disabled during pytest")

    class _MemoryStore:
        def __init__(self):
            self.items: dict[str, bytes] = {}

        def availability(self):
            return ProtectionAvailability.AVAILABLE

        def store(self, secret, *, purpose):
            del purpose
            reference = f"{MACOS_KEYCHAIN_PREFIX}{uuid.uuid4()}"
            self.items[reference] = bytes(secret)
            return reference

        def load(self, reference):
            try:
                return self.items[reference]
            except KeyError as error:
                raise ProtectedSecretError(
                    ProtectedSecretOperation.LOAD,
                    ProtectedSecretFailure.NOT_FOUND,
                ) from error

        def delete(self, reference):
            if reference not in self.items:
                raise ProtectedSecretError(
                    ProtectedSecretOperation.DELETE,
                    ProtectedSecretFailure.NOT_FOUND,
                )
            del self.items[reference]

    monkeypatch.setattr(
        macos_protected_secret,
        "_SecurityFrameworkBridge",
        _BlockedSecurityFrameworkBridge,
    )
    memory_store = _MemoryStore()
    monkeypatch.setattr(
        key_encryption,
        "get_protected_secret_store",
        lambda: memory_store,
    )
    monkeypatch.setattr(
        transcript_crypto,
        "get_protected_secret_store",
        lambda: memory_store,
    )
    try:
        yield
    finally:
        memory_store.items.clear()
@pytest.fixture
def accepted_eula(monkeypatch):
    """Make legacy app tests explicit accepted-state regressions.

    Production now fails closed when the isolated test data directory has no
    acceptance ledger. Existing app tests that exercise post-onboarding paths
    opt into this fixture explicitly. Denial tests deliberately do not inherit
    it, so a missing or revoked acceptance cannot be masked globally.
    """
    import importlib.util

    import whisperstream.main as main_mod
    import whisperstream.server.app as server_mod
    from whisperstream.eula import AcceptanceGrant, AcceptanceGrantReason

    grant = AcceptanceGrant(
        True,
        AcceptanceGrantReason.ACCEPTED,
        "1.3",
        "a" * 64,
        "b" * 64,
    )
    monkeypatch.setattr(main_mod, "get_acceptance_grant", lambda: grant)
    monkeypatch.setattr(
        main_mod,
        "acceptance_grant_is_current",
        lambda candidate: candidate == grant,
    )
    monkeypatch.setattr(server_mod, "get_acceptance_grant", lambda: grant)
    monkeypatch.setattr(
        server_mod,
        "acceptance_grant_is_current",
        lambda candidate: candidate == grant,
    )
    # The development endpoints are not part of source releases.
    if importlib.util.find_spec("whisperstream.dev_endpoints") is None:
        return
    import whisperstream.dev_endpoints as dev_endpoints_mod

    monkeypatch.setattr(dev_endpoints_mod, "get_acceptance_grant", lambda: grant)
    monkeypatch.setattr(
        dev_endpoints_mod,
        "acceptance_grant_is_current",
        lambda candidate: candidate == grant,
    )

# Auto-load .env from the repo root so test env vars (e.g.
# WHISPERSTREAM_TEST_PROVIDER, WHISPERSTREAM_TEST_API_KEY) are available
# without having to source them into the shell. Missing .env is a no-op.
try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    _REPO_ROOT = Path(__file__).resolve().parents[1]
    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    # python-dotenv is in the [dev] extra; without it, fall back to whatever
    # is already in os.environ.
    pass

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def configure_test_model_storage() -> bool:
    """Point the isolated test config at the installed development model cache."""

    data_dir = Path(os.environ["WHISPERSTREAM_DATA_DIR"])
    model_base = Path(
        os.environ.get(
            "WHISPERSTREAM_TEST_MODEL_BASE",
            str(Path(os.environ.get("LOCALAPPDATA", data_dir)) / "Whisperstream-dev"),
        )
    )
    model_dir = model_base / "models" / "nemo-parakeet-tdt-0.6b-v3-int8"
    required = (
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "joiner.int8.onnx",
        "tokens.txt",
    )
    if not all((model_dir / name).is_file() for name in required):
        return False
    escaped = str(model_base).replace("\\", "\\\\").replace('"', '\\"')
    (data_dir / "config.toml").write_text(
        f'model_storage_dir = "{escaped}"\n',
        encoding="utf-8",
    )
    return True


def configure_test_acceptance_authority(app) -> None:
    """Pin a real-app fixture to one synthetic, content-free accepted grant."""
    from whisperstream.eula import AcceptanceGrant, AcceptanceGrantReason

    grant = AcceptanceGrant(
        True,
        AcceptanceGrantReason.ACCEPTED,
        "1.4",
        "a" * 64,
        "b" * 64,
    )
    app._acceptance_grant_provider = lambda: grant
    app._acceptance_grant_revalidator = lambda candidate: candidate == grant


def get_fixture_pairs() -> list[tuple[Path, Path]]:
    """Return list of (wav_path, txt_path) pairs from tests/fixtures/."""
    pairs = []
    for wav in sorted(FIXTURES_DIR.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if txt.exists():
            pairs.append((wav, txt))
    return pairs


@pytest.fixture(scope="session")
def whisperstream_client():
    """Create a TestClient backed by a real WhisperstreamApp + WhisperstreamServer.

    The model is loaded once per session (~10-15s). If the model is not
    available, the entire session is skipped.
    """
    # Keep config, logs, history, and diagnostics in the autouse temporary data
    # directory while allowing this native-fixture test to read the dedicated
    # development model cache. CI without that cache retains the existing skip.
    configure_test_model_storage()

    # This session-scoped real app cannot consume the function-scoped
    # ``accepted_eula`` monkeypatch used by ordinary tests. Persist a synthetic
    # exact acceptance inside the already isolated test data directory instead.
    from whisperstream.eula import load_eula_policy_status, write_acceptance

    policy = load_eula_policy_status()
    if not policy.valid:
        pytest.fail("bundled EULA policy must be valid for native fixture tests")
    write_acceptance(
        policy.terms_version or "",
        policy.terms_sha256 or "",
        "test",
    )

    from whisperstream.main import WhisperstreamApp
    from whisperstream.server import WhisperstreamServer

    app = WhisperstreamApp(
        verbose=False,
        headless=True,
        file_only=sys.platform == "darwin",
    )
    configure_test_acceptance_authority(app)
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)

    if app.stt_engine is None:
        if sys.platform == "darwin":
            pytest.fail("native macOS Parakeet worker/model is required for this run")
        pytest.skip("Parakeet model not available")

    client = TestClient(server.fastapi)
    setattr(client, "_whisperstream_app", app)
    try:
        yield client
    finally:
        app.cleanup()


@pytest.fixture(scope="session")
def vbcable_devices():
    """Auto-detect VB-Cable virtual audio devices via sounddevice.

    Returns a dict with input (capture) and output (playback) device info.
    Skips the test session if VB-Cable is not installed.

    - "input" is what Whisperstream captures from ("CABLE Output", an input device).
    - "output" is what the test plays audio to ("CABLE Input", an output device).
    Prefers the MME host API for the output device to match how _resolve_device
    works for the input side. Falls back to any host API.
    """
    import sounddevice as sd

    devices = sd.query_devices()

    # Find "CABLE Output" as an input device (what Whisperstream records from)
    input_device = None
    for info in devices:
        if "CABLE Output" in info["name"] and info["max_input_channels"] > 0:
            input_device = info
            break

    if input_device is None:
        pytest.skip("VB-Cable not installed (CABLE Output input device not found)")

    # Find "CABLE Input" as an output device (what the test plays audio to)
    # Prefer MME host API, fall back to any.
    output_mme = None
    output_any = None
    for info in devices:
        if "CABLE Input" in info["name"] and info["max_output_channels"] > 0:
            api = sd.query_hostapis(info["hostapi"])
            if "MME" in api["name"] and output_mme is None:
                output_mme = info
            if output_any is None:
                output_any = info

    output_device = output_mme if output_mme is not None else output_any

    if output_device is None:
        pytest.skip("VB-Cable not installed (CABLE Input output device not found)")

    return {
        "input_name": input_device["name"],
        "input_index": input_device["index"],
        "output_name": output_device["name"],
        "output_index": output_device["index"],
    }
