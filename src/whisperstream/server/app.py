"""FastAPI + WebSocket server for Whisperstream.

Exposes the WhisperstreamApp engine over REST (config CRUD) and WebSocket
(real-time events: recording state, audio levels, transcription results).
"""

from __future__ import annotations

import asyncio
import collections
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import difflib
import inspect
import json
import logging
import os
import platform
import random
import re
import secrets
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Dict, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from ..audio_limits import MAX_AUDIO_FILE_BYTES, AudioLimitExceeded, TranscriptionBusy
from pydantic import BaseModel, Field, StringConstraints

from .middleware import (
    _EULA_BLOCK_CODE,
    _is_allowed_websocket_origin,
    _is_trusted_hud_command_request,
    install_middleware,
)
from .lifecycle import run_server_loop
from ..managed_policy import history_disabled_by_policy, HISTORY_DISABLED_MESSAGE
from .import_operation import ImportOperation
from .benchmark_operation import BenchmarkOperation
from .gguf_download import RegisteredGGUFDownload
from .model_relocation import ModelRelocation, _RESIDENT_MODELS_RESTART_REQUIRED
from .routes.local_models import register_local_model_status_routes
from .routes.imports import register_import_route
from .diagnostics import DiagnosticLogs
from .routes.diagnostics import register_diagnostic_routes
from .websocket import DictationStateCache, WebSocketDelivery, _is_content_ws_event
from .stats import UsageStats
from .routes.enhancement import register_enhancement_execution_routes, register_enhancement_catalog_routes
from .routes.consent import register_consent_status_route, register_consent_mutation_routes
from .routes.config import register_config_routes
from .config_payload import _config_payload, _coerce_enhancement_under_lockdown
from .routes.transcripts import register_transcript_list_route, register_transcript_record_routes
from .routes.stats import register_stats_routes
from .routes.system import register_application_inventory_routes, register_version_route
from .routes.speech import register_speech_routes
from .routes.agent_setup import register_agent_setup_routes
from .routes.permissions import register_permission_routes
from ..capture import list_input_devices
from ..agent_plugin_runtime import HOOK_TOKEN_HEADER, plugin_identity
from ..enhancement_security import (
    enhancement_is_admitted, require_enhancement_admission, EnhancementAdmissionError,
)
from ..config_manager import (
    CLAUDE_REPLY_HINT_RETIRE_AFTER,
    WhisperstreamConfig,
    get_app_dir,
    get_local_gpu_verdict,
    get_log_dir,
)
from ..model_downloader import (
    DOWNLOAD_REGISTRY,
    ModelId,
    delete_model,
    delete_unused_stt_models,
    is_present,
    model_dir,
    model_id_for_parakeet,
    stt_artifact_inventory,
)
from ..model_lifecycle import PlatformModelLifecycleFactory
from ..model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelFamily,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelState,
)
from ..history_security import PlatformHistorySecurityFactory
from ..history_security_contract import (
    HistoryFailure,
    LicenseSnapshot,
    LicenseState,
    SecurityAvailability,
)
from ..review_manager import ReviewManager
from ..screen_reader_monitor import ScreenReaderMonitor
from ..startup import is_startup_enabled, is_startup_supported, set_startup_enabled
from ..temp_lifecycle import (
    close_then_unlink,
    create_owned_temp_file,
    safe_source_suffix,
)
from .. import _build_flags
from .. import lockdown_keystore
from .. import lockdown_ack
from ..access_log import log_access
from ..lockdown_state import LockdownState
from ..protected_secret import get_protected_secret_store
from ..stt_errors import SttFailure
from ..logger import reconfigure_logging
from ..managed_session_auth import auth as managed_session_auth
from ..safe_diagnostics import SafeDiagnosticSink
from ..transcript_store import run_retention_sweep
from .. import history_recovery
from ..password_rule import check_new_password
from ..protected_secret_contract import ProtectedSecretError, ProtectedSecretFailure
from ..transcript_crypto import delete_data_key, save_data_key
from ..eula import (
    AcceptanceGrant,
    AcceptanceGrantReason,
    acceptance_grant_is_current,
    get_acceptance_grant,
)

# NOTE: claude_hud is an agent-speech experimental module. It is imported
# LAZILY inside the gated endpoints (see _experimental_enabled) so the module
# stays excludable via Nuitka --nofollow-import-to in production builds.

_AGENT_COMPLETION_EVENT_VERSION = 1
_AGENT_RESPONSE_QUEUE_EVENT_VERSION = 1
_AGENT_INTERACTION_EVENT_VERSION = 1
_MAX_AGENT_SESSION_HANDLES = 400
_BAKEOFF_EVENT_VERSION = 1
_MIN_BAKEOFF_PAIRS = 2
_MAX_BAKEOFF_PAIRS = 8
_MAX_BAKEOFF_MODELS = 4
_ENGINE_GENERATION_HEADER = "x-whisperstream-engine-generation"
# Carries the wrapper-minted per-session correlation token (Task 2.4),
# present only for `whisperstream claude`-wrapped sessions: the wrapper mints
# a random token per run (Task 3.1), exports it into the child's environment
# as WHISPERSTREAM_SESSION_TOKEN, and forward-hook.ps1 echoes it here on every
# hook it forwards. Distinct from managed_session_auth.HEADER_NAME, which
# gates /api/managed-sessions/* with the per-run capability token: this is a
# different, per-session value that travels alongside the runtime hook credential
# on the fail-open /api/claude-hook path as a header, never a body field, so the
# existing payload allowlist and its tests stay untouched.
_MANAGED_SESSION_TOKEN_HEADER = "x-whisperstream-managed-session-token"

# How often the reply long-poll (Task 3.3) re-checks the pending slot while it
# holds. Small enough that a dictated reply reaches the wrapper promptly, large
# enough that an idle wrapper costs nothing measurable.

_MISSING_CONFIG_VALUE = object()


class ResidentLlamaConflictError(RuntimeError):
    """A stable, content-free conflict blocks resident model acquisition."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

# Inbound messages that can reveal or mutate retained Agent/Claude content.
# Acknowledgement, defer, shortcut-stop, speech-stop, and other discard/cleanup
# commands deliberately remain available while acceptance is blocked.
_CONTENT_WS_MESSAGE_TYPES = frozenset(
    {
        "agent.interaction.engage",
        "agent.question.answer",
        "agent.permission.decide",
        "agent.permission.allow",
        "agent.permission.deny",
        "agent.draft.update",
        "agent.draft.submit",
        "agent.draft.cancel",
    }
)

# ---------------------------------------------------------------------------
# Software version — read once at import from src-tauri/tauri.conf.json.
# Cached at module scope so every EULA endpoint gets the same value without
# re-reading the file on every request.
# ---------------------------------------------------------------------------

def _read_software_version() -> str:
    """Resolve the application version for legal record-keeping and API responses.

    Order:
    1. ``whisperstream._version.__version__`` if it's been overwritten by
       ``scripts/build.sh`` (i.e. not the sentinel ``0.0.0+dev``). This is the
       only path that works inside a Nuitka onefile build, where the repo
       layout used by step 2 isn't reachable from ``__file__``.
    2. Parse ``src-tauri/tauri.conf.json`` relative to the repo root. Works in
       dev runs (``uv run python -m whisperstream.main``).
    3. Fall back to ``"0.0.0"`` so tests don't hard-fail when neither path is
       available.
    """
    try:
        from .._version import __version__ as embedded_version
        if embedded_version and embedded_version != "0.0.0+dev":
            return embedded_version
    except Exception:
        pass
    try:
        repo_root = Path(__file__).resolve().parents[3]
        conf_path = repo_root / "src-tauri" / "tauri.conf.json"
        data = json.loads(conf_path.read_text(encoding="utf-8"))
        ver = data.get("version", "")
        if ver and isinstance(ver, str):
            return ver
    except Exception:
        pass
    return "0.0.0"


_SOFTWARE_VERSION: str = _read_software_version()


# ---------------------------------------------------------------------------
# Dev-tools env-var gate.  Set WHISPERSTREAM_DEV_TOOLS=1 to enable the
# /api/dev/* surface (Fixture Test tab in the frontend).  With the variable
# unset or set to anything other than "1", every endpoint under /api/dev/*
# (except the discovery probe /api/dev/enabled) returns 404.
#
# Experimental gate. Claude HUD + Agent Speak shipped in 1.3.0 and
# were taken off this gate. Transcript Re-enhance now uses it for its backend
# route and frontend action. Keep the strict-"1" semantics of the dev-tools gate
# so this unfinished surface stays opt-in. The always-on discovery probe
# /api/experimental/enabled reports EXPERIMENTAL_AVAILABLE and
# _experimental_enabled(); production builds compile the flag to False, so it
# answers {"enabled": false}.
# ---------------------------------------------------------------------------

# How long a Codex plugin status read is reused by non-refresh polls.
CODEX_STATUS_REUSE_SECONDS = 30

_DEV_TOOLS_ENV = "WHISPERSTREAM_DEV_TOOLS"
_EXPERIMENTAL_ENV = "WHISPERSTREAM_EXPERIMENTAL"

# Minimum free space (MB) required to download the Parakeet ASR bundle.
# Covers the fp16 model (~1 GB) plus the int8 variant plus extraction headroom.
PARAKEET_REQUIRED_MB = 1500

def _dev_tools_enabled() -> bool:
    """Return True only when WHISPERSTREAM_DEV_TOOLS is exactly "1".

    Mirrors the strict-set semantics of ``_DEV_OVERRIDE_ENV`` in
    ``license_manager.py`` — no truthy-ish acceptance of "true" / "yes" / etc.
    """
    return os.environ.get(_DEV_TOOLS_ENV, "").strip() == "1"


# Dev-only latency simulator (Task 1.7). Lets a developer pad ``LLMEnhancer.enhance()`` calls to a target tok/s so
# the tri-state benchmark thresholds can be exercised without real slow
# hardware. IN-MEMORY ONLY -- never read from or written to config.toml (a
# forgotten throttle must not survive a restart).
_SIMULATED_TPS_ENV = "WHISPERSTREAM_SIMULATED_TPS"


def _parse_simulated_tps_env() -> Optional[float]:
    """Parse WHISPERSTREAM_SIMULATED_TPS into a positive float, or None.

    Seeded ONCE at ``WhisperstreamServer`` construction; unset, empty,
    non-numeric, or <=0 all mean "off" (None). Afterward, only the
    optional ``/api/dev/simulated-tps`` dev-tools routes (Nuitka-excluded
    from the production binary) mutate the live value.
    """
    raw = os.environ.get(_SIMULATED_TPS_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _experimental_enabled() -> bool:
    """Return True only when WHISPERSTREAM_EXPERIMENTAL is exactly "1".

    The runtime half of the agent-speech experimental gate; mirrors
    ``_dev_tools_enabled()``'s strict-"1" semantics (no "true"/"yes" acceptance).
    The compile-time half is ``_build_flags.EXPERIMENTAL_AVAILABLE``; gated code
    checks ``_build_flags.EXPERIMENTAL_AVAILABLE and _experimental_enabled()``.
    """
    return os.environ.get(_EXPERIMENTAL_ENV, "").strip() == "1"


class StartupBody(BaseModel):
    enabled: bool


class ActivateBody(BaseModel):
    key: str


def _wipe_transcript_files(data_dir: Path) -> None:
    """Ordinarily remove the encrypted transcript index + recordings directly.

    A FLOOR for the forgot-password reset when the live ``TranscriptStore`` is
    unavailable (init failure) or its row-level ``clear()`` failed — so the
    ciphertext becomes cryptographically inaccessible even without the store
    object. This ordinarily removes app-managed files and the key envelope; it
    never decrypts. SQLite deletion and filesystem unlink do not physically
    overwrite storage and are not secure erasure. Best-effort and never raises.
    When the store IS live it
    holds ``transcripts.db`` open (Windows file lock), so the store's own
    ``clear()`` is the correct path there and this is a no-op fallback.
    """
    import shutil  # noqa: PLC0415

    for name in ("transcripts.db", "transcripts.db-wal", "transcripts.db-shm"):
        try:
            (data_dir / name).unlink(missing_ok=True)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "lockdown reset file delete failed error_type=%s",
                type(exc).__name__,
            )
    recordings = data_dir / "recordings"
    if recordings.exists():
        shutil.rmtree(recordings, ignore_errors=True)


class EulaAcceptBody(BaseModel):
    eula_version: str
    eula_sha256: str


class EulaDeclineAmendmentBody(BaseModel):
    declined_version: str
    pinned_version: str


class ReviewBody(BaseModel):
    rating: int = Field(ge=1, le=5)
    customer_email: str


class FeedbackBody(BaseModel):
    feedback: str
    customer_email: str


class DismissBody(BaseModel):
    reason: Literal["later", "not-now", "never", "github"]


class LocalLLMDownloadBody(BaseModel):
    model_id: str
    recovery: bool = False


class BakeoffPairBody(BaseModel):
    model_id: str = ""
    profile_id: str = ""


class BakeoffStartBody(BaseModel):
    selections: list[BakeoffPairBody] = Field(default_factory=list)


class BakeoffRetryBody(BaseModel):
    pair_index: int = -1


class SttDeleteBody(BaseModel):
    model_id: str | None = None
    # Transitional compatibility for the pre-exact picker. New callers must
    # send ``model_id`` so Parakeet v2 and v3 remain independently addressable.
    model: str | None = None


# --- Lockdown Mode request bodies ---
# NEVER log any field below: password / recovery_code are secrets. Validation
# (min length, presence of a secret) happens in the handlers, not via pydantic
# constraints, so the handlers can return the project's structured error shapes.


class LockdownAcknowledgeBody(BaseModel):
    # The bundled acknowledgment VERSION the user just accepted (owned by the
    # frontend, next to the Step-1 copy). Persisted with a UTC timestamp +
    # software version so a later material copy change (version bump) re-shows
    # the disclosure. Not a secret, but carries no PHI either.
    ack_version: str


class LockdownEnableBody(BaseModel):
    # Password is OPTIONAL. Present (non-empty) -> keep-history path: wrap the
    # data key under the password + a fresh recovery code (an envelope is
    # written). Absent/empty -> no-envelope (history-off) path: mark lockdown on
    # WITHOUT wrapping a key, minting no recovery code and writing no
    # lockdown.key.json. NEVER log this field.
    keychain_consent: bool = False
    password: str | None = None
    # Optional idle auto-lock window persisted with lockdown_mode. Omitted ->
    # keep the config's current lockdown_idle_lock_seconds (default 300).
    idle_lock_seconds: int | None = None
    # Optional transcript-history settings persisted alongside lockdown_mode.
    # The keep-history path persists whatever is provided (None -> leave as-is);
    # the no-envelope path forces history_enabled=False regardless (the intended
    # pairing — nothing needs a password-wrapped key when history is off).
    history_enabled: bool | None = None
    save_audio: bool | None = None
    transcript_retention_days: int | None = None


class LockdownDisableBody(BaseModel):
    # Password is OPTIONAL. A keep-history (envelope-backed) lockdown still
    # requires the correct password to unwrap + re-seal. A no-envelope
    # (history-off) lockdown has no key to unwrap, so disable accepts an
    # empty/absent password in that case. NEVER log this field.
    password: str | None = None


class LockdownResetBody(BaseModel):
    # Typed confirmation echoed from the destructive-reset dialog. Must equal
    # "DELETE" (case-insensitive) — a server-side guard so a stray or empty POST
    # can never wipe the encrypted history. Not a secret and carries no content.
    confirm: str = ""


class LockdownUnlockBody(BaseModel):
    # Exactly one of these carries the secret. password is the day-to-day gate;
    # recovery_code is the one-time escape hatch (its own backoff accounting).
    password: str | None = None
    recovery_code: str | None = None


class LockdownChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class LockdownViewRecoveryBody(BaseModel):
    password: str


class HistoryRecoveryPasswordBody(BaseModel):
    # SECRET — DO NOT LOG. None or empty removes the recovery password.
    password: Optional[str] = None
    # Confirm only: names the key the client showed (from setup/regenerate).
    generation: Optional[str] = None


class HistoryRecoveryUnlockBody(BaseModel):
    # SECRETS — DO NOT LOG. Exactly one must be present.
    recovery_key: Optional[str] = None
    password: Optional[str] = None


class HistoryResetBody(BaseModel):
    confirm: str = ""


class RelocateCheckBody(BaseModel):
    destination: str


class RegisteredRelocateBody(BaseModel):
    destination_token: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ManagedSessionRegisterBody(BaseModel):
    token: str
    pid: int | None = None
    cwd: str | None = None


class ManagedSessionRegisterV2Body(ManagedSessionRegisterBody):
    agent: Literal["claude", "codex"]


class ManagedSessionDeregisterBody(BaseModel):
    token: str


class ManagedSessionReplyBody(BaseModel):
    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # ``text`` is a dictated reply. Never log this body or any field of it.
    # ========================================================================
    text: str


def _spi_screen_reader_flag_probe() -> bool:
    from ..main import _spi_screen_reader_flag  # noqa: PLC0415

    return _spi_screen_reader_flag()


def _live_screen_reader_active() -> bool:
    from ..main import screen_reader_active  # noqa: PLC0415

    return screen_reader_active()


class WhisperstreamServer:
    """Wraps a WhisperstreamApp instance and serves it over HTTP + WebSocket."""

    def __init__(
        self,
        app,  # WhisperstreamApp — imported lazily to avoid circular deps
        *,
        port: int = 38420,
        logger: Optional[logging.Logger] = None,
        register_hotkeys: bool = True,
        owner_pid: int | None = None,
        engine_generation: str | None = None,
    ) -> None:
        self._app = app
        self._port = port
        self._agent_hook_token = secrets.token_hex(32)
        self._logger = logger or logging.getLogger(__name__)
        self._register_hotkeys = register_hotkeys
        shortcut_recovery = getattr(app, "_shortcut_recovery", None)
        if shortcut_recovery is not None:
            shortcut_recovery.enabled = register_hotkeys
        self._owner_pid = owner_pid
        self._engine_generation = engine_generation or None
        application_context = (
            getattr(app, "application_context", None)
            if "application_context" in getattr(app, "__dict__", {})
            else None
        )
        if application_context is None:
            from ..app_context import create_application_context_adapter

            application_context = create_application_context_adapter()
            try:
                app.application_context = application_context
            except Exception:  # noqa: BLE001 - lightweight test doubles may be frozen
                pass
        self._application_context = application_context
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Latest content-free lifecycle snapshot for reconnecting WebViews.
        # broadcast() may run on hotkey/finalizer threads, so cache access is
        # guarded independently from the asyncio client set.
        self._dictation_state = DictationStateCache()
        self._socket_delivery = WebSocketDelivery(
            logger=self._logger,
            acceptance_is_current=self._acceptance_is_current,
            bakeoff_is_current=lambda grant: self._bakeoff_authority_error(grant) is None,
        )
        # Event-driven screen-reader state, run only while the persistent
        # recording control (its only idle consumer) is enabled. The SPI probe
        # resolves through main at call time so tests patch one definition.
        self._screen_reader_monitor = ScreenReaderMonitor(
            flag_probe=_spi_screen_reader_flag_probe,
            on_change=self._broadcast_screen_reader_state,
            on_degraded=self._screen_reader_monitor_degraded,
            logger=self._logger,
        )
        snapshot_provider = getattr(self._app, "dictation_state_snapshot", None)
        if callable(snapshot_provider):
            try:
                initial_state = snapshot_provider()
            except Exception:
                initial_state = None
            if isinstance(initial_state, dict):
                self._dictation_state.retain(initial_state)
        self.stats = UsageStats()
        app_safe_diagnostics = getattr(self._app, "safe_diagnostics", None)
        self._safe_diagnostics = (
            app_safe_diagnostics
            if isinstance(app_safe_diagnostics, SafeDiagnosticSink)
            else SafeDiagnosticSink(get_log_dir())
        )
        self._diagnostic_logs = DiagnosticLogs(
            logger=self._logger,
            safe_diagnostics=self._safe_diagnostics,
            software_version=_SOFTWARE_VERSION,
            model_loaded=lambda: self._app.stt_engine is not None,
        )
        self.review_manager = ReviewManager(logger=self._logger)

        # Model download cancellation state. Both attributes are set when a
        # download thread is spawned via /api/download/start and cleared in the
        # worker's finally clause when it exits (success, error, or cancel).
        self._download_cancel_event: threading.Event | None = None
        self._download_thread: threading.Thread | None = None
        self._model_lifecycle = PlatformModelLifecycleFactory().create()
        self._model_generation_lock = threading.Lock()
        self._model_operation_generation = 0
        # The API-wide _download_thread slot prevents cross-family background
        # work. Reuse the app-owned lock so deferred startup construction also
        # serializes with STT prepare/activation, user deletion, and relocation.
        app_operation_lock = getattr(
            self._app,
            "_stt_model_operation_lock",
            None,
        )
        if not isinstance(app_operation_lock, type(threading.Lock())):
            app_operation_lock = threading.Lock()
            # Lightweight embedding/test apps may predate the app-owned guard.
            # Attach the fallback so any later startup load can join the same
            # lifecycle instead of silently creating another lock domain.
            self._app._stt_model_operation_lock = app_operation_lock
        self._model_operation_lock = app_operation_lock
        # Compatibility alias for Phase 1 callers/tests. Both names point at
        # the same lock; there is only one model-operation critical section.
        self._stt_activation_lock = self._model_operation_lock

        # Import batches are deliberately single-flight and separately owned
        # from dictation persistence. The event permanently revokes the active
        # batch on Lockdown entry or shutdown, even if Lockdown is later lifted.
        # Concrete voice engine owned by the current TTS download worker. The
        # TTS download shares the single-download guard above with every other
        # model family; retaining the engine identity keeps status and every
        # progress event unambiguous without loading either speech engine.
        self._tts_download_engine: Literal["kokoro", "supertonic"] | None = None

        # GPU benchmark state. Set when a benchmark thread is spawned via
        # /api/enhancement/benchmark/start and cleared in the worker's finally
        # clause. The benchmark's download leg reuses _download_thread /
        # _download_cancel_event so the existing local-download/cancel endpoint
        # aborts a mid-download benchmark.

        # Set True by _maybe_prepare_llama_server when a launch-time GPU-set
        # change invalidated the cached verdict. The frontend reads it once via
        # GET /api/enhancement/benchmark/verdict (read-and-clear) and auto-runs
        # the benchmark with progress ("turning the toggle back on").

        # Set by run() so /api/shutdown can flip should_exit on the live uvicorn
        # server. None until run() begins.
        self._uvicorn_server: Optional[Any] = None

        # Explicit permission actions remain lazy. Ordinary construction,
        # startup, hydration, and GET /api/permissions must not import the
        # prompt-capable Darwin adapter or mint a one-shot intent.
        self._permission_action_service: Optional[Any] = None
        self._permission_action_service_lock = threading.Lock()

        # Most-recent-prompt speech focus. This is only a selective attention
        # policy for routine speech, not a registry of active agent sessions.
        # Lazily constructed inside the gated /api/agent-event handler because
        # agent_events is excluded from production builds.
        self._agent_speech_focus: Optional[Any] = None

        # Bounded, memory-only visual completion retention. Constructed only
        # when the first display-eligible response arrives. Empty snapshots and
        # stray acknowledgements must not load experimental state.
        self._agent_activity: Optional[Any] = None
        from ..agent_conversation import AgentConversationStore
        self._agent_conversations = AgentConversationStore()

        # In-memory registry of `whisperstream claude`-wrapped sessions
        # (Task 2.2). Lazily constructed so a build/run that never registers a
        # managed session never loads this experimental state.
        self._managed_sessions: Optional[Any] = None
        # Mod-route bookkeeping that outlives a reaped routing record: turns the
        # Stop hook already delivered, and per-session mod poll timing.
        self._claude_hook_delivered: Optional[Any] = None
        self._claude_mod_poll_gaps: Optional[Any] = None

        # Memory-only HUD interaction state and scoped compose leases. These
        # remain lazy so production builds can exclude experimental modules.
        self._agent_session_handles: collections.OrderedDict[object, str] = (
            collections.OrderedDict()
        )
        self._agent_session_handles_lock = threading.RLock()
        self._agent_compose_leases: Optional[Any] = None
        self._claude_interactions: Optional[Any] = None
        self._claude_response_coordinator: Optional[Any] = None
        self._claude_question_coordinator: Optional[Any] = None
        # Provider-neutral aliases are canonical for new integrations. The
        # Claude-prefixed attributes remain synchronized for rolling tests and
        # older internal callers.
        self._agent_interactions: Optional[Any] = None
        self._agent_response_coordinator: Optional[Any] = None
        self._agent_question_coordinator: Optional[Any] = None
        # Content-free ordering survives teardown of the content owners.
        self._agent_snapshot_lock = threading.Lock()
        self._agent_snapshot_generation = 0
        self._agent_snapshot_clearing = False
        self._agent_snapshot_revisions = {"question": 0, "response": 0, "interactions": 0}
        self._agent_snapshot_clear_revisions = {"question": 0, "response": 0, "interactions": 0}
        self._managed_expiry_timer: asyncio.TimerHandle | None = None
        self._claude_activation_registry: Optional[Any] = None
        self._codex_activation_registry: Optional[Any] = None
        self._claude_plugin_status: dict[str, Any] = {
            "ok": True,
            "status": "unknown",
            "reload_required": False,
        }
        from ..agent_integration import create_agent_integration_adapters

        (
            self._agent_plugin_adapter,
            self._agent_hook_adapter,
        ) = create_agent_integration_adapters(reconcile_cli=self._claude_uses_cli)
        self._agent_integration_lock = threading.Lock()
        self._agent_integration_generation = 0
        # Last published master Agents state, so a config save can tell a
        # pause or resume transition apart from an unrelated setting change.
        self._agents_enabled_published = (
            getattr(self._app.config, "agents_enabled", True) is not False
        )
        self._codex_plugin_status: dict[str, Any] = {
            "ok": True,
            "status": "unknown",
            "codex_cli_available": False,
            "codex_cli_supported": False,
            "trust_required": False,
        }
        # Hotkey-routed reply capture is recorded once, on its draft in the
        # reply owner. This is association only. Native Enter/Escape handlers are installed
        # by WhisperstreamApp's ordinary hotkey-registration path so server
        # construction remains safe before permission UI hydration.
        # The single reply owner (agent_reply_desk) and the exact targets a
        # global Enter/Escape claimed at key-down, consumed by its callback.
        self._reply_desk: Optional[Any] = None
        self._reply_hint_save_lock = threading.Lock()
        # Each claim pairs with exactly one queued key callback, in order.
        # A revision of None claims a draft still being transcribed.
        self._claimed_enters: collections.deque[tuple[str, int | None]] = collections.deque()
        self._claimed_escapes: collections.deque[tuple[str, Optional[str]]] = collections.deque()
        # Pickup-window timers for replies handed to a wrapper, by send id.
        self._reply_withdrawals: Dict[str, Any] = {}
        # Reading-history turns for replies in flight, written only once the
        # wrapper collects them (transcript-class, memory-only).
        self._unsettled_turns: Dict[str, tuple[str, str, str]] = {}
        self._setup_delivery_preview: Optional[Dict[str, bool]] = None
        speech_controller = getattr(self._app, "speech_controller", None)
        if speech_controller is not None:
            speech_controller._speech_enabled_provider = (
                self._effective_agent_speech_enabled
            )
        reply_router_setter = getattr(
            self._app, "set_agent_reply_router", None
        )
        if callable(reply_router_setter):
            reply_router_setter(self)


        # Dev-only latency simulator (Task 1.7). IN-MEMORY ONLY, never
        # persisted to config.toml. Seeded once here from
        # WHISPERSTREAM_SIMULATED_TPS; afterward only the
        # optional /api/dev/simulated-tps routes (Nuitka-excluded from prod)
        # mutate it. LLMEnhancer needs to read this value without a hard
        # dependency on the dev-tools module, so we wire a getter into the app: the
        # app owns a `_simulated_tps_provider` callable (default no-op, set in
        # main.py) and LLMEnhancer captures `lambda: self._simulated_tps_provider()`
        # -- late-bound, so overwriting the app's attribute here (AFTER the app
        # + its first LLMEnhancer already exist) takes effect immediately, and
        # keeps working across every reload_config() that reconstructs
        # LLMEnhancer with a fresh config.
        self._simulated_tps: Optional[float] = _parse_simulated_tps_env()
        self._app._simulated_tps_provider = lambda: self._simulated_tps
        self._app.set_local_warm_requester(self._request_local_warm)

        # --- Lockdown Mode runtime state ---
        # Backend-managed lock + exponential-backoff state for the transcript
        # surface (NEVER persisted to config.toml). The held data key lives only
        # in this object's memory while unlocked. On startup, if lockdown is
        # already enabled on disk, the surface starts LOCKED (no key held) and
        # stays locked until the user enters the password. If lockdown is not
        # enabled, the surface is conceptually "unlocked" (no gate); we leave the
        # default locked=True but the status endpoint reports locked only when
        # lockdown is actually enabled, so a non-lockdown install is never gated.
        # F6: persist the failed-attempt / lockout counters to a backend-managed,
        # provider-protected state file (``lockdown.backoff`` — NOT config.toml, never
        # user-editable) so the escalating unlock backoff survives a backend
        # restart. LockdownState loads it on init and rewrites it on every
        # failure/success; the file holds only counters + wall-clock deadlines,
        # no key material or secret.
        self._lockdown_state = LockdownState(
            state_path=self._lockdown_data_dir() / "lockdown.backoff"
        )
        # Standard-history recovery attempts get their own persisted backoff,
        # separate from Lockdown's, with the same tiers.
        self._history_recovery_state = LockdownState(
            state_path=self._lockdown_data_dir() / "history_recovery.backoff"
        )
        self._history_recovery_state._locked = False
        # Repair a crash between writing the Lockdown envelope and removing the
        # other key copies: under Lockdown nothing else may unwrap the data key.
        # The app runs the full repair (including the protected key) at startup
        # before history loads; this covers servers constructed without it.
        history_recovery.discard_if_lockdown(self._lockdown_data_dir())
        if not self._is_lockdown_enabled_on_disk():
            # No envelope on disk -> no gate. Mark unlocked so an
            # accidental future read of is_locked doesn't gate a non-lockdown
            # install. No data key is held (none is needed when not enabled).
            self._lockdown_state._locked = False

        self._initialize_history_security_adapters()
        # Exact EULA grants are retained only for live, memory-only Bakeoff
        # sessions. They let delayed pair work and output delivery prove that
        # the same authority is still current without accepting a replacement.
        self._bakeoff_authority_lock = threading.RLock()
        self._bakeoff_authorities: dict[
            str, tuple[AcceptanceGrant, int]
        ] = {}
        if _build_flags.EXPERIMENTAL_AVAILABLE:
            manager = getattr(self._app, "enhancement_bakeoff", None)
            set_progress_callback = getattr(
                manager, "set_progress_callback", None
            )
            if callable(set_progress_callback):
                set_progress_callback(self._on_bakeoff_progress)

        self._relocation = ModelRelocation(
            current_config=lambda: self._app.config,
            save_config=lambda cfg: self._app.config_manager.save(cfg),
            refresh_models_root=lambda: self._app.config_manager.refresh_models_root(),
            activation_busy=lambda: self._stt_activation_busy(),
            has_resident_models=lambda: self._has_resident_models(),
            unload_models=lambda: self._unload_models(),
            reload_models=lambda: self._reload_models(),
            broadcast=lambda event: self.broadcast(event),
            logger=self._logger,
        )
        self._gguf_download = RegisteredGGUFDownload(
            next_generation=self._next_model_generation,
            execute=self._execute_windows_model_operation,
            broadcast=lambda event: self.broadcast(event),
        )
        self._benchmark = BenchmarkOperation(
            current_config=lambda: self._app.config,
            save_config=lambda cfg: self._app.config_manager.save(cfg),
            reload_config=lambda cfg: self._app.reload_config(cfg),
            fetch_gguf=lambda *args: self._gguf_download.fetch(*args),
            set_download_activity=self._set_model_download_activity,
            broadcast=lambda event: self.broadcast(event),
            logger=self._logger,
        )
        self._imports = ImportOperation(
            current_config=lambda: self._app.config,
            current_engine=self._current_import_engine,
            current_store=lambda: getattr(self._app, "transcript_store", None),
            current_executor=lambda: getattr(self._app, "_import_executor", None),
            current_file_workflows=self._import_file_workflows,
            snapshot_profile=lambda profile: self._app._snapshot_enhancement_profile(profile),
            enhance=lambda *args, **kwargs: self._app._enhance_with_bounded_outcome(*args, **kwargs),
            provenance=lambda *args: self._app._enhancement_provenance(*args),
            apply_signature=lambda *args: self._app._apply_email_signature(*args),
            process_text=lambda text: self._app.text_injector.process(text),
            current_acceptance_grant=self._current_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            diagnostics_locked=self._diagnostics_locked,
            broadcast=lambda *args, **kwargs: self.broadcast(*args, **kwargs),
            logger=self._logger,
            history_disabled=self._history_disabled,
        )
        self.fastapi = self._build_fastapi()

    # ------------------------------------------------------------------
    # Lockdown helpers
    # ------------------------------------------------------------------

    def _initialize_history_security_adapters(self) -> None:
        """Bind the platform owners to the frozen schema-v1 contracts."""

        factory = PlatformHistorySecurityFactory(
            history_store=lambda: getattr(self._app, "transcript_store", None),
            license_manager=lambda: getattr(self._app, "license_manager", None),
            protected_store=lambda: (
                self._protected_secret_store() or get_protected_secret_store()
            ),
            lockdown_enabled=self._diagnostics_locked,
            lockdown_locked=lambda: bool(
                self._is_lockdown_enabled_on_disk()
                and self._lockdown_state.is_locked
            ),
        )
        self._history_security_factory = factory
        self._history_store_adapter = factory.history()
        self._file_workflows = factory.files()
        self._license_state_adapter = factory.licensing()

    def _ensure_history_security_adapters(self) -> None:
        """Compatibility seam for narrowly constructed test/tool servers."""

        if not hasattr(self, "_history_security_factory"):
            self._initialize_history_security_adapters()

    def _history_availability(self) -> SecurityAvailability:
        """Return the typed history state without touching transcript content."""

        self._ensure_history_security_adapters()
        try:
            state = self._history_store_adapter.availability()
        except Exception:  # noqa: BLE001 - fail closed at the content boundary
            return SecurityAvailability.UNAVAILABLE
        return (
            state
            if isinstance(state, SecurityAvailability)
            else SecurityAvailability.UNAVAILABLE
        )

    @staticmethod
    def _history_unavailable_response(
        availability: SecurityAvailability,
    ) -> JSONResponse | None:
        """Map unusable history states to a content-free typed HTTP denial."""

        if availability not in {
            SecurityAvailability.KEY_ERROR,
            SecurityAvailability.KEY_MISSING,
            SecurityAvailability.NOT_DECRYPTABLE,
            SecurityAvailability.UNAVAILABLE,
        }:
            return None
        return JSONResponse(
            status_code=(
                503
                if availability is SecurityAvailability.UNAVAILABLE
                else 409
            ),
            content={
                "error": "history_unavailable",
                "availability": availability.value,
            },
        )

    def _license_status_payload(self) -> dict[str, Any]:
        """Merge legacy status with the fail-closed D-047 capability snapshot."""

        self._ensure_history_security_adapters()
        adapter = self._license_state_adapter
        try:
            snapshot = adapter.snapshot()
        except Exception:  # noqa: BLE001 - unavailable is the safe default
            snapshot = LicenseSnapshot(
                state=LicenseState.UNAVAILABLE,
                cache_integrity_protected=False,
                cache_confidential=False,
                activation_available=False,
            )
        if not isinstance(snapshot, LicenseSnapshot):
            snapshot = LicenseSnapshot(
                state=LicenseState.UNAVAILABLE,
                cache_integrity_protected=False,
                cache_confidential=False,
                activation_available=False,
            )
        status_provider = getattr(adapter, "status", None)
        try:
            status = dict(status_provider()) if callable(status_provider) else {}
        except Exception:  # noqa: BLE001 - capability snapshot remains authoritative
            status = {}
        if snapshot.state is LicenseState.UNAVAILABLE:
            status = {"is_pro": False, "trial_remaining_seconds": 0.0}
        else:
            status.setdefault("is_pro", snapshot.state is LicenseState.PRO)
            status.setdefault("trial_remaining_seconds", 0.0)
        return {
            **status,
            "state": snapshot.state.value,
            "cache_integrity_protected": snapshot.cache_integrity_protected,
            "cache_confidential": snapshot.cache_confidential,
            "activation_available": snapshot.activation_available,
            "offline_validation_available": snapshot.offline_validation_available,
            "deactivation_available": snapshot.deactivation_available,
        }

    def _diagnostics_locked(self) -> bool:
        """Return the fail-closed diagnostic boundary for transition windows.

        Config and the password envelope are written/removed in separate
        operations.  Either one means diagnostic ingest/export must stay
        closed, including after a partial enable or disable failure.
        """
        if self._managed_lockdown_enabled():
            return True
        if getattr(self._app.config, "lockdown_mode", False) is True:
            return True
        try:
            return lockdown_keystore.is_lockdown_enabled(
                self._lockdown_data_dir()
            )
        except OSError:
            # An unreadable envelope location cannot safely authorize reading
            # or exporting pre-existing diagnostics.
            return True

    def _managed_lockdown_enabled(self) -> bool:
        """Return the immutable machine-policy authority for this process.

        Tests and older embedders may provide app-like objects without the
        policy field. Requiring the exact boolean value avoids treating a
        dynamically-created mock attribute as enabled.
        """
        policy = getattr(self._app, "managed_lockdown_policy", None)
        return getattr(policy, "enabled", False) is True

    def _history_disabled(self) -> bool:
        return history_disabled_by_policy(self._app)

    @staticmethod
    def _history_disabled_rejection() -> JSONResponse:
        return JSONResponse(status_code=403, content={
            "ok": False, "error_code": "history_disabled_by_organization",
            "message": HISTORY_DISABLED_MESSAGE,
        })

    def _managed_lockdown_state(self) -> str | None:
        if not self._managed_lockdown_enabled():
            return None
        state = getattr(
            getattr(self._app, "managed_lockdown_policy", None),
            "state",
            None,
        )
        return state if isinstance(state, str) else "inaccessible"

    @staticmethod
    def _managed_lockdown_rejection() -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error_code": "managed_by_organization",
                "message": (
                    "Lockdown Mode is managed by your organization and can't be "
                    "turned off here."
                ),
            },
        )

    def _begin_lockdown_entry(self) -> None:
        """Revoke content-bearing features before Lockdown entry work."""
        self._imports.cancel()
        self._revoke_bakeoffs("lockdown_locked")
        self._safe_diagnostics.emit(
            "lockdown_transition",
            phase="entry_started",
            managed=self._managed_lockdown_enabled(),
            history_enabled=(
                getattr(self._app.config, "history_enabled", False) is True
            ),
        )
        self._clear_agent_speak_state(reason="lockdown_entered")
        begin_persistence = getattr(
            self._app, "begin_persistence_transition", None
        )
        if callable(begin_persistence):
            begin_persistence()

    def _end_lockdown_entry(self) -> None:
        end_persistence = getattr(
            self._app, "end_persistence_transition", None
        )
        if callable(end_persistence):
            end_persistence()

    def _restore_diagnostics_after_failed_entry(self) -> None:
        """Record a failed entry and end the persistence transition."""
        self._safe_diagnostics.emit(
            "lockdown_transition",
            phase="entry_failed",
            managed=self._managed_lockdown_enabled(),
            history_enabled=(
                getattr(self._app.config, "history_enabled", False) is True
            ),
        )
        self._end_lockdown_entry()

    def _lockdown_data_dir(self) -> Path:
        """Resolve the data dir for lockdown envelopes.

        ``get_app_dir()`` honors ``WHISPERSTREAM_DATA_DIR``, so dev/test runs and
        the installed app each resolve to their own dir. The lockdown envelope
        (``lockdown.key.json``) and protected ``transcript.key`` both live
        here.
        """
        return get_app_dir()

    def _protected_secret_store(self):
        """Return an injected isolated store, or None for lazy native selection."""

        from ..config_manager import ConfigManager  # noqa: PLC0415

        manager = self._app.config_manager
        if not isinstance(manager, ConfigManager):
            return None
        return manager.protected_secret_store

    def _arm_agent_setup_preview(self, summary_enabled: bool, speech_enabled: bool) -> None:
        self._setup_delivery_preview = {
            "agent_summary_text_enabled": summary_enabled,
            "agent_speech_enabled": speech_enabled,
        }
        try:
            self.broadcast(self._agent_response_queue_snapshot_event())
        except Exception:
            pass


    def _teardown_agent_setup(self):
        """Release whatever the setup fire routes may have left live.

        Idempotent and never raising, by design: with nothing live this is
        a no-op that still returns 200. Without it, a synthetic Stop leaves
        a broker request blocked for its whole grace window (up to 120
        seconds) and a live compose lease that swallows the user's next
        push-to-talk.
        """
        self._setup_delivery_preview = None
        closed = 0

        capture = self._reply_desk.capturing() if self._reply_desk is not None else None
        if capture is not None:
            try:
                self._cancel_routed_compose(
                    capture[0], "agent_setup_teardown"
                )
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

        question = (
            self._agent_question_coordinator
            or self._claude_question_coordinator
        )
        if question is not None:
            try:
                question.expire_current()
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

        response = (
            self._agent_response_coordinator
            or self._claude_response_coordinator
        )
        if response is not None:
            try:
                response.dismiss_responses()
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

        # Release any hook request still blocked on a decision, so a stale
        # waiter cannot resolve into a later HUD state.
        broker = self._agent_interactions or self._claude_interactions
        if broker is not None:
            try:
                for interaction in broker.snapshot():
                    if broker.close(interaction.request_id) is not None:
                        closed += 1
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

        # Rebroadcast the authoritative snapshots so a connected HUD
        # settles immediately instead of waiting for the next event.
        for build_event in (
            self._agent_completion_snapshot_event,
            self._agent_response_queue_snapshot_event,
            self._agent_question_snapshot_event,
        ):
            try:
                self.broadcast(build_event())
            except Exception:  # noqa: BLE001 — teardown never raises
                pass

        self._logger.info(
            "[ClaudeHUD] setup teardown closed_interactions=%d", closed
        )
        return {"ok": True, "closed_interactions": closed}


    def _permission_actions(self):
        """Return the process-local action service after an explicit POST."""

        with self._permission_action_service_lock:
            if self._permission_action_service is None:
                from ..permission_actions import (  # noqa: PLC0415
                    PermissionActionService,
                )

                self._permission_action_service = PermissionActionService()
            return self._permission_action_service

    def _is_lockdown_enabled_on_disk(self) -> bool:
        """True when the lockdown envelope file is present under the data dir."""
        try:
            return lockdown_keystore.is_lockdown_enabled(self._lockdown_data_dir())
        except Exception:
            # An unreadable authority marker cannot safely prove normal mode.
            return True

    # --- Transcript-store key lifecycle under lockdown (F2) ---
    # The unlocked data key lives in memory in TWO places while unlocked: the
    # LockdownState (the lock gate's copy) and the TranscriptStore (the copy the
    # CRUD/persist paths encrypt/decrypt with). These helpers keep the store's
    # copy in lockstep so that, under lockdown, the store can only decrypt while
    # the surface is unlocked. NEVER log the data key here.

    def _history_startup_busy_response(self) -> Optional[JSONResponse]:
        if getattr(self._app, "history_initializing", False) is True:
            return JSONResponse(status_code=503, content={
                "error": "History is still opening. Finish any Keychain prompt and try again.",
                "reason": "history_initializing",
            })
        return None

    def _set_store_key(self, data_key: bytes) -> bool:
        """Hand the unlocked key to history, then run deferred cleanup."""
        store = getattr(self._app, "transcript_store", None)
        if store is None or not hasattr(store, "set_data_key"):
            return True
        try:
            # DO NOT LOG: data_key is key material. set_data_key performs any
            # deferred plaintext-peaks migration and reports transactional
            # failure distinctly from a no-work migration.
            if store.set_data_key(data_key) is False:
                raise RuntimeError("history migration unavailable")
            # Retention/orphan cleanup was skipped at locked startup, so run it
            # immediately while the key is available after every unlock.
            run_retention_sweep(
                self._app.config, store, raise_on_error=True
            )
            return True
        except Exception:
            try:
                store.clear_data_key()
            except Exception:
                pass
            self._logger.warning("lockdown history prepare status=retryable")
            return False

    def _repair_lockdown_key_copies(self) -> None:
        """Retry the startup repair once the user has proven the password.

        Startup can't always remove a leftover protected copy (Keychain may
        decline without user interaction); an unlock is a user action, so the
        repair runs again with interaction allowed. Never logs key material.
        """
        try:
            if platform.system() == "Darwin":
                from ..macos_protected_secret import allow_keychain_interaction

                with allow_keychain_interaction():
                    lockdown_keystore.repair_interrupted_transition(
                        self._lockdown_data_dir(), store=self._protected_secret_store()
                    )
            else:
                lockdown_keystore.repair_interrupted_transition(
                    self._lockdown_data_dir(), store=self._protected_secret_store()
                )
        except Exception:  # noqa: BLE001 - best effort; startup retries
            self._logger.warning("lockdown unlock repair status=retryable")

    def _clear_store_key(self) -> None:
        """Drop the transcript store's in-memory data key on lock / idle."""
        store = getattr(self._app, "transcript_store", None)
        if store is not None and hasattr(store, "clear_data_key"):
            store.clear_data_key()

    def _reload_store_key(self) -> bool:
        """Re-point the store at the protected key after Lockdown disable."""
        store = getattr(self._app, "transcript_store", None)
        if store is not None and hasattr(store, "reload_data_key"):
            try:
                if store.reload_data_key() is False:
                    store.clear_data_key()
                    return False
                return True
            except Exception:
                # A store re-key failure must never crash disable; the store
                # simply reports not-decryptable until the next construction.
                try:
                    store.clear_data_key()
                except Exception:
                    pass
                return False
        return True

    def _transcript_locked_response(self) -> Optional[JSONResponse]:
        """Return a 423 response if lockdown is enabled and the surface is locked.

        Returns None when the caller should proceed normally (lockdown is off,
        or the surface is currently unlocked).  When it returns a 423 the caller
        must return it immediately without touching the TranscriptStore.

        The 423 body is ``{"locked": true}`` — consistent with the existing
        ``{"decryptable": false}`` shape: a structured signal the frontend can
        branch on, not a generic error string.
        """
        cfg = self._app.config
        # Only an envelope-backed lockdown can gate the surface: there must be a
        # password (or recovery code) to unlock with. A no-envelope, history-off
        # lockdown has NO key, so it must never return a dead 423 — there would
        # be no way to clear it. Requiring the envelope on disk keeps that path
        # permanently unlocked (history is off anyway, so there is nothing to
        # protect behind a lock).
        if self._is_lockdown_enabled_on_disk() and self._lockdown_state.is_locked:
            return JSONResponse(
                status_code=423,
                content={"locked": True},
            )
        return None

    def _maybe_idle_lock(self) -> None:
        """Check whether the transcript surface should auto-lock due to idle.

        Call this on a periodic timer (see the on_startup background task).
        It is also unit-testable in isolation — inject a fake clock into
        ``LockdownState`` and call this directly without the async loop.

        Rules:
          - Does nothing when ``lockdown_mode`` is False (lockdown not enabled).
          - Does nothing when the surface is already locked (avoids duplicate
            broadcast on repeated timer firings).
          - Does nothing when ``idle_lock_seconds == 0`` (Never setting).
          - When lockdown is enabled, surface is unlocked, and
            ``LockdownState.idle_expired(idle_lock_seconds)`` returns True:
            call ``LockdownState.lock()`` and broadcast a
            ``{"type": "transcripts.locked", "reason": "idle"}`` event to all
            connected WebSocket clients.
        """
        cfg = self._app.config
        if not self._diagnostics_locked():
            return
        # A no-envelope (history-off) lockdown has no password to unlock with, so
        # it must NEVER auto-lock — doing so would wedge the surface with no way
        # back. Only an envelope-backed lockdown participates in the idle timer.
        if not self._is_lockdown_enabled_on_disk():
            return
        if self._lockdown_state.is_locked:
            # Already locked — nothing to do.
            return
        idle_lock_seconds = getattr(cfg, "lockdown_idle_lock_seconds", 0)
        if not self._lockdown_state.idle_expired(idle_lock_seconds):
            return
        # Idle window elapsed — lock, drop the store key, audit-log, broadcast.
        self._lock_surface("idle")

    def _lock_surface(self, reason: str) -> None:
        """Lock the transcript surface: drop the key, audit-log, broadcast.

        Shared by the idle auto-lock timer (``_maybe_idle_lock``) and the manual
        ``POST /api/lockdown/lock`` endpoint.

        - F2: drops the store's in-memory data key so a locked surface genuinely
          cannot decrypt.
        - F8: emits a content-free ``lock`` access-log entry (45 CFR 164.312(b):
          "when your history is ... locked") — action + timestamp only, NEVER a
          secret or transcript content (record_id is None).
        """
        self._imports.cancel()
        self._revoke_bakeoffs("lockdown_locked")
        self._lockdown_state.lock()
        self._clear_store_key()
        log_access("lock", None, enabled=self._diagnostics_locked())
        self.broadcast({"type": "transcripts.locked", "reason": reason})

    # ------------------------------------------------------------------
    # FastAPI app construction
    # ------------------------------------------------------------------

    def _current_acceptance_grant(self) -> AcceptanceGrant:
        """Read the backend-authoritative exact grant and fail closed."""
        provider = getattr(self._app, "_current_acceptance_grant", None)
        if callable(provider):
            try:
                grant = provider()
                if isinstance(grant, AcceptanceGrant):
                    return grant
            except Exception:  # noqa: BLE001 - the authority fails closed
                pass
        try:
            grant = get_acceptance_grant()
            if isinstance(grant, AcceptanceGrant):
                return grant
        except Exception:  # noqa: BLE001 - the authority fails closed
            pass
        return AcceptanceGrant(
            False, AcceptanceGrantReason.ACCEPTANCE_UNAVAILABLE
        )

    def _acceptance_is_current(self, grant: AcceptanceGrant) -> bool:
        """Revalidate one captured grant without accepting a replacement."""
        validator = getattr(self._app, "_acceptance_is_current", None)
        if callable(validator):
            try:
                result = validator(grant)
                if isinstance(result, bool):
                    return result
            except Exception:  # noqa: BLE001 - the authority fails closed
                return False
        try:
            return bool(acceptance_grant_is_current(grant))
        except Exception:  # noqa: BLE001 - the authority fails closed
            return False

    @staticmethod
    def _content_blocked_response(path: str) -> JSONResponse:
        """Return the stable denial while preserving compatibility envelopes."""
        if path in {"/api/agent-event", "/api/claude-event"}:
            return JSONResponse(status_code=200, content={"ok": True})
        if path == "/api/claude-hook":
            return JSONResponse(status_code=200, content={})
        if path in {"/api/dictation/start", "/api/dictation/complete"}:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": _EULA_BLOCK_CODE},
            )
        return JSONResponse(
            status_code=403,
            content={"error": _EULA_BLOCK_CODE},
        )

    def _request_acceptance_grant(self, request: Request) -> AcceptanceGrant:
        """Return the exact grant captured before request-body parsing."""
        grant = getattr(request.state, "eula_acceptance_grant", None)
        if isinstance(grant, AcceptanceGrant):
            return grant
        return self._current_acceptance_grant()

    def _reconcile_macos_input_selection(
        self,
        devices: list[dict[str, Any]] | None = None,
    ) -> tuple[WhisperstreamConfig, list[dict[str, Any]] | None]:
        """Atomically reconcile Mac's persisted input identity and label."""

        current = self._app.config
        if platform.system() != "Darwin":
            return current, None
        if not isinstance(current, WhisperstreamConfig):
            return current, None
        if getattr(getattr(self._app, "audio_capture", None), "capture_available", True) is False:
            return current, []
        recording_lock = getattr(self._app, "_lock", None)
        if recording_lock is None:
            self._logger.warning(
                "audio input selection reconciliation skipped reason=recording_lock_unavailable"
            )
            return current, None
        # Avoid the native inventory query when recording is already active.
        # This is only an optimization; the decisive check below is repeated
        # under this same owner lock after enumeration.
        with recording_lock:
            current = self._app.config
            if getattr(self._app, "_recording", False) is True:
                return current, None
        try:
            if devices is None:
                from ..capture import list_input_devices  # noqa: PLC0415

                devices = list_input_devices()
            from ..input_device_selection import (  # noqa: PLC0415
                reconcile_macos_input_selection,
            )

            # Enumeration is deliberately outside the recording-owner lock.
            # Re-enter it for the authoritative state read and keep it held
            # across the check, pair save, and capture/config reload. A start
            # attempt either completes first (and makes this defer) or waits
            # until the migrated capture selection is fully installed.
            with recording_lock:
                current = self._app.config
                if not isinstance(current, WhisperstreamConfig):
                    return current, None
                if getattr(self._app, "_recording", False) is True:
                    return current, None
                reconciled = reconcile_macos_input_selection(
                    current.audio_input_device,
                    current.audio_input_device_label,
                    devices,
                )
                if (
                    reconciled.value == current.audio_input_device
                    and reconciled.label == current.audio_input_device_label
                ):
                    return current, devices
                updated = replace(
                    current,
                    audio_input_device=reconciled.value,
                    audio_input_device_label=reconciled.label,
                )
                self._app.config_manager.save(updated)
                self._app._reload_config_locked(updated)
                return updated, devices
        except Exception as exc:  # noqa: BLE001 - passive reconciliation never blocks settings
            self._logger.warning(
                "audio input selection reconciliation failed reason=%s",
                type(exc).__name__,
            )
            return current, None

    def _bakeoff_authority_error(
        self, grant: AcceptanceGrant
    ) -> str | None:
        """Recheck every authority required for delayed Bakeoff content work."""
        if not _experimental_enabled():
            return "experimental_disabled"
        if not self._acceptance_is_current(grant):
            return _EULA_BLOCK_CODE
        if bool(getattr(self._app.config, "lockdown_mode", False)):
            return "lockdown_locked"
        if self._transcript_locked_response() is not None:
            return "lockdown_locked"
        return None

    def _remember_bakeoff_authority(
        self,
        run_id: str,
        grant: AcceptanceGrant,
        generation: int,
    ) -> None:
        with self._bakeoff_authority_lock:
            self._bakeoff_authorities[run_id] = (grant, generation)

    def _revoke_bakeoffs(self, reason: str) -> None:
        """Synchronously scrub session content and invalidate delayed events."""
        with self._bakeoff_authority_lock:
            authorities = tuple(self._bakeoff_authorities.items())
            self._bakeoff_authorities.clear()
        revoke = getattr(self._app, "revoke_enhancement_bakeoff", None)
        if callable(revoke):
            revoke()
        for run_id, (grant, generation) in authorities:
            self.broadcast(
                {
                    "type": "enhancement.bakeoff.revoked",
                    "version": _BAKEOFF_EVENT_VERSION,
                    "run_id": run_id,
                    "generation": generation,
                    "reason": reason,
                },
                acceptance_grant=grant,
            )

    @staticmethod
    def _bakeoff_metadata_payload(metadata: Any) -> dict[str, Any]:
        return {
            "serving_contract": getattr(metadata, "serving_contract", None),
            "device_mode": getattr(metadata, "device_mode", None),
            "resident_warm": getattr(metadata, "resident_warm", None),
            "load_ms": getattr(metadata, "load_ms", None),
            "inference_ms": getattr(metadata, "inference_ms", None),
            "execution_mode": getattr(metadata, "execution_mode", None),
            "chunk_count": getattr(metadata, "chunk_count", None),
        }

    def _on_bakeoff_progress(self, progress: Any) -> None:
        """Translate one metadata-only manager update into an authorized event."""
        run_id = getattr(progress, "run_id", None)
        generation = getattr(progress, "generation", None)
        if not isinstance(run_id, str) or not isinstance(generation, int):
            return
        with self._bakeoff_authority_lock:
            authority = self._bakeoff_authorities.get(run_id)
            if authority is None:
                return
            grant, current_generation = authority
            if generation < current_generation:
                return
            if generation > current_generation:
                self._bakeoff_authorities[run_id] = (grant, generation)
        authority_error = self._bakeoff_authority_error(grant)
        if authority_error is not None:
            self._revoke_bakeoffs(authority_error)
            return

        state = str(getattr(progress, "state", ""))
        event = {
            "type": "enhancement.bakeoff.progress",
            "version": _BAKEOFF_EVENT_VERSION,
            "run_id": run_id,
            "generation": generation,
            "pair_index": getattr(progress, "pair_index", -1),
            "state": state,
            "finished_count": getattr(progress, "finished_count", 0),
            "total_count": getattr(progress, "total_count", 0),
            "error_code": getattr(progress, "error_code", None),
            "metadata": self._bakeoff_metadata_payload(
                getattr(progress, "metadata", None)
            ),
        }
        if state not in {"complete", "unchanged"}:
            self.broadcast(event, acceptance_grant=grant)
            return

        manager = getattr(self._app, "enhancement_bakeoff", None)
        snapshot_provider = getattr(manager, "snapshot", None)
        snapshot = snapshot_provider(run_id) if callable(snapshot_provider) else None
        pair_index = event["pair_index"]
        pairs = getattr(snapshot, "pairs", ())
        if (
            snapshot is None
            or getattr(snapshot, "generation", None) != generation
            or not isinstance(pair_index, int)
            or pair_index < 0
            or pair_index >= len(pairs)
        ):
            return
        pair = pairs[pair_index]
        if str(getattr(pair, "state", "")) != state:
            return
        output = getattr(pair, "output", None)
        if not isinstance(output, str):
            return
        authority_error = self._bakeoff_authority_error(grant)
        if authority_error is not None:
            self._revoke_bakeoffs(authority_error)
            return
        event["type"] = "enhancement.bakeoff.result"
        event.pop("error_code", None)
        event["output"] = output
        event["metadata"] = self._bakeoff_metadata_payload(
            getattr(pair, "metadata", None)
        )
        self.broadcast(event, acceptance_grant=grant)

    def _bakeoff_executor(
        self,
        selections: list[Any],
        grant: AcceptanceGrant,
        *,
        resident_pool: object | None = None,
    ):
        """Wrap production evaluation with per-pair authority rechecks."""
        from ..enhancement_bakeoff import (
            BakeoffExecutionError,
            ProductionBakeoffPairExecutor,
        )
        from ..resident_llama_pool import MemoryAdmissionError

        executor = ProductionBakeoffPairExecutor(
            config=self._app.config,
            selections=selections,
            resident_pool=(
                resident_pool
                if resident_pool is not None
                else self._resident_llama_pool()
            ),
            production_supervisor_provider=lambda: self._app.llama_server,
            logger=self._logger,
            simulated_tps_provider=lambda: getattr(
                self._app, "_simulated_tps_provider", lambda: None
            )(),
        )

        async def execute_pair(selection, raw_text, mark_running):
            authority_error = self._bakeoff_authority_error(grant)
            if authority_error is not None:
                self._revoke_bakeoffs(authority_error)
                raise BakeoffExecutionError(authority_error)

            def authorized_mark_running(metadata=None):
                authority_error = self._bakeoff_authority_error(grant)
                if authority_error is not None:
                    self._revoke_bakeoffs(authority_error)
                    return False
                return mark_running(metadata)

            try:
                result = await executor(
                    selection, raw_text, authorized_mark_running
                )
            except MemoryAdmissionError as exc:
                raise BakeoffExecutionError(exc.reason) from exc
            authority_error = self._bakeoff_authority_error(grant)
            if authority_error is not None:
                self._revoke_bakeoffs(authority_error)
                raise BakeoffExecutionError(authority_error)
            return result

        return execute_pair

    def _validate_bakeoff_selections(
        self,
        requested: list[BakeoffPairBody],
        *,
        minimum: int = _MIN_BAKEOFF_PAIRS,
    ) -> tuple[list[Any], list[tuple[dict[str, Any], Path]]] | JSONResponse:
        """Resolve semantic pair selections onto authoritative local assets."""
        from ..enhancement_bakeoff import BakeoffPairSelection
        from ..enhancement_models import (
            get_local_model_catalog,
            resolve_local_model_path,
        )
        from ..resident_llama_pool import canonical_model_path

        if len(requested) < minimum or len(requested) > _MAX_BAKEOFF_PAIRS:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_selection_count"},
            )
        catalog = {
            str(row.get("id", "")).casefold(): row
            for row in get_local_model_catalog()
            if isinstance(row, dict) and row.get("id")
        }
        profiles = {
            str(profile.get("id")): profile
            for profile in getattr(self._app.config, "profiles", [])
            if isinstance(profile, dict) and profile.get("id")
        }
        selections: list[Any] = []
        bindings: list[tuple[dict[str, Any], Path]] = []
        pair_keys: set[tuple[str, str]] = set()
        model_keys: set[str] = set()
        for item in requested:
            model = catalog.get(item.model_id.casefold())
            if model is None:
                return JSONResponse(
                    status_code=409, content={"error": "model_unavailable"}
                )
            profile = profiles.get(item.profile_id)
            if profile is None:
                return JSONResponse(
                    status_code=409, content={"error": "profile_unavailable"}
                )
            model_path = resolve_local_model_path(model)
            try:
                artifact_available = bool(
                    model_path is not None
                    and model_path.is_file()
                    and model_path.stat().st_size > 0
                )
            except OSError:
                artifact_available = False
            if not artifact_available:
                code = (
                    "candidate_artifact_unavailable"
                    if model.get("source") == "candidate"
                    else "model_artifact_unavailable"
                )
                return JSONResponse(status_code=409, content={"error": code})
            profile_id = str(profile["id"])
            configured_mode = profile.get("mode")
            profile_mode = (
                profile_id
                if profile_id in {"chat", "code", "email"}
                else (
                    str(configured_mode)
                    if configured_mode in {"chat", "code", "email"}
                    else "chat"
                )
            )
            trained_modes = model.get("trained_modes")
            if (
                model.get("source") == "candidate"
                and isinstance(trained_modes, list)
                and profile_mode not in trained_modes
            ):
                return JSONResponse(
                    status_code=409, content={"error": "profile_unsupported"}
                )
            model_key = canonical_model_path(model_path)
            pair_key = (model_key, profile_id)
            if pair_key in pair_keys:
                return JSONResponse(
                    status_code=400, content={"error": "duplicate_selection"}
                )
            pair_keys.add(pair_key)
            model_keys.add(model_key)
            if len(model_keys) > _MAX_BAKEOFF_MODELS:
                return JSONResponse(
                    status_code=400, content={"error": "too_many_models"}
                )
            selections.append(
                BakeoffPairSelection(str(model["id"]), profile_id)
            )
            bindings.append((model, model_path))
        return selections, bindings

    def _bakeoff_memory_preflight(
        self, bindings: list[tuple[dict[str, Any], Path]]
    ) -> JSONResponse | None:
        """Reject an aggregate lineup that cannot preserve RAM headroom."""
        from ..resident_llama_pool import (
            HEADROOM_FRACTION,
            MINIMUM_HEADROOM_MB,
            canonical_model_path,
            estimate_resident_ram_mb,
        )
        from ..system_info import get_memory_snapshot

        pool = getattr(self._app, "resident_llama_pool", None)
        production = getattr(self._app, "llama_server", None)
        production_path = getattr(production, "_gguf_path", None)
        production_key = (
            canonical_model_path(production_path)
            if production_path is not None
            else None
        )
        production_active = bool(
            production is not None
            and (
                getattr(production, "is_running", False)
                or getattr(production, "is_ready", False)
            )
        )
        required_mb = 0
        seen: set[str] = set()
        for model, model_path in bindings:
            key = canonical_model_path(model_path)
            if key in seen:
                continue
            seen.add(key)
            resident = (
                pool.find_resident(model_path)
                if pool is not None
                else None
            )
            if resident is not None or (
                key == production_key and production_active
            ):
                continue
            try:
                required_mb += estimate_resident_ram_mb(
                    model_path,
                    catalog_ram_mb=model.get("ram_mb", 0),
                )
            except (OSError, TypeError, ValueError):
                return JSONResponse(
                    status_code=409,
                    content={"error": "memory_estimate_unavailable"},
                )

        # Protect the configured production model even when global
        # enhancement is cloud/off and no lazy supervisor was prepared. A
        # later normal dictation must still be able to start that model. Do not
        # double-count it when the lineup already admits the same artifact or
        # when a genuinely running/resident process already consumes live RAM.
        from ..enhancement_models import get_local_model, resolve_local_model_path

        configured = get_local_model(
            str(getattr(self._app.config, "enhancement_local_model", ""))
        )
        configured_path = (
            resolve_local_model_path(configured)
            if configured is not None
            else None
        )
        try:
            configured_available = bool(
                configured_path is not None
                and configured_path.is_file()
                and configured_path.stat().st_size > 0
            )
        except OSError:
            configured_available = False
        if configured_available and configured is not None:
            configured_key = canonical_model_path(configured_path)
            configured_resident = (
                pool.find_resident(configured_path)
                if pool is not None
                else None
            )
            configured_running = bool(
                configured_key == production_key and production_active
            )
            if (
                configured_key not in seen
                and configured_resident is None
                and not configured_running
            ):
                try:
                    required_mb += estimate_resident_ram_mb(
                        configured_path,
                        catalog_ram_mb=configured.get("ram_mb", 0),
                    )
                except (OSError, TypeError, ValueError):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "memory_estimate_unavailable"},
                    )
        if required_mb == 0:
            return None
        snapshot = get_memory_snapshot()
        if (
            snapshot is None
            or snapshot.total_mb <= 0
            or snapshot.available_mb < 0
        ):
            return JSONResponse(
                status_code=409,
                content={"error": "memory_probe_unavailable"},
            )
        headroom_mb = max(
            MINIMUM_HEADROOM_MB,
            int(snapshot.total_mb * HEADROOM_FRACTION + 0.999999),
        )
        total_required_mb = required_mb + headroom_mb
        if total_required_mb > snapshot.available_mb:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "insufficient_current_memory",
                    "required_mb": total_required_mb,
                    "available_mb": snapshot.available_mb,
                },
            )
        return None

    def _build_fastapi(self) -> FastAPI:
        # Handlers registered below run in registration order, as on_event did.
        startup_hooks: list[Callable[[], Awaitable[None]]] = []
        shutdown_hooks: list[Callable[[], Awaitable[None]]] = []

        @asynccontextmanager
        async def lifespan(_api: FastAPI):
            for hook in startup_hooks:
                await hook()
            yield
            for hook in shutdown_hooks:
                await hook()

        api = FastAPI(
            title="Whisperstream Backend",
            docs_url=None,
            redoc_url=None,
            lifespan=lifespan,
        )

        install_middleware(
            api,
            logger=self._logger,
            current_acceptance_grant=self._current_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            content_blocked_response=self._content_blocked_response,
            history_startup_busy_response=self._history_startup_busy_response,
            experimental_enabled=lambda: _experimental_enabled(),
        )

        # --- REST endpoints ---

        @api.get("/api/health")
        async def health(response: Response):
            if self._engine_generation is not None:
                response.headers[_ENGINE_GENERATION_HEADER] = self._engine_generation
            engine = self._app.stt_engine
            failure = getattr(self._app, "last_stt_failure", None)
            if not isinstance(failure, SttFailure) and engine is not None:
                failure = getattr(engine, "last_failure", None)
            if isinstance(failure, SttFailure):
                return {
                    "status": "ok",
                    "model_loaded": False,
                    "model_health": "degraded",
                    "stt_failure": failure.public_metadata(),
                }
            return {"status": "ok", "model_loaded": engine is not None}

        @api.get("/api/accessibility/screen-reader")
        async def screen_reader_status():
            active = await self._screen_reader_active()
            return {"active": active, "monitor": self._screen_reader_monitor_status()}

        async def _run_dictation_command(command, success_status: str):
            result = await asyncio.to_thread(command)
            if result == success_status:
                return {"ok": True, "status": result}
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": result},
            )

        @api.post("/api/dictation/start")
        async def dictation_start(request: Request):
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if not _is_trusted_hud_command_request(request):
                return JSONResponse(
                    status_code=403,
                    content={"ok": False, "error": "untrusted_hud_client"},
                )
            return await _run_dictation_command(
                self._app.start_hud_dictation,
                "started",
            )

        @api.post("/api/dictation/complete")
        async def dictation_complete(request: Request):
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if not _is_trusted_hud_command_request(request):
                return JSONResponse(
                    status_code=403,
                    content={"ok": False, "error": "untrusted_hud_client"},
                )
            return await _run_dictation_command(
                self._app.complete_hud_dictation,
                "completed",
            )

        @api.post("/api/dictation/cancel")
        async def dictation_cancel(request: Request):
            if not _is_trusted_hud_command_request(request):
                return JSONResponse(
                    status_code=403,
                    content={"ok": False, "error": "untrusted_hud_client"},
                )
            return await _run_dictation_command(
                self._app.cancel_hud_dictation,
                "cancelled",
            )

        @api.get("/api/system/info")
        async def system_info():
            # Total physical RAM in MB. Used by the Enhancement tab to warn
            # before downloading a local model that needs more RAM than the
            # machine has. Returns total_ram_mb=null on detection failure;
            # the frontend fails open (skips the warning) on null.
            from whisperstream.system_info import get_total_ram_mb

            return {"total_ram_mb": get_total_ram_mb()}

        @api.post("/api/shutdown")
        async def shutdown(request: Request):
            # Flips uvicorn's should_exit so the current loop iteration drains
            # in-flight requests and returns from server.serve(). Tauri dev
            # calls this on tray-menu Quit so the manually-started backend does
            # not outlive the desktop window. Packaged lifecycle adapters use
            # the same request as their graceful first stage, followed by
            # native bounded escalation when needed. A packaged engine accepts
            # shutdown only from the shell generation that spawned it. Manual
            # development servers omit the generation and retain the existing
            # localhost behavior.
            if self._engine_generation is not None:
                supplied = request.headers.get(_ENGINE_GENERATION_HEADER, "")
                if not secrets.compare_digest(supplied, self._engine_generation):
                    return JSONResponse(
                        status_code=409,
                        content={"ok": False, "error": "stale_engine_generation"},
                    )
            self._logger.info("shutdown requested via api")
            self._imports.cancel()
            self._revoke_bakeoffs("shutdown")
            if self._uvicorn_server is not None:
                self._uvicorn_server.should_exit = True
            return {"ok": True}

        @api.post("/api/agent-event")
        async def agent_event(request: Request):
            # Rolling compatibility for older provider forwarders.
            if not self._valid_agent_hook_token(request):
                return {"ok": True}
            return await self._handle_agent_event(request)

        @api.post("/api/claude-event")
        async def claude_event(request: Request):
            # Thin alias for the installed Claude forwarder scripts (the hook
            # command embedded in ~/.claude/settings.json points here). Same
            # handler, agent pinned to "claude".
            if not self._valid_agent_hook_token(request):
                return {"ok": True}
            return await self._handle_agent_event(request, pinned_agent="claude")

        @api.get("/api/agent-hook/verify")
        async def agent_hook_verify(request: Request):
            # No content is sent until a new forwarder confirms its target. Old
            # app versions do not implement this route and therefore fail open.
            valid = self._valid_agent_hook_token(request)
            return JSONResponse(status_code=200 if valid else 403, content={"ok": valid})

        @api.post("/api/claude-hook")
        async def claude_hook(request: Request):
            # Decision-bearing Claude hooks use a separate fail-open endpoint.
            # Every disabled, malformed, expired, or internal-failure path
            # returns exactly {} so Claude Code keeps its native flow.
            if not self._valid_agent_hook_token(request):
                return {}
            return await self._handle_claude_hook(request)

        @api.post("/api/agent-transport/claude")
        async def claude_transport(request: Request):
            if not self._valid_agent_hook_token(request) or self._diagnostics_locked():
                return {}
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant) or not self._agent_provider_enabled("claude"):
                return {}
            try:
                raw = await request.body()
                if len(raw) > 1048576:
                    return {}
                body = json.loads(raw)
                from ..agent_transports import accept_claude_packet

                if (not self._acceptance_is_current(grant)
                        or self._diagnostics_locked()
                        or not self._agent_provider_enabled("claude")):
                    return {}
                if not isinstance(body, dict):
                    return {}
                store = self._get_managed_sessions()
                def visible_state(handle):
                    current = store.get_by_handle(handle)
                    if current is None:
                        return None
                    return (
                        current.transport_revision, current.last_event_kind,
                        store.can_reply(current), store.has_pending_reply(handle),
                    )
                before = visible_state(body.get("handle"))
                session, response, event = accept_claude_packet(store, body)
                if session is not None:
                    from ..agent_transports import MOD_POLL_GAP_LOG_SECONDS  # noqa: PLC0415

                    gap = self._get_claude_mod_poll_gaps().observe(body.get("session_id"))
                    if gap is not None and gap >= MOD_POLL_GAP_LOG_SECONDS:
                        # Seconds and validated enums only; never the session ID.
                        self._logger.info(
                            "[ClaudeMod] poll gap seconds=%d reconnect=%s state=%s",
                            int(gap),
                            "true" if body.get("action") == "connect" else "false",
                            body.get("state"),
                        )
                if session is not None and body.get("action") == "connect":
                    self._record_claude_connection(body.get("plugin_version"))
                if event is not None and session is not None:
                    await self._handle_claude_hook_body(event, correlation_token=session.token,
                        acceptance_grant=grant, persistent_transport=True)
                if (not self._acceptance_is_current(grant) or self._diagnostics_locked()
                        or not self._agent_provider_enabled("claude")):
                    return {}
                if (
                    body.get("action") != "poll" or event is not None
                    or before != visible_state(body.get("handle"))
                ):
                    self._broadcast_agent_sessions()
                return response
            except (ValueError, TypeError, KeyError):
                return {}

        @api.post("/api/codex-hook")
        async def codex_hook(request: Request):
            # Decision-bearing Codex hooks use the provider-specific adapter
            # and exact Codex response encoder. Every failure remains {}.
            if not self._valid_agent_hook_token(request):
                return {}
            return await self._handle_codex_hook(request)

        @api.post("/api/claude-hud/enable")
        async def claude_hud_enable(request: Request):
            # Install our Stop and Notification hooks into ~/.claude/settings.json.
            # Does NOT flip claude_hud_enabled in the app config — the frontend
            # writes that bool separately via the normal config PUT (Task 2.5).
            # Returns {"ok": True} on success; 500 + error body on failure so the
            # frontend can surface the appropriate error state.
            if self._diagnostics_locked():
                self._clear_agent_speak_state(reason="lockdown_effective")
                return JSONResponse(
                    status_code=423,
                    content={"ok": False, "error": "lockdown"},
                )
            try:
                from ..agent_integration_contract import AgentLifecycleAction

                force = await self._lifecycle_force_requested(request)
                lifecycle, result = await asyncio.to_thread(
                    self._apply_agent_plugin_action,
                    AgentLifecycleAction.RECONCILE,
                    force=force,
                )
                if result is None:
                    error_type = (
                        getattr(self._agent_plugin_adapter, "last_error_type", None)
                        or (
                            lifecycle.failure.value
                            if lifecycle.failure is not None
                            else "unknown"
                        )
                    )
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": error_type},
                    )
                public_result = self._public_claude_plugin_status(result)
                self._claude_plugin_status = public_result
                self._note_claude_reconcile(public_result)
                if public_result.get("ok") is True and self._loop is not None:
                    self._write_claude_discovery_file()
                self._logger.info(
                    "[ClaudeHUD] enable status=%s",
                    public_result.get("status", "unknown"),
                )
                return public_result
            except Exception as exc:
                self._logger.error(
                    "[ClaudeHUD] enable failed reason=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": type(exc).__name__},
                )

        @api.get("/api/claude-hud/status")
        async def claude_hud_status(refresh: bool = False):
            compatibility = await self._agent_runtime_status("claude", refresh=refresh)
            return {
                **self._claude_plugin_status,
                "compatibility": compatibility,
                "connection": self._claude_connection_state(compatibility),
            }

        @api.post("/api/claude-hud/disable")
        async def claude_hud_disable():
            # Remove our Stop and Notification hooks from ~/.claude/settings.json.
            # Does NOT flip claude_hud_enabled in the app config — the frontend
            # writes that bool separately via the normal config PUT (Task 2.5).
            # Returns {"ok": True} on success; 500 + error body on failure so the
            # frontend can show the disable-failure toast (copy-review.md #13).
            try:
                from ..agent_integration_contract import AgentLifecycleAction

                lifecycle, result = await asyncio.to_thread(
                    self._apply_agent_plugin_action,
                    AgentLifecycleAction.REMOVE,
                )
                if result is None:
                    error_type = (
                        getattr(self._agent_plugin_adapter, "last_error_type", None)
                        or (
                            lifecycle.failure.value
                            if lifecycle.failure is not None
                            else "unknown"
                        )
                    )
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": error_type},
                    )
                public_result = self._public_claude_plugin_status({
                    "ok": True,
                    **result,
                })
                self._claude_plugin_status = public_result
                if self._agent_provider_enabled("codex"):
                    self._remove_claude_discovery_file()
                    self._remove_managed_sessions_for_agent("claude")
                else:
                    self._clear_agent_speak_state(reason="feature_disabled")
                self._logger.info(
                    "[ClaudeHUD] disable status=%s",
                    public_result.get("status", "unknown"),
                )
                return public_result
            except Exception as exc:
                self._logger.error(
                    "[ClaudeHUD] disable failed reason=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": type(exc).__name__},
                )

        # -- /api/managed-sessions/* -- managed coding-agent sessions ----------
        # Registration and deregistration for CLI-wrapped coding agents.
        # wrapped sessions (Task 2.2's in-memory store). Both require the
        # per-run capability token (Task 2.1) and stay registered in every
        # build, matching the existing always-registered Claude HUD route
        # policy; the real gate is the existing claude_hud_enabled config flag
        # (scope decision 5), never a new config key or a build flag. This
        # never touches /api/claude-hook, ClaudeInteractionBroker deadlines, or
        # its fail-open {} contract.

        @api.post(
            "/api/managed-sessions/register",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_register(
            request: Request,
            body: ManagedSessionRegisterBody,
        ):
            if self._diagnostics_locked():
                return JSONResponse(status_code=423, content={"error": "lockdown"})
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if not self._agent_provider_enabled("claude"):
                return JSONResponse(
                    status_code=409, content={"error": "hud_disabled"}
                )
            session = self._get_managed_sessions().register(
                body.token, agent="claude", pid=body.pid, cwd=body.cwd
            )
            self._broadcast_agent_sessions()
            return {"session_handle": session.session_handle}

        @api.post(
            "/api/managed-sessions/v2/register",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_register_v2(
            request: Request,
            body: ManagedSessionRegisterV2Body,
        ):
            if self._diagnostics_locked():
                return JSONResponse(status_code=423, content={"error": "lockdown"})
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if not self._agent_provider_enabled(body.agent):
                return JSONResponse(
                    status_code=409, content={"error": "hud_disabled"}
                )
            try:
                session = self._get_managed_sessions().register(
                    body.token,
                    agent=body.agent,
                    pid=body.pid,
                    cwd=body.cwd,
                )
            except ValueError:
                return JSONResponse(
                    status_code=409, content={"error": "provider_mismatch"}
                )
            self._broadcast_agent_sessions()
            return {
                "protocol_version": 2,
                "agent": session.agent,
                "session_handle": session.session_handle,
            }

        @api.post(
            "/api/managed-sessions/deregister",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_deregister(body: ManagedSessionDeregisterBody):
            store = self._managed_sessions
            removed = store.deregister(body.token) if store is not None else False
            if removed:
                self._broadcast_agent_sessions()
            return {"ok": removed}

        # -- reply delivery -- claude-managed-sessions Phase 3, Task 3.3 -------
        # Three routes complete the delivery path. The app POSTs a dictated
        # reply; the wrapper long-polls for it and writes it to the PTY master
        # wrapped in bracketed paste; the wrapper then reports the write so the
        # session is marked touched.
        #
        # **Block, never queue** (scope decision 6). Every refusal is returned
        # to the caller as `accepted: false` and the payload is dropped on the
        # spot. There is no retry slot, no backlog, and no timer that revives a
        # refused reply: the only way a blocked reply reaches Claude is a new
        # explicit user action that POSTs it again.
        #
        # Refusals are reason-free by design, matching the bare-boolean
        # `can_reply` (Task 2.6).
        # Do not add a reason field "for debugging"; it leaks session state to
        # whatever renders it.

        @api.post(
            "/api/managed-sessions/{handle}/reply",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_reply(
            handle: str,
            request: Request,
            body: ManagedSessionReplyBody,
        ):
            # ================================================================
            # TRANSCRIPT DATA - DO NOT LOG
            # `body.text` is a dictated reply. Nothing in this handler may log
            # it, the handle, or a serialized session.
            # ================================================================
            from ..managed_session_paste_guard import guard_reply_payload  # noqa: PLC0415 — lazy, matching the store import below

            if self._diagnostics_locked():
                return JSONResponse(status_code=423, content={"error": "lockdown"})
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            store = self._managed_sessions
            if store is None:
                return JSONResponse(status_code=404, content={"accepted": False})
            session = store.get_by_handle(handle)
            if session is None:
                return JSONResponse(status_code=404, content={"accepted": False})
            if not self._agent_provider_enabled(session.agent):
                return JSONResponse(
                    status_code=409, content={"error": "hud_disabled"}
                )
            # Task 2.6's single safe-to-inject chokepoint, then Task 2.7's
            # payload guard. The guard runs *here*, at the backend, so a
            # hazardous payload never reaches the wire, let alone the PTY.
            if not store.can_reply(session):
                return {"accepted": False}
            if not body.text.strip():
                return {"accepted": False}
            if not guard_reply_payload(body.text):
                return {"accepted": False}
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            return {"accepted": store.set_pending_reply(handle, body.text)}

        @api.get(
            "/api/managed-sessions/{handle}/pending",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_pending(
            handle: str,
            request: Request,
            wait: Optional[float] = None,
        ):
            # Long-poll, deliberately chosen over a WebSocket: the wrapper
            # already needs a blocking HTTP client, and a long-poll degrades to
            # "no reply available" on every failure mode - a timeout, a dropped
            # connection, a restarted backend, an unknown handle - which is the
            # correct fail-closed default for a delivery channel.
            from .. import managed_sessions as _managed  # noqa: PLC0415 — experimental state

            if self._diagnostics_locked():
                return {"reply": None, "shutdown_requested": False}
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return {"reply": None, "shutdown_requested": False}
            hold = (
                _managed.PENDING_LONG_POLL_SECONDS if wait is None else float(wait)
            )
            hold = max(0.0, min(hold, _managed.MAX_PENDING_LONG_POLL_SECONDS))
            store = self._managed_sessions
            if store is None:
                return JSONResponse(
                    status_code=404,
                    content={"reply": None, "shutdown_requested": False},
                )
            session = store.get_by_handle(handle)
            if session is None:
                # A wrapper can retain a handle after selective provider
                # cleanup without observing a capability-token rotation. A
                # 404 tells it to discard that connection and re-register;
                # a successful empty response would otherwise spin forever
                # against a handle the backend can never revive.
                return JSONResponse(
                    status_code=404,
                    content={"reply": None, "shutdown_requested": False},
                )
            if not self._agent_provider_enabled(session.agent):
                return {"reply": None, "shutdown_requested": False}
            # The delivery poll is also the wrapper's liveness signal. Keeping
            # both on one connection avoids a second periodic protocol.
            if store.refresh_poll(handle) is None:
                return {"reply": None, "shutdown_requested": False}
            loop = asyncio.get_running_loop()
            deadline = loop.time() + hold
            changed = asyncio.Event()
            unsubscribe = store.watch_reply(
                handle, lambda: loop.call_soon_threadsafe(changed.set),
            )
            try:
                while True:
                    changed.clear()
                    if not self._acceptance_is_current(grant):
                        store.withdraw_pending_reply(handle)
                        return {"reply": None, "shutdown_requested": False}
                    if store.get_by_handle(handle) is None:
                        return JSONResponse(
                            status_code=404,
                            content={"reply": None, "shutdown_requested": False},
                        )
                    if store.is_shutdown_requested(handle):
                        return {"reply": None, "shutdown_requested": True}
                    payload = store.take_pending_reply(handle)
                    if payload is not None:
                        return {"reply": payload, "shutdown_requested": False}
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return {"reply": None, "shutdown_requested": False}
                    try:
                        await asyncio.wait_for(changed.wait(), remaining)
                    except asyncio.TimeoutError:
                        return {"reply": None, "shutdown_requested": False}
            finally:
                unsubscribe()

        @api.post(
            "/api/managed-sessions/{handle}/delivered",
            dependencies=[Depends(managed_session_auth.require_capability_token)],
        )
        async def managed_session_delivered(handle: str):
            # Mark the session active immediately, before Claude's matching
            # UserPromptSubmit hook arrives, so another HUD reply cannot race
            # into the same turn.
            if self._diagnostics_locked():
                return JSONResponse(status_code=423, content={"error": "lockdown"})
            store = self._managed_sessions
            session = store.get_by_handle(handle) if store is not None else None
            if session is None:
                return {"ok": False}
            if not self._agent_provider_enabled(session.agent):
                return JSONResponse(
                    status_code=409, content={"error": "hud_disabled"}
                )
            ok = store.mark_replied(handle)
            if ok:
                self._broadcast_agent_sessions()
            return {"ok": ok}

        # PATH exposure is an explicit Agent Speak setup action. The signed
        # CLI remains in the app's bin directory so updates replace it safely;
        # these routes add or remove only that exact directory from the current
        # user's PATH.
        @api.get("/api/managed-cli/status")
        async def managed_cli_status():
            from ..managed_cli import status  # noqa: PLC0415 - cold settings path

            return await asyncio.to_thread(status)

        @api.post("/api/managed-cli/install")
        async def managed_cli_install():
            from ..managed_cli import install  # noqa: PLC0415 - cold settings path

            result = await asyncio.to_thread(install)
            self._logger.info(
                "[ManagedSession] cli install status=%s",
                result.get("status", "unknown"),
            )
            return result

        @api.post("/api/managed-cli/uninstall")
        async def managed_cli_uninstall():
            from ..managed_cli import uninstall  # noqa: PLC0415 - cold settings path

            result = await asyncio.to_thread(uninstall)
            self._logger.info(
                "[ManagedSession] cli uninstall status=%s",
                result.get("status", "unknown"),
            )
            return result

        @api.post("/api/codex-hud/enable")
        async def codex_hud_enable(request: Request):
            """Install or update Whisperstream's owned Codex plugin.

            Connect sends ``{"force": true}`` so it always installs fresh.
            """

            if self._diagnostics_locked():
                self._clear_agent_speak_state(reason="lockdown_effective")
                return JSONResponse(
                    status_code=423,
                    content={"ok": False, "error": "lockdown"},
                )
            try:
                from functools import partial  # noqa: PLC0415

                from ..codex_plugin_lifecycle import enable_codex_plugin

                force = await self._lifecycle_force_requested(request)
                result = await asyncio.to_thread(
                    self._run_codex_lifecycle,
                    partial(enable_codex_plugin, force=True) if force else enable_codex_plugin,
                )
                public_result = self._public_codex_plugin_status(result)
                self._codex_plugin_status = public_result
                if public_result.get("ok") is True and self._loop is not None:
                    self._write_codex_discovery_file()
                self._logger.info(
                    "[CodexPlugin] enable status=%s",
                    public_result.get("status", "unknown"),
                )
                return public_result
            except Exception as exc:
                self._logger.error(
                    "[CodexPlugin] enable failed reason=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": type(exc).__name__},
                )

        @api.get("/api/codex-hud/status")
        async def codex_hud_status(refresh: bool = False):
            """Refresh Codex CLI and hook-trust status without mutating it."""

            # Every read launches several Codex processes (engine selection,
            # version probes, marketplace and plugin listing), about a second
            # in all. Polls reuse a recent read; any other write to the
            # status (connect, update, disable) replaces the object and so
            # invalidates it.
            cached = getattr(self, "_codex_status_read", None)
            if (
                not refresh
                and cached is not None
                and cached[1] is self._codex_plugin_status
                and time.monotonic() - cached[0] < CODEX_STATUS_REUSE_SECONDS
            ):
                return {**self._codex_plugin_status, "compatibility": await self._agent_runtime_status("codex")}
            try:
                from ..codex_plugin_lifecycle import codex_plugin_status

                result = await asyncio.to_thread(
                    self._run_codex_lifecycle,
                    codex_plugin_status,
                )
                self._codex_plugin_status = self._public_codex_plugin_status(result)
                self._codex_status_read = (time.monotonic(), self._codex_plugin_status)
            except Exception as exc:
                self._logger.warning(
                    "[CodexPlugin] status failed reason=%s", type(exc).__name__
                )
                self._codex_plugin_status = {"ok": False, "status": "failed"}
            return {**self._codex_plugin_status, "compatibility": await self._agent_runtime_status("codex", refresh=refresh)}

        @api.post("/api/codex-hud/disable")
        async def codex_hud_disable():
            """Remove only Whisperstream's owned Codex registration."""

            try:
                from ..codex_plugin_lifecycle import disable_codex_plugin

                result = await asyncio.to_thread(
                    self._run_codex_lifecycle,
                    disable_codex_plugin,
                )
                public_result = self._public_codex_plugin_status(result)
                self._codex_plugin_status = public_result
                if public_result.get("ok") is True:
                    if self._agent_provider_enabled("claude"):
                        self._remove_codex_discovery_file()
                        self._remove_managed_sessions_for_agent("codex")
                    else:
                        self._clear_agent_speak_state(reason="feature_disabled")
                self._logger.info(
                    "[CodexPlugin] disable status=%s",
                    public_result.get("status", "unknown"),
                )
                return public_result
            except Exception as exc:
                self._logger.error(
                    "[CodexPlugin] disable failed reason=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": type(exc).__name__},
                )

        # -- /api/setup/agent-speak/* -- always registered, same hook admission.
        register_agent_setup_routes(
            api, current_config=lambda: self._app.config,
            current_grant=lambda: self._current_acceptance_grant(),
            acceptance_is_current=lambda grant: self._acceptance_is_current(grant),
            handle_hook=lambda payload: self._handle_claude_hook_body(payload),
            blocked_response=self._content_blocked_response,
            arm_preview=self._arm_agent_setup_preview,
            teardown=self._teardown_agent_setup,
            get_app_dir=lambda: get_app_dir(), logger=self._logger,
        )

        # -- /api/speech/* -- agent-speech Phase 2, Task 2.2 --------------------
        # Four endpoints for the Experimental-tab speech rows + HUD stop button.
        # Always registered since 1.3.0 (the experimental 404 gate was removed);
        # agent_speech_enabled / claude_hud_enabled are the real gate, both
        # default-off. speech imports stay lazy (there are none at module scope
        # here) so importing this module never pulls in the TTS stack; the only
        # speech-package touch remains behind the live controller callback.

        register_speech_routes(
            api,
            current_config=lambda: self._app.config,
            current_controller=lambda: getattr(self._app, "speech_controller", None),
            current_download_engine=lambda: (
                self._tts_download_engine
                if (
                    self._tts_download_engine is not None
                    and self._download_thread is not None
                    and self._download_thread.is_alive()
                )
                else None
            ),
            try_start_download=self._try_start_tts_model_download,
        )

        register_version_route(api, _SOFTWARE_VERSION)

        @api.get("/api/protected-secrets/status")
        async def get_protected_secrets_status():
            injected_store = self._protected_secret_store()
            store = injected_store or get_protected_secret_store()
            return {
                "availability": store.availability().value,
            }

        register_permission_routes(
            api,
            current_shortcut_recovery=lambda: getattr(self._app, "_shortcut_recovery", None),
            permission_actions=self._permission_actions,
        )

        register_config_routes(
            api, current_config=lambda: self._app.config,
            current_config_manager=lambda: self._app.config_manager,
            reload_config=lambda cfg: self._app.reload_config(cfg),
            reconcile_input_selection=self._reconcile_macos_input_selection,
            managed_lockdown_enabled=self._managed_lockdown_enabled,
            history_disabled=self._history_disabled,
            config_saved=self._publish_config_change,
            screen_reader_detected=self._screen_reader_active,
        )

        def _current_stt_artifact_ids(
            legacy: dict[str, dict[str, bool]] | None = None,
        ) -> tuple[ModelId, ModelId | None]:
            """Resolve exact configured and loaded identities for API guards."""
            from ..stt_factory import stt_model_status

            if legacy is None:
                legacy = stt_model_status(
                    self._app.config, self._app.config_manager
                )
            configured = getattr(self._app, "configured_stt_artifact_id", None)
            try:
                configured_id = ModelId(configured) if isinstance(configured, str) else None
            except ValueError:
                configured_id = None
            if (
                configured_id is None
                or DOWNLOAD_REGISTRY[configured_id].kind != "stt"
            ):
                # Compatibility for lightweight app doubles and older embedding
                # callers. Production WhisperstreamApp owns the exact property.
                configured_id = (
                    ModelId.COHERE_TRANSCRIBE_INT8
                    if legacy.get("cohere", {}).get("active", False)
                    else ModelId.QWEN3_ASR_06B_INT8
                    if legacy["qwen"]["active"]
                    else model_id_for_parakeet(
                        self._app.config.parakeet_model
                    )
                )
            loaded = getattr(self._app, "loaded_stt_artifact_id", None)
            if isinstance(loaded, str):
                try:
                    loaded_id = ModelId(loaded)
                except ValueError:
                    loaded_id = None
                if (
                    loaded_id is not None
                    and DOWNLOAD_REGISTRY[loaded_id].kind != "stt"
                ):
                    loaded_id = None
            elif loaded is None:
                loaded_id = None
            else:
                loaded_id = (
                    configured_id if self._app.parakeet is not None else None
                )
            return configured_id, loaded_id

        def _exact_stt_status(
            legacy: dict[str, dict[str, bool]] | None = None,
        ) -> dict[str, object]:
            configured, loaded = _current_stt_artifact_ids(legacy)
            inventory = stt_artifact_inventory(
                configured_artifact_id=configured,
                loaded_artifact_id=loaded,
            )
            if getattr(self._model_lifecycle, "stt_asset_id", None):
                # The public v3 choice owns FluidAudio assets on Mac, never the
                # Windows ONNX directory with the same model-family label.
                from ..macos_stt_manager import macos_stt_model_present, macos_stt_model_root
                from ..model_downloader import model_size_bytes
                root = macos_stt_model_root()
                for row in inventory["artifacts"]:
                    if row["id"] != ModelId.PARAKEET_V3_INT8.value:
                        continue
                    size = model_size_bytes(ModelId.PARAKEET_V3_INT8, root=root) if root else 0
                    if ModelId.PARAKEET_V3_INT8 not in {configured, loaded}:
                        inventory["unused_size_bytes"] += size - row["size_bytes"]
                    row.update(downloaded=macos_stt_model_present(), size_bytes=size, adapter="fluidaudio")
            return inventory

        def _delete_stt_artifact(model_id: ModelId) -> None:
            native_asset = getattr(self._model_lifecycle, "stt_asset_id", None)
            if native_asset and model_id is ModelId.PARAKEET_V3_INT8:
                result = self._model_lifecycle.execute(ModelOperationRequest(
                    generation=self._next_model_generation(),
                    operation=ModelOperation.DELETE,
                    asset=ModelAssetRef(native_asset, ModelFamily.STT),
                ))
                if result.failure is not ModelFailure.NONE:
                    raise OSError(result.failure.value)
            else:
                delete_model(model_id)

        def _delete_unused_stt_artifacts(protected):
            if not getattr(self._model_lifecycle, "stt_asset_id", None):
                return delete_unused_stt_models(protected)
            removed = []
            for row in _exact_stt_status()["artifacts"]:
                model = ModelId(row["id"])
                if model not in protected and (row["downloaded"] or row["size_bytes"]):
                    _delete_stt_artifact(model)
                    removed.append(model)
            return removed

        @api.get("/api/stt/status")
        async def get_stt_status():
            # Authoritative exact-artifact inventory for model selection and
            # storage management. Configured and loaded identities can differ
            # during activation, so both remain visible and protected from the
            # reclaimable-byte total.
            from ..stt_factory import stt_model_status

            legacy = stt_model_status(
                self._app.config, self._app.config_manager
            )

            exact = _exact_stt_status(legacy)
            # Compatibility for older frontend callers. The current frontend
            # uses the exact artifact list; these collapsed backend fields are
            # not authoritative and remain only as an additive legacy shape.
            return {**exact, **legacy}

        @api.post("/api/stt/delete")
        async def delete_stt_model(body: SttDeleteBody):
            """Delete one exact, inactive STT artifact from disk."""
            legacy_model = (body.model or "").strip().lower()
            raw_model_id = (body.model_id or "").strip()
            if not raw_model_id and legacy_model in {"parakeet", "qwen", "cohere"}:
                raw_model_id = (
                    ModelId.COHERE_TRANSCRIBE_INT8.value
                    if legacy_model == "cohere"
                    else ModelId.QWEN3_ASR_06B_INT8.value
                    if legacy_model == "qwen"
                    else model_id_for_parakeet(self._app.config.parakeet_model).value
                )
            try:
                model_id = ModelId(raw_model_id)
            except ValueError:
                model_id = None
            if (
                model_id is None
                or DOWNLOAD_REGISTRY[model_id].kind != "stt"
            ):
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "error",
                        "reason": "unknown_model",
                        "model_id": raw_model_id,
                    },
                )

            busy_probe = getattr(self._app, "stt_activation_busy", None)
            if callable(busy_probe) and busy_probe() is True:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "error",
                        "reason": "dictation_active",
                    },
                )
            existing = self._download_thread
            if existing is not None and existing.is_alive():
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "operation_busy"},
                )
            if not self._model_operation_lock.acquire(blocking=False):
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "operation_busy"},
                )

            try:
                if callable(busy_probe) and busy_probe() is True:
                    return JSONResponse(
                        status_code=409,
                        content={"status": "error", "reason": "dictation_active"},
                    )
                configured, loaded = _current_stt_artifact_ids()
                protected = {configured}
                if loaded is not None:
                    protected.add(loaded)
                if model_id in protected:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "error",
                            "reason": "active",
                            "model_id": model_id.value,
                            "message": (
                                "Can't delete the model you're currently using. "
                                "Switch to another model first."
                            ),
                        },
                    )
                delete = (_delete_stt_artifact
                          if getattr(self._model_lifecycle, "stt_asset_id", None) and model_id is ModelId.PARAKEET_V3_INT8
                          else delete_model)
                await asyncio.to_thread(delete, model_id)
            except OSError as exc:
                self._logger.warning(
                    "[SttDelete] unlink_failed model_id=%s reason=%s",
                    model_id.value,
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "status": "error",
                        "reason": "unlink_failed",
                        "model_id": model_id.value,
                        "detail": type(exc).__name__,
                        "stt_status": _exact_stt_status(),
                    },
                )
            finally:
                self._model_operation_lock.release()

            self._logger.info("[SttDelete] deleted model_id=%s", model_id.value)
            refreshed_status = _exact_stt_status()
            if body.model_id is None and legacy_model:
                # Preserve the old response while legacy callers migrate.
                return {"status": "deleted", "model": legacy_model}
            return {
                "status": "deleted",
                "model_id": model_id.value,
                "stt_status": refreshed_status,
            }

        @api.post("/api/stt/delete-unused")
        async def delete_unused_stt_artifacts():
            """Delete every unconfigured, unloaded exact STT artifact."""
            busy_probe = getattr(self._app, "stt_activation_busy", None)
            if callable(busy_probe) and busy_probe() is True:
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "dictation_active"},
                )
            existing = self._download_thread
            if existing is not None and existing.is_alive():
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "operation_busy"},
                )
            if not self._model_operation_lock.acquire(blocking=False):
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "operation_busy"},
                )

            try:
                if callable(busy_probe) and busy_probe() is True:
                    return JSONResponse(
                        status_code=409,
                        content={"status": "error", "reason": "dictation_active"},
                    )
                configured, loaded = _current_stt_artifact_ids()
                protected = {configured}
                if loaded is not None:
                    protected.add(loaded)
                removed = await asyncio.to_thread(
                    _delete_unused_stt_artifacts,
                    protected,
                )
            except OSError as exc:
                self._logger.warning(
                    "[SttDelete] bulk_unlink_failed reason=%s",
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "status": "error",
                        "reason": "unlink_failed",
                        "detail": type(exc).__name__,
                        "stt_status": _exact_stt_status(),
                    },
                )
            finally:
                self._model_operation_lock.release()

            self._logger.info(
                "[SttDelete] bulk_deleted count=%d",
                len(removed),
            )
            return {
                "status": "deleted",
                "removed_model_ids": [model.value for model in removed],
                "stt_status": _exact_stt_status(),
            }

        # --- Lockdown Mode endpoints ---
        # Real cryptographic access control over the transcript store. Enabling
        # wraps the EXISTING protected data key under a password + a one-time
        # recovery code (lockdown_keystore), forces enhancement to the on-device
        # LLM (config.effective_provider), and hardens diagnostic logging.
        #
        # SECRETS — DO NOT LOG. These handlers receive the user's password and
        # the recovery code and hold the unwrapped data key in LockdownState.
        # Never log any of them, their bytes, or any envelope contents. The
        # status endpoint and every response below carry NO key material.
        # New and changed passwords follow the shared rule (password_rule);
        # unlock never re-checks it, so older shorter passwords still work.
        _PASSWORD_RULE_MESSAGES = {
            "password_too_short": "Use at least 12 characters.",
            "password_too_common": "That password is too common. Try a longer phrase.",
        }

        def _password_rule_response(password: str) -> JSONResponse | None:
            failure = check_new_password(password)
            if failure is None:
                return None
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error_code": failure.value,
                    "message": _PASSWORD_RULE_MESSAGES[failure.value],
                },
            )

        history_key_lock = asyncio.Lock()

        async def hold_history_key_lock():
            """Serialize every operation that creates, wraps or removes the data key.

            Recovery restore, Start fresh, recovery setup and the Lockdown
            transitions each read and replace key copies across several steps;
            one at a time keeps an earlier request from publishing a key after
            a later reset or mode change.
            """
            async with history_key_lock:
                yield

        def _lockdown_password_rejection(data_dir: Path, password: str) -> Dict[str, Any] | None:
            """Check a Lockdown password under the same backoff as unlock.

            Disable, change password and view recovery all verify the password,
            so each attempt counts toward (and is refused during) the unlock
            lockout. Returns the failure body, or None when the password is
            correct. Recovery-key attempts keep their own separate counter.
            """
            state = self._lockdown_state
            remaining = state.remaining_lockout_seconds()
            if remaining > 0:
                return {"ok": False, "lockout_remaining_seconds": remaining}
            if lockdown_keystore.unlock_with_password(data_dir, password) is None:
                remaining = state.register_failed_attempt()
                log_access("unlock_failed", None, enabled=self._diagnostics_locked())
                return {"ok": False, "lockout_remaining_seconds": remaining}
            return None

        def _lockdown_status_payload() -> Dict[str, Any]:
            """Build the GET /api/lockdown/status body (no secrets)."""
            cfg = self._app.config
            # An envelope on disk means a password-wrapped key exists (the
            # keep-history path). A no-envelope, history-off lockdown has no
            # envelope but still flips config.lockdown_mode true.
            has_envelope = self._is_lockdown_enabled_on_disk()
            enabled = self._diagnostics_locked()
            state = self._lockdown_state
            # Only an envelope-backed lockdown can be "locked" (there must be a
            # password/recovery code to unlock with); a no-envelope lockdown
            # never gates the surface.
            locked = bool(has_envelope and state.is_locked)
            return {
                "enabled": enabled,
                "managed": self._managed_lockdown_enabled(),
                "history_disabled": self._history_disabled(),
                "managed_policy_state": self._managed_lockdown_state(),
                "locked": locked,
                "lockout_remaining_seconds": state.remaining_lockout_seconds(),
                "idle_lock_seconds": cfg.lockdown_idle_lock_seconds,
                # A recovery code only exists in the envelope path. False for a
                # no-envelope lockdown — the frontend uses this (plus has_envelope)
                # to pick the slim vs full Manage/disable UI variant.
                "has_recovery_code": has_envelope,
                "has_envelope": has_envelope,
                "history_enabled": bool(getattr(cfg, "history_enabled", True)),
                # The acknowledgment VERSION the user has on record (or null).
                # The frontend compares this to its bundled LOCKDOWN_ACK_VERSION:
                # an exact match means Step 1 was already accepted for the current
                # copy and may be skipped; null / any mismatch fails closed and
                # re-shows it. (Legal Condition-2 re-review, 2026-07-12.)
                "acknowledged_version": lockdown_ack.read_ack_version(),
            }

        @api.get("/api/lockdown/status")
        async def lockdown_status():
            return _lockdown_status_payload()

        @api.post("/api/lockdown/acknowledge")
        async def lockdown_acknowledge(body: LockdownAcknowledgeBody):
            """Persist that the user accepted the Step-1 HIPAA acknowledgment.

            Records the accepted acknowledgment version + a UTC timestamp +
            software version (EULA-style, ``lockdown_ack``). The frontend calls
            this once when the user passes Step 1; subsequent enables skip the
            screen while the stored version matches the bundled one. A missing
            ``ack_version`` is rejected so a blank record can never satisfy the
            gate (fail closed).
            """
            version = (body.ack_version or "").strip()
            if not version:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "missing_ack_version"},
                )
            try:
                lockdown_ack.write_ack(version, _SOFTWARE_VERSION)
            except OSError:
                self._logger.error("lockdown acknowledgment write failed")
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "write_failed"},
                )
            return {"ok": True, "ack_version": version}

        async def require_history_not_initializing():
            busy = self._history_startup_busy_response()
            if busy is not None:
                raise HTTPException(status_code=503, detail="history_initializing")

        @api.post(
            "/api/lockdown/enable",
            dependencies=[Depends(require_history_not_initializing), Depends(hold_history_key_lock)],
        )
        async def lockdown_enable(body: LockdownEnableBody):
            if self._history_disabled() and (body.password or body.history_enabled or body.save_audio):
                return self._history_disabled_rejection()
            data_dir = self._lockdown_data_dir()
            cfg = self._app.config
            password = body.password or ""

            def _persist_history(target_cfg) -> None:
                """Persist the optional transcript-history fields when supplied.

                ``None`` leaves the current value untouched (the keep-history
                path may omit them and rely on the frontend's combined config
                write); a supplied value is coerced + stored so the enable call
                and the config stay consistent.
                """
                if body.history_enabled is not None:
                    target_cfg.history_enabled = bool(body.history_enabled)
                if body.save_audio is not None:
                    target_cfg.save_audio = bool(body.save_audio)
                if body.transcript_retention_days is not None:
                    target_cfg.transcript_retention_days = max(
                        0, int(body.transcript_retention_days)
                    )

            if password:
                if (
                    platform.system() == "Darwin"
                    and getattr(cfg, "mac_history_opt_in", False) is not True
                    and not body.keychain_consent
                ):
                    return JSONResponse(status_code=409, content={
                        "ok": False, "error_code": "history_consent_required",
                        "message": "Confirm Keychain access before saving transcripts.",
                    })
                # ---- Keep-history path (password present) — UNCHANGED crypto. ----
                # Validate the password before touching any key material.
                rule = _password_rule_response(password)
                if rule is not None:
                    return rule
                # Revoke content-bearing Agent Speak and bakeoff state before
                # the envelope or config can change. Safe diagnostics remain
                # active throughout the transition.
                self._begin_lockdown_entry()
                try:
                    # Mints + wraps under password AND a fresh recovery code;
                    # returns the recovery code to show exactly once. NEVER logged.
                    if platform.system() == "Darwin":
                        from ..macos_protected_secret import allow_keychain_interaction
                        with allow_keychain_interaction():
                            recovery_code = lockdown_keystore.enable_lockdown(
                                data_dir, password, store=self._protected_secret_store()
                            )
                    else:
                        recovery_code = lockdown_keystore.enable_lockdown(
                            data_dir, password, store=self._protected_secret_store()
                        )
                except Exception:
                    self._restore_diagnostics_after_failed_entry()
                    # Generic message only; key material stays silent.
                    self._logger.warning("lockdown enable failed: data key unavailable")
                    return JSONResponse(
                        status_code=500,
                        content={
                            "ok": False,
                            "error_code": "key_unavailable",
                            "message": "Could not enable Lockdown Mode on this device.",
                        },
                    )

                # Persist lockdown_mode=true + the idle window via the config save
                # + reload path. The stored enhancement_provider is coerced to
                # "local" so validate() passes (it rejects lockdown_mode + a
                # non-local stored provider). If AI cleanup was ON with a cloud
                # provider it is also turned OFF here (the local engine requires a
                # benchmark before it may run) — see
                # _coerce_enhancement_under_lockdown. We do NOT stash the prior
                # provider: disable never silently re-selects cloud (the user must
                # re-opt affirmatively), so there is no restore-on-off state.
                cfg.lockdown_mode = True
                if platform.system() == "Darwin":
                    cfg.mac_history_opt_in = True
                if body.idle_lock_seconds is not None:
                    cfg.lockdown_idle_lock_seconds = max(0, int(body.idle_lock_seconds))
                _persist_history(cfg)
                _coerce_enhancement_under_lockdown(cfg)
                cfg.validate()
                self._app.config_manager.save(cfg)
                self._app.reload_config(cfg)

                # Unlock the live state with the freshly-wrapped data key so the
                # transcript surface is immediately usable post-setup. Enable just
                # deleted the provider-only transcript.key (F2), so the store now
                # depends on this in-memory key — hand it over so reads/persists
                # keep working without a re-login.
                data_key = lockdown_keystore.unlock_with_password(data_dir, password)
                if platform.system() == "Darwin":
                    # The envelope now exists, so constructing history does not
                    # read Keychain. Unlock must hand its key to a real store.
                    self._app.ensure_transcript_store_initialized(history_opt_in=True)
                if data_key is None or not self._set_store_key(data_key):
                    # Envelope/config are already enabled and remain safely
                    # locked. The user can retry the normal password unlock;
                    # never advertise a partially prepared history as enabled
                    # and unlocked.
                    self._lockdown_state.lock()
                    self._end_lockdown_entry()
                    return JSONResponse(
                        status_code=503,
                        content={"ok": False, "error_code": "history_prepare_retry"},
                    )
                self._lockdown_state.register_success()
                self._lockdown_state.unlock(data_key)

                self._safe_diagnostics.emit(
                    "lockdown_transition",
                    phase="entry_complete",
                    managed=self._managed_lockdown_enabled(),
                    history_enabled=(cfg.history_enabled is True),
                )
                self._end_lockdown_entry()
                self.broadcast({"type": "lockdown.changed", "enabled": True})
                return {
                    "recovery_code": recovery_code,
                    "config": _config_payload(cfg, self._app.config_manager),
                }

            # ---- No-envelope path (no password) — history-off, no key. ----
            if self._is_lockdown_enabled_on_disk():
                # Lockdown is already on with a password. This path would clear
                # the unlock backoff and offer unlimited password guesses.
                return JSONResponse(
                    status_code=409,
                    content={"ok": False, "error_code": "lockdown_already_enabled"},
                )
            # Mark lockdown on WITHOUT calling lockdown_keystore.enable_lockdown:
            # no lockdown.key.json is written and no recovery code is minted. The
            # intended pairing is history-off, so history_enabled is forced False
            # (nothing needs a password-wrapped key when history is off). Cloud-AI
            # + logging hardening apply here exactly as in the keep-history path.
            self._begin_lockdown_entry()
            cfg.lockdown_mode = True
            cfg.history_enabled = False
            if body.idle_lock_seconds is not None:
                cfg.lockdown_idle_lock_seconds = max(0, int(body.idle_lock_seconds))
            _persist_history(cfg)
            # Never leave history_enabled true in the no-envelope path even if the
            # body asked to keep it (there is no key to gate it behind).
            cfg.history_enabled = False
            # Force provider local + turn AI cleanup off if it was ON with a cloud
            # provider (benchmark-gated local), same as the keep-history path.
            _coerce_enhancement_under_lockdown(cfg)
            cfg.validate()
            self._app.config_manager.save(cfg)
            self._app.reload_config(cfg)

            # No envelope => no password to unlock with, so the surface must never
            # gate. Force the live state unlocked (and clear any backoff) so an
            # idle timer or a stale is_locked can never wedge it into a dead 423.
            self._lockdown_state.register_success()
            self._lockdown_state._locked = False

            self._safe_diagnostics.emit(
                "lockdown_transition",
                phase="entry_complete",
                managed=self._managed_lockdown_enabled(),
                history_enabled=False,
            )
            self._end_lockdown_entry()
            self.broadcast({"type": "lockdown.changed", "enabled": True})
            # No recovery code exists in the no-envelope path.
            return {
                "recovery_code": None,
                "config": _config_payload(cfg, self._app.config_manager),
            }

        @api.post(
            "/api/lockdown/disable",
            dependencies=[Depends(require_history_not_initializing), Depends(hold_history_key_lock)],
        )
        async def lockdown_disable(body: LockdownDisableBody):
            if self._managed_lockdown_enabled():
                return self._managed_lockdown_rejection()
            data_dir = self._lockdown_data_dir()
            cfg = self._app.config

            envelope_backed = self._is_lockdown_enabled_on_disk()
            if envelope_backed:
                rejected = _lockdown_password_rejection(data_dir, body.password or "")
                if rejected is not None:
                    return rejected
                self._lockdown_state.register_success()
                data_key = lockdown_keystore.prepare_disable_lockdown(
                    data_dir,
                    body.password or "",
                    store=self._protected_secret_store(),
                )
                if data_key is None:
                    return {"ok": False}
                # Finish deferred migration/cleanup while the password
                # envelope still exists, so a retryable preparation failure
                # cannot tear down the user's only working unlock route.
                if not self._set_store_key(data_key):
                    lockdown_keystore.abort_disable_lockdown(
                        data_dir, store=self._protected_secret_store()
                    )
                    return JSONResponse(
                        status_code=503,
                        content={"ok": False, "error_code": "history_prepare_retry"},
                    )
                if not self._reload_store_key():
                    # Keep the password envelope authoritative on every live
                    # preparation failure. Remove the staged protected duplicate so
                    # a restart cannot bypass the password gate.
                    lockdown_keystore.abort_disable_lockdown(
                        data_dir, store=self._protected_secret_store()
                    )
                    self._clear_store_key()
                    return JSONResponse(
                        status_code=503,
                        content={"ok": False, "error_code": "history_prepare_retry"},
                    )
            else:
                # No-envelope (history-off) lockdown: there is no key to unwrap,
                # so no password is required. Only proceed when lockdown is
                # actually on; otherwise this is a no-op.
                if not getattr(cfg, "lockdown_mode", False):
                    return {"ok": False}

            # Persist lockdown_mode=false ONLY. Deliberately DO NOT restore the
            # prior cloud provider or re-enable AI cleanup: enable coerced the
            # provider to "local" (and may have turned cleanup off), and silently
            # re-selecting cloud on disable would resume sending transcripts
            # off-device with no affirmative user action — a privacy violation. A
            # user may disable Lockdown for unrelated reasons (e.g. they dislike
            # transcript passwording); they must opt back into cloud themselves
            # (which routes through the normal cloud-consent gate). Restore
            # diagnostic logging.
            cfg.lockdown_mode = False
            cfg.validate()
            try:
                self._app.config_manager.save(cfg)
                self._app.reload_config(cfg)
            except Exception:
                if envelope_backed:
                    cfg.lockdown_mode = True
                    try:
                        self._app.config_manager.save(cfg)
                        self._app.reload_config(cfg)
                    except Exception:
                        # The envelope remains the authoritative runtime and
                        # restart gate even if config repair is unavailable.
                        pass
                    lockdown_keystore.abort_disable_lockdown(
                        data_dir, store=self._protected_secret_store()
                    )
                    self._clear_store_key()
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error_code": "history_prepare_retry"},
                )

            if envelope_backed and not lockdown_keystore.finalize_disable_lockdown(
                data_dir, data_key
            ):
                # Config is restored to fail-closed Lockdown before the staged
                # normal-mode key is removed. The password envelope remains.
                cfg.lockdown_mode = True
                self._app.config_manager.save(cfg)
                self._app.reload_config(cfg)
                lockdown_keystore.abort_disable_lockdown(
                    data_dir, store=self._protected_secret_store()
                )
                self._clear_store_key()
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error_code": "history_prepare_retry"},
                )

            # F2: disable re-materialized transcript.key. Re-point the store at
            # the provider-protected key so post-disable reads/persists work exactly as
            # the non-lockdown path (whether or not the store held a key while
            # locked). Do this BEFORE resetting the gate.
            # Reset the gate so a later re-enable starts clean. The store key was
            # re-established just above, so this lock() only clears the (now
            # unused) LockdownState copy, not the store.
            self._lockdown_state.lock()
            self._lockdown_state.register_success()
            self._lockdown_state._locked = False  # no gate when not enabled
            self.broadcast({"type": "lockdown.changed", "enabled": False})
            return {
                "ok": True,
                "config": _config_payload(cfg, self._app.config_manager),
            }

        @api.post(
            "/api/lockdown/reset",
            dependencies=[Depends(require_history_not_initializing), Depends(hold_history_key_lock)],
        )
        async def lockdown_reset(body: LockdownResetBody):
            """Last-resort reset for a user who forgot BOTH the password and the
            recovery code. Ordinarily removes the encrypted transcript history
            and envelope, mints a fresh key, and turns Lockdown off. Nothing is
            decrypted; old ciphertext becomes cryptographically inaccessible.
            No password required; a typed "DELETE" confirmation is the guard.

            Only valid for an envelope-backed (keep-history) lockdown — the only
            case that is password-gated and can strand a user. A history-off
            lockdown already disables without a password, and a non-lockdown
            install has nothing to reset.
            """
            if self._managed_lockdown_enabled():
                return self._managed_lockdown_rejection()
            if (body.confirm or "").strip().upper() != "DELETE":
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "confirmation_required"},
                )

            data_dir = self._lockdown_data_dir()
            cfg = self._app.config
            if not self._is_lockdown_enabled_on_disk():
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "not_envelope_lockdown"},
                )
            access_log_enabled = self._diagnostics_locked()

            # 1-3. Ordinarily remove encrypted rows and recordings without
            #    decrypting (SQLite DELETE/unlink is not physical overwrite),
            #    delete the envelope + stale sealed key, and mint a fresh key
            #    for the now-empty store. The store does all three under its
            #    lock so a concurrent save can't land under the deleted key.
            store = getattr(self._app, "transcript_store", None)
            protected_store = self._protected_secret_store()

            def remove_old_keys() -> None:
                lockdown_keystore.reset_lockdown(data_dir, store=protected_store)

            store_ready = False
            wiped_via_store = False
            if store is not None and hasattr(store, "wipe_and_rekey"):
                try:
                    store_ready = bool(store.wipe_and_rekey(remove_old_keys))
                    wiped_via_store = True
                except Exception:
                    self._logger.warning("lockdown reset: transcript store wipe failed")
            if not wiped_via_store:
                # Store object unavailable (init failure) or its wipe failed:
                # delete the encrypted transcript files from disk directly so the
                # ciphertext becomes cryptographically inaccessible after the
                # envelope is removed. A live store holds transcripts.db open
                # (Windows), so the store's wipe above is the path there.
                _wipe_transcript_files(data_dir)
                remove_old_keys()
                store_ready = self._reload_store_key()

            # 4. Turn lockdown off. As with disable, do NOT restore a prior cloud
            #    provider or re-enable AI cleanup (privacy). Restore logging.
            cfg.lockdown_mode = False
            if not store_ready:
                # Reset is destructive and cannot restore the removed envelope.
                # Fail closed for future writes instead of advertising history
                # while the live store cannot encrypt them. A restart/config
                # re-enable can retry the newly sealed empty-store key.
                cfg.history_enabled = False
            cfg.validate()
            self._app.config_manager.save(cfg)
            self._app.reload_config(cfg)
            # 5. Reset the gate so a later re-enable starts clean.
            self._lockdown_state.lock()
            self._lockdown_state.register_success()
            self._lockdown_state._locked = False

            # 6. Content-free audit event (action + timestamp only, no content).
            log_access("reset", None, enabled=access_log_enabled)

            self.broadcast({"type": "lockdown.changed", "enabled": False})
            # Tell any open Transcripts view to drop its now-stale snippets,
            # parity
            # with /api/transcripts/clear. Without this a locked-out user who
            # resets keeps seeing the deleted transcript previews until remount.
            self.broadcast({"type": "transcripts.changed"})
            if not store_ready:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "error_code": "history_prepare_retry",
                        "config": _config_payload(cfg, self._app.config_manager),
                    },
                )
            return {
                "ok": True,
                "config": _config_payload(cfg, self._app.config_manager),
            }

        @api.post("/api/lockdown/unlock", dependencies=[Depends(require_history_not_initializing)])
        async def lockdown_unlock(body: LockdownUnlockBody):
            data_dir = self._lockdown_data_dir()
            state = self._lockdown_state

            # Exactly one secret must be present.
            has_password = bool(body.password)
            has_recovery = bool(body.recovery_code)
            if has_password == has_recovery:
                # Either both supplied or neither — a client error.
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error_code": "missing_secret",
                        "message": "Provide a password or a recovery code.",
                    },
                )

            if has_recovery:
                # Recovery path uses its OWN backoff accounting so it can't be
                # cross-throttled by main-path failures.
                if state.remaining_recovery_lockout_seconds() > 0:
                    return {
                        "ok": False,
                        "lockout_remaining_seconds": state.remaining_recovery_lockout_seconds(),
                    }
                data_key = lockdown_keystore.unlock_with_recovery_code(
                    data_dir, body.recovery_code or ""
                )
                if data_key is None:
                    remaining = state.register_recovery_failed_attempt()
                    # F8: content-free audit line for a failed unlock (recovery
                    # path). Action + timestamp only — NEVER the recovery code.
                    log_access(
                        "unlock_failed", None, enabled=self._diagnostics_locked()
                    )
                    return {"ok": False, "lockout_remaining_seconds": remaining}
                if not self._set_store_key(data_key):
                    return JSONResponse(
                        status_code=503,
                        content={"ok": False, "error_code": "history_prepare_retry"},
                    )
                state.register_recovery_success()
                state.unlock(data_key)
                self._repair_lockdown_key_copies()
                # F2: the store has no key while locked — hand it the unlocked
                # data key so decryption becomes possible.
                # F8: content-free audit line for a successful unlock.
                log_access("unlock", None, enabled=self._diagnostics_locked())
                # Recovery success should prompt the user to set a fresh password.
                return {"ok": True, "needs_new_password": True}

            # Password path.
            if state.remaining_lockout_seconds() > 0:
                return {
                    "ok": False,
                    "lockout_remaining_seconds": state.remaining_lockout_seconds(),
                }
            data_key = lockdown_keystore.unlock_with_password(data_dir, body.password or "")
            if data_key is None:
                remaining = state.register_failed_attempt()
                # F8: content-free audit line for a failed unlock (password
                # path). Action + timestamp only — NEVER the password.
                log_access(
                    "unlock_failed", None, enabled=self._diagnostics_locked()
                )
                return {"ok": False, "lockout_remaining_seconds": remaining}
            if not self._set_store_key(data_key):
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "error_code": "history_prepare_retry"},
                )
            state.register_success()
            state.unlock(data_key)
            self._repair_lockdown_key_copies()
            # F2: the store has no key while locked — hand it the unlocked key.
            # F8: content-free audit line for a successful unlock.
            log_access("unlock", None, enabled=self._diagnostics_locked())
            return {"ok": True}

        @api.post("/api/lockdown/lock", dependencies=[Depends(require_history_not_initializing)])
        async def lockdown_lock():
            """Manually lock the transcript surface ("Lock now").

            Only an envelope-backed, enabled lockdown can be locked: a
            no-envelope (history-off) lockdown has no password to unlock with, so
            locking it would wedge the surface into a dead 423. Idempotent — a
            second call while already locked is a no-op. Emits a content-free
            ``lock`` access-log entry (F8), the same audit line the idle
            auto-lock path writes, satisfying the 45 CFR 164.312(b) claim that
            the log records "when your history is ... locked."
            """
            cfg = self._app.config
            if not self._diagnostics_locked():
                return {"ok": False, "locked": False}
            if not self._is_lockdown_enabled_on_disk():
                # No-envelope lockdown: nothing to lock behind (no key).
                return {"ok": False, "locked": False}
            if not self._lockdown_state.is_locked:
                self._lock_surface("manual")
            return {"ok": True, "locked": True}

        @api.post(
            "/api/lockdown/change-password",
            dependencies=[Depends(require_history_not_initializing), Depends(hold_history_key_lock)],
        )
        async def lockdown_change_password(body: LockdownChangePasswordBody):
            rule = _password_rule_response(body.new_password or "")
            if rule is not None:
                return rule
            data_dir = self._lockdown_data_dir()
            if not self._is_lockdown_enabled_on_disk():
                return {"ok": False}
            rejected = _lockdown_password_rejection(data_dir, body.current_password or "")
            if rejected is not None:
                return rejected
            self._lockdown_state.register_success()
            ok = lockdown_keystore.change_password(
                data_dir, body.current_password, body.new_password
            )
            return {"ok": bool(ok)}

        @api.post(
            "/api/lockdown/view-recovery",
            dependencies=[Depends(require_history_not_initializing), Depends(hold_history_key_lock)],
        )
        async def lockdown_view_recovery(body: LockdownViewRecoveryBody):
            # Regenerate-on-view (decisions.md): the original one-time code is not
            # re-displayable, so viewing mints + rewraps a FRESH code (gated on
            # the current password) and invalidates the old recovery wrapper.
            data_dir = self._lockdown_data_dir()
            if not self._is_lockdown_enabled_on_disk():
                return {"ok": False, "recovery_code": None}
            rejected = _lockdown_password_rejection(data_dir, body.password or "")
            if rejected is not None:
                return {**rejected, "recovery_code": None}
            self._lockdown_state.register_success()
            code = lockdown_keystore.regenerate_recovery_code(data_dir, body.password)
            if code is None:
                return {"ok": False, "recovery_code": None}
            return {"ok": True, "recovery_code": code}

        # --- EULA endpoints ---

        @api.get("/api/eula/status")
        async def get_eula_status():
            from ..eula import (
                AcceptanceGrantReason,
                get_acceptance_session,
            )

            session = get_acceptance_session()
            policy = session.policy
            grant = self._current_acceptance_grant()
            acceptance = session.acceptance

            if not policy.valid:
                state = "configuration_error"
            elif grant.allowed:
                state = "ready"
            elif grant.reason is AcceptanceGrantReason.ACCEPTANCE_MISSING:
                state = "first_run"
            elif grant.reason in {
                AcceptanceGrantReason.ACCEPTANCE_INVALID,
                AcceptanceGrantReason.ACCEPTANCE_UNAVAILABLE,
            }:
                state = "configuration_error"
            elif policy.requirement_kind == "substantive_update":
                state = "substantive_update_required"
            else:
                state = "reacknowledgement_required"

            needs_first_run = state == "first_run"
            needs_amendment = state == "substantive_update_required"
            needs_reacknowledgement = state == "reacknowledgement_required"
            return {
                "state": state,
                "acceptance": acceptance,
                "notice_id": policy.notice_id,
                "requirement_kind": policy.requirement_kind,
                "summary": list(session.summary),
                "policy_reason": policy.reason.value,
                "grant_reason": grant.reason.value,
                "bundled_version": policy.terms_version,
                "bundled_sha256": policy.terms_sha256,
                "bundled_text": policy.terms_text,
                "software_version": _SOFTWARE_VERSION,
                "needs_first_run": needs_first_run,
                "needs_amendment": needs_amendment,
                "pinned_version": (
                    acceptance.get("eula_version")
                    if needs_amendment and acceptance is not None
                    else None
                ),
                "needs_reacknowledgement": needs_reacknowledgement,
            }

        @api.post("/api/eula/accept")
        async def post_eula_accept(body: EulaAcceptBody):
            from ..eula import (
                AcceptanceLedgerError,
                AcceptancePolicyError,
                write_acceptance,
            )

            try:
                record = write_acceptance(
                    body.eula_version,
                    body.eula_sha256,
                    _SOFTWARE_VERSION,
                )
            except AcceptancePolicyError:
                self._logger.warning("eula accept rejected reason=target_mismatch")
                return JSONResponse(
                    status_code=400,
                    content={"error": "mismatch"},
                )
            except AcceptanceLedgerError:
                self._logger.warning("eula accept rejected reason=evidence_invalid")
                return JSONResponse(
                    status_code=409,
                    content={"error": "acceptance_evidence_invalid"},
                )
            except OSError as exc:
                self._logger.error(
                    "[EULA] write failed error_type=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": "write_failed"},
                )

            grant = self._current_acceptance_grant()
            if not self._acceptance_is_current(grant):
                return JSONResponse(
                    status_code=409,
                    content={"error": _EULA_BLOCK_CODE},
                )
            initializer = getattr(
                self._app, "initialize_accepted_content_services", None
            )
            if callable(initializer):
                try:
                    initialized = initializer()
                except Exception:  # noqa: BLE001 - acceptance stays fail closed
                    initialized = False
                if initialized is not True or not self._acceptance_is_current(grant):
                    return JSONResponse(
                        status_code=409,
                        content={"error": _EULA_BLOCK_CODE},
                    )

            self._logger.info(
                "[EULA] Accepted: version=%s sha256=%.12s... at %s",
                body.eula_version,
                body.eula_sha256,
                record.get("eula_accepted_at"),
            )
            return record

        @api.post("/api/eula/decline-amendment")
        async def post_eula_decline_amendment(body: EulaDeclineAmendmentBody):
            from ..eula import write_opt_out, utc_now_iso8601

            if self._current_acceptance_grant().allowed:
                return JSONResponse(status_code=409, content={"error": "session_already_accepted"})
            try:
                record = write_opt_out(
                    body.declined_version,
                    body.pinned_version,
                    _SOFTWARE_VERSION,
                    expires_at=utc_now_iso8601(),  # recomputed internally by write_opt_out
                )
            except OSError as exc:
                self._logger.error("eula decline-amendment write failed reason=%s", type(exc).__name__)
                return JSONResponse(
                    status_code=500,
                    content={"error": "write_failed"},
                )

            self._logger.info(
                "eula amendment declined declined_version=%s pinned_version=%s expires_at=%s",
                body.declined_version,
                body.pinned_version,
                record.get("expires_at"),
            )
            return record

        @api.get("/api/devices")
        def get_devices():
            if getattr(getattr(self._app, "audio_capture", None), "capture_available", True) is False:
                return JSONResponse(status_code=503, content={"error": "audio_device_inventory_unavailable"})
            try:
                from ..capture import list_input_devices  # noqa: PLC0415

                devices = list_input_devices()
                self._reconcile_macos_input_selection(devices)
                return devices
            except Exception as exc:
                self._logger.warning("audio device list failed reason=%s", type(exc).__name__)
                if platform.system() == "Darwin":
                    return JSONResponse(
                        status_code=503,
                        content={"error": "audio_device_inventory_unavailable"},
                    )
                return []

        register_application_inventory_routes(
            api,
            application_context=self._application_context,
            current_config=lambda: self._app.config,
            logger=self._logger,
        )

        @api.get("/api/startup")
        async def get_startup():
            supported = is_startup_supported()
            return {
                "enabled": is_startup_enabled() if supported else False,
                "supported": supported,
            }

        @api.put("/api/startup")
        async def put_startup(body: StartupBody):
            if not is_startup_supported():
                return {"enabled": False, "supported": False}
            set_startup_enabled(body.enabled)
            return {"enabled": is_startup_enabled(), "supported": True}

        @api.post("/api/download/start")
        async def start_download():
            # Defensive: refuse a second concurrent click while a worker thread
            # is still alive. The frontend should already prevent this, but a
            # rapid double-POST shouldn't spawn two competing download threads.
            existing = self._download_thread
            if existing is not None and existing.is_alive():
                return JSONResponse(
                    status_code=409,
                    content={"status": "busy"},
                )
            if not self._model_operation_lock.acquire(blocking=False):
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "busy",
                        "reason": "operation_busy",
                    },
                )
            try:
                # The shared thread slot covers model families that do not use
                # this STT guard. Recheck it after atomically reserving the STT
                # lifecycle so acceptance cannot race either ownership domain.
                existing = self._download_thread
                if existing is not None and existing.is_alive():
                    self._model_operation_lock.release()
                    return JSONResponse(
                        status_code=409,
                        content={"status": "busy"},
                    )
                busy_probe = getattr(self._app, "stt_activation_busy", None)
                if callable(busy_probe) and busy_probe() is True:
                    self._model_operation_lock.release()
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "busy",
                            "reason": "dictation_active",
                        },
                    )
                target_config = self._app.config
                thread = threading.Thread(
                    target=self._run_model_download,
                    args=(target_config,),
                    kwargs={"operation_lock_held": True},
                    daemon=True,
                )
                self._download_thread = thread
                thread.start()
            except Exception:
                self._download_thread = None
                self._model_operation_lock.release()
                raise
            return {"status": "started"}

        @api.post("/api/download/cancel")
        async def cancel_download():
            evt = self._download_cancel_event
            thread = self._download_thread
            if evt is None or thread is None:
                return {"status": "not_running"}
            generation = getattr(self._model_lifecycle, "active_generation", None)
            if generation is not None:
                self._model_lifecycle.cancel(generation)
            else:
                evt.set()
            # Join with a generous timeout so the frontend's awaited POST
            # resolves only after the worker has cleaned up its .tmp files.
            # 10s covers a mid-chunk cancel that has to wait for the current
            # httpx chunk read to return; in practice this is sub-second.
            thread.join(timeout=10.0)
            return {"status": "cancelled" if not thread.is_alive() else "cancelling"}

        @api.get("/api/models/disk-free")
        async def get_models_disk_free(
            path: str = "",
            required_mb: int = PARAKEET_REQUIRED_MB,
        ):
            """Return free disk space for a target model-storage path.

            Used by the Parakeet onboarding pre-check and the low-disk
            re-target re-check. ``path`` is the intended destination folder
            (or any path on the desired drive); ``required_mb`` is the
            minimum free space needed (defaults to ``PARAKEET_REQUIRED_MB``).

            If ``path`` is omitted, probes the drive containing the resolved
            default models root (``get_models_root().parent``).

            If ``path`` does not exist yet, walks up to the nearest existing
            ancestor so the drive can still be probed before the folder is
            created.

            On ``OSError`` (permission denied, etc.) returns ``free_mb=-1``
            and ``sufficient=True`` (fail-open) to avoid blocking the user
            on a disk-info lookup failure.
            """
            import shutil
            from pathlib import Path as _Path
            from ..config_manager import get_models_root

            if path:
                probe = _Path(path)
                # Walk up to the nearest existing ancestor so we can probe
                # the drive even before the destination folder is created.
                while not probe.exists() and probe.parent != probe:
                    probe = probe.parent
                # If we walked all the way to an imaginary root, use cwd.
                if not probe.exists():
                    probe = _Path(".")
            else:
                # Default: probe the drive holding the configured models root.
                probe = get_models_root().parent

            try:
                usage = shutil.disk_usage(probe)
                free_mb = int(usage.free // (1024 * 1024))
            except OSError:
                free_mb = -1  # unknown — fail-open so the caller proceeds

            sufficient = free_mb < 0 or free_mb >= required_mb
            return {"free_mb": free_mb, "required_mb": required_mb, "sufficient": sufficient}

        @api.post("/api/models/relocate/check")
        async def relocate_check(body: RelocateCheckBody):
            """Classify a relocation destination before moving model files.

            Thin wrapper around :meth:`_classify_relocation`, which is shared
            with the relocation worker (the worker re-runs the same
            classification before it copies anything).
            """
            if getattr(self._model_lifecycle, "stt_asset_id", None):
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "unavailable",
                        "reason": "native-picker-required",
                    },
                )
            if self._has_resident_models():
                return JSONResponse(
                    status_code=409,
                    content={"error": _RESIDENT_MODELS_RESTART_REQUIRED},
                )
            return self._relocation.classify(body.destination)

        @api.post("/api/models/relocate")
        async def start_relocate(body: RelocateCheckBody):
            """Spawn the background relocation worker (Task 2.3).

            Reuses the shared ``_download_thread`` / ``_download_cancel_event``
            slot, so any in-flight download / benchmark / relocation blocks a new
            relocation (409) and the existing ``POST /api/download/cancel``
            aborts a running relocation.
            """
            if getattr(self._model_lifecycle, "stt_asset_id", None):
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "unavailable",
                        "reason": "native-picker-required",
                    },
                )
            if self._has_resident_models():
                return JSONResponse(
                    status_code=409,
                    content={"error": _RESIDENT_MODELS_RESTART_REQUIRED},
                )
            busy_probe = getattr(self._app, "stt_activation_busy", None)
            if callable(busy_probe) and busy_probe() is True:
                return JSONResponse(
                    status_code=409,
                    content={"status": "busy", "reason": "dictation_active"},
                )
            existing = self._download_thread
            if existing is not None and existing.is_alive():
                return JSONResponse(status_code=409, content={"status": "busy"})
            if not self._model_operation_lock.acquire(blocking=False):
                return JSONResponse(
                    status_code=409,
                    content={"status": "busy", "reason": "operation_busy"},
                )
            try:
                # Recheck the process-wide background slot after reserving the
                # model guard. A different model family does not necessarily
                # take this guard before it claims the shared thread slot.
                existing = self._download_thread
                if existing is not None and existing.is_alive():
                    self._model_operation_lock.release()
                    return JSONResponse(status_code=409, content={"status": "busy"})
                thread = threading.Thread(
                    target=self._run_model_relocation,
                    args=(body.destination,),
                    kwargs={"operation_lock_held": True},
                    daemon=True,
                )
                self._download_thread = thread
                thread.start()
            except Exception:
                self._download_thread = None
                if self._model_operation_lock.locked():
                    self._model_operation_lock.release()
                raise
            return {"status": "started"}

        @api.post("/api/models/relocate/registered")
        async def start_registered_relocate(body: RegisteredRelocateBody):
            """Start Darwin relocation from a native-owner opaque token only."""
            if not getattr(self._model_lifecycle, "stt_asset_id", None):
                self._discard_registered_model_destination(body.destination_token)
                return JSONResponse(
                    status_code=409,
                    content={"status": "unavailable", "reason": "unsupported"},
                )
            if not body.destination_token.startswith("macos-native:"):
                return JSONResponse(
                    status_code=400,
                    content={"status": "unavailable", "reason": "invalid-token"},
                )
            if self._has_resident_models():
                self._discard_registered_model_destination(body.destination_token)
                return JSONResponse(status_code=409, content={"error": _RESIDENT_MODELS_RESTART_REQUIRED})
            busy_probe = getattr(self._app, "stt_activation_busy", None)
            if callable(busy_probe) and busy_probe() is True:
                self._discard_registered_model_destination(body.destination_token)
                return JSONResponse(status_code=409, content={"status": "busy", "reason": "dictation_active"})
            existing = self._download_thread
            if existing is not None and existing.is_alive():
                self._discard_registered_model_destination(body.destination_token)
                return JSONResponse(status_code=409, content={"status": "busy"})
            thread = threading.Thread(
                target=self._run_registered_model_relocation,
                args=(body.destination_token,),
                daemon=True,
            )
            self._download_thread = thread
            thread.start()
            return {"status": "started"}

        @api.get("/api/enhancement/disk-free")
        async def get_disk_free(model_id: str = ""):
            """Return free disk space at the local-LLM cache dir.

            Used by the frontend to decide whether to fire LowDiskSpaceDialog
            before initiating a model download. The model's required disk
            footprint comes from LOCAL_BUNDLED_MODELS[model_id]['disk_mb']
            plus a 200 MB safety buffer (extraction temp file, etc.). If
            ``model_id`` is omitted or unknown, the response omits required
            fields and the frontend can fall back to its own 3 GB default.
            """
            import shutil
            from ..config_manager import get_local_llm_dir
            from ..enhancement_models import get_local_bundled_model

            target = get_local_llm_dir()
            target.parent.mkdir(parents=True, exist_ok=True)
            probe = target if target.exists() else target.parent
            try:
                usage = shutil.disk_usage(probe)
                free_mb = usage.free // (1024 * 1024)
            except OSError:
                free_mb = -1  # signal "unknown" — frontend treats as fail-open

            payload: dict = {"free_mb": free_mb}
            mid = (model_id or "").strip().lower()
            if mid:
                entry = get_local_bundled_model(mid)
                if entry is not None:
                    required = int(entry.get("disk_mb", 0)) + 200
                    payload["required_mb"] = required
                    payload["model_id"] = mid
                    payload["sufficient"] = free_mb < 0 or free_mb >= required
            return payload

        @api.post("/api/enhancement/local-activate")
        async def activate_local_candidate(body: LocalLLMDownloadBody):
            """Activate one available development candidate without downloading it.

            This is deliberately separate from the production GGUF download and
            benchmark surfaces. Candidate artifacts are supplied by the developer,
            so activation only resolves the validated external path, repoints (or
            creates) the lazy llama-server supervisor, and persists the registry's
            canonical exact ID. Missing artifacts fail before config or supervisor
            state is touched.
            """
            from ..enhancement_models import get_local_model, resolve_local_model_path
            from ..llama_server import (
                LlamaServerSupervisor,
                find_bundled_llama_server,
                runtime_acceleration_for_platform,
            )

            requested_id = (body.model_id or "").strip()
            catalog_entry = get_local_model(requested_id)
            if catalog_entry is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "unknown_model_id", "model_id": requested_id},
                )
            if catalog_entry.get("source") != "candidate":
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "candidate_model_required",
                        "model_id": requested_id,
                    },
                )

            model_id = str(catalog_entry["id"])
            gguf_path = resolve_local_model_path(catalog_entry)
            try:
                artifact_available = bool(
                    gguf_path is not None
                    and gguf_path.is_file()
                    and gguf_path.stat().st_size > 0
                )
            except OSError:
                artifact_available = False
            if not artifact_available:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "artifact_unavailable",
                        "model_id": model_id,
                    },
                )
            assert gguf_path is not None

            cfg = self._app.config
            old_model_id = cfg.enhancement_local_model
            old_gguf_path = resolve_local_model_path(old_model_id)
            supervisor = getattr(self._app, "llama_server", None)
            replacement_supervisor = None
            supervisor_repointed = False
            pool = self._resident_llama_pool()
            resident = pool.find_resident(gguf_path) if pool is not None else None

            if resident is not None:
                replacement_supervisor = resident.supervisor
            elif supervisor is None or (
                getattr(supervisor, "is_residency_pinned", False) is True
            ):
                binary_path = find_bundled_llama_server()
                if binary_path is None:
                    self._logger.warning(
                        "[CandidateModel] activation blocked reason=binary_not_found model=%s",
                        model_id,
                    )
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": "llama_server_unavailable",
                            "model_id": model_id,
                        },
                    )
                try:
                    self._ensure_resident_llama_pool(binary_path)
                    replacement_supervisor = self._new_production_llama_supervisor(
                        gguf_path,
                        binary_path=binary_path,
                    )
                except Exception as exc:  # noqa: BLE001 - controlled API failure
                    self._logger.warning(
                        "[CandidateModel] activation blocked reason=supervisor_create_failed "
                        "model=%s error=%s",
                        model_id,
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "supervisor_prepare_failed",
                            "model_id": model_id,
                        },
                    )
            else:
                try:
                    await self._restart_local_llm_for_model(
                        gguf_path, catalog_id=model_id
                    )
                    supervisor_repointed = True
                except Exception as exc:  # noqa: BLE001 - controlled API failure
                    self._logger.warning(
                        "[CandidateModel] activation blocked reason=supervisor_repoint_failed "
                        "model=%s error=%s",
                        model_id,
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "supervisor_repoint_failed",
                            "model_id": model_id,
                        },
                    )

            config_saved = False
            try:
                cfg.enhancement_local_model = model_id
                cfg.validate()
                self._app.config_manager.save(cfg)
                config_saved = True
                self._app.reload_config(cfg)
            except Exception as exc:  # noqa: BLE001 - restore live config on failure
                cfg.enhancement_local_model = old_model_id
                if config_saved:
                    try:
                        self._app.config_manager.save(cfg)
                    except Exception:  # noqa: BLE001 - original failure wins
                        pass
                if supervisor_repointed and old_gguf_path is not None:
                    try:
                        await self._restart_local_llm_for_model(
                            old_gguf_path, catalog_id=old_model_id
                        )
                    except Exception:  # noqa: BLE001 - original failure wins
                        pass
                self._logger.warning(
                    "[CandidateModel] activation blocked reason=config_save_failed "
                    "model=%s error=%s",
                    model_id,
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": "config_save_failed", "model_id": model_id},
                )

            if replacement_supervisor is not None:
                if (
                    supervisor is not None
                    and supervisor is not replacement_supervisor
                    and getattr(supervisor, "is_residency_pinned", False) is not True
                ):
                    try:
                        await supervisor.stop()
                    except Exception as exc:  # noqa: BLE001 - activation succeeded
                        self._logger.warning(
                            "[CandidateModel] old supervisor stop failed reason=%s",
                            type(exc).__name__,
                        )
                self._app.llama_server = replacement_supervisor
            self._logger.info(
                "[CandidateModel] activated model=%s action=supervisor_repointed",
                model_id,
            )
            return {"status": "activated", "model_id": model_id}

        @api.post("/api/enhancement/local-download/start")
        async def start_local_llm_download(body: LocalLLMDownloadBody):
            """Kick off a background GGUF download for a local LLM model.

            Validates *model_id* against the bundled catalog and refuses a
            second concurrent download with 409 (mirrors Parakeet path).
            Defensively re-checks disk space and refuses with 412 if the
            target dir has less than ``disk_mb + 200 MB buffer`` free, so a
            direct API hit cannot bypass the frontend's LowDiskSpaceDialog
            gate. Progress is broadcast on the shared WebSocket channel using
            the existing ``download.*`` event names, tagged with
            ``"kind": "local_llm"`` so the frontend can discriminate between
            Parakeet downloads (no ``kind`` tag) and GGUF downloads.

            On completion, saves the new ``enhancement_local_model`` to config,
            then (re-)spawns the llama-server supervisor for the new model.
            Previously downloaded GGUFs are kept on disk (no auto-eviction).
            """
            import shutil
            from ..config_manager import get_local_llm_dir
            from ..enhancement_models import get_local_bundled_model

            model_id = (body.model_id or "").strip().lower()
            catalog_entry = get_local_bundled_model(model_id)
            if catalog_entry is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "unknown model_id", "model_id": model_id},
                )

            # Defensive disk-space check: refuse with 412 if the cache dir has
            # less free space than the model + 200 MB safety buffer. The
            # frontend should already have surfaced LowDiskSpaceDialog, but a
            # direct API hit (or a stale UI) shouldn't kick off a download
            # that will fail mid-stream. shutil.disk_usage failures are
            # treated as fail-open so the download still proceeds on systems
            # where the OS denies the query — the in-flight write will fail
            # later with a clearer error if there's really no space.
            try:
                target = get_local_llm_dir()
                target.parent.mkdir(parents=True, exist_ok=True)
                probe = target if target.exists() else target.parent
                free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
                required_mb = int(catalog_entry.get("disk_mb", 0)) + 200
                if free_mb < required_mb:
                    return JSONResponse(
                        status_code=412,
                        content={
                            "error": "insufficient_disk_space",
                            "free_mb": free_mb,
                            "required_mb": required_mb,
                            "model_id": model_id,
                        },
                    )
            except OSError:
                pass  # fail-open

            existing = self._download_thread
            if existing is not None and existing.is_alive():
                return JSONResponse(
                    status_code=409,
                    content={"status": "busy"},
                )

            if body.recovery:
                from ..enhancement_models import resolve_local_model_path
                if self._benchmark.thread is not None and self._benchmark.thread.is_alive():
                    return JSONResponse(status_code=409, content={"error": "benchmark_busy"})
                if self._has_resident_models(resolve_local_model_path(catalog_entry)):
                    return JSONResponse(status_code=409, content={"error": "restart_required"})

            thread = threading.Thread(
                target=self._run_llm_model_download,
                args=(model_id,),
                kwargs={"recovery": True} if body.recovery else {},
                daemon=True,
            )
            self._download_thread = thread
            thread.start()
            return {"status": "started"}

        @api.post("/api/enhancement/local-download/cancel")
        async def cancel_local_llm_download():
            """Cancel an in-progress local LLM GGUF download.

            Mirrors ``/api/download/cancel``. Sets the cancel event and joins
            with a 10-second timeout so any in-progress ``.tmp`` cleanup
            completes before the response is returned.
            """
            evt = self._download_cancel_event
            thread = self._download_thread
            if evt is None or thread is None:
                return {"status": "not_running"}
            generation = getattr(self._model_lifecycle, "active_generation", None)
            if generation is not None:
                self._model_lifecycle.cancel(generation)
            else:
                evt.set()
            thread.join(timeout=10.0)
            return {"status": "cancelled" if not thread.is_alive() else "cancelling"}

        @api.post("/api/enhancement/local-download/delete")
        async def delete_local_llm_model(body: LocalLLMDownloadBody):
            """Delete an installed local GGUF from disk.

            Resolves the requested ``model_id`` against the bundled catalog and
            unlinks both the GGUF and any sibling ``.tmp`` left over from an
            interrupted download. When the deleted model id matches the
            currently active local model AND a llama-server supervisor is
            running, the supervisor is stopped first — Windows holds the file
            open via the supervisor process, so deletion would otherwise fail
            with a sharing violation.

            Intentionally does NOT mutate ``enhancement_local_model`` or
            ``enhancement_provider``: keeping ``enhancement_local_model`` as
            the "intended" model means re-selecting Local later prompts a
            clean re-download.
            """
            from ..enhancement_models import (
                get_local_bundled_model,
                resolve_local_model_path,
            )

            model_id = (body.model_id or "").strip().lower()
            catalog_entry = get_local_bundled_model(model_id)
            if catalog_entry is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "unknown model_id", "model_id": model_id},
                )

            gguf_path = resolve_local_model_path(catalog_entry)
            assert gguf_path is not None

            from .. import gemma_artifacts
            delete_paths = (
                [gemma_artifacts.artifact_path(gguf_path.parent, key) for key in gemma_artifacts.ARTIFACT_IDS]
                if model_id == gemma_artifacts.PUBLIC_ID else [gguf_path]
            )
            if any(self._has_resident_models(path) for path in delete_paths):
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": _RESIDENT_MODELS_RESTART_REQUIRED,
                        "model_id": model_id,
                    },
                )

            cfg = self._app.config
            if (
                getattr(cfg, "enhancement_local_model", "") == model_id
                and getattr(self._app, "llama_server", None) is not None
            ):
                await self._maybe_stop_llama_server()

            try:
                request = ModelOperationRequest(
                    generation=self._next_model_generation(),
                    operation=ModelOperation.DELETE,
                    asset=ModelAssetRef(model_id, ModelFamily.ENHANCEMENT),
                )

                def delete_gguf(_event, _report) -> ModelState:
                    for path in delete_paths:
                        path.unlink(missing_ok=True)
                        path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
                    if model_id == gemma_artifacts.PUBLIC_ID:
                        (gguf_path.parent / gemma_artifacts.MARKER).unlink(missing_ok=True)
                    return ModelState.ABSENT

                result = self._execute_windows_model_operation(request, delete_gguf)
                if result.failure is not ModelFailure.NONE:
                    raise OSError(result.failure.value)
            except OSError as exc:
                self._logger.warning(
                    "[LocalLLMDelete] unlink_failed reason=%s",
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "unlink_failed",
                        "model_id": model_id,
                        "reason": type(exc).__name__,
                    },
                )

            self._logger.info(
                "[LocalLLMDelete] deleted model_id=%s", model_id
            )
            return {"status": "deleted", "model_id": model_id}

        register_local_model_status_routes(
            api, self._benchmark, current_config=lambda: self._app.config,
            current_supervisor=lambda: getattr(self._app, "llama_server", None),
            current_download_thread=lambda: self._download_thread,
            logger=self._logger,
        )

        register_stats_routes(api, self.stats)

        register_diagnostic_routes(api, self._diagnostic_logs)

        @api.get("/api/license")
        async def get_license():
            return self._license_status_payload()

        @api.post("/api/license/activate")
        async def activate_license(body: ActivateBody):
            self._ensure_history_security_adapters()
            activate = getattr(self._license_state_adapter, "activate", None)
            result = (
                await activate(body.key)
                if callable(activate)
                else await self._app.license_manager.activate(body.key)
            )
            if not result["success"]:
                content: dict = {"error": result["error"]}
                if "error_code" in result:
                    content["error_code"] = result["error_code"]
                if "portal_url" in result:
                    content["portal_url"] = result["portal_url"]
                return JSONResponse(
                    status_code=400,
                    content=content,
                )
            status = self._license_status_payload()
            self.broadcast({"type": "license.status", **status})
            return status

        @api.post("/api/license/deactivate")
        async def deactivate_license():
            self._ensure_history_security_adapters()
            deactivate = getattr(self._license_state_adapter, "deactivate", None)
            result = (
                await deactivate()
                if callable(deactivate)
                else await self._app.license_manager.deactivate()
            )
            status = self._license_status_payload()
            response = {**status}
            if "warning" in result:
                response["warning"] = result["warning"]
            self.broadcast({"type": "license.status", **status})
            return response

        register_enhancement_execution_routes(
            api, current_config=lambda: self._app.config,
            current_enhancer=lambda: self._app.llm_enhancer,
            request_grant=self._request_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            blocked_response=self._content_blocked_response,
            broadcast=self.broadcast,
        )

        register_consent_status_route(api, current_config=lambda: self._app.config)

        register_enhancement_catalog_routes(
            api, current_config=lambda: self._app.config,
            current_llama=lambda: getattr(self._app, "llama_server", None),
            diagnostics_locked=self._diagnostics_locked,
            software_version=_SOFTWARE_VERSION,
        )

        # --- Dev tools (gated by WHISPERSTREAM_DEV_TOOLS) ---

        @api.get("/api/dev/enabled")
        async def get_dev_enabled():
            # Discovery probe — must NEVER return 404.  Returns {"enabled": false}
            # when the env var is unset so the frontend can hide the Fixture Test
            # tab without special-casing a network error.
            return {"enabled": _build_flags.DEV_TOOLS_AVAILABLE and _dev_tools_enabled()}

        # --- Experimental gate discovery probe ---

        @api.get("/api/experimental/enabled")
        async def get_experimental_enabled():
            # Always-on discovery probe (like /api/dev/enabled) — must NEVER 404.
            # Returns {"enabled": false} unless the build kept the experimental
            # modules (EXPERIMENTAL_AVAILABLE) AND WHISPERSTREAM_EXPERIMENTAL=1, so
            # the frontend can hide gated UI without special-casing a network error.
            # Production builds compile the flag to False, so this stays false there.
            return {
                "enabled": _build_flags.EXPERIMENTAL_AVAILABLE and _experimental_enabled()
            }

        # --- Dev-tools routes (gated by the build-time flag) ---
        # Registered only when DEV_TOOLS_AVAILABLE and the optional dev-tools
        # module is present. Source checkouts may omit it, and the Nuitka prod
        # binary excludes it (see _build_flags + build.py --nofollow-import-to).
        if _build_flags.DEV_TOOLS_AVAILABLE:
            # Defensive: a missing module (a source checkout, or the flag and
            # the Nuitka exclusion drifting apart) degrades to "no dev tools"
            # instead of crashing the app at startup.
            try:
                from ..dev_endpoints import register_dev_endpoints

                register_dev_endpoints(api, self)
            except ImportError:
                self._logger.info(
                    "[DevTools] dev-tools module not in this build; dev tools disabled"
                )

        register_consent_mutation_routes(
            api, current_config=lambda: self._app.config,
            save_config=lambda cfg: self._app.config_manager.save(cfg),
            reload_config=lambda cfg: self._app.reload_config(cfg),
        )

        @api.post("/api/review")
        async def post_review(body: ReviewBody, request: Request):
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            # Client-authoritative email. Reject empty/whitespace-only values
            # with 400 as defense in depth against a broken client; do not fall
            # back to license_manager.status().
            customer_email = body.customer_email.strip()
            if not customer_email:
                return JSONResponse(
                    status_code=400,
                    content={"error": "customer_email is required"},
                )
            result = await self.review_manager.submit_review({
                "customer_email": customer_email,
                "rating": body.rating,
                "app_version": _SOFTWARE_VERSION,
            })
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if result.get("success"):
                self._app.review_state.record_submission()
            return result

        @api.post("/api/feedback")
        async def post_feedback(body: FeedbackBody, request: Request):
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            # Client-authoritative email. Reject empty/whitespace-only values
            # with 400; do not fall back to license_manager.status().
            customer_email = body.customer_email.strip()
            if not customer_email:
                return JSONResponse(
                    status_code=400,
                    content={"error": "customer_email is required"},
                )
            result = await self.review_manager.submit_feedback({
                "customer_email": customer_email,
                "feedback": body.feedback,
                "app_version": _SOFTWARE_VERSION,
            })
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response(request.url.path)
            if result.get("success"):
                self._app.review_state.record_submission()
            return result

        @api.post("/api/review/dismiss")
        async def dismiss_review(body: DismissBody):
            if body.reason == "later":
                self._app.review_state.record_later()
            elif body.reason == "never":
                self._app.review_state.record_permanent_dismissal()
            elif body.reason == "github":
                # The prompt handed off to GitHub; never ask again.
                self._app.review_state.record_submission()
            else:
                self._app.review_state.record_dismissal()
            return {"status": "ok"}

        # --- Transcripts (history) endpoints ---

        @api.post("/api/transcripts/enable")
        async def enable_transcript_history(body: Dict[str, Any], request: Request):
            if self._history_disabled():
                return self._history_disabled_rejection()
            system = platform.system()
            if system not in {"Darwin", "Windows"}:
                return JSONResponse(status_code=404, content={"error": "unsupported_platform"})
            password_protected = self._is_lockdown_enabled_on_disk()
            consent_field = "history_consent" if password_protected else "keychain_consent"
            consented = body.get(consent_field) is True or (
                system == "Windows" and body.get("history_consent") is True
            )
            if not consented:
                return JSONResponse(status_code=400, content={"error": "history_consent_required"})
            grant = self._request_acceptance_grant(request)
            if not self._acceptance_is_current(grant):
                return self._content_blocked_response("/api/transcripts/enable")
            if self._diagnostics_locked() and not password_protected:
                return JSONResponse(status_code=409, content={"error": "lockdown_setup_required"})
            if password_protected and self._lockdown_state.is_locked:
                return JSONResponse(status_code=423, content={"error": "history_locked"})
            if getattr(self._app, "history_initializing", False) is True:
                return JSONResponse(status_code=409, content={"error": "history_initializing"})
            self._app.initialize_accepted_content_services(history_opt_in=True)
            thread = getattr(self._app, "_transcript_store_init_thread", None)
            if thread is not None:
                await asyncio.to_thread(thread.join)
            if not self._acceptance_is_current(grant) or getattr(self._app, "_shutting_down", False) is True:
                return self._content_blocked_response("/api/transcripts/enable")
            if self._is_lockdown_enabled_on_disk() != password_protected:
                return JSONResponse(status_code=409, content={"error": "history_protection_changed"})
            if self._diagnostics_locked() and not password_protected:
                return JSONResponse(status_code=409, content={"error": "lockdown_setup_required"})
            if password_protected:
                if self._lockdown_state.is_locked or self._lockdown_state.data_key is None:
                    return JSONResponse(status_code=423, content={"error": "history_locked"})
                if not self._set_store_key(self._lockdown_state.data_key):
                    return JSONResponse(status_code=503, content={"error": "history_key_unavailable"})
            store = getattr(self._app, "transcript_store", None)
            if (
                store is not None
                and store.decryptable is not True
                and getattr(store, "key_failure", None) == "missing"
            ):
                # The protected key is gone for good. Keep the store so history
                # reports key-missing and the user can restore or start fresh.
                return JSONResponse(status_code=409, content={"error": "history_key_missing"})
            if store is None or store.decryptable is not True:
                # No consent is persisted on denial. Release the failed store
                # so list/status polling cannot initiate more Keychain probes.
                if store is not None:
                    self._app.transcript_store = None
                    store.close()
                self._app._transcript_store_initialized = False
                return JSONResponse(status_code=503, content={"error": "history_key_unavailable"})
            # Commit consent and saving together only after the key is usable.
            cfg = (
                replace(self._app.config, mac_history_opt_in=True, history_enabled=True)
                if system == "Darwin"
                else replace(self._app.config, history_enabled=True)
            )
            self._app.config_manager.save(cfg)
            self._app.reload_config(cfg)
            self.broadcast({"type": "transcripts.changed"})
            return {"ok": True, "needs_recovery_setup": not history_recovery.has_recovery(
                self._lockdown_data_dir()
            )}

        # --- Standard history recovery ---
        # A recovery key (required) and recovery password (optional) wrap the
        # same data key Keychain/DPAPI holds, so a lost protected copy can be
        # restored. Lockdown owns recovery while it is on; these routes defer.
        #
        # SECRETS — DO NOT LOG the recovery key, password, or data key.

        def _recovery_error(status: int, code: str, **extra: Any) -> JSONResponse:
            return JSONResponse(status_code=status, content={"ok": False, "error": code, **extra})

        def _recovery_precheck(request: Request) -> JSONResponse | None:
            if self._history_disabled():
                return self._history_disabled_rejection()
            if getattr(self._app, "history_initializing", False) is True:
                return _recovery_error(409, "history_initializing")
            if self._is_lockdown_enabled_on_disk() or self._diagnostics_locked():
                return _recovery_error(409, "lockdown_active")
            if not self._acceptance_is_current(self._request_acceptance_grant(request)):
                return self._content_blocked_response(request.url.path)
            return None

        def _ready_data_key() -> bytes | None:
            store = getattr(self._app, "transcript_store", None)
            getter = getattr(store, "data_key_for_recovery", None)
            return getter() if callable(getter) else None

        def _verified_data_key() -> tuple[bytes | None, JSONResponse | None]:
            """Return the live key only when it authenticates all saved history.

            A key that loads but belongs to different history (for example a
            restored backup) must never be wrapped into a recovery envelope:
            that would overwrite the envelope that can still open the rows.
            """
            data_key = _ready_data_key()
            if data_key is None:
                return None, _recovery_error(409, "history_not_ready")
            store = getattr(self._app, "transcript_store", None)
            identity = getattr(store, "key_identity", None)
            if callable(identity):
                opened, total = identity(data_key)
                if opened < total:
                    return None, _recovery_error(409, "history_key_mismatch")
            return data_key, None

        def _live_key_mismatch(data_dir: Path) -> bool:
            """The loaded key opens none of the saved rows, and an envelope exists.

            Damaged content (a missing recording, one corrupt row) leaves the
            key opening the rest, so it isn't offered as a key restore.
            """
            if not history_recovery.has_envelope(data_dir):
                return False
            data_key = _ready_data_key()
            store = getattr(self._app, "transcript_store", None)
            identity = getattr(store, "key_identity", None)
            if data_key is None or not callable(identity):
                return False
            opened, total = identity(data_key)
            return total > 0 and opened == 0

        def _rule_rejection(password: str) -> JSONResponse | None:
            failure = check_new_password(password)
            return None if failure is None else _recovery_error(400, failure.value)

        @api.get("/api/history/recovery/status")
        async def history_recovery_status(request: Request):
            if self._history_disabled():
                return self._history_disabled_rejection()
            data_dir = self._lockdown_data_dir()
            lockdown = self._is_lockdown_enabled_on_disk() or self._diagnostics_locked()
            store = getattr(self._app, "transcript_store", None)
            availability = self._history_availability()
            key_missing = not lockdown and availability is SecurityAvailability.KEY_MISSING
            has_envelope = not lockdown and history_recovery.has_recovery(data_dir)
            # The live key loads but can't read saved history (for example a
            # restored backup key). The envelope may still open it: offer restore.
            key_mismatch = not lockdown and _live_key_mismatch(data_dir)
            ready_key = _ready_data_key()
            # An envelope for an older key (the protected key was replaced on an
            # empty store) no longer protects anything; ask for setup again.
            has_key = has_envelope and (
                ready_key is None
                or key_mismatch
                or history_recovery.matches_key(data_dir, ready_key)
            )
            count: int | None = None
            has_audio = False
            if not lockdown and store is not None and self._acceptance_is_current(
                self._request_acceptance_grant(request)
            ):
                try:
                    count = store.record_count()
                    has_audio = store.has_audio()
                except Exception:  # noqa: BLE001 - content-free status only
                    count = None
            state = self._history_recovery_state
            return {
                "lockdown": lockdown,
                "has_recovery_key": has_key,
                "has_recovery_password": not lockdown and history_recovery.has_password(data_dir),
                "key_missing": key_missing,
                "key_mismatch": key_mismatch,
                "needs_setup": (
                    not lockdown
                    and not key_missing
                    and not key_mismatch
                    and bool(getattr(self._app.config, "history_enabled", False))
                    and ready_key is not None
                    and not has_key
                ),
                "record_count": count,
                "has_audio": has_audio,
                "key_lockout_remaining_seconds": state.remaining_recovery_lockout_seconds(),
                "password_lockout_remaining_seconds": state.remaining_lockout_seconds(),
            }

        @api.post("/api/history/recovery/setup", dependencies=[Depends(hold_history_key_lock)])
        async def history_recovery_setup(request: Request):
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            data_key, rejected = _verified_data_key()
            if rejected is not None:
                return rejected
            data_dir = self._lockdown_data_dir()
            confirmed = history_recovery.has_recovery(data_dir)
            if confirmed and history_recovery.matches_key(data_dir, data_key):
                # Replacing a working key goes through regenerate, which keeps
                # the old key valid until the new one is confirmed.
                return _recovery_error(409, "recovery_already_set_up")
            try:
                if confirmed and not history_recovery.has_key_check(data_dir):
                    # An older envelope that may still hold this key: stage the
                    # new key beside it so the saved one keeps working until
                    # the user confirms the replacement.
                    recovery_key = history_recovery.regenerate(data_dir, data_key)
                    if recovery_key is None:
                        raise RuntimeError("recovery staging unavailable")
                else:
                    # No envelope, a pending setup, or one provably for another
                    # key (history verified above opens only with this key).
                    recovery_key = history_recovery.create(data_dir, data_key, confirmed=False)
            except Exception:  # noqa: BLE001 - generic failure, no secret detail
                self._logger.warning("history recovery setup status=failed")
                return _recovery_error(500, "recovery_setup_failed")
            return {
                "ok": True,
                "recovery_key": recovery_key,
                "generation": history_recovery.pending_generation(data_dir),
            }

        @api.post("/api/history/recovery/confirm", dependencies=[Depends(hold_history_key_lock)])
        async def history_recovery_confirm(body: HistoryRecoveryPasswordBody, request: Request):
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            if body.password:
                rule = _rule_rejection(body.password)
                if rule is not None:
                    return rule
            data_key, rejected = _verified_data_key()
            if rejected is not None:
                return rejected
            data_dir = self._lockdown_data_dir()
            if not history_recovery.generation_matches(data_dir, body.generation):
                # A newer setup or new-key request replaced the key this client
                # showed. Confirming would promote a key the user never saw.
                return _recovery_error(409, "recovery_generation_stale")
            pending = history_recovery.pending_generation(data_dir) is not None or (
                history_recovery.has_envelope(data_dir)
                and not history_recovery.has_recovery(data_dir)
            )
            if not history_recovery.confirm(
                data_dir,
                data_key,
                password=body.password or None,
                generation=body.generation,
            ):
                if not pending and history_recovery.has_envelope(data_dir):
                    return _recovery_error(409, "recovery_generation_stale")
                return _recovery_error(409, "recovery_setup_required")
            self.broadcast({"type": "transcripts.changed"})
            return {"ok": True}

        @api.post("/api/history/recovery/regenerate", dependencies=[Depends(hold_history_key_lock)])
        async def history_recovery_regenerate(request: Request):
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            data_key, rejected = _verified_data_key()
            if rejected is not None:
                return rejected
            data_dir = self._lockdown_data_dir()
            recovery_key = history_recovery.regenerate(data_dir, data_key)
            if recovery_key is None:
                return _recovery_error(409, "recovery_setup_required")
            return {
                "ok": True,
                "recovery_key": recovery_key,
                "generation": history_recovery.pending_generation(data_dir),
            }

        @api.post("/api/history/recovery/password", dependencies=[Depends(hold_history_key_lock)])
        async def history_recovery_password(body: HistoryRecoveryPasswordBody, request: Request):
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            if body.password:
                rule = _rule_rejection(body.password)
                if rule is not None:
                    return rule
            data_key, rejected = _verified_data_key()
            if rejected is not None:
                return rejected
            if not history_recovery.set_password(
                self._lockdown_data_dir(), data_key, body.password or None
            ):
                return _recovery_error(409, "recovery_setup_required")
            return {"ok": True, "has_recovery_password": bool(body.password)}

        @api.post("/api/history/recovery/unlock", dependencies=[Depends(hold_history_key_lock)])
        async def history_recovery_unlock(body: HistoryRecoveryUnlockBody, request: Request):
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            if bool(body.recovery_key) == bool(body.password):
                return _recovery_error(400, "missing_secret")
            state = self._history_recovery_state
            data_dir = self._lockdown_data_dir()
            availability = self._history_availability()
            restorable = availability is SecurityAvailability.KEY_MISSING or (
                _live_key_mismatch(data_dir)
            )
            if not restorable:
                return _recovery_error(409, "history_key_not_missing")
            if body.recovery_key:
                remaining = state.remaining_recovery_lockout_seconds()
                if remaining > 0:
                    return {"ok": False, "lockout_remaining_seconds": remaining}
                data_key = history_recovery.unlock(data_dir, recovery_key=body.recovery_key)
                if data_key is None:
                    return {
                        "ok": False,
                        "error": "recovery_key_invalid",
                        "lockout_remaining_seconds": state.register_recovery_failed_attempt(),
                    }
                state.register_recovery_success()
            else:
                remaining = state.remaining_lockout_seconds()
                if remaining > 0:
                    return {"ok": False, "lockout_remaining_seconds": remaining}
                data_key = history_recovery.unlock(data_dir, password=body.password)
                if data_key is None:
                    return {
                        "ok": False,
                        "error": "recovery_password_invalid",
                        "lockout_remaining_seconds": state.register_failed_attempt(),
                    }
                state.register_success()
            store = getattr(self._app, "transcript_store", None)
            opens = getattr(store, "key_opens_history", None)
            if callable(opens) and not opens(data_key):
                # The envelope wraps an older key that can't read this history.
                return _recovery_error(409, "recovery_key_stale")
            # Put the key back in Keychain/DPAPI, then re-point the live store.
            try:
                if platform.system() == "Darwin":
                    from ..macos_protected_secret import allow_keychain_interaction

                    with allow_keychain_interaction():
                        await asyncio.to_thread(
                            save_data_key,
                            data_dir / "transcript.key",
                            data_key,
                            store=self._protected_secret_store(),
                        )
                else:
                    await asyncio.to_thread(
                        save_data_key,
                        data_dir / "transcript.key",
                        data_key,
                        store=self._protected_secret_store(),
                    )
            except ProtectedSecretError as error:
                self._logger.warning(
                    "history recovery restore status=failed failure=%s", error.failure.value
                )
                code = (
                    "restore_access_denied"
                    if error.failure is ProtectedSecretFailure.ACCESS_DENIED
                    else "restore_failed"
                )
                return _recovery_error(503, code)
            except Exception:  # noqa: BLE001 - generic failure, no secret detail
                self._logger.warning("history recovery restore status=failed")
                return _recovery_error(503, "restore_failed")
            if not self._reload_store_key():
                return _recovery_error(503, "restore_failed")
            run_retention_sweep(self._app.config, self._app.transcript_store)
            self.broadcast({"type": "transcripts.changed"})
            return {"ok": True}

        @api.post("/api/history/reset", dependencies=[Depends(hold_history_key_lock)])
        async def history_reset(body: HistoryResetBody, request: Request):
            """Start fresh: delete all saved history and its keys without decrypting."""
            rejected = _recovery_precheck(request)
            if rejected is not None:
                return rejected
            if body.confirm != "DELETE":
                return _recovery_error(400, "confirm_required")
            data_dir = self._lockdown_data_dir()
            store = getattr(self._app, "transcript_store", None)
            protected_store = self._protected_secret_store()

            def remove_old_keys() -> None:
                history_recovery.delete(data_dir)
                try:
                    delete_data_key(data_dir / "transcript.key", store=protected_store)
                except Exception:  # noqa: BLE001 - a dangling reference is removed below
                    try:
                        (data_dir / "transcript.key").unlink(missing_ok=True)
                    except OSError:
                        self._logger.warning("history reset: failed to remove stale protected key")

            def wipe() -> bool:
                if store is not None and hasattr(store, "wipe_and_rekey"):
                    try:
                        return bool(store.wipe_and_rekey(remove_old_keys))
                    except Exception:  # noqa: BLE001
                        self._logger.warning("history reset: transcript store wipe failed")
                        return False
                _wipe_transcript_files(data_dir)
                remove_old_keys()
                return True

            if platform.system() == "Darwin":
                from ..macos_protected_secret import allow_keychain_interaction

                with allow_keychain_interaction():
                    ready = await asyncio.to_thread(wipe)
            else:
                ready = await asyncio.to_thread(wipe)
            self._history_recovery_state.register_success()
            self._history_recovery_state.register_recovery_success()
            if not ready:
                return _recovery_error(503, "history_key_unavailable")
            self.broadcast({"type": "transcripts.changed"})
            return {"ok": True}

        register_transcript_list_route(
            api,
            current_store=lambda: getattr(self._app, "transcript_store", None),
            request_grant=self._request_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            blocked_response=self._content_blocked_response,
            locked_response=self._transcript_locked_response,
            history_availability=self._history_availability,
            diagnostics_locked=self._diagnostics_locked,
            note_activity=lambda: self._lockdown_state.note_activity(),
        )

        # Re-enhance is intentionally absent from production builds. Dogfood
        # builds retain the route, then require the strict runtime flag on every
        # request so a stale frontend cannot reach the surface after restart.
        if _build_flags.EXPERIMENTAL_AVAILABLE:

            @api.post("/api/experimental/transcripts/{id}/bakeoff")
            async def start_transcript_bakeoff(
                id: str, body: BakeoffStartBody, request: Request
            ):  # noqa: A002 - matches the transcript-store API
                """Start an ephemeral comparison from one saved raw record."""
                # ========================================================================
                # TRANSCRIPT DATA - DO NOT LOG
                # Never log the record, its raw text, Bakeoff output, or exceptions.
                # ========================================================================
                if not _experimental_enabled():
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )
                grant = self._request_acceptance_grant(request)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                locked_resp = self._transcript_locked_response()
                if locked_resp is not None or bool(
                    getattr(self._app.config, "lockdown_mode", False)
                ):
                    return locked_resp or JSONResponse(
                        status_code=423, content={"locked": True}
                    )

                validated = self._validate_bakeoff_selections(body.selections)
                if isinstance(validated, JSONResponse):
                    return validated
                selections, bindings = validated
                resident_pool = self._prepare_bakeoff_resident_pool()
                if isinstance(resident_pool, JSONResponse):
                    return resident_pool
                memory_error = self._bakeoff_memory_preflight(bindings)
                if memory_error is not None:
                    return memory_error

                store = getattr(self._app, "transcript_store", None)
                if store is None:
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )
                record = store.get(id)
                raw_text = record.get("raw_text") if isinstance(record, dict) else None
                if not isinstance(raw_text, str) or not raw_text:
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )
                log_access("get", id, enabled=self._diagnostics_locked())
                self._lockdown_state.note_activity()
                authority_error = self._bakeoff_authority_error(grant)
                if authority_error is not None:
                    return self._content_blocked_response(request.url.path)

                from ..enhancement_bakeoff import (
                    BakeoffBusyError,
                    BakeoffCapacityError,
                )

                try:
                    execute_pair = self._bakeoff_executor(
                        selections,
                        grant,
                        resident_pool=resident_pool,
                    )
                    run_id = self._app.start_enhancement_bakeoff(
                        raw_text=raw_text,
                        selections=selections,
                        execute_pair=execute_pair,
                    )
                except BakeoffBusyError:
                    snapshot_provider = getattr(
                        self._app, "dictation_state_snapshot", None
                    )
                    state = (
                        snapshot_provider()
                        if callable(snapshot_provider)
                        else {}
                    )
                    recording = bool(getattr(self._app, "_recording", False)) or (
                        isinstance(state, dict)
                        and state.get("phase") == "recording"
                    )
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": (
                                "recording_active"
                                if recording
                                else "evaluation_busy"
                            )
                        },
                    )
                except BakeoffCapacityError:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "session_capacity_exceeded"},
                    )
                except Exception as exc:  # noqa: BLE001 - content-free failure
                    self._logger.warning(
                        "[Bakeoff] start failed error_type=%s",
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=500,
                        content={"error": "bakeoff_start_failed"},
                    )

                manager = getattr(self._app, "enhancement_bakeoff", None)
                snapshot_provider = getattr(manager, "snapshot", None)
                snapshot = (
                    snapshot_provider(run_id)
                    if callable(snapshot_provider)
                    else None
                )
                generation = getattr(snapshot, "generation", None)
                pairs = getattr(snapshot, "pairs", ())
                if not isinstance(generation, int) or len(pairs) != len(selections):
                    self._app.revoke_enhancement_bakeoff()
                    return JSONResponse(
                        status_code=500,
                        content={"error": "bakeoff_start_failed"},
                    )
                self._remember_bakeoff_authority(run_id, grant, generation)
                self.broadcast(
                    {
                        "type": "enhancement.bakeoff.started",
                        "version": _BAKEOFF_EVENT_VERSION,
                        "run_id": run_id,
                        "generation": generation,
                        "total_count": len(pairs),
                    },
                    acceptance_grant=grant,
                )
                return JSONResponse(
                    status_code=202,
                    content={
                        "run_id": run_id,
                        "generation": generation,
                        "total_count": len(pairs),
                    },
                )

            @api.post("/api/experimental/bakeoffs/{run_id}/cancel")
            async def cancel_transcript_bakeoff(run_id: str):
                if not _experimental_enabled():
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )
                # Cancellation is content-free cleanup and stays available
                # while content authority is being revoked.
                manager = getattr(self._app, "enhancement_bakeoff", None)
                snapshot = manager.snapshot(run_id)
                if snapshot is None:
                    return JSONResponse(
                        status_code=404, content={"error": "run_not_found"}
                    )
                if not manager.cancel(run_id):
                    return JSONResponse(
                        status_code=409, content={"error": "run_not_active"}
                    )
                return {"run_id": run_id, "cancelled": True}

            @api.post("/api/experimental/bakeoffs/{run_id}/retry")
            async def retry_transcript_bakeoff(
                run_id: str, body: BakeoffRetryBody, request: Request
            ):
                if not _experimental_enabled():
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )
                grant = self._request_acceptance_grant(request)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                locked_resp = self._transcript_locked_response()
                if locked_resp is not None:
                    return locked_resp
                manager = getattr(self._app, "enhancement_bakeoff", None)
                snapshot = manager.snapshot(run_id)
                pairs = getattr(snapshot, "pairs", ())
                if snapshot is None:
                    return JSONResponse(
                        status_code=404, content={"error": "run_not_found"}
                    )
                if (
                    isinstance(body.pair_index, bool)
                    or body.pair_index < 0
                    or body.pair_index >= len(pairs)
                ):
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid_pair_index"},
                    )
                selection = pairs[body.pair_index].selection
                resident_pool = self._prepare_bakeoff_resident_pool()
                if isinstance(resident_pool, JSONResponse):
                    return resident_pool
                requested = [
                    BakeoffPairBody(
                        model_id=selection.model_id,
                        profile_id=selection.profile_id,
                    )
                ]
                validated = self._validate_bakeoff_selections(
                    requested, minimum=1
                )
                if isinstance(validated, JSONResponse):
                    return validated
                selections, bindings = validated
                memory_error = self._bakeoff_memory_preflight(bindings)
                if memory_error is not None:
                    return memory_error
                from ..enhancement_bakeoff import BakeoffBusyError

                run_lock = getattr(self._app, "_lock", None)
                if run_lock is None or not run_lock.acquire(blocking=False):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "evaluation_busy"},
                    )
                try:
                    state_provider = getattr(
                        self._app, "dictation_state_snapshot", None
                    )
                    state = (
                        state_provider() if callable(state_provider) else {}
                    )
                    recording = bool(
                        getattr(self._app, "_recording", False)
                    ) or (
                        isinstance(state, dict)
                        and state.get("phase") == "recording"
                    )
                    if recording:
                        return JSONResponse(
                            status_code=409,
                            content={"error": "recording_active"},
                        )
                    if isinstance(state, dict) and state.get("phase") != "idle":
                        return JSONResponse(
                            status_code=409,
                            content={"error": "evaluation_busy"},
                        )
                    execute_pair = self._bakeoff_executor(
                        selections,
                        grant,
                        resident_pool=resident_pool,
                    )
                    retried = manager.retry(
                        run_id,
                        pair_index=body.pair_index,
                        execute_pair=execute_pair,
                    )
                except BakeoffBusyError:
                    return JSONResponse(
                        status_code=409, content={"error": "evaluation_busy"}
                    )
                except Exception as exc:  # noqa: BLE001 - content-free failure
                    self._logger.warning(
                        "[Bakeoff] retry failed error_type=%s",
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=500,
                        content={"error": "bakeoff_retry_failed"},
                    )
                finally:
                    # The synchronous retry call claims the evaluation lease;
                    # its awaited pair work runs only after this lock is gone.
                    run_lock.release()
                if not retried:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "result_not_retryable"},
                    )
                refreshed = manager.snapshot(run_id)
                generation = getattr(refreshed, "generation", None)
                if not isinstance(generation, int):
                    return JSONResponse(
                        status_code=500,
                        content={"error": "bakeoff_retry_failed"},
                    )
                self._remember_bakeoff_authority(run_id, grant, generation)
                return JSONResponse(
                    status_code=202,
                    content={
                        "run_id": run_id,
                        "generation": generation,
                        "pair_index": body.pair_index,
                    },
                )

            @api.post("/api/experimental/transcripts/{id}/reenhance")
            async def reenhance_transcript(
                id: str, request: Request
            ):  # noqa: A002 - matches the transcript-store API
                """Ephemerally enhance one saved record's server-held raw text."""
                # ============================================================================
                # TRANSCRIPT DATA - DO NOT LOG
                # MAKE SURE you do not log transcript data anywhere. If you add a new function
                # that receives, returns, or passes transcript text, COPY this banner to it.
                # Use `log_transcription_event()` from whisperstream.logger for metadata only
                # (char count, word count, duration, stage name). Never pass `text`,
                # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
                # ============================================================================
                if not _experimental_enabled():
                    return JSONResponse(
                        status_code=404, content={"detail": "Not Found"}
                    )

                grant = self._request_acceptance_grant(request)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                locked_resp = self._transcript_locked_response()
                if locked_resp is not None:
                    return locked_resp

                app = self._app
                cfg = app.config
                if not bool(getattr(cfg, "enhancement_enabled", False)):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "enhancement_disabled"},
                    )

                provider = str(cfg.effective_provider() or "").strip().lower()
                if not provider:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "enhancement_not_configured"},
                    )

                if not enhancement_is_admitted(cfg):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "enhancement_consent_required"},
                    )
                if provider not in {"local", "ollama"} and not cfg.active_api_key():
                    return JSONResponse(
                        status_code=409,
                        content={"error": "enhancement_not_configured"},
                    )

                profile_id = str(getattr(cfg, "default_profile_id", "") or "")
                profiles = getattr(cfg, "profiles", [])
                profile = next(
                    (
                        candidate
                        for candidate in profiles
                        if isinstance(candidate, dict)
                        and candidate.get("id") == profile_id
                    ),
                    None,
                )
                if not profile_id or profile is None:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "default_profile_unavailable"},
                    )
                profile_name = str(profile.get("name") or profile_id)

                # The bundled Local path is lazy. Start the existing prepare +
                # warm lifecycle, then ask the user to retry rather than running
                # a call that production would silently fall back to raw.
                if provider == "local" and not str(
                    getattr(cfg, "enhancement_local_endpoint", "") or ""
                ).strip():
                    supervisor = getattr(app, "llama_server", None)
                    if supervisor is None or not bool(
                        getattr(supervisor, "is_ready", False)
                    ):
                        await self._ensure_selected_llama_server(warm_if_enabled=True)
                        if not self._acceptance_is_current(grant):
                            return self._content_blocked_response(request.url.path)
                        supervisor = getattr(app, "llama_server", None)
                        error = (
                            "local_model_warming"
                            if supervisor is not None
                            else "local_model_unavailable"
                        )
                        return JSONResponse(
                            status_code=409,
                            content={"error": error},
                        )

                enhancer = getattr(app, "llm_enhancer", None)
                resolver = getattr(enhancer, "_resolve_prompt_contract", None)
                if enhancer is None or not callable(resolver):
                    return JSONResponse(
                        status_code=503,
                        content={"error": "enhancer_unavailable"},
                    )
                try:
                    resolution = resolver(profile_id)
                except Exception:  # noqa: BLE001 - stable, content-free API failure
                    return JSONResponse(
                        status_code=500,
                        content={"error": "transcript_reenhancement_failed"},
                    )
                profile_mode = str(
                    getattr(resolution, "profile_mode", "") or "custom"
                )

                model_fields = {
                    "gemini": "enhancement_gemini_model",
                    "openai": "enhancement_openai_model",
                    "anthropic": "enhancement_anthropic_model",
                    "custom": "enhancement_custom_model",
                    "ollama": "enhancement_ollama_model",
                    "local": "enhancement_local_model",
                }

                def _configuration_snapshot(config) -> tuple[str, str, str]:
                    current_provider = str(
                        config.effective_provider() or ""
                    ).strip().lower()
                    model_field = model_fields.get(current_provider, "")
                    current_model = str(
                        getattr(config, model_field, "") if model_field else ""
                    )
                    current_profile = str(
                        getattr(config, "default_profile_id", "") or ""
                    )
                    return current_provider, current_model, current_profile

                configuration = _configuration_snapshot(cfg)
                model_id = configuration[1]
                run_lock = getattr(app, "_lock", None)
                if run_lock is None or not run_lock.acquire(blocking=False):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "enhancement_busy"},
                    )

                from ..enhancement_bakeoff import EvaluationKind

                evaluation_lease = getattr(app, "evaluation_lease", None)
                try_acquire = getattr(evaluation_lease, "try_acquire", None)
                release_lease = getattr(evaluation_lease, "release", None)
                evaluation_token = None
                try:
                    if bool(getattr(app, "_recording", False)):
                        return JSONResponse(
                            status_code=409,
                            content={"error": "recording_active"},
                        )
                    snapshot_provider = getattr(app, "dictation_state_snapshot", None)
                    snapshot = snapshot_provider() if callable(snapshot_provider) else {}
                    phase = snapshot.get("phase") if isinstance(snapshot, dict) else None
                    if phase == "recording":
                        return JSONResponse(
                            status_code=409,
                            content={"error": "recording_active"},
                        )
                    if phase == "processing":
                        return JSONResponse(
                            status_code=409,
                            content={"error": "enhancement_busy"},
                        )
                    if not self._acceptance_is_current(grant):
                        return self._content_blocked_response(request.url.path)

                    if not callable(try_acquire) or not callable(release_lease):
                        return JSONResponse(
                            status_code=503,
                            content={"error": "enhancer_unavailable"},
                        )
                    evaluation_token = try_acquire(EvaluationKind.NORMAL)
                    if evaluation_token is None:
                        return JSONResponse(
                            status_code=409,
                            content={"error": "enhancement_busy"},
                        )
                finally:
                    # Never retain the app's threading lock while awaited
                    # provider/model work is running. The evaluation lease is
                    # the cross-path serialization capability from here on.
                    run_lock.release()

                try:
                    store = getattr(app, "transcript_store", None)
                    if store is None:
                        return JSONResponse(
                            status_code=404, content={"detail": "Not Found"}
                        )
                    record = store.get(id)
                    if record is None or not isinstance(record.get("raw_text"), str):
                        return JSONResponse(
                            status_code=404, content={"detail": "Not Found"}
                        )
                    raw_text = record["raw_text"]
                    if not self._acceptance_is_current(grant):
                        return self._content_blocked_response(request.url.path)

                    log_access("get", id, enabled=self._diagnostics_locked())
                    self._lockdown_state.note_activity()

                    target_words = int(
                        getattr(cfg, "enhancement_words_per_chunk", 120) or 120
                    )
                    mode = (
                        "batched"
                        if bool(getattr(cfg, "enhancement_chunked_cleanup", True))
                        and enhancer.will_enhance(raw_text)
                        and len(raw_text.split()) > target_words
                        else "whole"
                    )

                    from ..main import run_transcript_evaluation

                    started_at = time.perf_counter()
                    output, chunk_count = await run_transcript_evaluation(
                        enhancer,
                        raw_text,
                        mode=mode,
                        profile_id=profile_id,
                        target_words=target_words,
                        limit_first_chunk=bool(
                            getattr(cfg, "enhancement_limit_first_chunk", True)
                        ),
                    )
                    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                    if not self._acceptance_is_current(grant):
                        return self._content_blocked_response(request.url.path)
                    if _configuration_snapshot(app.config) != configuration:
                        return JSONResponse(
                            status_code=409,
                            content={"error": "enhancement_configuration_changed"},
                        )

                    unchanged = output == raw_text
                    unchanged_reason = None
                    if unchanged:
                        unchanged_reason = (
                            "short_input"
                            if len(raw_text.split()) <= 3
                            else "provider_or_safeguard_fallback"
                        )
                    return {
                        "provider": provider,
                        "model_id": model_id,
                        "profile_id": profile_id,
                        "profile_name": profile_name,
                        "profile_mode": profile_mode,
                        "mode": mode,
                        "chunk_count": chunk_count,
                        "elapsed_ms": elapsed_ms,
                        "output": output,
                        "unchanged": unchanged,
                        "unchanged_reason": unchanged_reason,
                    }
                except Exception as exc:  # noqa: BLE001 - stable API failure
                    # Metadata only. Provider failures may echo submitted text,
                    # so never include the exception message in diagnostics.
                    self._logger.warning(
                        "[Reenhance] run failed provider=%s reason=%s",
                        provider,
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=500,
                        content={"error": "transcript_reenhancement_failed"},
                    )
                finally:
                    release_lease(evaluation_token)

        register_transcript_record_routes(
            api,
            current_store=lambda: getattr(self._app, "transcript_store", None),
            request_grant=self._request_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            blocked_response=self._content_blocked_response,
            locked_response=self._transcript_locked_response,
            history_availability=self._history_availability,
            diagnostics_locked=self._diagnostics_locked,
            note_activity=lambda: self._lockdown_state.note_activity(),
            current_store_adapter=lambda: self._history_store_adapter,
            unavailable_response=self._history_unavailable_response,
            broadcast=self.broadcast,
        )

        register_import_route(
            api, self._imports, current_config=lambda: self._app.config,
            current_store=lambda: getattr(self._app, "transcript_store", None),
            current_file_workflows=self._import_file_workflows,
            request_grant=self._request_acceptance_grant,
            acceptance_is_current=self._acceptance_is_current,
            blocked_response=self._content_blocked_response,
            locked_response=self._transcript_locked_response,
            history_availability=self._history_availability,
            unavailable_response=self._history_unavailable_response,
        )

        @api.post("/api/transcribe")
        async def transcribe(request: Request, file: UploadFile):
            # ============================================================================
            # TRANSCRIPT DATA — DO NOT LOG
            # MAKE SURE you do not log transcript data anywhere. If you add a new function
            # that receives, returns, or passes transcript text, COPY this banner to it.
            # Use `log_transcription_event()` from whisperstream.logger for metadata only
            # (char count, word count, duration, stage name). Never pass `text`,
            # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
            # ============================================================================
            # Multipart uploads are the only transcription path that stages
            # source audio on disk for decoding. The path carries Whisperstream's
            # attributable prefix and only a sanitized source suffix. Cleanup
            # is ordinary unlink, is verified/retried, and runs after explicitly
            # closing both the UploadFile spool and our destination handle.
            # Audio lifecycle hardening (Task 4.5 / research Q5):
            # In-memory retention statement — verbatim from research Q5:
            #   "Raw audio lives in memory only for one recording. The chunk
            #    buffer is bounded to ~20s (chunk_duration, 1s overlap); the
            #    full waveform is transcribed fully in-memory (no temp file in
            #    the live mic path) and discarded, superseded at the next
            #    recording. Audio reaches disk only when both history_enabled
            #    and save_audio are on, as a single AES-256-GCM-encrypted
            #    full-recording WAV."
            # This upload-transcribe path IS the exception: it must write a
            # temp .wav so soundfile/librosa can decode arbitrary uploaded
            # formats. The finally block below guarantees deletion regardless
            # of outcome (success, exception, or lockdown). Under lockdown,
            # this is the only disk write in the transcription pipeline —
            # and it is always cleaned up.
            tmp_path = None
            tmp_handle = None
            try:
                grant = self._request_acceptance_grant(request)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                if self._app.stt_engine is None:
                    return JSONResponse(
                        status_code=503,
                        content={"error": "Model not loaded"},
                    )

                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                contents = await file.read(MAX_AUDIO_FILE_BYTES + 1)
                if len(contents) > MAX_AUDIO_FILE_BYTES:
                    raise AudioLimitExceeded("audio_limit_exceeded")
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                tmp_handle, tmp_path = create_owned_temp_file(
                    purpose="transcribe-upload",
                    suffix=safe_source_suffix(file.filename),
                )
                tmp_handle.write(contents)
                tmp_handle.flush()
                tmp_handle.close()
                tmp_handle = None
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)

                from ..main import _load_audio_file

                def decode_and_infer():
                    # Hold the guard through decoding too: a cancelled caller
                    # must not admit more expensive jobs while its thread lives.
                    if not self._model_operation_lock.acquire(blocking=False):
                        raise TranscriptionBusy("transcription_busy")
                    try:
                        if not self._acceptance_is_current(grant):
                            return None, 0.0
                        waveform, sample_rate = _load_audio_file(str(tmp_path), self._logger)
                        if not self._acceptance_is_current(grant):
                            return None, 0.0
                        start = time.perf_counter()
                        text = self._app.stt_engine.transcribe(
                            waveform, sample_rate=sample_rate,
                            language=self._app.config.language,
                        )
                        return text, time.perf_counter() - start
                    finally:
                        self._model_operation_lock.release()

                raw_text, elapsed = await run_in_threadpool(decode_and_infer)
                if hasattr(self._app, "_last_stt_failure"):
                    self._app._last_stt_failure = None
                duration_ms = int(elapsed * 1000)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)

                if self._app.config.enhancement_enabled:
                    # Use effective_provider() so that under lockdown the
                    # dispatch always resolves to "local" (on-device, no consent
                    # needed) regardless of the stored provider (Task 4.6).
                    provider = (self._app.config.effective_provider() or "").lower()
                    if enhancement_is_admitted(self._app.config):
                        if not self._acceptance_is_current(grant):
                            return self._content_blocked_response(request.url.path)
                        raw_text = await self._app.llm_enhancer.enhance(raw_text)
                        if not self._acceptance_is_current(grant):
                            return self._content_blocked_response(request.url.path)
                    else:
                        self._logger.warning(
                            "enhancement requested but no consent record for provider=%s; "
                            "skipping",
                            provider,
                        )

                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                processed_text = self._app.text_injector.process(raw_text)
                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)

                self.broadcast(
                    {
                        "type": "transcription.result",
                        "text": processed_text,
                        "duration_ms": duration_ms,
                    },
                    acceptance_grant=grant,
                )

                if not self._acceptance_is_current(grant):
                    return self._content_blocked_response(request.url.path)
                return {
                    "text": processed_text,
                    "raw": raw_text,
                    "duration_ms": duration_ms,
                }
            except AudioLimitExceeded:
                return JSONResponse(status_code=413, content={"error": "audio_limit_exceeded"})
            except TranscriptionBusy:
                return JSONResponse(status_code=409, content={"error": "transcription_busy"})
            except SttFailure as exc:
                publish_failure = getattr(
                    self._app,
                    "_broadcast_stt_failure",
                    None,
                )
                if callable(publish_failure):
                    publish_failure(exc)
                else:
                    self.broadcast({"type": "stt.failure", **exc.public_metadata()})
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "stt_failure",
                        "stt": exc.public_metadata(),
                    },
                )
            except Exception as exc:
                self._logger.error(
                    "transcription failed error_type=%s", type(exc).__name__
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": "transcription_failed"},
                )
            finally:
                try:
                    await file.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask result
                    pass
                if not close_then_unlink(tmp_path, (tmp_handle,)):
                    self._logger.warning(
                        "transcription temp cleanup failed status=retry_exhausted"
                    )

        async def stop_screen_reader_monitor():
            # Signal only, never join: shutdown is not held by the event thread.
            self._stop_screen_reader_monitor()
            if self._managed_expiry_timer is not None:
                self._managed_expiry_timer.cancel()
                self._managed_expiry_timer = None

        shutdown_hooks.append(stop_screen_reader_monitor)

        # --- Startup hook ---

        async def on_startup():
            if sys.platform == "darwin" and getattr(self._app, "_headless", False) is True:
                self._app.initialize_accepted_content_services()
            self._sync_screen_reader_monitor()
            asyncio.create_task(self._app.license_manager.maybe_revalidate())

            if self._diagnostics_locked():
                self._clear_agent_speak_state(reason="lockdown_effective")
            else:
                # Plugins update with the app: every installed owned plugin is
                # refreshed, including a provider the user paused or switched
                # off, so a stale cached copy never outlives an app update.
                if (
                    getattr(self._app.config, "claude_hud_enabled", False) is True
                    or self._owned_plugin_on_disk("claude")
                ):
                    asyncio.create_task(self._refresh_claude_plugin())
                if (
                    getattr(self._app.config, "codex_hud_enabled", False) is True
                    or self._owned_plugin_on_disk("codex")
                ):
                    asyncio.create_task(self._refresh_codex_plugin())

            async def _idle_lock_loop():
                """Periodic idle auto-lock check (Task 4.4).

                Runs every 10 seconds for the lifetime of the server. When
                lockdown is enabled, the surface is unlocked, and the idle
                window has elapsed, calls _maybe_idle_lock() which locks and
                broadcasts ``transcripts.locked``.

                The check interval (10s) is well below the minimum useful idle
                window (60s at the lowest non-zero tier the frontend exposes),
                so the granularity is invisible to users.
                """
                while True:
                    await asyncio.sleep(10)
                    try:
                        self._maybe_idle_lock()
                    except Exception:
                        pass  # Never let the loop die silently on a bug.

            asyncio.create_task(_idle_lock_loop())

        startup_hooks.append(on_startup)

        # --- WebSocket endpoint ---

        @api.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            if not _is_allowed_websocket_origin(ws.headers.get("origin")):
                # Origin values can contain private hostnames. Keep diagnostics
                # content-free and do not interpolate the rejected header.
                self._logger.warning("ws reject reason=origin_policy")
                await ws.close(code=1008)
                return

            await ws.accept()
            n_before = len(self._socket_delivery.clients)
            self._socket_delivery.register(ws)
            disconnect_reason = "unknown"
            try:
                # A reconnect candidate is a new connect that arrives while other
                # clients are still connected — the server cannot distinguish a
                # genuine first connect from a client-driven reconnect.
                if n_before > 0:
                    self._logger.debug("ws reconnect_candidate status=observed")
                self._logger.info("ws connect status=accepted")
                # Notify new client if the ACTIVE backend's model FILES are actually
                # missing on disk. parakeet may be None transiently while the
                # deferred load runs in the background — that's not a download
                # trigger, so we must not flash model.missing during the 2-3 seconds
                # before model.loaded arrives.
                #
                # Resolve presence against whichever backend the config/env override
                # currently selects, NOT the always-Parakeet self._app._model_dir.
                # A Qwen-active install with Parakeet absent must NOT report missing.
                if self._app.parakeet is None:
                    from ..stt_factory import (
                        COHERE_MODEL_NAME,
                        QWEN_MODEL_NAME,
                        stt_model_status,
                    )

                    status = stt_model_status(
                        self._app.config, self._app.config_manager
                    )
                    active_backend = next(
                        (name for name, st in status.items() if st["active"]),
                        "parakeet",
                    )
                    if not status[active_backend]["downloaded"]:
                        model_name = (
                            COHERE_MODEL_NAME
                            if active_backend == "cohere"
                            else QWEN_MODEL_NAME
                            if active_backend == "qwen"
                            else self._app.config.parakeet_model
                        )
                        await self._socket_delivery.send(ws, json.dumps({
                            "type": "model.missing",
                            "model_name": model_name,
                        }))
                # Push license status to new clients
                await self._socket_delivery.send(ws, json.dumps({
                    "type": "license.status",
                    **self._license_status_payload(),
                }))
                # Authoritative content-free dictation lifecycle is replayed on
                # every connection so a persistent HUD can recover after losing
                # a processing/idle transition.
                dictation_state = self._dictation_state.snapshot()
                if dictation_state is not None:
                    await self._socket_delivery.send(ws, json.dumps(dictation_state))
                # Every app webview connects to this shared endpoint. Sending the
                # snapshot to each is harmless; only the HUD acknowledges items.
                # An empty snapshot does not construct the experimental store.
                # ====================================================================
                # TRANSCRIPT DATA - DO NOT LOG
                # The snapshot may contain agent response content and semantic
                # source metadata. Never log or interpolate the serialized event.
                # ====================================================================
                snapshot_grant = self._current_acceptance_grant()
                if self._acceptance_is_current(snapshot_grant):
                    completion_snapshot = self._agent_completion_snapshot_event()
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, json.dumps(completion_snapshot))
                    # One backend-owned response lifecycle snapshot replaces the
                    # HUD's former independent six-second dwell/ack clock.
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, 
                            json.dumps(self._agent_response_queue_snapshot_event())
                        )
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, 
                            json.dumps(self._agent_question_snapshot_event())
                        )
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, json.dumps(self._agent_conversation_snapshot_event()))
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, 
                            json.dumps(self._agent_sessions_snapshot_event())
                        )
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(
                            ws, json.dumps(self._agent_reply_snapshot_event())
                        )
                    interaction_snapshot = self._agent_interactions_snapshot_event()
                    if self._acceptance_is_current(snapshot_grant):
                        await self._socket_delivery.send(ws, json.dumps(interaction_snapshot))
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._logger.debug("ws message_in status=accepted")
                    msg_type = msg.get("type", "unknown")
                    message_grant = None
                    if msg_type in _CONTENT_WS_MESSAGE_TYPES:
                        message_grant = self._current_acceptance_grant()
                        if not self._acceptance_is_current(message_grant):
                            await self._socket_delivery.send(ws, json.dumps({
                                "type": "recording.blocked",
                                "reason": _EULA_BLOCK_CODE,
                            }))
                            continue
                    await self._handle_ws_message(
                        msg, ws, acceptance_grant=message_grant
                    )
            except WebSocketDisconnect as exc:
                disconnect_reason = "client_close" if exc.code == 1000 else "transport_close"
            finally:
                self._socket_delivery.unregister(ws)
                self._release_reply_socket(ws)
                self._logger.info("ws disconnect reason=%s", disconnect_reason)

        return api

    # ------------------------------------------------------------------
    # Screen-reader state
    # ------------------------------------------------------------------

    def _sync_screen_reader_monitor(self) -> None:
        """Run the monitor exactly while the persistent recording control is on.

        Idempotent. Called at startup, after committed config saves, and on the
        two read paths so any other config replacement is reconciled too.
        """

        monitor = getattr(self, "_screen_reader_monitor", None)
        if monitor is None:
            return
        config = getattr(self._app, "config", None)
        wanted = getattr(config, "show_recording_control", False) is True
        try:
            if wanted:
                monitor.start()
            else:
                monitor.stop()
        except Exception as exc:  # noqa: BLE001 - detection is best-effort
            self._logger.warning(
                "[Accessibility] screen_reader monitor sync=failed error_type=%s",
                type(exc).__name__,
            )

    def _stop_screen_reader_monitor(self) -> None:
        monitor = getattr(self, "_screen_reader_monitor", None)
        if monitor is not None:
            monitor.stop()

    async def _screen_reader_active(self) -> bool:
        """Cached state while the monitor runs; otherwise one live check."""

        self._sync_screen_reader_monitor()
        monitor = getattr(self, "_screen_reader_monitor", None)
        cached = monitor.cached() if monitor is not None else None
        if cached is not None:
            return cached
        return await asyncio.to_thread(_live_screen_reader_active)

    def _screen_reader_monitor_status(self) -> str:
        monitor = getattr(self, "_screen_reader_monitor", None)
        return "off" if monitor is None else monitor.status()

    def _broadcast_screen_reader_state(self, active: bool) -> None:
        # Content-free: a boolean plus monitor status, never reader or process
        # names. Runs on the monitor thread under its publication lock.
        self.broadcast({
            "type": "accessibility.screen_reader",
            "active": bool(active),
            "monitor": self._screen_reader_monitor_status(),
        })

    def _screen_reader_monitor_degraded(self, status: str) -> None:
        """An event source was lost: push one live value so clients refresh.

        Runs on the monitor thread, outside its lock. Event-triggered only.
        """

        self.broadcast({
            "type": "accessibility.screen_reader",
            "active": bool(_live_screen_reader_active()),
            "monitor": status,
        })

    # ------------------------------------------------------------------
    # WebSocket helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_completion_to_wire(completion: Any) -> Dict[str, Any]:
        """Serialize a completion without exposing backend routing identity."""
        payload = completion.to_dict()
        payload.pop("session_id", None)
        payload.pop("source_key", None)
        return payload

    def _pending_agent_completion_ids(self) -> list[str]:
        """Return all currently pending completion IDs in authoritative order."""
        if self._agent_activity is None:
            return []
        return [
            completion.completion_id
            for completion in self._agent_activity.snapshot()
        ]

    def _agent_completion_snapshot_event(self) -> Dict[str, Any]:
        """Build the v1 pending-completion snapshot without creating a store.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Completion mappings can contain agent response content and semantic
        # source IDs. Never log this event, its entries, labels, or IDs.
        # ====================================================================
        """
        completions = []
        if (
            bool(getattr(self._app.config, "claude_hud_show_text", True))
            and self._agent_activity is not None
        ):
            completions = [
                self._agent_completion_to_wire(completion)
                for completion in self._agent_activity.snapshot()
            ]
        return {
            "type": "agent.completions.snapshot",
            "version": _AGENT_COMPLETION_EVENT_VERSION,
            "completions": completions,
        }

    def _effective_agent_summary_text_enabled(self) -> bool:
        """Read summary text enabled status, honoring setup preview override if armed."""
        preview = self._setup_delivery_preview
        if isinstance(preview, dict) and "agent_summary_text_enabled" in preview:
            return bool(preview["agent_summary_text_enabled"])
        return bool(getattr(self._app.config, "agent_summary_text_enabled", False))

    def _effective_agent_speech_enabled(self) -> bool:
        """Read agent speech enabled status, honoring setup preview override if armed."""
        preview = self._setup_delivery_preview
        if isinstance(preview, dict) and "agent_speech_enabled" in preview:
            return bool(preview["agent_speech_enabled"])
        return bool(getattr(self._app.config, "agent_speech_enabled", False))

    def _next_agent_snapshot_revision(self, family: str) -> int:
        """Allocate at a real state transition, never when sending old content."""
        with self._agent_snapshot_lock:
            self._agent_snapshot_revisions[family] += 1
            return self._agent_snapshot_revisions[family]

    def _publish_agent_coordinator_snapshot(
        self, family: str, generation: int
    ) -> None:
        with self._agent_snapshot_lock:
            if (
                generation != self._agent_snapshot_generation
                or self._agent_snapshot_clearing
            ):
                return
        if family == "question":
            # The current question outranks every reply for voice, Enter and
            # Escape; the desk learns of it before the HUD repaints.
            self._sync_reply_question()
        event = (
            self._agent_question_snapshot_event() if family == "question"
            else self._agent_response_queue_snapshot_event()
        )
        # Never acquire a coordinator lock while holding this lock. These two
        # snapshot types only validate consent and schedule socket delivery in
        # broadcast; they cannot enter compose/dictation/domain callbacks.
        with self._agent_snapshot_lock:
            if (
                generation == self._agent_snapshot_generation
                and not self._agent_snapshot_clearing
            ):
                self.broadcast(event)
        # Routes follow hooks and questions; keep each draft's delivery current.
        self._reply_desk_changed()

    def _agent_response_queue_snapshot_event(self) -> Dict[str, Any]:
        """Build the authoritative queue snapshot carrying every active state."""
        generation = self._agent_snapshot_generation
        coordinator = (
            self._agent_response_coordinator
            or self._claude_response_coordinator
        )
        if coordinator is None or self._agent_snapshot_clearing:
            lifecycle: Dict[str, Any] = {
                "version": _AGENT_RESPONSE_QUEUE_EVENT_VERSION,
                "revision": self._agent_snapshot_clear_revisions["response"],
                "phase": "idle",
                "ordered_completion_ids": [],
                "active": None,
                "quiet_delay_remaining_seconds": 0.0,
            }
        else:
            lifecycle = coordinator.snapshot()

        ordered = lifecycle["ordered_completion_ids"]
        completions_by_id: Dict[str, Any] = {}
        activity = self._agent_activity
        if (
            bool(getattr(self._app.config, "claude_hud_show_text", True))
            and activity is not None
        ):
            completions_by_id = {
                completion.completion_id: completion
                for completion in activity.snapshot()
            }
        lifecycle.update(
            {
                "type": "agent.response_queue.snapshot",
                "version": _AGENT_RESPONSE_QUEUE_EVENT_VERSION,
                "show_summary_text": self._effective_agent_summary_text_enabled(),
                "completions": [
                    self._agent_completion_to_wire(completions_by_id[completion_id])
                    for completion_id in ordered
                    if completion_id in completions_by_id
                ],
            }
        )
        with self._agent_snapshot_lock:
            if (
                generation != self._agent_snapshot_generation
                or self._agent_snapshot_clearing
            ):
                # A read overtaken by teardown must not borrow the successor's
                # revision: an equal-revision empty could erase its live state.
                return {
                    "type": "agent.response_queue.snapshot",
                    "version": _AGENT_RESPONSE_QUEUE_EVENT_VERSION,
                    "revision": self._agent_snapshot_clear_revisions["response"],
                    "phase": "idle",
                    "ordered_completion_ids": [],
                    "active": None,
                    "quiet_delay_remaining_seconds": 0.0,
                    "completions": [],
                    "show_summary_text": lifecycle["show_summary_text"],
                }
        return lifecycle

    def _agent_question_snapshot_event(self) -> Dict[str, Any]:
        """Build the authoritative surfaced/answerable question snapshot."""
        generation = self._agent_snapshot_generation
        coordinator = (
            self._agent_question_coordinator
            or self._claude_question_coordinator
        )
        snapshot = (
            coordinator.snapshot()
            if coordinator is not None and not self._agent_snapshot_clearing
            else None
        )
        with self._agent_snapshot_lock:
            current = (
                generation == self._agent_snapshot_generation
                and not self._agent_snapshot_clearing
            )
            revision = self._agent_snapshot_clear_revisions["question"]
        if snapshot is None or not current:
            return {
                "type": "agent.question.snapshot",
                "version": 1,
                "revision": revision,
                "active": None,
            }
        return snapshot

    def _agent_sessions_snapshot_event(self) -> Dict[str, Any]:
        """Build the ``agent.sessions`` managed-session-list snapshot (Task 4.1).

        Replayed on every WebSocket connect exactly like the other
        authoritative HUD snapshots, and re-broadcast on registration,
        deregistration, and every observed state change so a persistent HUD
        tab strip stays live without polling. An absent store (no session has
        ever registered) reports an empty list without constructing one,
        matching ``_agent_completion_snapshot_event``'s treatment of
        ``_agent_activity``.

        Privacy boundary, unchanged from existing practice: ``session_handle``
        is the opaque routing identity and is never painted as text by the
        frontend; ``source_name`` is the already-sanitized resolved name;
        ``can_reply`` is copied from the Task 2.6 chokepoint as a bare boolean
        with **no reason string**. Never includes ``cwd``, the raw provider
        ``session_id``, or the wrapper's correlation token.

        ``unread`` reuses ``agent_activity``'s pending-completion identity
        (``SourceKey(agent, session_id)``) rather than inventing a second
        read/unread concept: a managed session has something unread exactly
        when a completion for its bound session id is still pending
        acknowledgement. ``needs_input`` reuses the same ``QUESTION_EVENT_KINDS``
        the safety predicate already treats as "parked in a blocked question"
        (Task 2.6) rather than a second question-tracking path.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # ``source_name`` is transcript-class. Never log this event or its
        # entries.
        # ====================================================================
        """
        store = self._managed_sessions
        if store is None:
            return {"type": "agent.sessions", "sessions": []}

        sessions = store.sessions()
        self._arm_managed_session_expiry()
        unread_source_keys: set = set()
        if sessions and self._agent_activity is not None:
            unread_source_keys = {
                completion.source_key
                for completion in self._agent_activity.snapshot()
                if completion.source_key is not None
            }

        from ..managed_sessions import QUESTION_EVENT_KINDS  # noqa: PLC0415 — experimental state

        return {
            "type": "agent.sessions",
            "sessions": [
                {
                    "agent": session.agent,
                    "session_handle": session.session_handle,
                    "source_name": session.source_name,
                    "unread": (
                        session.session_id is not None
                        and session.source_key in unread_source_keys
                    ),
                    "needs_input": (
                        not session.ended
                        and session.last_event_kind in QUESTION_EVENT_KINDS
                    ),
                    "can_reply": store.can_reply(session),
                    "accepts_reply": store.can_accept_reply(session),
                    "reply_held": store.has_held_reply(session.session_handle),
                    "conversation_handle": self._agent_session_handle(session.session_id, session.agent) if session.session_id else None,
                    "latest_response": (
                        view.latest_response if (view := store.view(session.session_handle)) else None
                    ),
                    # The conversation ended: its console keeps any text for Copy.
                    "ended": session.ended,
                    # The reply target this tab's console and its hook cards
                    # share. Drafts live in agent.reply.snapshot under it. A
                    # session whose conversation is not bound yet has none.
                    **(
                        {"reply_target": target}
                        if (target := self._managed_reply_target(session)) is not None
                        else {}
                    ),
                }
                for session in sessions
            ],
        }

    async def _handle_ws_message(
        self,
        msg: Dict[str, Any],
        ws: WebSocket,
        *,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> None:
        """Handle an incoming WebSocket command from the frontend."""
        msg_type = msg.get("type", "")
        if self._diagnostics_locked() and isinstance(msg_type, str) and (
            msg_type.startswith("agent.") or msg_type.startswith("claude.")
        ):
            # Check before every lazy Agent accessor. Stale WebViews remain
            # connected during Lockdown transitions and must not recreate
            # cleared brokers, drafts, questions, or session handles.
            return
        if msg_type in _CONTENT_WS_MESSAGE_TYPES:
            grant = acceptance_grant or self._current_acceptance_grant()
            if not self._acceptance_is_current(grant):
                await self._socket_delivery.send(ws, json.dumps({
                    "type": "recording.blocked",
                    "reason": _EULA_BLOCK_CODE,
                }))
                return

        if msg_type == "agent.completion.ack":
            # Idempotent and content-free. Completion IDs are opaque
            # transcript-adjacent metadata and must never enter diagnostics,
            # including when confirming an already-absent ID.
            if msg.get("version") not in (None, _AGENT_COMPLETION_EVENT_VERSION):
                return
            completion_id = msg.get("completion_id")
            if isinstance(completion_id, str):
                if self._agent_activity is not None:
                    self._agent_activity.acknowledge(completion_id)
                await self._socket_delivery.send(ws, json.dumps({
                    "type": "agent.completion.acknowledged",
                    "version": _AGENT_COMPLETION_EVENT_VERSION,
                    "completion_id": completion_id,
                }))

        elif msg_type == "agent.question.ack":
            if msg.get("version") not in (None, 1):
                return
            session_handle = msg.get("session_handle")
            generation = msg.get("generation")
            coordinator = (
                self._agent_question_coordinator
                or self._claude_question_coordinator
            )
            if (
                isinstance(session_handle, str)
                and session_handle
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and generation > 0
                and coordinator is not None
            ):
                coordinator.acknowledge(
                    session_handle,
                    generation,
                )
                self._prune_agent_session_handles()

        elif msg_type in {
            "agent.interaction.engage",
            "agent.interaction.defer",
            "agent.question.answer",
            "agent.permission.decide",
            "agent.permission.allow",
            "agent.permission.deny",
        }:
            await self._handle_claude_interaction_ws_message(msg, ws)

        elif msg_type in {
            "agent.hud.presented",
            "agent.draft.update",
            "agent.draft.submit",
            "agent.draft.cancel",
        }:
            await self._handle_reply_ws_message(msg, ws)

        elif msg_type == "shortcut.capture.start":
            target = msg.get("target", "primary")
            threading.Thread(
                target=self._capture_shortcut, args=(target,), daemon=True
            ).start()

        elif msg_type == "shortcut.capture.stop":
            self._cancel_capture()

        elif msg_type == "inject.suppress":
            suppressed = bool(msg.get("suppressed", False))
            self._app.set_inject_suppressed(suppressed)

        elif msg_type == "profile.override.clear":
            # "Resume auto-routing" action (issue 14): clear the manual
            # profile override so the next dictation auto-routes. clear_manual_profile
            # broadcasts the cleared {profile_id: null} state to all clients.
            self._app.clear_manual_profile()

    # ------------------------------------------------------------------
    # Agent Speak replies: one owner (agent_reply_desk), one key policy
    # ------------------------------------------------------------------

    def _get_reply_desk(self):
        """Lazily construct the single reply owner."""
        if self._reply_desk is None:
            from ..agent_reply_desk import ReplyDesk  # noqa: PLC0415

            self._reply_desk = ReplyDesk(
                changed=self._reply_desk_changed,
                delivered=self._reply_delivered,
            )
        return self._reply_desk

    def _reply_hint_retired(self) -> bool:
        count = getattr(self._app.config, "claude_reply_hint_dictated_replies", 0)
        return isinstance(count, int) and count >= CLAUDE_REPLY_HINT_RETIRE_AFTER

    def _reply_delivered(self, dictated: bool) -> None:
        """Count dictated replies until the resting reply hints retire.

        The live config is updated before the desk publishes, so the snapshot
        carrying this receipt already says whether the hint is retired. The
        file write happens off the delivery path, serialized.
        """
        if not dictated or self._reply_hint_retired():
            return
        config = self._app.config
        count = getattr(config, "claude_reply_hint_dictated_replies", 0)
        config.claude_reply_hint_dictated_replies = min(
            CLAUDE_REPLY_HINT_RETIRE_AFTER, (count if isinstance(count, int) else 0) + 1
        )

        def persist() -> None:
            with self._reply_hint_save_lock:
                try:
                    self._app.config_manager.save(replace(self._app.config))
                except Exception as exc:  # noqa: BLE001 - a hint counter never blocks replies
                    self._logger.warning(
                        "reply hint counter save failed reason=%s", type(exc).__name__
                    )

        threading.Thread(target=persist, name="reply-hint-save", daemon=True).start()

    def _reply_desk_changed(self) -> None:
        """Publish one coherent reply snapshot, fenced against later changes.

        Routes are computed outside the desk lock (coordinators take the desk
        lock through ``draft_present``); the broadcast only schedules delivery,
        so it may run under the desk lock.
        """
        desk = self._reply_desk
        if desk is None:
            return
        # Outside the desk lock: the coordinator takes it through draft_present.
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        if coordinator is not None:
            coordinator.set_compose_hold(desk.composing)
        snapshot = desk.snapshot()
        event = self._agent_reply_snapshot_event(snapshot)
        desk.publish_if_current(snapshot.revision, lambda: self.broadcast(event))

    def _broadcast_agent_sessions(self) -> None:
        self.broadcast(self._agent_sessions_snapshot_event())
        self._reply_desk_changed()

    def _sync_reply_question(self) -> None:
        """Tell the desk which question (if any) outranks every reply."""
        desk = self._reply_desk
        if desk is None:
            return
        question = self._agent_question_coordinator or self._claude_question_coordinator
        current = question.current_target() if question is not None else None
        from ..agent_reply_desk import question_target  # noqa: PLC0415

        orphaned = desk.set_question(None if current is None else question_target(*current))
        if orphaned is not None:
            # A capture into a question that moved on would answer nothing.
            self._cancel_routed_compose(orphaned, "question_changed")

    @staticmethod
    def _split_reply_target(target_id: str) -> tuple[str, str]:
        kind, _, rest = target_id.partition(":")
        return kind, rest

    def _managed_reply_target(self, session: Any) -> str | None:
        """A wrapped session's reply target; None until its conversation binds."""
        from ..agent_reply_desk import reply_target  # noqa: PLC0415

        if session.session_id:
            handle = self._agent_session_handle(session.session_id, session.agent)
            if handle is not None:
                return reply_target(handle)
        return None

    def _managed_session_for_target(self, target_id: str) -> Any:
        store = self._managed_sessions
        if store is None or not target_id.startswith("conv:"):
            return None
        for session in store.sessions():
            if session.session_id and self._managed_reply_target(session) == target_id:
                return session
        return None

    def _reply_route(self, target_id: str) -> tuple[str, Any] | None:
        """The delivery route a submit would use now: hook, managed or question."""
        kind, rest = self._split_reply_target(target_id)
        if kind == "question":
            question = self._agent_question_coordinator or self._claude_question_coordinator
            current = question.current_target() if question is not None else None
            if current is not None and f"{current[0]}:{current[1]}" == rest:
                return "question", current
            return None
        if kind == "conv":
            coordinator = self._agent_response_coordinator or self._claude_response_coordinator
            if coordinator is not None and coordinator.live_reply_remaining(rest) is not None:
                return "hook", rest
        session = self._managed_session_for_target(target_id)
        store = self._managed_sessions
        if session is not None and store is not None and store.can_accept_reply(session):
            return "managed", session
        return None

    def _reply_delivery(self, target_id: str) -> str:
        route = self._reply_route(target_id)
        if route is None:
            return "none"
        if route[0] != "managed":
            return "now"
        store = self._managed_sessions
        session = route[1]
        if store is not None and store.can_reply(session) and not store.has_held_reply(session.session_handle):
            return "now"
        return "hold"

    def _reply_draft_present(self, conversation_handle: str) -> bool:
        """At a hook's ceiling: unsent text with no other route stays visible."""
        desk = self._reply_desk
        if desk is None:
            return False
        from ..agent_reply_desk import reply_target  # noqa: PLC0415

        target = reply_target(conversation_handle)
        draft = desk.draft(target)
        if draft is None or not draft.text.strip():
            return False
        session = self._managed_session_for_target(target)
        store = self._managed_sessions
        return not (session is not None and store is not None and store.can_accept_reply(session))

    def _agent_reply_snapshot_event(self, snapshot: Any = None) -> Dict[str, Any]:
        """``agent.reply.snapshot``: the one reply state the HUD renders.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Draft text is transcript-class. Never log this event.
        # ====================================================================
        """
        desk = self._reply_desk
        if snapshot is None and desk is not None:
            snapshot = desk.snapshot()
        if snapshot is None or self._diagnostics_locked():
            return {
                "type": "agent.reply.snapshot",
                "version": 1,
                "epoch": None if snapshot is None else snapshot.epoch,
                "revision": 0 if snapshot is None else snapshot.revision,
                "foreground": None,
                "drafts": [],
                "receipts": [],
                "enter_owner": "none",
                "reply_hint_retired": self._reply_hint_retired(),
            }
        foreground = snapshot.foreground
        drafts = []
        for draft in snapshot.drafts:
            drafts.append({
                "draft_id": draft.draft_id,
                "target_id": draft.target_id,
                "kind": draft.kind,
                "phase": draft.phase,
                "text": draft.text,
                "text_revision": draft.text_revision,
                "compose_generation": draft.compose_generation,
                "capture_mode": draft.capture_mode,
                "held": draft.held,
                "delivery": self._reply_delivery(draft.target_id),
                "outcome": (
                    None
                    if draft.outcome is None
                    else {
                        "revision": draft.outcome[0],
                        "result": "refused",
                        "reason": draft.outcome[1],
                    }
                ),
            })
        return {
            "type": "agent.reply.snapshot",
            "version": 1,
            "epoch": snapshot.epoch,
            "revision": snapshot.revision,
            "foreground": (
                None
                if foreground is None
                else {
                    "target_id": foreground.target_id,
                    "surface": foreground.surface,
                    "painted": foreground.painted,
                    "focused": foreground.focused,
                }
            ),
            "drafts": drafts,
            "receipts": [
                {
                    "target_id": receipt.target_id,
                    "draft_id": receipt.draft_id,
                    "result": "sent",
                    "text": receipt.text,
                }
                for receipt in snapshot.receipts
            ],
            "enter_owner": snapshot.enter_owner,
            "reply_hint_retired": self._reply_hint_retired(),
        }

    async def _handle_reply_ws_message(self, msg: Dict[str, Any], ws: Any) -> None:
        """HUD reply commands. Each gets one correlated ``agent.reply.result``."""
        msg_type = msg.get("type")
        if msg.get("version") not in (None, 1):
            return
        if msg_type == "agent.hud.presented":
            if self._diagnostics_locked() or not self._any_agent_provider_enabled():
                return
            self._sync_reply_question()
            self._get_reply_desk().present(
                seq=msg.get("seq"),
                target_id=msg.get("target_id"),
                surface=msg.get("surface"),
                painted=msg.get("painted") is True,
                focused=msg.get("focused") is True,
                socket=ws,
            )
            return
        result = "refused"
        draft_payload: Dict[str, Any] | None = None
        if not self._diagnostics_locked() and self._any_agent_provider_enabled():
            desk = self._get_reply_desk()
            if msg_type == "agent.draft.update":
                outcome = desk.update(
                    target_id=msg.get("target_id"),
                    draft_id=msg.get("draft_id"),
                    base_revision=msg.get("base_revision"),
                    text=msg.get("text"),
                )
                result = outcome.result
                if outcome.result == "applied" and outcome.draft is not None:
                    self._engage_reply_route(outcome.draft.target_id)
                if outcome.draft is not None:
                    draft_payload = {
                        "draft_id": outcome.draft.draft_id,
                        "target_id": outcome.draft.target_id,
                        "text_revision": outcome.draft.text_revision,
                    }
            elif msg_type == "agent.draft.submit":
                draft_id = msg.get("draft_id")
                target = msg.get("target_id")
                if draft_id is None and isinstance(target, str):
                    # Keys typed before the first acknowledgement carry no
                    # draft id; the exact revision still binds the submit.
                    view = desk.draft(target)
                    draft_id = view.draft_id if view is not None else None
                result = self._submit_reply(draft_id, msg.get("revision"), msg.get("text"))
            elif msg_type == "agent.draft.cancel":
                target = msg.get("target_id")
                result = "applied" if isinstance(target, str) and self._cancel_reply_target(target) else "stale"
            else:
                return
        reply: Dict[str, Any] = {
            "type": "agent.reply.result",
            "version": 1,
            "command": msg_type,
            "result": result,
        }
        command_id = msg.get("command_id")
        if isinstance(command_id, int) and not isinstance(command_id, bool):
            reply["command_id"] = command_id
        if draft_payload is not None:
            reply.update(draft_payload)
        try:
            await self._socket_delivery.send(ws, json.dumps(reply))
        except Exception:
            pass

    def _engage_reply_route(self, target_id: str) -> None:
        """A reply started: hold this conversation's open Stop, stop speech."""
        kind, rest = self._split_reply_target(target_id)
        if kind != "conv":
            return
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        if coordinator is not None:
            try:
                coordinator.engage(rest)
            except Exception:
                pass

    def _cancel_reply_target(self, target_id: str) -> bool:
        """Explicit discard: stop a capture into it, then drop the text."""
        desk = self._reply_desk
        if desk is None:
            return False
        generation = desk.capture_generation(target_id)
        if generation is not None:
            self._cancel_routed_compose(generation, "discard")
        return desk.discard(target_id)

    def _submit_reply(self, draft_id: object, revision: object, text: object = None) -> str:
        """Deliver one exact draft revision exactly once, or refuse and keep it."""
        desk = self._reply_desk
        if desk is None:
            return "stale"
        check = desk.check_submit(draft_id, revision, text)
        if check.result != "applied" or check.draft is None:
            if check.result == "refused" and check.draft is not None and check.draft.text.strip():
                desk.record_outcome(check.draft.draft_id, check.draft.text_revision, "refused")
            return check.result
        draft = check.draft
        outcome = "refused"
        reason = "no_route"
        if self._diagnostics_locked() or not self._acceptance_is_current(self._current_acceptance_grant()):
            reason = "blocked"
        else:
            route = self._reply_route(draft.target_id)
            if route is not None and route[0] == "question":
                question = self._agent_question_coordinator or self._claude_question_coordinator
                accepted = question.submit_text(route[1][0], route[1][1], draft.text) if question is not None else None
                outcome, reason = ("sent", "question") if accepted else ("refused", "question_refused")
            elif route is not None and route[0] == "hook":
                coordinator = self._agent_response_coordinator or self._claude_response_coordinator
                settled: list[str] = []

                def on_resolved(_interaction: Any) -> None:
                    # Before the coordinator publishes its terminal state, so
                    # the receipt reaches the HUD while the card still shows.
                    settled.append(self._settle_hook_reply(draft))

                interaction = (
                    coordinator.resolve_reply(route[1], draft.text, on_resolved=on_resolved)
                    if coordinator is not None
                    else None
                )
                if interaction is not None:
                    outcome = settled[0] if settled else self._settle_hook_reply(draft)
                    reason = "hook"
                    self._record_sent_reply(interaction)
                else:
                    # A wrapped session's hook routes through the PTY slot and
                    # can refuse; its managed transport may still hold it.
                    session = self._managed_session_for_target(draft.target_id)
                    if session is not None:
                        outcome, reason = self._deliver_managed(session, draft)
                    else:
                        reason = "hook_refused"
            elif route is not None and route[0] == "managed":
                outcome, reason = self._deliver_managed(route[1], draft)
        self._logger.info(
            "[ClaudeHUD] reply submit result=%s reason=%s kind=%s",
            outcome,
            reason,
            draft.kind,
        )
        if outcome in {"sent", "refused"}:
            desk.record_outcome(draft.draft_id, draft.text_revision, outcome)  # type: ignore[arg-type]
        return "applied" if outcome != "refused" else "refused"

    def _settle_hook_reply(self, draft: Any) -> str:
        """A resolved hook either sent the text or parked it for the wrapper.

        A wrapped session's Stop resolves into the PTY slot; the reply is only
        sent once the wrapper collects it, so it is tagged and kept as
        "sending". Returns "sent" or "sending".
        """
        desk = self._reply_desk
        session = self._managed_session_for_target(draft.target_id)
        store = self._managed_sessions
        if desk is None or session is None or store is None:
            if desk is not None:
                desk.record_outcome(draft.draft_id, draft.text_revision, "sent")
            return "sent"
        send_id = desk.mark_sending(draft.draft_id, draft.text_revision, held=False)
        if send_id is None:
            return "sent"
        if store.tag_pending_reply(session.session_handle, send_id):
            self._arm_reply_withdrawal(session.session_handle, send_id)
        else:
            # Already collected: it was sent.
            desk.settle(send_id, True)
        return "sending"

    def _deliver_managed(self, session: Any, draft: Any) -> tuple[str, str]:
        """Hand the text to the wrapper now, or hold it for the next Stop.

        The draft stays (read-only, "sending") until the wrapper collects it;
        a drop or an uncollected send gives it back with Copy.
        """
        store = self._managed_sessions
        desk = self._reply_desk
        payload = draft.text
        if store is None or desk is None or not payload.strip():
            return "refused", "empty"
        if not store.can_accept_reply(session):
            return "refused", "cannot_accept"
        if not self._agent_provider_enabled(session.agent):
            return "refused", "blocked"
        from ..managed_session_paste_guard import guard_reply_payload  # noqa: PLC0415

        if not guard_reply_payload(payload):
            return "refused", "payload_guard"
        handle = session.session_handle
        now = store.can_reply(session) and not store.has_held_reply(handle)
        send_id = desk.mark_sending(draft.draft_id, draft.text_revision, held=not now)
        if send_id is None:
            return "refused", "stale"
        # Recorded before the hand-off: the wrapper may collect at once.
        if session.session_id:
            self._unsettled_turns[send_id] = (
                self._agent_session_handle(session.session_id, session.agent),
                f"managed-{draft.draft_id}",
                payload,
            )
        if now:
            if not store.set_pending_reply(
                handle, payload, expected_revision=session.transport_revision, tag=send_id
            ):
                self._unsettled_turns.pop(send_id, None)
                return "refused", "pending_refused"
            self._arm_reply_withdrawal(handle, send_id)
        elif not store.hold_reply(handle, payload, tag=send_id):
            self._unsettled_turns.pop(send_id, None)
            return "refused", "hold_refused"
        self._broadcast_agent_sessions()
        return "sending", "managed"

    _REPLY_PICKUP_SECONDS = 5.0

    def _arm_reply_withdrawal(self, handle: str, send_id: str) -> None:
        """Withdraw a reply the wrapper does not collect within the window.

        A late pickup can then never send text already handed back to the
        user with Copy.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        timers = self._reply_withdrawals

        def arm() -> None:
            if send_id in timers:
                return
            timers[send_id] = loop.call_later(self._REPLY_PICKUP_SECONDS, withdraw)

        def withdraw() -> None:
            timers.pop(send_id, None)
            store = self._managed_sessions
            if store is not None:
                store.withdraw_pending_reply(handle, send_id)

        try:
            loop.call_soon_threadsafe(arm)
        except RuntimeError:
            pass

    def _on_reply_settled(self, send_id: str, delivered: bool) -> None:
        """The managed store collected or dropped one tagged reply."""
        timer = self._reply_withdrawals.pop(send_id, None)
        if timer is not None:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(timer.cancel)
                except RuntimeError:
                    pass
        turn = self._unsettled_turns.pop(send_id, None)
        if delivered and turn is not None:
            # A reply that never reached the agent is not part of the thread.
            self._remember_agent_turn(turn[0], turn[1], "user", turn[2])
        desk = self._reply_desk
        if desk is not None:
            desk.settle(send_id, delivered)

    @staticmethod
    def _agent_compose_state_event(
        lease: Any, *, state: str, reason: str | None = None
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "type": "agent.compose.state",
            "version": _AGENT_INTERACTION_EVENT_VERSION,
            "session_handle": lease.session_handle,
            "generation": lease.generation,
            "state": state,
        }
        if lease.recording_id is not None:
            event["recording_id"] = lease.recording_id
        if reason is not None:
            event["reason"] = reason
        return event

    def _release_reply_socket(self, ws: Any) -> None:
        """A closing HUD socket paints nothing any more."""
        desk = self._reply_desk
        if desk is not None and ws is not None:
            desk.revoke_presented(ws)

    def _log_reply_route(self, mode: str, reason: str, action: str) -> None:
        self._logger.info(
            "[ClaudeHUD] reply route mode=%s reason=%s action=%s",
            mode,
            reason,
            action,
        )

    def start_routed_reply(self, mode: str) -> bool:
        """PTT/toggle: capture into the painted foreground draft, or fall through.

        True means an exact compose started into one draft. False means the
        caller records ordinary focused-window dictation. A gesture is never
        swallowed without a recording.
        """

        if self._diagnostics_locked():
            self._log_reply_route(mode, "diagnostics_locked", "fallthrough")
            return False
        desk = self._reply_desk
        if desk is None:
            self._log_reply_route(mode, "no_reply_surface", "fallthrough")
            return False
        self._sync_reply_question()
        target = desk.key_policy.ptt
        if target is None:
            self._log_reply_route(mode, "no_painted_reply", "fallthrough")
            return False
        kind, rest = self._split_reply_target(target)
        if kind == "question":
            question = self._agent_question_coordinator or self._claude_question_coordinator
            remaining = question.begin_capture() if question is not None else None
            if remaining is None:
                self._log_reply_route(mode, "question_no_route", "fallthrough")
                return False
            handle, ttl = rest.rpartition(":")[0], remaining
        else:
            self._engage_reply_route(target)
            # The lease only binds this recording to its draft; the draft, not
            # the hook, decides delivery, so a long toggle can outlive a Stop.
            handle, ttl = rest, 900.0
        generation = self._prepare_compose_lease(handle, ttl)
        if generation is None:
            self._log_reply_route(mode, "lease_unavailable", "fallthrough")
            return False
        if desk.begin_capture(target, generation, mode) is None:
            self._release_routed_generation(generation)
            self._log_reply_route(mode, "draft_busy", "fallthrough")
            return False
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        if coordinator is not None:
            coordinator.set_capture_active(True)
        try:
            started = bool(self._app.start_agent_compose(generation))
        except Exception:
            started = False
        if started:
            self._log_reply_route(mode, f"{'question' if kind == 'question' else 'reply'}_compose_started", "route")
            return True
        self._log_reply_route(mode, "compose_failed", "fallthrough")
        desk.compose(generation, "cancelled")
        self._release_routed_generation(generation)
        if coordinator is not None:
            coordinator.set_capture_active(False)
        return False

    def _prepare_compose_lease(self, handle: str, ttl: float) -> int | None:
        leases = self._get_agent_compose_leases()
        try:
            lease = leases.acquire(handle, ttl_seconds=max(0.001, ttl))
        except (TypeError, ValueError, RuntimeError):
            return None
        return lease.generation

    def _release_routed_generation(self, generation: int) -> None:
        """Drop one prepared compose lease."""

        leases = self._agent_compose_leases
        if leases is not None:
            leases.release(generation=generation)

    def _routed_capture(self) -> tuple[int, str | None, str] | None:
        desk = self._reply_desk
        return desk.capturing() if desk is not None else None

    def stop_routed_reply(self, mode: str) -> bool:
        """Stop only the matching routed PTT/toggle capture."""

        capture = self._routed_capture()
        if capture is None or capture[1] != mode:
            return False
        generation = capture[0]
        try:
            self._app.stop_agent_compose(generation)
        except Exception:
            self._cancel_routed_compose(generation, "exception")
        return True

    def should_consume_response_escape(self) -> bool:
        """Escape belongs to the painted foreground only (listener thread).

        Each True queues exactly one callback, which pops this claim.
        """
        desk = self._reply_desk
        if desk is None:
            return False
        target = desk.key_policy.escape
        if target is None:
            return False
        self._claimed_escapes.append((target, desk.key_policy.surface))
        return True

    def dismiss_response_routes(self) -> bool:
        """Escape on the exact target claimed at key-down. True if it acted."""
        try:
            target, surface = self._claimed_escapes.popleft()
        except IndexError:
            return False
        desk = self._reply_desk
        if desk is None:
            return True
        kind, _rest = self._split_reply_target(target)
        if kind == "question":
            question = self._agent_question_coordinator or self._claude_question_coordinator
            if question is not None and question.can_defer():
                question.defer_current()
            return True
        if surface == "console":
            # A recording into the console: Escape cancels only the recording.
            generation = desk.capture_generation(target)
            if generation is not None:
                self._cancel_routed_compose(generation, "escape")
                return True
            # Speech next: the console has no stop button (owner, 2026-10-24),
            # so the first Escape silences the agent and keeps the console.
            coordinator = self._agent_response_coordinator or self._claude_response_coordinator
            if coordinator is not None and coordinator.is_speaking():
                self._stop_response_speech()
                return True
            # Otherwise Escape dismisses the console and discards its draft. It
            # is an explicit user action, not a silent erase.
            desk.discard(target)
            self.broadcast({"type": "agent.reply.close", "version": 1, "target_id": target, "surface": "console"})
            return True
        # A card: Escape skips the card only. Its draft stays for the next card
        # or the console.
        generation = desk.capture_generation(target)
        if generation is not None:
            self._cancel_routed_compose(generation, "escape")
        self.broadcast({"type": "agent.reply.close", "version": 1, "target_id": target, "surface": "card"})
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        if coordinator is not None and coordinator.can_dismiss_responses():
            coordinator.skip_current()
        return True

    def should_consume_response_enter(self) -> bool:
        """Global Enter: a painted spoken draft, ready or still transcribing."""
        desk = self._reply_desk
        if desk is None:
            return False
        policy = desk.key_policy
        if policy.enter is not None:
            self._claimed_enters.append(policy.enter)
            return True
        if policy.enter_when_ready is not None:
            self._claimed_enters.append((policy.enter_when_ready, None))
            return True
        if policy.foreground is not None:
            # Content-free: why a reply on screen did not take this Enter.
            foreground = desk.foreground()
            draft = desk.draft(policy.foreground)
            self._logger.debug(
                "[ClaudeHUD] enter pass reason=%s surface=%s draft=%s",
                "unpainted" if foreground is None or not foreground.painted
                else "hud_focused" if policy.hud_focused
                else "no_ready_draft",
                policy.surface,
                draft.phase if draft is not None else "none",
            )
        return False

    def submit_ready_response_draft(self) -> None:
        """Submit exactly the draft and revision claimed at key-down."""
        try:
            draft_id, revision = self._claimed_enters.popleft()
        except IndexError:
            return
        if revision is None:
            # Pressed while the dictated text was still on its way. The desk
            # binds it to that capture: submit now if it landed meanwhile,
            # otherwise its final submits it.
            desk = self._reply_desk
            now = desk.claim_enter_when_ready(draft_id) if desk is not None else None
            if now is not None:
                self._submit_reply(*now)
            return
        self._submit_reply(draft_id, revision)

    def _cancel_routed_compose(
        self, generation: int, reason: str
    ) -> None:
        """Cancel/release only the hotkey-owned compose generation."""

        capture = self._routed_capture()
        if capture is None or capture[0] != generation:
            return
        capture_mode = capture[1]
        try:
            self._app.cancel_agent_compose(generation, reason=reason)
        except Exception:
            pass
        leases = self._agent_compose_leases
        released = (
            leases.release(generation=generation)
            if leases is not None
            else None
        )
        desk = self._reply_desk
        if desk is not None:
            desk.compose(generation, "cancelled")
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        if coordinator is not None:
            coordinator.set_capture_active(False)
        if released is not None:
            self.broadcast(
                self._agent_compose_state_event(
                    released, state="cancelled", reason=reason
                )
            )
        reset_toggle = getattr(self._app.keyboard, "reset_toggle_state", None)
        if capture_mode == "toggle" and callable(reset_toggle):
            reset_toggle()

    def _observe_routed_compose_event(
        self, event: Dict[str, Any]
    ) -> None:
        """Fold the routed capture's compose events into its one draft.

        A capture's idle or cancel never touches draft text: it only ends the
        capture (Codex review finding A).
        """

        generation = event.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
        ):
            return
        event_type = event.get("type")
        state = "final" if event_type == "agent.compose.final" else event.get("state")
        if state not in {"processing", "final", "idle", "cancelled"}:
            return
        capture = self._routed_capture()
        if capture is None or capture[0] != generation:
            # The capture already ended (final, then idle): only its lease remains.
            if state in {"idle", "cancelled"}:
                self._release_routed_generation(generation)
            return
        desk = self._reply_desk
        coordinator = self._agent_response_coordinator or self._claude_response_coordinator
        draft = desk.compose(generation, state, text=event.get("text")) if desk is not None else None
        if state in {"final", "idle", "cancelled"}:
            if coordinator is not None:
                coordinator.set_capture_active(False)
        if state in {"idle", "cancelled"}:
            self._release_routed_generation(generation)
        if (
            state == "final"
            and draft is not None
            and draft.phase == "ready"
            and (
                draft.submit_requested
                or bool(getattr(self._app.config, "claude_reply_auto_submit", False))
            )
        ):
            self._submit_reply(draft.draft_id, draft.text_revision)

    async def _handle_claude_interaction_ws_message(
        self, msg: Dict[str, Any], ws: WebSocket
    ) -> None:
        """Resolve one opaque agent interaction command idempotently."""
        if self._diagnostics_locked():
            return
        if not self._any_agent_provider_enabled():
            return
        if msg.get("version") not in (None, _AGENT_INTERACTION_EVENT_VERSION):
            return
        msg_type = msg.get("type")
        if msg_type in {"agent.question.answer", "agent.interaction.defer"}:
            session_handle = msg.get("session_handle")
            generation = msg.get("generation")
            if (
                isinstance(session_handle, str)
                and session_handle
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and generation > 0
            ):
                coordinator = self._get_claude_question_coordinator()
                try:
                    interaction = (
                        coordinator.answer(
                            session_handle,
                            generation,
                            msg.get("answers"),
                        )
                        if msg_type == "agent.question.answer"
                        else coordinator.defer(session_handle, generation)
                    )
                except (TypeError, ValueError):
                    return
                if interaction is not None:
                    event = self._agent_interaction_state_event(interaction)
                    self.broadcast(event)
                    await self._socket_delivery.send(ws, json.dumps(event))
                else:
                    await self._socket_delivery.send(ws, 
                        json.dumps(self._agent_question_snapshot_event())
                    )
                return
        request_id = msg.get("request_id")
        broker = self._agent_interactions or self._claude_interactions
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 512
            or broker is None
        ):
            return

        before = broker.get(request_id)
        if before is None:
            return
        interaction = None
        newly_resolved = before.status == "pending"
        try:
            if msg_type == "agent.interaction.engage":
                interaction = broker.engage(request_id)
                newly_resolved = False
            elif msg_type == "agent.interaction.defer":
                interaction = broker.defer(request_id)
            elif msg_type == "agent.question.answer":
                interaction = broker.resolve_question(
                    request_id, msg.get("answers")
                )
            elif msg_type in {
                "agent.permission.decide",
                "agent.permission.allow",
                "agent.permission.deny",
            }:
                if not getattr(
                    self._app.config,
                    "claude_permission_decisions_enabled",
                    False,
                ):
                    return
                decision = msg.get("decision")
                if msg_type == "agent.permission.allow":
                    decision = "allow"
                elif msg_type == "agent.permission.deny":
                    decision = "deny"
                interaction = broker.resolve_permission(request_id, decision)
        except (TypeError, ValueError):
            return
        if interaction is None:
            return
        if newly_resolved:
            self._record_sent_reply(interaction)

        response_coordinator = (
            self._agent_response_coordinator
            or self._claude_response_coordinator
        )
        if interaction.kind == "stop" and response_coordinator is not None:
            response_coordinator.interaction_changed(interaction)

        event = self._agent_interaction_state_event(interaction)
        self.broadcast(event)
        await self._socket_delivery.send(ws, json.dumps(event))

    def broadcast(
        self,
        event: Dict[str, Any],
        *,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> None:
        """Thread-safe broadcast of an event to all connected WebSocket clients.

        Can be called from any thread (audio callback, transcription thread, etc.).
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        content_event = _is_content_ws_event(event)
        bakeoff_result = event.get("type") == "enhancement.bakeoff.result"
        grant = acceptance_grant
        if content_event:
            grant = grant or self._current_acceptance_grant()
            if not self._acceptance_is_current(grant):
                self._logger.debug(
                    "ws broadcast dropped type=%s reason=%s",
                    event.get("type", "unknown"),
                    _EULA_BLOCK_CODE,
                )
                return
            if bakeoff_result and self._bakeoff_authority_error(grant) is not None:
                return

        # Track usage stats for transcription results only after the exact
        # content grant has been validated.
        if event.get("type") == "transcription.result":
            try:
                self.stats.record_transcription(
                    event.get("text", ""), event.get("duration_ms", 0)
                )
            except Exception as exc:
                # Optional accounting cannot prevent an admitted result reaching UI.
                self._logger.warning(
                    "[Stats] update result=failed error_type=%s", type(exc).__name__
                )

        event_type = event.get("type", "unknown")
        if event_type in {
            "agent.compose.state",
            "agent.compose.final",
        }:
            self._observe_routed_compose_event(event)
        if event_type == "dictation.state":
            normalized = self._dictation_state.retain(event)
            if normalized is None:
                self._logger.debug("ws broadcast ignored type=dictation.state reason=invalid_or_stale")
                return
            # Never forward or retain fields outside the content-free contract.
            event = normalized
        self._socket_delivery.publish(
            event,
            loop=self._loop,
            acceptance_grant=grant,
        )
        if event_type == "agent.interaction.state":
            # Legacy deltas remain for compatibility. HUD authority comes from
            # a replacement read, never from a possibly delayed delta object.
            self.broadcast(
                self._agent_interactions_snapshot_event(),
                acceptance_grant=grant,
            )

    # ------------------------------------------------------------------
    # Shortcut capture
    # ------------------------------------------------------------------

    def _capture_shortcut(self, target: str = "primary") -> None:
        """Capture a keyboard shortcut (runs in a background thread).

        *target* identifies which shortcut is being captured (e.g.
        ``"primary"`` or ``"paste_last"``). It is passed through in the
        ``shortcut.captured`` broadcast so the frontend knows which config
        field to update.
        """
        def on_keys_changed(keys: str) -> None:
            self.broadcast({"type": "shortcut.capturing", "keys": keys, "target": target})

        def on_captured(hotkey: str) -> None:
            self.broadcast({"type": "shortcut.captured", "keys": hotkey, "target": target})
            # Re-register all hotkeys that were removed during capture
            try:
                self._app._register_hotkey()
            except Exception:
                pass

        recovery = getattr(self._app, "_shortcut_recovery", None)
        if recovery is None:
            self._app.keyboard.capture_shortcut(on_keys_changed, on_captured)
            return
        with recovery.lock:
            if recovery.closed or not recovery.enabled:
                self.broadcast({"type": "shortcut.capture.failed", "target": target})
                return
            try:
                self._app.keyboard.capture_shortcut(on_keys_changed, on_captured)
            except Exception:
                recovery.state, recovery.failure = "failed", "native_error"
                self.broadcast({"type": "shortcut.capture.failed", "target": target})

    def _cancel_capture(self) -> None:
        """Cancel any in-progress shortcut capture."""
        recovery = getattr(self._app, "_shortcut_recovery", None)
        if recovery is not None:
            with recovery.lock:
                if not recovery.closed and recovery.enabled:
                    self._app.keyboard.stop_capture()
                    recovery.reconcile(force=True)
            return
        self._app.keyboard.stop_capture()
        # Re-register the hotkey handler that was removed during capture
        try:
            self._app._register_hotkey()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Model download
    # ------------------------------------------------------------------

    def _stt_activation_busy(self) -> bool:
        probe = getattr(self._app, "stt_activation_busy", None)
        return callable(probe) and probe() is True

    def _set_model_download_activity(self, thread, cancel_event) -> None:
        """Hand the shared download slot to the benchmark download leg."""
        self._download_thread = thread
        self._download_cancel_event = cancel_event

    def _import_file_workflows(self):
        self._ensure_history_security_adapters()
        return self._file_workflows

    def _current_import_engine(self):
        """Resolve the current engine, including the legacy embedding seam."""
        app_vars = vars(self._app)
        return (
            self._app.parakeet
            if "stt_engine" not in app_vars and "parakeet" in app_vars
            else self._app.stt_engine
        )

    def _publish_config_change(
        self, new_config: WhisperstreamConfig,
        hud_was_enabled: bool, codex_was_enabled: bool,
    ) -> None:
        """Publish agent state only after the config transaction has committed."""
        self._sync_screen_reader_monitor()
        agents_before = self._agents_enabled_published
        agents_now = getattr(new_config, "agents_enabled", True) is not False
        self._agents_enabled_published = agents_now
        claude_remembered = new_config.claude_hud_enabled is True
        # Effective connections: remembered provider AND the master switch.
        hud_was_enabled = bool(hud_was_enabled and agents_before)
        codex_was_enabled = bool(codex_was_enabled and agents_before)
        claude_now = bool(claude_remembered and agents_now)
        codex_now = bool(new_config.codex_hud_enabled is True and agents_now)
        if agents_before and not agents_now and claude_remembered:
            # Pause, not uninstall: Claude's plugin is disabled so its hooks
            # and mod stop; Codex stays installed and trusted, muted by the gate.
            self._schedule_claude_pause()
        if (not new_config.claude_hud_show_text
            or (hud_was_enabled and not claude_now)
            or (codex_was_enabled and not codex_now)):
            self._unsettled_turns.clear()
            self._agent_conversations.clear()
            self.broadcast(self._agent_conversation_snapshot_event())
        if getattr(new_config, "agent_speak_setup_completed", False):
            self._setup_delivery_preview = None
        if self._diagnostics_locked():
            self._clear_agent_speak_state(reason="lockdown_effective")
            self._remove_codex_discovery_file()
        else:
            if (
                not claude_now
                and not codex_now
                and (hud_was_enabled or codex_was_enabled)
            ):
                self._clear_agent_speak_state(
                    reason="feature_paused" if agents_before and not agents_now else "feature_disabled"
                )
            else:
                if hud_was_enabled and not claude_now:
                    self._remove_claude_discovery_file()
                    self._remove_managed_sessions_for_agent("claude")
                if codex_was_enabled and not codex_now:
                    self._remove_codex_discovery_file()
                    self._remove_managed_sessions_for_agent("codex")
            if self._loop is not None:
                if not hud_was_enabled and claude_now:
                    self._write_claude_discovery_file()
                if not codex_was_enabled and codex_now:
                    self._write_codex_discovery_file()

    def _next_model_generation(self) -> int:
        """Return a process-local, content-free model operation generation."""

        with self._model_generation_lock:
            self._model_operation_generation += 1
            return self._model_operation_generation

    def _execute_windows_model_operation(
        self,
        request: ModelOperationRequest,
        operation,
        *,
        on_progress=None,
        cancel_event: threading.Event | None = None,
    ):
        """Bridge registered artifacts into the native-free lifecycle contract."""

        lifecycle = self._model_lifecycle
        if (
            request.asset.family is ModelFamily.ENHANCEMENT
            and getattr(lifecycle, "stt_asset_id", None)
        ):
            # The Darwin storage owner handles FluidAudio's compiled STT
            # layout, not registry GGUFs. Reuse the native-free registry
            # operation wrapper, as the Mac Qwen/Cohere download path does.
            # The shared download slot and supplied event own cancellation.
            from ..windows_model_lifecycle import WindowsModelLifecycleAdapter

            return WindowsModelLifecycleAdapter().execute_windows(
                request, operation, on_progress=on_progress,
                cancel_event=cancel_event,
            )
        execute_windows = getattr(lifecycle, "execute_windows", None)
        if not callable(execute_windows):
            return lifecycle.execute(request, on_progress=on_progress)
        return execute_windows(
            request,
            operation,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def _run_model_download(
        self,
        target_config: WhisperstreamConfig | None = None,
        *,
        operation_lock_held: bool = False,
    ) -> None:
        """Idempotently prepare and activate one exact configured STT model.

        Present and absent artifacts intentionally share this worker. Files are
        prepared first, a replacement manager is fully constructed second, and
        the app performs one short atomic install only after both succeed.
        """
        if not operation_lock_held:
            if not self._model_operation_lock.acquire(blocking=False):
                self.broadcast(
                    {
                        "type": "download.error",
                        "reason": "operation_busy",
                        "message": (
                            "Whisperstream is already updating a model. "
                            "Try again when it finishes."
                        ),
                    }
                )
                return

        try:
            self._run_model_download_locked(target_config)
        finally:
            self._model_operation_lock.release()

    def _run_model_download_locked(
        self,
        target_config: WhisperstreamConfig | None,
    ) -> None:
        """Run the serialized STT prepare/construct/install transaction."""
        from ..cohere_download import download_cohere_model
        from ..qwen_download import download_qwen_model
        from ..setup import DownloadCancelled, download_model
        from ..stt_factory import _resolve_backend, _selection_from_config, make_stt_manager, selected_model_id

        # Fresh cancel flag per download attempt. Stashed on the server so the
        # /api/download/cancel endpoint can set it from the request thread.
        cancel_event = threading.Event()
        self._download_cancel_event = cancel_event

        target_config = target_config or self._app.config
        rollback_state = None
        stage = "prepare"

        def on_progress(progress) -> None:
            self.broadcast({
                "type": "download.progress",
                "downloaded": progress.completed_bytes,
                "total": progress.total_bytes or 0,
                "speed_bps": 0,
            })

        try:
            rollback_state = self._app.stt_activation_rollback_state(
                target_config
            )
            backend, _source = _resolve_backend(target_config)
            target_model = selected_model_id(target_config)
            if cancel_event.is_set():
                raise DownloadCancelled

            native_asset = (getattr(self._model_lifecycle, "stt_asset_id", None)
                            if target_model is ModelId.PARAKEET_V3_INT8 else None)
            request = ModelOperationRequest(
                generation=self._next_model_generation(),
                operation=ModelOperation.ACQUIRE,
                asset=ModelAssetRef(native_asset or target_model.value, ModelFamily.STT),
            )
            operation_errors = []

            def acquire(_event, report):
                try:
                    if not is_present(target_model):
                        if backend == "qwen":
                            download_qwen_model(model_dir(target_model), progress_callback=report, cancel_event=cancel_event)
                        elif backend == "cohere":
                            download_cohere_model(model_dir(target_model), progress_callback=report, cancel_event=cancel_event)
                        else:
                            download_model(_selection_from_config(target_config).parakeet_model, model_dir(target_model),
                                           quantization=target_config.parakeet_quantization,
                                           progress_callback=report, cancel_event=cancel_event)
                except Exception as exc:
                    operation_errors.append(exc)
                    raise
                return ModelState.READY

            if native_asset:
                result = self._model_lifecycle.execute(request, on_progress=on_progress)
            elif getattr(self._model_lifecycle, "stt_asset_id", None):
                # Registry-backed models have the same file contract on both
                # platforms. This owner contains no Windows native imports.
                from ..windows_model_lifecycle import WindowsModelLifecycleAdapter
                result = WindowsModelLifecycleAdapter().execute_windows(
                    request, acquire, on_progress=on_progress, cancel_event=cancel_event,
                )
            else:
                result = self._execute_windows_model_operation(
                    request, acquire, on_progress=on_progress, cancel_event=cancel_event,
                )
            if result.failure is ModelFailure.CANCELLED:
                raise DownloadCancelled
            if result.failure is not ModelFailure.NONE:
                if operation_errors:
                    raise operation_errors[0]
                raise RuntimeError(result.failure.value)

            if cancel_event.is_set():
                raise DownloadCancelled

            stage = "activate"
            self._logger.info(
                "model prepare complete action=construct_manager artifact=%s",
                target_model.value,
            )
            with self._app.stt_replacement():
                manager = make_stt_manager(
                    target_config,
                    logger=self._logger,
                    config_manager=self._app.config_manager,
                )
                if cancel_event.is_set():
                    try:
                        manager.shutdown()
                    finally:
                        raise DownloadCancelled
                self._app.install_stt_manager(manager, target_config)

            self.broadcast({"type": "download.complete"})
        except DownloadCancelled:
            # User cancelled mid-download. The setup module's chunked path
            # already deleted any .tmp files on its way out. Do NOT instantiate
            # a manager and do NOT call cleanup_stale_models -- the previous
            # model on disk is still the valid one.
            self._logger.info("model download cancelled")
            if rollback_state is not None:
                try:
                    self._app.restore_stt_routing_state(
                        rollback_state,
                        target_config,
                    )
                except Exception as exc:
                    self._logger.error(
                        "stt routing restore failed outcome=cancel error_type=%s",
                        type(exc).__name__,
                    )
            self.broadcast({"type": "download.cancelled"})
        except Exception as exc:
            self._logger.error(
                "model activation failed stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )
            restored = False
            if rollback_state is not None:
                try:
                    restored = bool(
                        self._app.restore_stt_routing_state(
                            rollback_state,
                            target_config,
                        )
                    )
                except Exception as restore_exc:
                    self._logger.error(
                        "stt routing restore failed outcome=error error_type=%s",
                        type(restore_exc).__name__,
                    )
            self.broadcast(
                {
                    "type": "download.error",
                    "reason": (
                        "activation_failed"
                        if stage == "activate"
                        else "download_failed"
                    ),
                    "restored": restored,
                    "message": (
                        "Could not switch models. Whisperstream restored "
                        "your previous settings."
                        if restored
                        else "Could not switch models. Try again."
                    ),
                }
            )
        finally:
            # Clear the per-attempt state so subsequent retries start with a
            # fresh flag and the cancel endpoint reports not_running. This
            # runs after the cancel endpoint's thread.join returns, so the
            # cancel handler has already observed the event and the worker
            # exit -- the order is safe.
            self._download_cancel_event = None
            self._download_thread = None

    # ------------------------------------------------------------------
    # Model relocation lifecycle helpers (Task 2.2)
    # ------------------------------------------------------------------

    def _unload_models(self) -> None:
        """Release both model handles so files can be moved on Windows.

        Explicitly closes and detaches the exact STT manager, then schedules
        ``supervisor.stop()`` on ``self._loop`` via
        ``call_soon_threadsafe``.  Fire-and-forget: the relocation worker
        (Task 2.3) owns any settle / retry loop to absorb lagging ONNX /
        llama handle release before touching files on Windows.
        """
        # OnnxSttManager.close() stops its idle monitor and releases native
        # recognizer handles. The app also clears its exact loaded identity in
        # the same ownership transition.
        self._app.unload_stt_manager()

        supervisor = getattr(self._app, "llama_server", None)
        if supervisor is None:
            return

        async def _do_stop() -> None:
            try:
                await supervisor.stop()
            except Exception as exc:
                self._logger.error(
                    "[Relocate] supervisor stop failed reason=%s", type(exc).__name__
                )

        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(asyncio.ensure_future, _do_stop())
            except RuntimeError:
                pass  # Loop already closed

    def _reload_models(self) -> None:
        """Reload both models at their new location after relocation.

        Called by the relocation worker (Task 2.3) after ``config_manager``
        has been updated with the new ``model_storage_dir`` and the files are
        in place.  Mirrors the post-download patterns at ``_run_model_download``
        and ``_refresh_supervisor``.

        Constructs the configured exact STT manager synchronously and schedules the
        supervisor repoint (or fresh-supervisor creation) on ``self._loop``
        the same way ``_refresh_supervisor`` does — fire-and-forget so the
        caller thread is not blocked while the async stop completes.
        """
        from ..config_manager import get_local_llm_dir
        from ..enhancement_models import get_local_model, resolve_local_model_path
        from ..stt_factory import make_stt_manager

        # Refresh the cached models root to point at the new location.
        self._app.config_manager.refresh_models_root()

        # Use the same factory and atomic installer as ordinary activation so
        # Manual Qwen and exact Parakeet variants survive relocation.
        stt_config = self._app.config
        manager = make_stt_manager(
            stt_config,
            logger=self._logger,
            config_manager=self._app.config_manager,
        )
        self._app.install_stt_manager(manager, stt_config)

        # Production relocation historically preserves the running GGUF's
        # filename, or scans the new managed directory when no supervisor is
        # prepared yet. Keep that behavior: the configured catalog ID may not
        # describe an older/manual GGUF already living in managed storage.
        # Development candidates are different: their validated artifact path
        # is external and must never be rewritten under the relocated cache.
        catalog_entry = get_local_model(self._app.config.enhancement_local_model)
        existing_supervisor = getattr(self._app, "llama_server", None)
        if catalog_entry is not None and catalog_entry.get("source") == "candidate":
            resolved_gguf = resolve_local_model_path(catalog_entry)
        else:
            local_llm_dir = get_local_llm_dir()
            if existing_supervisor is not None:
                resolved_gguf = local_llm_dir / existing_supervisor._gguf_path.name
            else:
                configured_path = (
                    resolve_local_model_path(catalog_entry)
                    if catalog_entry is not None
                    else None
                )
                if configured_path is not None and configured_path.is_file():
                    resolved_gguf = configured_path
                else:
                    resolved_gguf = (
                        next(
                            (p for p in local_llm_dir.iterdir() if p.suffix == ".gguf"),
                            None,
                        )
                        if local_llm_dir.exists()
                        else None
                    )

        try:
            new_gguf: Optional[Path] = (
                resolved_gguf
                if resolved_gguf is not None
                and resolved_gguf.is_file()
                and resolved_gguf.stat().st_size > 0
                else None
            )
        except OSError:
            new_gguf = None

        async def _do_reload_supervisor() -> None:
            from ..gpu_benchmark import ngl_for_verdict
            from ..llama_server import (
                LlamaServerSupervisor,
                find_bundled_llama_server,
                runtime_acceleration_for_platform,
            )

            binary_path = find_bundled_llama_server()
            if binary_path is None:
                self._logger.warning(
                    "[Relocate] llama-server binary not found; supervisor not prepared"
                )
                return

            supervisor = getattr(self._app, "llama_server", None)
            if supervisor is not None:
                if new_gguf is not None:
                    try:
                        await self._restart_local_llm_for_model(new_gguf)
                    except Exception as exc:
                        self._logger.error(
                            "[Relocate] supervisor restart_for_model failed reason=%s",
                            type(exc).__name__,
                        )
                else:
                    self._logger.warning(
                        "[Relocate] no GGUF found in new location; supervisor not repointed"
                    )
            else:
                if new_gguf is None:
                    self._logger.warning(
                        "[Relocate] no GGUF found in new location; skipping supervisor creation"
                    )
                    return
                self._app.llama_server = LlamaServerSupervisor(
                    new_gguf,
                    binary_path=binary_path,
                    logger=self._logger,
                    idle_timeout_seconds=self._app.config.enhancement_local_idle_timeout,
                    on_state_change=self.broadcast,
                    n_gpu_layers_provider=lambda: ngl_for_verdict(
                        get_local_gpu_verdict(
                            self._app.config,
                            self._app.config.enhancement_local_model,
                        )
                    ),
                    device_index_provider=lambda: (
                        self._app.config.enhancement_local_gpu_device_index
                    ),
                    model_id_provider=lambda: (
                        self._app.config.enhancement_local_model
                    ),
                    acceleration=runtime_acceleration_for_platform(),
                )
                self._logger.info(
                    "[Relocate] prepared lazy supervisor (post-relocate) model=%s",
                    new_gguf.name,
                )

        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future, _do_reload_supervisor()
                )
            except RuntimeError:
                pass  # Loop already closed

    # ------------------------------------------------------------------
    # Model relocation classification + worker (Task 2.1 / 2.3)
    # ------------------------------------------------------------------

    def _run_model_relocation(
        self,
        destination: str,
        *,
        operation_lock_held: bool = False,
    ) -> None:
        """Serialize relocation with STT activation and deletion."""
        if not operation_lock_held:
            if not self._model_operation_lock.acquire(blocking=False):
                self.broadcast(
                    {
                        "type": "download.error",
                        "kind": "relocate",
                        "reason": "operation_busy",
                        "message": (
                            "Whisperstream is already updating a model. "
                            "Try again when it finishes."
                        ),
                    }
                )
                return
        try:
            self._run_model_relocation_locked(destination)
        finally:
            self._model_operation_lock.release()

    def _run_model_relocation_locked(self, destination: str) -> None:
        """Route one path-private Windows relocation through the contract."""

        if getattr(self._model_lifecycle, "stt_asset_id", None):
            self.broadcast({
                "type": "download.error",
                "kind": "relocate",
                "message": ModelFailure.DESTINATION_UNAVAILABLE.value,
            })
            self._download_cancel_event = None
            self._download_thread = None
            return

        cancel_event = threading.Event()
        self._download_cancel_event = cancel_event
        destination_token = f"windows-relocation-{secrets.token_hex(8)}"
        request = ModelOperationRequest(
            generation=self._next_model_generation(),
            operation=ModelOperation.RELOCATE,
            asset=ModelAssetRef("managed-model-storage", ModelFamily.STT),
            destination_token=destination_token,
        )

        def on_progress(progress) -> None:
            self.broadcast({
                "type": "download.progress",
                "kind": "relocate",
                "downloaded": progress.completed_bytes,
                "total": progress.total_bytes or 0,
                "speed_bps": 0,
            })

        operation_started = False

        def relocate(_event, report) -> ModelState:
            nonlocal operation_started
            operation_started = True
            self._relocation.run(destination, cancel_event, report)
            return ModelState.READY

        try:
            result = self._execute_windows_model_operation(
                request,
                relocate,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            if not operation_started and result.failure is ModelFailure.CANCELLED:
                self.broadcast({"type": "download.cancelled", "kind": "relocate"})
            elif not operation_started and result.failure is not ModelFailure.NONE:
                self.broadcast({
                    "type": "download.error",
                    "kind": "relocate",
                    "message": result.failure.value,
                })
        finally:
            self._download_cancel_event = None
            self._download_thread = None

    def _discard_registered_model_destination(self, destination_token: str) -> None:
        """Idempotently release one native token without resolving its path."""

        storage = getattr(self._model_lifecycle, "storage", None)
        release = getattr(storage, "release_destination", None)
        if callable(release):
            try:
                release(destination_token)
            except Exception:  # noqa: BLE001 - content-free cleanup boundary
                pass

    def _run_registered_model_relocation(self, destination_token: str) -> None:
        # The same guard serializes native relocation with activation/deletion.
        if not self._model_operation_lock.acquire(blocking=False):
            self._discard_registered_model_destination(destination_token)
            self.broadcast({"type": "download.error", "kind": "relocate", "message": "operation_busy"})
            self._download_thread = None
            return
        try:
            busy_probe = getattr(self._app, "stt_activation_busy", None)
            if callable(busy_probe) and busy_probe() is True:
                self._discard_registered_model_destination(destination_token)
                self.broadcast({"type": "download.error", "kind": "relocate", "message": "dictation_active"})
                self._download_thread = None
                return
            self._run_registered_model_relocation_locked(destination_token)
        finally:
            self._model_operation_lock.release()

    def _run_registered_model_relocation_locked(self, destination_token: str) -> None:
        """Run one Mac relocation whose native owner already minted the token.

        The Task 4.2 backend bridge never accepts or registers a filesystem
        path. Task 4.5's native destination picker may pass only its opaque
        token here after native validation and registration.
        """

        asset_id = getattr(self._model_lifecycle, "stt_asset_id", None)
        execute_relocation = getattr(
            self._model_lifecycle,
            "execute_relocation",
            None,
        )
        if not asset_id or not callable(execute_relocation):
            self.broadcast({
                "type": "download.error",
                "kind": "relocate",
                "message": ModelFailure.UNSUPPORTED.value,
            })
            self._download_cancel_event = None
            self._download_thread = None
            self._discard_registered_model_destination(destination_token)
            return

        cancel_event = threading.Event()
        self._download_cancel_event = cancel_event
        request = ModelOperationRequest(
            generation=self._next_model_generation(),
            operation=ModelOperation.RELOCATE,
            asset=ModelAssetRef(asset_id, ModelFamily.STT),
            destination_token=destination_token,
        )

        def on_progress(progress) -> None:
            self.broadcast({
                "type": "download.progress",
                "kind": "relocate",
                "downloaded": progress.completed_bytes,
                "total": progress.total_bytes or 0,
                "speed_bps": 0,
            })

        activation_succeeded = False

        def activate() -> None:
            nonlocal activation_succeeded
            self._reload_models()
            activation_succeeded = True

        try:
            self._unload_models()
            if cancel_event.is_set():
                result = ModelOperationResult(
                    request.generation,
                    request.operation,
                    request.asset,
                    ModelState.READY,
                    ModelFailure.CANCELLED,
                )
            else:
                try:
                    result = execute_relocation(
                        request,
                        on_progress=on_progress,
                        activate=activate,
                    )
                except Exception:  # noqa: BLE001 - typed and content-free boundary
                    result = ModelOperationResult(
                        request.generation,
                        request.operation,
                        request.asset,
                        ModelState.READY,
                        ModelFailure.IO,
                    )

            # Acquisition/copy/cancel can fail before the adapter invokes its
            # activation callback. Reload the still-authoritative source in
            # that case. If activation itself failed, the adapter restores its
            # storage selection before retrying; this final retry keeps a
            # usable source whenever the manager can still be constructed.
            if result.failure is not ModelFailure.NONE and not activation_succeeded:
                try:
                    activate()
                except Exception:  # noqa: BLE001 - report typed terminal state
                    result = ModelOperationResult(
                        request.generation,
                        request.operation,
                        request.asset,
                        result.state,
                        ModelFailure.IO,
                    )

            if result.failure is ModelFailure.NONE and activation_succeeded:
                self.broadcast({"type": "download.complete", "kind": "relocate"})
            elif result.failure is ModelFailure.CANCELLED:
                self.broadcast({"type": "download.cancelled", "kind": "relocate"})
            else:
                failure = (
                    result.failure
                    if result.failure is not ModelFailure.NONE
                    else ModelFailure.IO
                )
                self.broadcast({
                    "type": "download.error",
                    "kind": "relocate",
                    "message": failure.value,
                })
        finally:
            self._discard_registered_model_destination(destination_token)
            self._download_cancel_event = None
            self._download_thread = None

    def _run_llm_model_download(self, model_id: str, *, recovery: bool = False) -> None:
        """Download a local GGUF LLM, streaming progress via WebSocket.

        Mirrors ``_run_model_download`` for Parakeet but targets GGUFs in
        ``local-llm/``.  Events are the same ``download.*`` names with an
        extra ``"kind": "local_llm"`` field so the frontend can discriminate
        between Parakeet and local-LLM download progress streams.

        On success:
        1. Saves ``enhancement_local_model = model_id`` to config (the user
           may have switched from one model to another).
        2. Calls ``app.llama_server.restart_for_model(new_path)`` (or spawns a
           fresh supervisor if none is running yet).

        Keeps all previously downloaded GGUFs on disk (no auto-eviction) so
        the picker can switch between models without re-downloading.
        Recovery downloads retain the current selection, even if it changed
        while the transfer was in progress.
        """
        from ..enhancement_models import (
            get_local_bundled_model,
            resolve_local_model_path,
        )
        from ..setup import DownloadCancelled

        cancel_event = threading.Event()
        self._download_cancel_event = cancel_event
        from .. import gemma_artifacts
        original_config = self._app.config
        resume_after_recheck = (
            model_id == gemma_artifacts.PUBLIC_ID
            and original_config.enhancement_local_model == model_id
            and original_config.enhancement_enabled
            and original_config.effective_provider() == "local"
            and original_config.enhancement_local_gpu_verdicts.get(model_id) in ("viable", "marginal")
        )

        try:
            catalog_entry = get_local_bundled_model(model_id)
            if catalog_entry is None:
                raise ValueError(f"unknown model_id {model_id!r}")

            gguf_path = resolve_local_model_path(catalog_entry)
            assert gguf_path is not None

            # Download leg (shared with the auto-ladder path via
            # _fetch_gguf_streaming). Streams download.progress kind=local_llm;
            # DownloadCancelled propagates to the except below. No config /
            # supervisor / download.complete side effects live here -- those are
            # this method's tail, so the MANUAL path stays byte-for-byte
            # behaviorally identical to before the extraction.
            self._gguf_download.fetch(model_id, cancel_event)

            if cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            cfg = self._app.config
            if resume_after_recheck and get_local_gpu_verdict(cfg, model_id) == "not_run":
                # A different verified artifact must earn its own speed receipt.
                # Disable during the check so neither a stale receipt nor old
                # slow-model consent can warm the replacement automatically.
                cfg.enhancement_enabled = False
                self._app.config_manager.save(cfg)
                self._app.reload_config(cfg)
                previous_benchmark_thread = self._benchmark.thread
                self._benchmark.thread = threading.current_thread()
                try:
                    self._benchmark.run_manual(model_id, _cancel_event=cancel_event)
                finally:
                    self._benchmark.thread = previous_benchmark_thread
                    self._download_thread = threading.current_thread()
                    self._download_cancel_event = cancel_event
                if cancel_event.is_set():
                    raise DownloadCancelled("download cancelled")
                cfg = self._app.config
                if get_local_gpu_verdict(cfg, model_id) != "viable":
                    self.broadcast({
                        "type": "download.error", "kind": "local_llm", "model_id": model_id,
                        "reason": "model_benchmark_required", "restored": True,
                        "message": "Model downloaded. Open Enhancement to review its speed check before enabling local cleanup.",
                    })
                    return
                if cfg.enhancement_local_model == model_id and cfg.effective_provider() == "local":
                    cfg.enhancement_enabled = True
                    self._app.config_manager.save(cfg)
                    self._app.reload_config(cfg)

            # Persist the new active model into config.
            self._logger.info(
                "[LLMDownload] complete model_id=%s recovery=%s", model_id, recovery
            )
            cfg = self._app.config
            if not recovery:
                cfg.enhancement_local_model = model_id
                self._app.config_manager.save(cfg)
                self._app.reload_config(cfg)

            # Lazy lifecycle: use the same authoritative ensure/repoint path
            # as config-transition warming. A direct download prepares the
            # selected model even when Local is not currently active, but only
            # an enabled Local config requests warmup.
            async def _refresh_supervisor():
                await self._ensure_selected_llama_server(
                    prepare_when_inactive=True,
                    warm_if_enabled=True,
                )

            loop = self._loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future, _refresh_supervisor()
                    )
                except RuntimeError:
                    pass  # Loop already closed

            self.broadcast({"type": "download.complete", "kind": "local_llm", "model_id": model_id})

        except DownloadCancelled:
            self._logger.info("[LLMDownload] cancelled model_id=%s", model_id)
            self.broadcast({"type": "download.cancelled", "kind": "local_llm", "model_id": model_id})
        except Exception as exc:
            self._logger.error(
                "[LLMDownload] failed error_type=%s", type(exc).__name__
            )
            self.broadcast({
                "type": "download.error",
                "kind": "local_llm",
                "model_id": model_id,
                "message": str(exc),
            })
        finally:
            self._download_cancel_event = None
            self._download_thread = None

    def _try_start_tts_model_download(self, engine: str) -> bool:
        """Reserve the existing shared model-download slot for a voice engine."""
        existing = self._download_thread
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(
            target=self._run_tts_model_download,
            args=(engine,),
            daemon=True,
        )
        self._tts_download_engine = engine
        self._download_thread = thread
        thread.start()
        return True

    def _run_tts_model_download(
        self,
        engine: Literal["kokoro", "supertonic"],
    ) -> None:
        """Download one explicitly selected agent-speech voice model.

        Clones ``_run_llm_model_download``'s cancel/thread bookkeeping but
        targets the archive-distributed voice model via
        :func:`model_downloader.download_archive` -- a single pinned archive +
        SHA256 (Task 1.2), not the source-fallback chain ``download_llm_model``
        uses. Broadcasts the same ``download.*`` event names tagged
        ``"kind": "tts"`` so the frontend's existing
        ``subscribeToDownloadEvents`` plumbing discriminates this stream from
        Parakeet/local-LLM progress. Unlike the LLM download, there is no
        config write on completion -- the voice model has no "active model"
        selection to persist, only a presence check
        (``GET /api/speech/status``).
        """
        from ..model_downloader import DownloadCancelled, download_archive
        from ..speech.engine_specs import get_engine_spec

        cancel_event = threading.Event()
        self._download_cancel_event = cancel_event
        self._tts_download_engine = engine
        model_id = get_engine_spec(engine).model_id

        def on_progress(downloaded: int, total: Optional[int]) -> None:
            self.broadcast({
                "type": "download.progress",
                "kind": "tts",
                "engine": engine,
                "downloaded": downloaded,
                "total": total or 0,
                "speed_bps": 0,
            })

        try:
            download_archive(
                model_id,
                progress_callback=on_progress,
                cancel_event=cancel_event,
            )
            self._logger.info(
                "[SpeechDownload] complete model=%s", model_id.value
            )
            self.broadcast({
                "type": "download.complete",
                "kind": "tts",
                "engine": engine,
            })
        except DownloadCancelled:
            self._logger.info("[SpeechDownload] cancelled model=%s", model_id.value)
            self.broadcast({
                "type": "download.cancelled",
                "kind": "tts",
                "engine": engine,
            })
        except Exception as exc:
            self._logger.error(
                "[SpeechDownload] failed model=%s error_type=%s",
                model_id.value,
                type(exc).__name__,
            )
            self.broadcast({
                "type": "download.error",
                "kind": "tts",
                "engine": engine,
                "message": str(exc),
            })
        finally:
            self._download_cancel_event = None
            self._download_thread = None
            self._tts_download_engine = None

    def _get_agent_speech_focus(self):
        """Lazily construct the selective routine-speech policy."""
        if self._agent_speech_focus is None:
            from ..agent_events import SpeechFocus  # noqa: PLC0415 — lazy: keeps the TTS stack off the startup path

            self._agent_speech_focus = SpeechFocus()
        return self._agent_speech_focus

    def _get_agent_activity(self):
        """Lazily construct bounded, memory-only visual completion state."""
        if self._diagnostics_locked():
            return None
        if self._agent_activity is None:
            from ..agent_activity import AgentActivityStore  # noqa: PLC0415 — experimental state

            self._agent_activity = AgentActivityStore()
        return self._agent_activity

    def _get_managed_sessions(self):
        """Lazily construct the in-memory managed-session store (Task 2.2)."""
        if self._managed_sessions is None:
            from ..managed_sessions import ManagedSessionStore  # noqa: PLC0415 — experimental state

            self._managed_sessions = ManagedSessionStore(
                on_reply_settled=self._on_reply_settled
            )
        return self._managed_sessions

    def _get_claude_hook_delivered(self):
        if self._claude_hook_delivered is None:
            from ..agent_transports import HookDeliveredTurns  # noqa: PLC0415

            self._claude_hook_delivered = HookDeliveredTurns()
        return self._claude_hook_delivered

    def _get_claude_mod_poll_gaps(self):
        if self._claude_mod_poll_gaps is None:
            from ..agent_transports import ModPollGaps  # noqa: PLC0415

            self._claude_mod_poll_gaps = ModPollGaps()
        return self._claude_mod_poll_gaps

    def _clear_claude_mod_route_state(self) -> None:
        for owner in (self._claude_hook_delivered, self._claude_mod_poll_gaps):
            if owner is not None:
                owner.clear()

    def _arm_managed_session_expiry(self) -> None:
        """Keep one deadline only while managed sessions exist.

        Heartbeats extend store deadlines; they do not restart a timer or
        enumerate operating-system processes.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._arm_managed_session_expiry)
            return
        store = self._managed_sessions
        delay = store.next_expiry_delay() if store is not None else None
        if delay is None:
            if self._managed_expiry_timer is not None:
                self._managed_expiry_timer.cancel()
                self._managed_expiry_timer = None
            return
        if self._managed_expiry_timer is not None:
            if self._managed_expiry_timer.when() <= loop.time() + delay:
                return
            self._managed_expiry_timer.cancel()

        def expire() -> None:
            self._managed_expiry_timer = None
            if store is not self._managed_sessions:
                return
            # A reaped session keeps its draft: a stalled heartbeat or a sleep
            # must not erase typed text. It returns with the session.
            removed = set(store.reap(probe_pids=False))
            if removed:
                self._broadcast_agent_sessions()
            self._arm_managed_session_expiry()

        self._managed_expiry_timer = loop.call_later(delay, expire)

    def _remove_managed_sessions_for_agent(self, agent: str) -> None:
        """Scrub one disabled provider without disturbing the other."""
        if agent == "claude":
            self._clear_claude_mod_route_state()
        store = self._managed_sessions
        if store is None:
            # Hook-only drafts exist without any wrapped session.
            self._drop_reply_drafts_for_agent(agent)
            return
        sessions = [session for session in store.sessions() if session.agent == agent]
        targets = {
            target
            for target in (self._managed_reply_target(session) for session in sessions)
            if target is not None
        }
        removed = set(store.remove_agent(agent))
        self._drop_reply_drafts_for_agent(agent, targets)
        if removed:
            self._broadcast_agent_sessions()

    def _drop_reply_drafts_for_agent(self, agent: str, extra: set[str] | None = None) -> None:
        """Provider disable: drop that provider's drafts, keep the other's."""
        desk = self._reply_desk
        if desk is None:
            return
        from ..agent_reply_desk import reply_target  # noqa: PLC0415

        with self._agent_session_handles_lock:
            targets = {
                reply_target(handle)
                for (provider, _key), handle in self._agent_session_handles.items()
                if provider == agent
            }
        targets |= extra or set()
        # Structured questions are Claude-only.
        drop_questions = agent == "claude"
        desk.drop_targets(
            lambda target: target in targets
            or (drop_questions and target.startswith("question:"))
        )

    def _record_managed_event(
        self, token: str, event_name: str, *, agent: str, session_id: object
    ) -> Any:
        """Record one hook event and keep the session's draft across rebinds."""
        store = self._get_managed_sessions()
        before = store.get(token)
        old_target = self._managed_reply_target(before) if before is not None else None
        session = store.record_event(token, event_name, agent=agent, session_id=session_id)
        if session is None:
            return session
        for send_id in store.pending_tags(session.session_handle):
            # A held reply was offered at this Stop: same pickup window.
            self._arm_reply_withdrawal(session.session_handle, send_id)
        desk = self._reply_desk
        if desk is None:
            return session
        new_target = self._managed_reply_target(session)
        if old_target is not None and new_target is not None and old_target != new_target:
            # Resume, /clear or a new Codex conversation: the text the user
            # was writing follows the session, never silently dropped. A
            # session that ends keeps its draft too (it offers Copy).
            desk.rekey(old_target, new_target)
        return session

    def _agent_provider_enabled(self, agent: str) -> bool:
        """Return one provider's connection gate, composed with the master switch.

        The per-provider flag remembers the connection while Agents is paused;
        only both together let hooks, transport and delivery through.
        """

        attribute = {
            "claude": "claude_hud_enabled",
            "codex": "codex_hud_enabled",
        }.get(agent)
        return bool(
            attribute is not None
            and getattr(self._app.config, "agents_enabled", True) is not False
            and getattr(self._app.config, attribute, False) is True
        )

    async def _agent_runtime_status(self, agent: str, *, refresh: bool = False) -> dict:
        from ..agent_runtime_compatibility import inspect_provider
        import copy

        if not hasattr(self, "_runtime_status_cache"):
            self._runtime_status_cache = {}
            self._runtime_status_locks = {p: asyncio.Lock() for p in ("claude", "codex")}
        async with self._runtime_status_locks[agent]:
            cached = self._runtime_status_cache.get(agent)
            if refresh or cached is None or time.monotonic() - cached[0] > 60:
                detected = await asyncio.to_thread(inspect_provider, agent)
                self._runtime_status_cache[agent] = (time.monotonic(), detected)
            result = copy.deepcopy(self._runtime_status_cache[agent][1])
        if self._agent_provider_enabled(agent) and not self._diagnostics_locked() and self._acceptance_is_current(self._current_acceptance_grant()):
            store = self._managed_sessions
            for session in store.sessions() if store is not None else ():
                if session.agent != agent or session.transport == "pty" or session.ended or not session.is_fresh(time.monotonic(), 8):
                    continue
                for surface in result["surfaces"]:
                    if surface["surface"] == session.surface:
                        surface.update(state="ready", engine_version=session.runtime_version,
                                       reply_verified=True, transport=session.transport)
        return result

    @staticmethod
    async def _lifecycle_force_requested(request: Request) -> bool:
        """Read an optional ``{"force": true}`` body; anything else is False."""
        try:
            raw = await request.body()
            if not raw or len(raw) > 1024:
                return False
            body = json.loads(raw)
        except (ValueError, TypeError):
            return False
        return isinstance(body, dict) and body.get("force") is True

    def _note_claude_reconcile(self, status: dict) -> None:
        """A fresh install or update needs a new session's handshake again."""
        if status.get("ok") is True and status.get("status") in {"installed", "updated"}:
            self._clear_claude_connection_record()

    def _pause_claude_plugin(self) -> None:
        from ..agent_integration_contract import AgentLifecycleAction  # noqa: PLC0415

        lifecycle, _result = self._apply_agent_plugin_action(AgentLifecycleAction.DISABLE)
        self._logger.info("[ClaudeHUD] pause status=%s", getattr(lifecycle.state, "value", "unknown"))

    def _schedule_claude_pause(self) -> None:
        """Disable Claude's plugin off the request path; failures fail open."""
        def run() -> None:
            try:
                self._pause_claude_plugin()
            except Exception as exc:  # noqa: BLE001 - the gate already mutes hooks
                self._logger.warning("[ClaudeHUD] pause failed reason=%s", type(exc).__name__)

        threading.Thread(target=run, name="claude-plugin-pause", daemon=True).start()

    @staticmethod
    def _owned_plugin_on_disk(agent: str) -> bool:
        """Whether Whisperstream's own plugin payload exists for a provider."""
        try:
            if agent == "claude":
                from ..claude_hud import _has_owner_marker, _owned_plugin_paths  # noqa: PLC0415

                _marketplace, plugin_dir = _owned_plugin_paths(get_app_dir())
                return _has_owner_marker(plugin_dir)
            from ..codex_plugin_lifecycle import (  # noqa: PLC0415
                _has_owner_marker as codex_owner_marker,
                _owned_codex_plugin_paths,
            )

            marketplace_dir, _plugin = _owned_codex_plugin_paths(get_app_dir())
            return codex_owner_marker(marketplace_dir)
        except Exception:
            return False

    async def _refresh_claude_plugin(self) -> None:
        """Startup refresh; an inactive connection is updated, never resumed."""
        try:
            from ..agent_integration_contract import AgentLifecycleAction  # noqa: PLC0415

            active = self._agent_provider_enabled("claude")
            lifecycle, result = await asyncio.to_thread(
                self._apply_agent_plugin_action,
                AgentLifecycleAction.RECONCILE,
                keep_disabled=not active,
            )
            if result is None:
                raise RuntimeError(
                    lifecycle.failure.value
                    if lifecycle.failure is not None
                    else "agent plugin reconcile failed"
                )
            public_result = self._public_claude_plugin_status(result)
            self._claude_plugin_status = public_result
            self._note_claude_reconcile(public_result)
            if (
                public_result.get("ok") is True
                and self._agent_provider_enabled("claude")
                and self._loop is not None
            ):
                self._write_claude_discovery_file()
            self._logger.info(
                "[ClaudeHUD] startup reconcile status=%s active=%s",
                public_result.get("status", "unknown"),
                active,
            )
        except Exception as exc:
            self._claude_plugin_status = {
                "ok": False,
                "status": "failed",
                "reload_required": False,
            }
            self._logger.warning(
                "[ClaudeHUD] startup reconcile failed reason=%s",
                type(exc).__name__,
            )

    async def _refresh_codex_plugin(self) -> None:
        """Startup refresh; an inactive connection is updated, never installed."""
        try:
            from functools import partial  # noqa: PLC0415

            from ..codex_plugin_lifecycle import enable_codex_plugin  # noqa: PLC0415

            active = self._agent_provider_enabled("codex")
            result = await asyncio.to_thread(
                self._run_codex_lifecycle,
                enable_codex_plugin if active else partial(enable_codex_plugin, refresh_only=True),
            )
            public_result = self._public_codex_plugin_status(result)
            self._codex_plugin_status = public_result
            if (
                public_result.get("ok") is True
                and self._agent_provider_enabled("codex")
                and self._loop is not None
            ):
                self._write_codex_discovery_file()
            self._logger.info(
                "[CodexPlugin] startup reconcile status=%s active=%s",
                public_result.get("status", "unknown"),
                active,
            )
        except Exception as exc:
            self._codex_plugin_status = {
                "ok": False,
                "status": "failed",
                "codex_cli_available": False,
                "codex_cli_supported": False,
                "trust_required": False,
            }
            self._logger.warning(
                "[CodexPlugin] startup reconcile failed reason=%s",
                type(exc).__name__,
            )

    def _bundled_claude_plugin_version(self) -> str | None:
        cached = getattr(self, "_bundled_claude_version_cache", None)
        if cached is not None:
            return cached
        try:
            from ..claude_hud import bundled_plugin_version  # noqa: PLC0415

            version = bundled_plugin_version(
                get_app_dir(),
                hook_platform="darwin" if sys.platform == "darwin" else "windows",
            )
        except Exception:
            return None
        self._bundled_claude_version_cache = version
        return version

    def _set_claude_connection_record(self, value: str) -> None:
        """Persist the server-authored Claude connection evidence."""
        cfg = self._app.config
        if getattr(cfg, "claude_connected_plugin_version", "") == value:
            return
        try:
            cfg.claude_connected_plugin_version = value
            self._app.config_manager.save(cfg)
        except Exception as exc:  # noqa: BLE001 - evidence must never break delivery
            self._logger.warning(
                "[ClaudeHUD] connection record save failed reason=%s",
                type(exc).__name__,
            )

    def _record_claude_connection(self, reported_version: object) -> None:
        """Remember that the plugin this app installs completed a handshake.

        Only a module that reports the exact bundled version counts, so an
        older cached plugin in an open session cannot mark a new one connected.
        """
        bundled = self._bundled_claude_plugin_version()
        if isinstance(reported_version, str) and bundled and reported_version == bundled:
            self._set_claude_connection_record(bundled)

    def _clear_claude_connection_record(self) -> None:
        self._set_claude_connection_record("")

    def _claude_connection_state(self, compatibility: object) -> str:
        """Return connected, waiting, or none without any surface preference."""
        if not self._agent_provider_enabled("claude"):
            return "none"
        surfaces = compatibility.get("surfaces", []) if isinstance(compatibility, dict) else []
        if any(
            isinstance(surface, dict)
            and surface.get("state") == "ready"
            and surface.get("reply_verified") is True
            for surface in surfaces
        ):
            return "connected"
        status = self._claude_plugin_status if isinstance(self._claude_plugin_status, dict) else {}
        installed = status.get("ok") is True and status.get("status") in {
            "current", "installed", "updated",
        }
        bundled = self._bundled_claude_plugin_version()
        current = bool(bundled) and status.get("installed_version") == bundled
        recorded = bool(bundled) and getattr(
            self._app.config, "claude_connected_plugin_version", ""
        ) == bundled
        if installed and current and recorded:
            return "connected"
        return "waiting" if installed else "none"

    def _claude_uses_cli(self) -> bool:
        from ..claude_hud import _claude_cli_path
        # One user-scope install serves every surface. Use the CLI whenever it
        # exists; Desktop-only installs fall back to settings registration.
        return _claude_cli_path() is not None

    def _run_codex_lifecycle(self, action):
        from ..codex_plugin_lifecycle import codex_plugin_runtime
        with codex_plugin_runtime():
            return action(get_app_dir())

    def _bind_persistent_stop(self, interaction: Any, session: Any, grant: AcceptanceGrant) -> bool:
        """Resolve through the same guarded slot as persistent typed/dictated drafts."""
        store = self._managed_sessions
        if session is None or store is None or not store.can_reply(session):
            return False
        handle, sid, agent = session.session_handle, session.session_id, session.agent
        revision = session.transport_revision

        def deliver(text: str) -> bool:
            from ..managed_session_paste_guard import guard_reply_payload
            current = store.get_by_handle(handle)
            if (self._managed_sessions is not store or current is None or current.session_id != sid
                or current.transport_revision != revision
                or not self._acceptance_is_current(grant) or self._diagnostics_locked()
                or not self._agent_provider_enabled(agent) or not store.can_reply(current)
                or not guard_reply_payload(text)):
                return False
            return store.set_pending_reply(handle, text, expected_revision=revision)

        self._get_agent_interactions().set_reply_handler(interaction.request_id, deliver)
        return True

    def _detach_persistent_stop(self, waiter: Any, interaction: Any) -> None:
        # The HUD waiter remains local; the provider's hook/turn is already free.
        # A done callback scrubs content without retaining an extra task or thread.
        broker = self._get_agent_interactions()
        def finished(future):
            if not future.cancelled():
                future.result()
            broker.discard_terminal(interaction.request_id)
            self._prune_agent_session_handles()
        waiter.add_done_callback(finished)

    def _agent_session_excluded(self, agent: str, body: dict) -> bool:
        """Drop subagent and headless sessions before any Agent Speak state.

        Logs only the content-free reason; never the payload or its paths.
        """
        from ..agent_session_filter import (  # noqa: PLC0415
            claude_session_excluded,
            codex_session_excluded,
        )

        reason = (
            codex_session_excluded(body)
            if agent == "codex"
            else claude_session_excluded(body)
        )
        if reason is None:
            return False
        self._logger.debug(
            "[%s] hook ignored reason=%s",
            "CodexHUD" if agent == "codex" else "ClaudeHUD",
            reason,
        )
        return True

    def _any_agent_provider_enabled(self) -> bool:
        return self._agent_provider_enabled("claude") or self._agent_provider_enabled(
            "codex"
        )

    def _agent_session_handle(
        self,
        raw_session_id: str,
        agent: str = "claude",
    ) -> str | None:
        """Return one process-local opaque handle for a provider session."""
        from ..agent_session_names import bounded_session_key  # noqa: PLC0415

        if agent not in {"claude", "codex"}:
            return None
        key = bounded_session_key(raw_session_id)
        if key is None:
            return None
        provider_key = (agent, key)
        with self._agent_session_handles_lock:
            handle = self._agent_session_handles.get(provider_key)
            if handle is not None:
                self._agent_session_handles.move_to_end(provider_key)
                return handle

            protected = self._active_agent_session_handles()
            if len(self._agent_session_handles) >= _MAX_AGENT_SESSION_HANDLES:
                removable = next(
                    (
                        candidate
                        for candidate, existing_handle
                        in self._agent_session_handles.items()
                        if existing_handle not in protected
                    ),
                    None,
                )
                if removable is None:
                    # Every mapping owns a currently actionable route. The
                    # hook must fail open instead of creating an untracked
                    # handle or evicting a live route.
                    return None
                self._agent_session_handles.pop(removable, None)
            handle = secrets.token_urlsafe(18)
            self._agent_session_handles[provider_key] = handle
            return handle

    def _active_agent_session_handles(self) -> set[str]:
        """Collect handles that an active broker/question/compose route owns."""

        protected: set[str] = set()
        broker = self._agent_interactions or self._claude_interactions
        question = self._agent_question_coordinator or self._claude_question_coordinator
        if broker is not None:
            protected.update(broker.active_session_handles())
        if question is not None:
            protected.update(
                question.active_session_handles()
            )
        if self._agent_compose_leases is not None:
            lease = self._agent_compose_leases.current()
            if lease is not None:
                protected.add(lease.session_handle)
        return protected

    def _prune_agent_session_handles(self) -> None:
        """LRU-evict only unprotected mappings above the strict cap."""

        with self._agent_session_handles_lock:
            protected = self._active_agent_session_handles()
            while len(self._agent_session_handles) > _MAX_AGENT_SESSION_HANDLES:
                removable = next(
                    (
                        key
                        for key, handle in self._agent_session_handles.items()
                        if handle not in protected
                    ),
                    None,
                )
                if removable is None:
                    return
                self._agent_session_handles.pop(removable, None)

    @staticmethod
    def _public_claude_plugin_status(result: object) -> dict[str, Any]:
        """Strip local marketplace/plugin paths from the REST status contract."""

        if not isinstance(result, dict):
            return {"ok": False, "status": "failed", "reload_required": False}
        allowed = {
            "ok",
            "status",
            "source_status",
            "version",
            "installed_version",
            "claude_cli_available",
            "reload_required",
            "reload_message",
        }
        return {key: value for key, value in result.items() if key in allowed}

    @staticmethod
    def _public_codex_plugin_status(result: object) -> dict[str, Any]:
        """Strip all local paths and internal CLI failure details."""

        if not isinstance(result, dict):
            return {
                "ok": False,
                "status": "failed",
                "codex_cli_available": False,
                "codex_cli_supported": False,
                "trust_required": False,
            }
        allowed = {
            "ok",
            "status",
            "source_status",
            "version",
            "installed_version",
            "codex_cli_available",
            "codex_cli_supported",
            "trust_required",
            "trust_message",
            "update_available",
        }
        return {key: value for key, value in result.items() if key in allowed}

    def _valid_agent_hook_token(self, request: Request) -> bool:
        supplied = request.headers.get(HOOK_TOKEN_HEADER, "")
        return (
            len(supplied) == 64
            and supplied.isascii()
            and secrets.compare_digest(supplied, self._agent_hook_token)
        )

    @staticmethod
    def _claude_discovery_path() -> Path:
        return get_app_dir() / "claude-hud-port.json"

    def _remove_claude_discovery_file(self) -> None:
        try:
            self._claude_discovery_path().unlink(missing_ok=True)
        except OSError:
            self._logger.warning("[ClaudeHUD] port file remove status=failed")

    def _write_claude_discovery_file(self) -> None:
        """Publish the active loopback port and per-process hook credential."""

        if self._diagnostics_locked():
            self._remove_claude_discovery_file()
            return

        try:
            from ..eula import _atomic_write_json  # noqa: PLC0415

            _atomic_write_json(
                self._claude_discovery_path(),
                {"port": self._port, "token": self._agent_hook_token},
            )
            self._logger.info("[ClaudeHUD] port file status=written")
        except Exception as exc:
            self._logger.warning(
                "[ClaudeHUD] port file write failed reason=%s",
                type(exc).__name__,
            )

    def _apply_agent_plugin_action(self, action, *, force: bool = False, keep_disabled: bool = False):
        """Run one generation-bound Claude plugin lifecycle operation."""

        from ..agent_integration_contract import (
            AgentLifecycleRequest,
            AgentProvider,
        )

        with self._agent_integration_lock:
            self._agent_integration_generation += 1
            generation = self._agent_integration_generation
        request = AgentLifecycleRequest(
            f"claude-plugin-{generation}",
            generation,
            AgentProvider.CLAUDE_CODE,
            action,
            force=force,
            keep_disabled=keep_disabled,
        )
        result = self._agent_plugin_adapter.apply(request)
        payload_for = getattr(self._agent_plugin_adapter, "take_payload", None)
        payload = (
            payload_for(request.request_id, request.generation)
            if callable(payload_for)
            else None
        )
        return result, payload

    def _close_agent_integration_adapters(self) -> None:
        """Idempotently release hook routes and provider CLI ownership."""

        for adapter in (self._agent_hook_adapter, self._agent_plugin_adapter):
            try:
                adapter.close()
            except Exception:
                self._logger.warning(
                    "[ClaudeHUD] integration close status=error adapter=%s",
                    type(adapter).__name__,
                )

    @staticmethod
    def _codex_discovery_path() -> Path:
        return get_app_dir() / "codex-hud-port.json"

    def _remove_codex_discovery_file(self) -> None:
        try:
            self._codex_discovery_path().unlink(missing_ok=True)
        except OSError:
            self._logger.warning("[CodexPlugin] port file remove status=failed")

    def _write_codex_discovery_file(self) -> None:
        """Publish the active loopback port and per-process hook credential."""

        if self._diagnostics_locked():
            self._remove_codex_discovery_file()
            return
        try:
            from ..eula import _atomic_write_json

            _atomic_write_json(
                self._codex_discovery_path(),
                {"port": self._port, "token": self._agent_hook_token},
            )
            self._logger.info("[CodexPlugin] port file status=written")
        except Exception as exc:
            self._logger.warning(
                "[CodexPlugin] port file write failed reason=%s",
                type(exc).__name__,
            )

    def _clear_agent_speak_state(self, *, reason: str) -> None:
        """Scrub every Agent Speak content/identifier store idempotently."""
        with self._agent_snapshot_lock:
            self._agent_snapshot_generation += 1
            self._agent_snapshot_clearing = True
        if self._managed_expiry_timer is not None:
            self._managed_expiry_timer.cancel()
            self._managed_expiry_timer = None

        response = self._agent_response_coordinator or self._claude_response_coordinator
        question = self._agent_question_coordinator or self._claude_question_coordinator
        broker = self._agent_interactions or self._claude_interactions
        leases = self._agent_compose_leases
        if response is not None:
            response.close()
        if question is not None:
            question.clear()
        # The controller also owns normal speech outside those two lifecycle
        # coordinators (for example deterministic announcements). Flush its
        # in-memory utterance FIFO unconditionally at the privacy boundary.
        speech_controller = getattr(self._app, "speech_controller", None)
        if speech_controller is not None:
            speech_controller.stop()
            release = getattr(speech_controller, "release_idle_engines", None)
            if callable(release):
                release()
        if leases is not None:
            leases.clear()
        if broker is not None:
            broker.clear()
        self._unsettled_turns.clear()
        self._agent_conversations.clear()
        self.broadcast({"type": "agent.conversation.snapshot", "version": 1, "sessions": []})
        if self._agent_activity is not None:
            self._agent_activity.clear()
        if self._agent_speech_focus is not None:
            self._agent_speech_focus.clear()
        if self._claude_activation_registry is not None:
            self._claude_activation_registry.clear()
        if self._codex_activation_registry is not None:
            self._codex_activation_registry.clear()
        if self._managed_sessions is not None:
            self._managed_sessions.clear()
        self._clear_claude_mod_route_state()
        try:
            from ..claude_hud import clear_claude_session_state  # noqa: PLC0415

            clear_claude_session_state()
        except Exception:
            pass
        try:
            from ..codex_hud import clear_codex_session_state  # noqa: PLC0415

            clear_codex_session_state()
        except Exception:
            pass
        try:
            from ..agent_session_filter import clear_session_filter_cache  # noqa: PLC0415

            clear_session_filter_cache()
        except Exception:
            pass
        with self._agent_session_handles_lock:
            self._agent_session_handles.clear()
        cancel_hook_routes = getattr(self._agent_hook_adapter, "cancel_all", None)
        if callable(cancel_hook_routes):
            cancel_hook_routes()
        setter = getattr(self._app, "set_agent_compose_leases", None)
        if callable(setter):
            setter(None)
        self._agent_activity = None
        self._agent_speech_focus = None
        self._agent_compose_leases = None
        self._claude_interactions = None
        self._claude_response_coordinator = None
        self._claude_question_coordinator = None
        self._agent_interactions = None
        self._agent_response_coordinator = None
        self._agent_question_coordinator = None
        self._claude_activation_registry = None
        self._codex_activation_registry = None
        self._managed_sessions = None
        self._claude_hook_delivered = None
        self._claude_mod_poll_gaps = None
        if self._reply_desk is not None:
            self._reply_desk.clear()
        self._reply_desk = None
        self._claimed_enters.clear()
        self._claimed_escapes.clear()
        for timer in self._reply_withdrawals.values():
            timer.cancel()
        self._reply_withdrawals.clear()
        self._unsettled_turns.clear()
        self._setup_delivery_preview = None
        self._remove_claude_discovery_file()
        self._remove_codex_discovery_file()
        with self._agent_snapshot_lock:
            for family in ("response", "question", "interactions"):
                self._agent_snapshot_revisions[family] += 1
                self._agent_snapshot_clear_revisions[family] = (
                    self._agent_snapshot_revisions[family]
                )
            response_revision = self._agent_snapshot_clear_revisions["response"]
            question_revision = self._agent_snapshot_clear_revisions["question"]
            interaction_revision = self._agent_snapshot_clear_revisions["interactions"]
        # Literal empty events prove cleanup cannot lazily recreate any store
        # while the feature is disabled, locked down, or shutting down.
        self.broadcast({
            "type": "agent.completions.snapshot",
            "version": _AGENT_COMPLETION_EVENT_VERSION,
            "completions": [],
        })
        self.broadcast({
            "type": "agent.response_queue.snapshot",
            "version": _AGENT_RESPONSE_QUEUE_EVENT_VERSION,
            "revision": response_revision,
            "phase": "idle",
            "ordered_completion_ids": [],
            "active": None,
            "quiet_delay_remaining_seconds": 0.0,
            "completions": [],
            "show_summary_text": self._effective_agent_summary_text_enabled(),
        })
        self.broadcast({
            "type": "agent.question.snapshot",
            "version": 1,
            "revision": question_revision,
            "active": None,
        })
        self.broadcast({"type": "agent.sessions", "sessions": []})
        self.broadcast({
            "type": "agent.interactions.snapshot",
            "version": _AGENT_INTERACTION_EVENT_VERSION,
            "revision": interaction_revision,
            "interactions": [],
        })
        with self._agent_snapshot_lock:
            self._agent_snapshot_clearing = False
        self._logger.info("[ClaudeHUD] state cleared reason=%s", reason)

    def _get_agent_compose_leases(self):
        """Lazily construct and attach the scoped compose lease store."""
        if self._agent_compose_leases is None:
            from ..agent_compose import AgentComposeLeaseStore  # noqa: PLC0415

            self._agent_compose_leases = AgentComposeLeaseStore()
            setter = getattr(self._app, "set_agent_compose_leases", None)
            if callable(setter):
                setter(self._agent_compose_leases)
        return self._agent_compose_leases

    def _get_agent_interactions(self):
        """Lazily construct the shared condition-backed decision broker."""
        if self._agent_interactions is None:
            if self._claude_interactions is not None:
                self._agent_interactions = self._claude_interactions
            else:
                from ..claude_interactions import AgentInteractionBroker  # noqa: PLC0415

                self._agent_interactions = AgentInteractionBroker()
        self._claude_interactions = self._agent_interactions
        return self._agent_interactions

    def _get_claude_interactions(self):
        """Compatibility alias for the shared agent decision broker."""
        return self._get_agent_interactions()

    def _get_agent_response_coordinator(self):
        """Lazily construct the one all-provider response lifecycle owner."""
        if self._agent_response_coordinator is None:
            if self._claude_response_coordinator is not None:
                self._agent_response_coordinator = self._claude_response_coordinator
            else:
                from ..claude_interactions import AgentResponseCoordinator  # noqa: PLC0415

                generation = self._agent_snapshot_generation
                self._agent_response_coordinator = AgentResponseCoordinator(
                    broker=self._get_agent_interactions(),
                    quiet_delay_provider=self._claude_reply_queue_delay_seconds,
                    next_revision=lambda: self._next_agent_snapshot_revision("response"),
                    state_changed=lambda: self._publish_agent_coordinator_snapshot(
                        "response", generation
                    ),
                    interaction_changed=self._response_interaction_changed,
                    completion_terminal=self._response_completion_terminal,
                    chime=self._play_claude_response_chime,
                    speech_stop=self._stop_response_speech,
                    speech_skip_delay=self._reset_response_speech_delay,
                    draft_present=self._reply_draft_present,
                )
        self._claude_response_coordinator = self._agent_response_coordinator
        return self._agent_response_coordinator

    def _get_claude_response_coordinator(self):
        """Compatibility alias for the shared response coordinator."""
        return self._get_agent_response_coordinator()

    def _get_agent_question_coordinator(self):
        """Lazily construct the exact-generation question lifecycle owner."""
        if self._agent_question_coordinator is None:
            if self._claude_question_coordinator is not None:
                self._agent_question_coordinator = self._claude_question_coordinator
            else:
                from ..claude_interactions import AgentQuestionCoordinator  # noqa: PLC0415

                generation = self._agent_snapshot_generation
                self._agent_question_coordinator = AgentQuestionCoordinator(
                    broker=self._get_agent_interactions(),
                    next_revision=lambda: self._next_agent_snapshot_revision("question"),
                    state_changed=lambda: self._publish_agent_coordinator_snapshot(
                        "question", generation
                    ),
                    interaction_changed=self._question_interaction_changed,
                    speech_stop=self._stop_response_speech,
                )
        self._claude_question_coordinator = self._agent_question_coordinator
        return self._agent_question_coordinator

    def _get_claude_question_coordinator(self):
        """Compatibility alias for the Claude-only structured question path."""
        return self._get_agent_question_coordinator()

    def _stop_response_speech(self) -> None:
        """Stop the sole lifecycle-owned response speech generation."""

        controller = getattr(self._app, "speech_controller", None)
        if controller is not None:
            controller.stop()

    def _reset_response_speech_delay(self) -> None:
        """Discard only the prior response's speech pacing after Escape."""
        controller = getattr(self._app, "speech_controller", None)
        if controller is not None:
            controller.reset_queue_delay()

    def _claude_reply_grace_seconds(self) -> int:
        """Return the validated Stop reply grace, including legacy runtime state."""
        raw = getattr(
            self._app.config,
            "claude_reply_grace_seconds",
            _MISSING_CONFIG_VALUE,
        )
        if (
            isinstance(raw, int)
            and not isinstance(raw, bool)
            and 0 <= raw <= 30
        ):
            return raw
        legacy = getattr(
            self._app.config,
            "claude_reply_parking_enabled",
            _MISSING_CONFIG_VALUE,
        )
        if isinstance(legacy, bool):
            return 6 if legacy else 0
        return 6

    def _claude_reply_queue_delay_seconds(self) -> int:
        """Return the validated visually silent inter-response delay."""
        raw = getattr(
            self._app.config,
            "claude_reply_queue_delay_seconds",
            _MISSING_CONFIG_VALUE,
        )
        if (
            isinstance(raw, int)
            and not isinstance(raw, bool)
            and 0 <= raw <= 10
        ):
            return raw
        return 2

    def _play_claude_response_chime(self) -> None:
        try:
            self._app.audio_feedback.play_start(
                self._app.config.start_sound_path
            )
        except Exception as exc:
            self._logger.warning(
                "[ClaudeHUD] chime error reason=%s", type(exc).__name__
            )

    def _response_completion_terminal(self, completion_id: str) -> None:
        """Retire one lifecycle-owned completion without a frontend ack."""
        if self._agent_activity is not None:
            self._agent_activity.acknowledge(completion_id)

    def _response_interaction_changed(self, interaction: Any) -> None:
        """Publish coordinator-owned Stop routability and fail-open state."""
        self._broadcast_agent_interaction(interaction)

    def _question_interaction_changed(self, interaction: Any) -> None:
        """Publish exact question routability without enabling permissions."""
        self._broadcast_agent_interaction(interaction)

    def _record_sent_reply(self, interaction: Any) -> None:
        """Retain accepted HUD replies for reading only, never for routing."""
        if interaction is not None and interaction.status == "resolved" and interaction.kind == "stop":
            self._remember_agent_turn(interaction.session_handle, interaction.request_id, "user", interaction.decision)

    def _remember_agent_turn(self, handle, turn_id, role, text) -> None:
        if not self._diagnostics_locked() and bool(getattr(self._app.config, "claude_hud_show_text", True)):
            self._agent_conversations.append(handle, turn_id, role, text)
            self.broadcast(self._agent_conversation_snapshot_event())

    def _agent_conversation_snapshot_event(self) -> Dict[str, Any]:
        visible = not self._diagnostics_locked() and bool(getattr(self._app.config, "claude_hud_show_text", True))
        if not visible:
            self._agent_conversations.clear()
        return {"type": "agent.conversation.snapshot", "version": 1, "sessions": self._agent_conversations.snapshot() if visible else []}


    def _get_claude_activation_registry(self):
        """Return the process-local Agent Speech activation registry."""
        if self._claude_activation_registry is None:
            from ..agent_events import SessionActivationRegistry  # noqa: PLC0415

            self._claude_activation_registry = SessionActivationRegistry()
        return self._claude_activation_registry

    def _get_codex_activation_registry(self):
        """Return the process-local Codex Agent Speech activation registry."""
        if self._codex_activation_registry is None:
            from ..agent_events import SessionActivationRegistry  # noqa: PLC0415

            self._codex_activation_registry = SessionActivationRegistry()
        return self._codex_activation_registry

    @staticmethod
    def _agent_interaction_state_event(interaction: Any) -> Dict[str, Any]:
        """Serialize an interaction without its raw session or decision."""
        return {
            "type": "agent.interaction.state",
            "version": _AGENT_INTERACTION_EVENT_VERSION,
            "interaction": interaction.to_wire(now=time.monotonic()),
        }

    def _agent_interactions_snapshot_event(self) -> Dict[str, Any]:
        """Replace HUD interaction authority, including an absent owner.

        Interaction names are transcript-class. Never log the payload.
        """
        generation = self._agent_snapshot_generation
        broker = self._agent_interactions or self._claude_interactions
        result = (
            broker.versioned_snapshot(
                lambda: self._next_agent_snapshot_revision("interactions")
            )
            if broker is not None and not self._agent_snapshot_clearing
            else None
        )
        with self._agent_snapshot_lock:
            current = (
                generation == self._agent_snapshot_generation
                and not self._agent_snapshot_clearing
            )
            clear_revision = self._agent_snapshot_clear_revisions["interactions"]
        revision, interactions = result if result is not None and current else (clear_revision, ())
        return {
            "type": "agent.interactions.snapshot",
            "version": _AGENT_INTERACTION_EVENT_VERSION,
            "revision": revision,
            "interactions": [
                self._agent_interaction_state_event(item)
                for item in interactions if item.status == "pending"
            ],
        }

    def _broadcast_agent_interaction(self, interaction: Any) -> None:
        self.broadcast(self._agent_interaction_state_event(interaction))

    def _agent_speech_is_active(self, agent: str, session_id: object) -> bool:
        activation = getattr(
            self._app.config, "agent_speech_activation", "always_on"
        )
        if activation == "always_on":
            return True
        if activation != "per_session":
            return False
        registry = (
            self._get_claude_activation_registry()
            if agent == "claude"
            else self._get_codex_activation_registry()
            if agent == "codex"
            else None
        )
        return bool(registry is not None and registry.is_active(session_id))

    def _claude_speech_is_active(self, session_id: object) -> bool:
        """Compatibility wrapper for the shipped Claude call sites."""
        return self._agent_speech_is_active("claude", session_id)

    def _publish_blocking_agent_response(
        self,
        agent: str,
        result: Dict[str, Any],
    ) -> tuple[str, int, bool]:
        """Retain one response and publish its authoritative visual records.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # ``result`` may contain the complete agent response and marked
        # summary. Never log either value or a serialized console event.
        # ====================================================================
        """
        cfg = self._app.config
        raw_session_id = result.get("session_id")
        show_text = bool(getattr(cfg, "claude_hud_show_text", True))
        completion_id: Optional[str] = None
        completion_sequence = 0

        activity = self._get_agent_activity()
        if activity is None:
            return "", 0, True
        enqueue_result = activity.enqueue(result)
        if enqueue_result.duplicate:
            return "", 0, True
        pending_ids = self._pending_agent_completion_ids()
        for completion in enqueue_result.changed:
            if completion.kind == "response":
                completion_id = completion.completion_id
                completion_sequence = completion.sequence
            if show_text:
                self.broadcast(
                    {
                        "type": "agent.completion",
                        "version": _AGENT_COMPLETION_EVENT_VERSION,
                        "show_text": True,
                        "pending_completion_ids": pending_ids,
                        "completion": self._agent_completion_to_wire(completion),
                    }
                )

        if completion_id is None:
            # Hidden or retention-overflow responses still need an exact
            # process-local lifecycle identity, but their text never enters
            # the response queue snapshot.
            completion_id = secrets.token_urlsafe(18)
        if show_text and isinstance(raw_session_id, str) and raw_session_id:
            self._remember_agent_turn(self._agent_session_handle(raw_session_id, agent), completion_id, "assistant", result.get("text"))
        if not show_text:
            # Move newly observed content into the bounded digest-only dedupe
            # history immediately. TTS and reply routing retain their local
            # inputs, but raw response text never enters a WebSocket event.
            for completion in enqueue_result.changed:
                self._agent_activity.acknowledge(completion.completion_id)

        if isinstance(raw_session_id, str) and raw_session_id:
            activity_event: Dict[str, Any] = {
                "type": "agent.hud.activity",
                "version": 1,
                "active": False,
            }
            if agent != "claude":
                activity_event["agent"] = agent
            self.broadcast(activity_event)
        return completion_id, completion_sequence, False

    def _publish_blocking_claude_response(
        self, result: Dict[str, Any]
    ) -> tuple[str, int, bool]:
        """Compatibility wrapper for the shipped Claude path."""
        return self._publish_blocking_agent_response("claude", result)

    def _queue_agent_response(
        self,
        *,
        agent: str,
        result: Dict[str, Any],
        completion_id: str,
        completion_sequence: int,
        interaction: Any = None,
        waiter_loop: Any = None,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> Any:
        """Enqueue one response with a promotion-time speech decision."""
        grant = acceptance_grant or self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return None
        from ..speech.content import select_speech_content

        raw_session_id = result.get("session_id")
        # Only a user-chosen name is ever spoken (scope decision 4): the
        # project-basename fallback and the literal "Claude" never set
        # source_name_is_custom, so name_prefix stays None for them.
        source_name = result.get("source_name")
        name_prefix = (
            source_name
            if result.get("source_name_is_custom") and isinstance(source_name, str)
            else None
        )

        def start_speech(on_terminal: Any) -> bool:
            if not self._acceptance_is_current(grant):
                return False
            controller = getattr(self._app, "speech_controller", None)
            speech_enabled = self._effective_agent_speech_enabled()
            spoken_summary = select_speech_content(result, self._app.config)
            will_speak = (
                speech_enabled
                and isinstance(spoken_summary, str)
                and bool(spoken_summary.strip())
                and controller is not None
                and controller.can_speak()
                and self._agent_speech_is_active(agent, raw_session_id)
            )
            if not will_speak:
                return False
            try:
                return (
                    controller.speak_spoken_summary(
                        spoken_summary,
                        completion_id=completion_id,
                        on_terminal=on_terminal,
                        name_prefix=name_prefix,
                    )
                    is True
                )
            except Exception as exc:
                self._logger.warning(
                    "[Speech] marked summary schedule error reason=%s",
                    type(exc).__name__,
                )
                return False

        return self._get_agent_response_coordinator().enqueue(
            completion_id=completion_id,
            sequence=completion_sequence,
            grace_seconds=self._claude_reply_grace_seconds(),
            speech_start=start_speech,
            agent=agent,
            interaction=interaction,
            waiter_loop=waiter_loop,
        )

    def _queue_claude_response(
        self,
        *,
        result: Dict[str, Any],
        completion_id: str,
        completion_sequence: int,
        interaction: Any = None,
        waiter_loop: Any = None,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> Any:
        """Compatibility wrapper for the shipped Claude path."""
        return self._queue_agent_response(
            agent="claude",
            result=result,
            completion_id=completion_id,
            completion_sequence=completion_sequence,
            interaction=interaction,
            waiter_loop=waiter_loop,
            acceptance_grant=acceptance_grant,
        )

    def _publish_agent_working(
        self,
        agent: str,
        raw_session_id: object,
        *,
        compatibility: bool = False,
    ) -> None:
        """Publish content-free work without creating answer authority."""
        if not isinstance(raw_session_id, str) or not raw_session_id:
            return
        if self._agent_session_handle(raw_session_id, agent) is None:
            return
        if bool(getattr(self._app.config, "claude_hud_show_text", True)):
            self._agent_conversations.activity(self._agent_session_handle(raw_session_id, agent), True)
            self.broadcast(self._agent_conversation_snapshot_event())
        activity_event: Dict[str, Any] = {
            "type": "agent.hud.activity",
            "version": 1,
            "active": True,
        }
        if agent != "claude":
            activity_event["agent"] = agent
        self.broadcast(activity_event)

    def _publish_claude_working(
        self,
        raw_session_id: object,
        *,
        compatibility: bool = False,
    ) -> None:
        """Compatibility wrapper for the shipped Claude path."""
        self._publish_agent_working(
            "claude",
            raw_session_id,
            compatibility=compatibility,
        )

    def _surface_claude_question(
        self,
        *,
        raw_session_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Show a validated question without claiming its answer park exists."""
        session_handle = self._agent_session_handle(raw_session_id)
        if session_handle is None:
            return
        self._get_claude_question_coordinator().surface(
            session_handle,
            result["questions"],
            # Already resolved and sanitized at the hook seam. The snapshot
            # carries this instead of the opaque compose lease handle.
            source_name=result.get("source_name"),
        )

    @staticmethod
    def _question_speech_text(
        questions: list[dict[str, Any]], index: int
    ) -> str:
        """Compose exact question text plus numbered labels, never descriptions."""
        if not 0 <= index < len(questions):
            return ""
        item = questions[index]
        question = item.get("question")
        options = item.get("options")
        if not isinstance(question, str) or not isinstance(options, list):
            return ""
        labels = [
            option.get("label")
            for option in options
            if isinstance(option, dict)
            and isinstance(option.get("label"), str)
        ]
        suffix = " ".join(
            f"{position}. {label}"
            for position, label in enumerate(labels, start=1)
        )
        return f"{question} {suffix}".rstrip()

    def _question_speech_start(
        self,
        questions: list[dict[str, Any]],
        index: int,
        on_terminal: Any,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> bool:
        """Schedule exact question speech under the existing voice policy."""
        grant = acceptance_grant or self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return False
        controller = getattr(self._app, "speech_controller", None)
        text = self._question_speech_text(questions, index)
        speech_enabled = self._effective_agent_speech_enabled()
        if (
            controller is None
            or not text
            or not speech_enabled
            or not controller.can_speak()
        ):
            return False
        try:
            # Deliberately no name_prefix here: speak_spoken_summary is shared
            # with question speech, and question speech opts out of the
            # session-name prefix (scope.md decision 4 / research.md
            # `## Constraints Discovered`). Do not add name_prefix to this call.
            return (
                controller.speak_spoken_summary(
                    text,
                    completion_id=f"question-{secrets.token_urlsafe(12)}",
                    on_terminal=on_terminal,
                )
                is True
            )
        except Exception as exc:
            self._logger.warning(
                "[Speech] question schedule error reason=%s",
                type(exc).__name__,
            )
            return False

    def _create_hud_interaction(
        self,
        *,
        agent: str = "claude",
        raw_session_id: str,
        kind: str,
        result: Dict[str, Any],
        passive_seconds: Optional[int] = None,
        active: bool = True,
    ) -> Any | None:
        """Create and publish one current actionable HUD interaction."""
        session_handle = self._agent_session_handle(raw_session_id, agent)
        if session_handle is None:
            return None
        interaction = self._get_agent_interactions().create(
            session_handle,
            kind,
            agent=agent,
            raw_session_id=raw_session_id,
            passive_seconds=passive_seconds,
            active=active,
            # Already resolved and sanitized at the hook seam (Task 1.6's
            # _resolve_session_name / _attach_session_name). The permission
            # pill shows this instead of the literal "Claude" (scope
            # decision 3).
            source_name=result.get("source_name"),
        )
        self._broadcast_agent_interaction(interaction)
        return interaction

    def _complete_hud_interaction(
        self, interaction: Any, raw_session_id: str
    ) -> None:
        """Publish terminal content-free state after endpoint wait completes."""
        self._broadcast_agent_interaction(interaction)

    def _encode_and_discard_claude_interaction(
        self,
        *,
        interaction: Any,
        kind: str,
        questions: object = None,
    ) -> Dict[str, Any]:
        """Encode the waiting hook result, then scrub its terminal broker row."""

        try:
            return self._encode_claude_hook_decision(
                kind=kind,
                decision=interaction.decision,
                questions=questions,
            )
        finally:
            broker = self._claude_interactions
            if broker is not None:
                broker.discard_terminal(interaction.request_id)
            self._prune_agent_session_handles()

    def _encode_and_discard_agent_interaction(
        self,
        *,
        agent: str,
        interaction: Any,
        kind: str,
        questions: object = None,
    ) -> Dict[str, Any]:
        """Encode one provider result, then scrub its terminal broker row."""

        try:
            encoder = (
                self._encode_claude_hook_decision
                if agent == "claude"
                else self._encode_codex_hook_decision
                if agent == "codex"
                else None
            )
            if encoder is None:
                return {}
            return encoder(
                kind=kind,
                decision=interaction.decision,
                questions=questions,
            )
        finally:
            broker = self._agent_interactions or self._claude_interactions
            if broker is not None:
                broker.discard_terminal(interaction.request_id)
            self._prune_agent_session_handles()

    @staticmethod
    def _encode_claude_hook_decision(
        *,
        kind: str,
        decision: object,
        questions: object = None,
    ) -> Dict[str, Any]:
        """Return validated Claude hook JSON, or fail open with ``{}``."""
        if kind == "stop":
            if not isinstance(decision, str) or not decision.strip():
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": (
                        "User reply from Whisperstream HUD: "
                        f"{decision}"
                    ),
                }
            }
        if kind == "permission":
            if decision not in {"allow", "deny"}:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": decision},
                }
            }
        if kind != "question" or not isinstance(questions, list):
            return {}
        if not isinstance(decision, dict) or not decision:
            return {}
        expected = {
            item.get("question")
            for item in questions
            if isinstance(item, dict) and isinstance(item.get("question"), str)
        }
        if (
            len(expected) != len(questions)
            or set(decision) != expected
            or any(
                not isinstance(answer, str) or not answer.strip()
                for answer in decision.values()
            )
        ):
            return {}
        return {
            # `hookEventName` is REQUIRED by Claude Code on every
            # `hookSpecificOutput`, including this one. Without it the CLI's
            # schema check fails with `hookSpecificOutput is missing required
            # field "hookEventName"`, discards the whole decision, and falls
            # back to its own terminal prompt - so every HUD answer (digit,
            # click, or dictated) resolved cleanly on our side and then
            # silently never reached the tool call.
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow",
                    "updatedInput": {
                        "questions": questions,
                        "answers": decision,
                    },
                },
            }
        }

    @staticmethod
    def _encode_codex_hook_decision(
        *,
        kind: str,
        decision: object,
        questions: object = None,
    ) -> Dict[str, Any]:
        """Return exact Codex hook JSON, or fail open with ``{}``."""

        if kind == "stop":
            if not isinstance(decision, str) or not decision.strip():
                return {}
            return {"decision": "block", "reason": decision}
        if kind == "permission":
            if decision not in {"allow", "deny"}:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": decision},
                }
            }
        # Codex has no answer-bearing structured-question hook contract.
        return {}

    async def _handle_claude_hook(self, request: Request) -> Dict[str, Any]:
        """Parse a decision-bearing Claude hook request and handle its body.

        Body handling lives in :meth:`_handle_claude_hook_body` so the dev HUD
        simulator can drive the exact same path with a synthesized payload and
        no HTTP round trip. The correlation token, if any, arrives as the
        ``_MANAGED_SESSION_TOKEN_HEADER`` header (Task 2.4), never as a body
        field, so it is read here rather than in the shared body handler.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The request, normalized result, decisions, and returned contexts may
        # contain transcript-class content. Log metadata only.
        # ====================================================================
        """
        grant = self._request_acceptance_grant(request)
        if not self._acceptance_is_current(grant):
            return {}
        try:
            body = await request.json()
        except Exception:
            return {}
        correlation_token = request.headers.get(_MANAGED_SESSION_TOKEN_HEADER)
        return await self._handle_claude_hook_body(
            body,
            correlation_token=correlation_token,
            acceptance_grant=grant,
        )

    async def _handle_claude_hook_body(
        self,
        body: Any,
        *,
        correlation_token: str | None = None,
        acceptance_grant: AcceptanceGrant | None = None,
        persistent_transport: bool = False,
    ) -> Dict[str, Any]:
        """Handle one decision-bearing Claude hook body with fail-open JSON.

        ``correlation_token`` is the wrapper-minted per-session token echoed
        by ``forward-hook.ps1`` (Task 2.4), present only for `whisperstream
        claude`-wrapped sessions. When present and it matches a registered
        managed session, it binds or rebinds that session's ``session_id`` and
        records the last-observed event kind. This is a pure side effect on
        the managed-session store: it never changes the response returned to
        Claude, and any failure inside it is swallowed so correlation can
        never make a hook response non-empty or fail closed. Absent
        (unwrapped sessions), behavior is byte-identical to before Task 2.4.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The body, normalized result, decisions, and returned contexts may
        # contain transcript-class content. Log metadata only.
        # ====================================================================
        """
        # Stale installed hooks must remain fail-open while either Lockdown
        # marker is present. Check before parsing or constructing any lazy
        # Agent Speak/session store so post-entry requests cannot repopulate
        # the content that Lockdown just cleared.
        if self._diagnostics_locked():
            return {}
        grant = acceptance_grant or self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return {}
        if not self._agent_provider_enabled("claude"):
            return {}
        try:
            if not isinstance(body, dict):
                return {}
            if not self._acceptance_is_current(grant):
                return {}
            if not persistent_transport and self._agent_session_excluded("claude", body):
                return {}

            from ..claude_hud import correlation_fields, parse_claude_event  # noqa: PLC0415

            if not persistent_transport and self._managed_sessions is not None:
                fields = correlation_fields(body)
                if fields is not None:
                    event_name, event_sid = fields
                    for peer in self._managed_sessions.sessions():
                        if peer.transport == "claude_mod" and peer.session_id == event_sid and peer.is_fresh(time.monotonic(), 8):
                            if event_name == "Stop":
                                return {}  # turn.complete owns this connected session
                            correlation_token = peer.token
                            break
                    else:
                        gaps = self._claude_mod_poll_gaps
                        if event_name == "Stop" and gaps is not None and gaps.seen(event_sid):
                            # The session has a mod, but its poll is stale (for
                            # example a paused background session), so the hook
                            # delivers this turn. The mod's later re-send is dropped.
                            self._logger.debug("[ClaudeHUD] Stop route=hook reason=mod_stale")

            bound_session = None
            if correlation_token:
                try:
                    fields = correlation_fields(body)
                    if fields is not None:
                        raw_event_name, event_session_id = fields
                        bound_session = self._record_managed_event(
                            correlation_token,
                            raw_event_name,
                            agent="claude",
                            session_id=event_session_id,
                        )
                        if bound_session is not None:
                            self._broadcast_agent_sessions()
                except Exception:  # noqa: BLE001 - correlation must never block the hook
                    pass

            result = parse_claude_event(
                body,
                activation_registry=self._get_claude_activation_registry(),
                speech_activation=getattr(
                    self._app.config,
                    "agent_speech_activation",
                    "per_session",
                ),
            )
            if result is None:
                return {}
            if not self._acceptance_is_current(grant):
                return {}
            kind = result.get("kind")
            raw_session_id = result.get("session_id")
            speech_controller = getattr(self._app, "speech_controller", None)

            if kind == "prompt_submitted":
                self._get_agent_speech_focus().note_prompt(
                    "claude", raw_session_id
                )
                # A new prompt empties the mod's queue, so it can no longer
                # re-send a turn the hook delivered before this prompt.
                if self._claude_hook_delivered is not None:
                    self._claude_hook_delivered.forget_session(raw_session_id)
                self._publish_claude_working(raw_session_id)
                context = result.get("additional_context")
                if isinstance(context, str) and context:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": context,
                        }
                    }
                return {}
            if kind == "speech_activation":
                return {}
            if kind == "session_name":
                # Silent state write (registry set/clear). No HUD broadcast,
                # no speech, no completion. named=true|false only; never log
                # the name itself.
                return {}
            if kind == "working":
                self._publish_claude_working(
                    raw_session_id,
                    compatibility=True,
                )
                return {}
            if kind in {"error", "progress"}:
                if (
                    speech_controller is not None
                    and self._get_agent_speech_focus().should_speak(
                        "claude", raw_session_id
                    )
                ):
                    speech_controller.speak_event(
                        kind,
                        "claude",
                        result.get("detail"),
                    )
                return {}

            if kind == "response":
                # The same turn reaches us once per route. A Stop hook that ran
                # while the mod looked stale already delivered it, so drop the
                # mod's copy when its paused timer resumes and sends it.
                turn_key = result.get("spoken_summary") or result.get("text")
                if persistent_transport:
                    if self._claude_hook_delivered is not None and self._claude_hook_delivered.consume(
                        raw_session_id, turn_key
                    ):
                        self._logger.debug(
                            "[ClaudeMod] completion dropped reason=hook_delivered"
                        )
                        return {}
                elif self._claude_mod_poll_gaps is not None and self._claude_mod_poll_gaps.seen(
                    raw_session_id
                ):
                    self._get_claude_hook_delivered().record(raw_session_id, turn_key)
                if (
                    bound_session is not None
                    and correlation_token
                    and bool(getattr(self._app.config, "claude_hud_show_text", True))
                ):
                    try:
                        if self._get_managed_sessions().record_response(
                            correlation_token, result.get("text", "")
                        ):
                            self._broadcast_agent_sessions()
                    except Exception:  # noqa: BLE001 - retention cannot block the hook
                        pass
                (
                    completion_id,
                    completion_sequence,
                    duplicate,
                ) = self._publish_blocking_claude_response(result)
                if duplicate:
                    return {}
                reply_grace_seconds = self._claude_reply_grace_seconds()
                if (
                    reply_grace_seconds == 0
                    or not result.get("park_eligible")
                    or not isinstance(raw_session_id, str)
                    or not raw_session_id
                ):
                    self._queue_claude_response(
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                        acceptance_grant=grant,
                    )
                    return {}
                interaction = self._create_hud_interaction(
                    raw_session_id=raw_session_id,
                    kind="stop",
                    result=result,
                    passive_seconds=reply_grace_seconds,
                    active=False,
                )
                if interaction is None:
                    self._queue_claude_response(
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                    )
                    return {}
                persistent_reply = self._bind_persistent_stop(interaction, bound_session, grant)
                waiter = self._queue_claude_response(
                    result=result,
                    completion_id=completion_id,
                    completion_sequence=completion_sequence,
                    interaction=interaction,
                    waiter_loop=asyncio.get_running_loop(),
                    acceptance_grant=grant,
                )
                if waiter is None:
                    broker = self._claude_interactions
                    if broker is not None:
                        final = broker.close(interaction.request_id)
                        if final is not None:
                            self._complete_hud_interaction(
                                final,
                                raw_session_id,
                            )
                            self._encode_and_discard_claude_interaction(
                                interaction=final,
                                kind="stop",
                            )
                    return {}
                if persistent_reply:
                    self._detach_persistent_stop(waiter, interaction)
                    return {}
                final = await waiter
                if final is None:
                    return {}
                return self._encode_and_discard_claude_interaction(
                    interaction=final,
                    kind="stop",
                )
            elif kind == "question":
                if not isinstance(raw_session_id, str) or not raw_session_id:
                    return {}
                if result.get("phase") == "surfaced":
                    self._surface_claude_question(
                        raw_session_id=raw_session_id,
                        result=result,
                    )
                    return {}
                if result.get("phase") != "answerable":
                    return {}
                reply_grace_seconds = self._claude_reply_grace_seconds()
                if reply_grace_seconds == 0:
                    if self._claude_question_coordinator is not None:
                        self._claude_question_coordinator.expire_current()
                    return {}
                interaction_kind = "question"
            elif kind == "permission":
                if (
                    not getattr(
                        self._app.config,
                        "claude_permission_decisions_enabled",
                        False,
                    )
                    or not isinstance(raw_session_id, str)
                    or not raw_session_id
                ):
                    return {}
                interaction_kind = "permission"
            else:
                return {}

            interaction = self._create_hud_interaction(
                raw_session_id=raw_session_id,
                kind=interaction_kind,
                result=result,
                passive_seconds=(
                    reply_grace_seconds
                    if interaction_kind == "question"
                    else None
                ),
                active=interaction_kind != "question",
            )
            if interaction is None:
                return {}
            if interaction_kind == "question":
                questions = result["questions"]
                coordinator = self._get_claude_question_coordinator()
                attached = coordinator.attach(
                    interaction,
                    questions,
                    grace_seconds=reply_grace_seconds,
                    speech_start=lambda index, on_terminal: (
                        self._question_speech_start(
                            questions,
                            index,
                            on_terminal,
                            acceptance_grant=grant,
                        )
                    ),
                )
                if attached.status != "pending":
                    final = attached
                else:
                    final = await asyncio.to_thread(
                        self._get_claude_interactions().wait,
                        interaction.request_id,
                    )
                if final is None:
                    return {}
                coordinator.complete(final)
                self._complete_hud_interaction(final, raw_session_id)
                return self._encode_and_discard_claude_interaction(
                    interaction=final,
                    kind="question",
                    questions=questions,
                )
            final = await asyncio.to_thread(
                self._get_claude_interactions().wait,
                interaction.request_id,
            )
            if final is None:
                return {}
            self._complete_hud_interaction(final, raw_session_id)
            return self._encode_and_discard_claude_interaction(
                interaction=final,
                kind=interaction_kind,
                questions=result.get("questions"),
            )
        except Exception as exc:
            self._logger.warning(
                "[ClaudeHook] internal error reason=%s", type(exc).__name__
            )
            return {}

    async def _handle_codex_hook(self, request: Request) -> Dict[str, Any]:
        """Parse one decision-bearing Codex request with fail-open JSON."""

        grant = self._request_acceptance_grant(request)
        if not self._acceptance_is_current(grant):
            return {}
        try:
            body = await request.json()
        except Exception:
            return {}
        correlation_token = request.headers.get(_MANAGED_SESSION_TOKEN_HEADER)
        return await self._handle_codex_hook_body(
            body,
            correlation_token=correlation_token,
            acceptance_grant=grant,
        )

    async def _handle_codex_hook_body(
        self,
        body: Any,
        *,
        correlation_token: str | None = None,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> Dict[str, Any]:
        """Handle current Codex hooks while retaining no raw input payload.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The body, normalized result, decisions, and returned continuation may
        # contain transcript-class content. Log metadata only.
        # ====================================================================
        """

        if self._diagnostics_locked():
            return {}
        grant = acceptance_grant or self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return {}
        if not self._agent_provider_enabled("codex"):
            return {}
        try:
            if not isinstance(body, dict):
                return {}
            if self._agent_session_excluded("codex", body):
                return {}
            from ..codex_hud import correlation_fields, parse_codex_event  # noqa: PLC0415

            bound_session = None
            if correlation_token:
                try:
                    fields = correlation_fields(body)
                    if fields is not None:
                        raw_event_name, event_session_id = fields
                        bound_session = self._record_managed_event(
                            correlation_token,
                            raw_event_name,
                            agent="codex",
                            session_id=event_session_id,
                        )
                        if bound_session is not None:
                            self._broadcast_agent_sessions()
                except Exception:  # noqa: BLE001 - correlation must stay fail-open
                    pass

            result = parse_codex_event(
                body,
                activation_registry=self._get_codex_activation_registry(),
                speech_activation=getattr(
                    self._app.config,
                    "agent_speech_activation",
                    "per_session",
                ),
            )
            if result is None or not self._acceptance_is_current(grant):
                return {}

            kind = result.get("kind")
            raw_session_id = result.get("session_id")
            speech_controller = getattr(self._app, "speech_controller", None)

            if kind == "prompt_submitted":
                self._get_agent_speech_focus().note_prompt(
                    "codex",
                    raw_session_id,
                )
                self._publish_agent_working("codex", raw_session_id)
                context = result.get("additional_context")
                if isinstance(context, str) and context:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": context,
                        }
                    }
                return {}

            if kind == "response":
                if (
                    bound_session is not None
                    and correlation_token
                    and bool(getattr(self._app.config, "claude_hud_show_text", True))
                ):
                    try:
                        if self._get_managed_sessions().record_response(
                            correlation_token, result.get("text", "")
                        ):
                            self._broadcast_agent_sessions()
                    except Exception:  # noqa: BLE001 - retention must stay fail-open
                        pass
                (
                    completion_id,
                    completion_sequence,
                    duplicate,
                ) = self._publish_blocking_agent_response("codex", result)
                if duplicate:
                    return {}
                reply_grace_seconds = self._claude_reply_grace_seconds()
                if (
                    reply_grace_seconds == 0
                    or not result.get("park_eligible")
                    or not isinstance(raw_session_id, str)
                    or not raw_session_id
                ):
                    self._queue_agent_response(
                        agent="codex",
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                        acceptance_grant=grant,
                    )
                    return {}
                interaction = self._create_hud_interaction(
                    agent="codex",
                    raw_session_id=raw_session_id,
                    kind="stop",
                    result=result,
                    passive_seconds=reply_grace_seconds,
                    active=False,
                )
                if interaction is None:
                    self._queue_agent_response(
                        agent="codex",
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                        acceptance_grant=grant,
                    )
                    return {}
                persistent_reply = self._bind_persistent_stop(interaction, bound_session, grant)
                waiter = self._queue_agent_response(
                    agent="codex",
                    result=result,
                    completion_id=completion_id,
                    completion_sequence=completion_sequence,
                    interaction=interaction,
                    waiter_loop=asyncio.get_running_loop(),
                    acceptance_grant=grant,
                )
                if waiter is None:
                    broker = self._agent_interactions
                    if broker is not None:
                        final = broker.close(interaction.request_id)
                        if final is not None:
                            self._complete_hud_interaction(final, raw_session_id)
                            self._encode_and_discard_agent_interaction(
                                agent="codex",
                                interaction=final,
                                kind="stop",
                            )
                    return {}
                if persistent_reply:
                    self._detach_persistent_stop(waiter, interaction)
                    return {}
                final = await waiter
                if final is None:
                    return {}
                self._complete_hud_interaction(final, raw_session_id)
                return self._encode_and_discard_agent_interaction(
                    agent="codex",
                    interaction=final,
                    kind="stop",
                )

            if kind != "permission":
                return {}
            if (
                not getattr(
                    self._app.config,
                    "claude_permission_decisions_enabled",
                    False,
                )
                or not isinstance(raw_session_id, str)
                or not raw_session_id
            ):
                return {}
            interaction = self._create_hud_interaction(
                agent="codex",
                raw_session_id=raw_session_id,
                kind="permission",
                result=result,
            )
            if interaction is None:
                return {}
            final = await asyncio.to_thread(
                self._get_agent_interactions().wait,
                interaction.request_id,
            )
            if final is None:
                return {}
            self._complete_hud_interaction(final, raw_session_id)
            return self._encode_and_discard_agent_interaction(
                agent="codex",
                interaction=final,
                kind="permission",
            )
        except Exception as exc:
            self._logger.warning(
                "[CodexHook] internal error reason=%s",
                type(exc).__name__,
            )
            return {}

    async def _handle_agent_event(
        self,
        request: Request,
        *,
        pinned_agent: str | None = None,
    ):
        """Shared body of ``/api/agent-event`` and the ``/api/claude-event`` alias.

        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # last_assistant_message in the payload is user/agent content. Log
        # metadata only (char count is fine). Never log
        # the message text itself. See text_injector.py's banner.
        # ============================================================================

        Fire-and-forget: always returns 200 ``{"ok": True}``. The PowerShell
        forwarders must never block on a slow response and no internal error
        may propagate back into an agent's hook runner. Outside an
        experimental build+run the handler is inert, but still returns 200 so
        a stale forwarder hook never breaks Claude.

        Event dispatch (normalized kinds from the Claude adapter):

        - ``response``          -> all-session visual completion retention and
                                   HUD broadcast, plus speech/chime selected by
                                   speech focus.
        - ``working``           -> content-free HUD activity only. It never
                                   opens an answer route.
        - ``error``/``progress``-> spoken template only (never the HUD),
                                   speech-focus-filtered; progress additionally
                                   requires conversational mode (controller policy).
        - ``prompt_submitted``  -> focuses routine speech and publishes working
                                   activity. Never spoken or used to stop speech.
        """
        # Keep stale fire-and-forget forwarders successful but inert throughout
        # effective Lockdown, including partial config/envelope transitions.
        # This must precede body parsing and every lazy Agent store accessor.
        if self._diagnostics_locked():
            return {"ok": True}
        grant = self._request_acceptance_grant(request)
        if not self._acceptance_is_current(grant):
            return {"ok": True}
        try:
            # Parse the body as JSON; treat any parse failure as an empty payload.
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}

            requested_agent = (
                pinned_agent
                or request.query_params.get("agent")
                or body.get("agent")
                or "claude"
            )
            if requested_agent not in {"claude", "codex"}:
                self._logger.warning(
                    "[AgentEvent] unsupported provider ignored"
                )
                return {"ok": True}

            if not self._agent_provider_enabled(requested_agent):
                return {"ok": True}
            if self._agent_session_excluded(requested_agent, body):
                return {"ok": True}
            if requested_agent == "claude":
                from ..claude_hud import parse_claude_event  # noqa: PLC0415

                result = parse_claude_event(
                    body,
                    activation_registry=self._get_claude_activation_registry(),
                    speech_activation=getattr(
                        self._app.config,
                        "agent_speech_activation",
                        "per_session",
                    ),
                )
            else:
                from ..codex_hud import parse_codex_event  # noqa: PLC0415

                result = parse_codex_event(
                    body,
                    activation_registry=self._get_codex_activation_registry(),
                    speech_activation=getattr(
                        self._app.config,
                        "agent_speech_activation",
                        "per_session",
                    ),
                )
            if result is None:
                return {"ok": True}

            agent = requested_agent
            kind = result.get("kind")
            session_id = result.get("session_id")
            speech_focus = self._get_agent_speech_focus()
            speech_controller = getattr(self._app, "speech_controller", None)
            cfg = self._app.config

            if kind == "prompt_submitted":
                # The semantic source the user last engaged gets routine
                # speech focus. This does not gate visual delivery from any
                # source or interrupt in-flight speech.
                speech_focus.note_prompt(agent, session_id)
                self._publish_agent_working(agent, session_id)

            elif kind == "response":
                (
                    completion_id,
                    completion_sequence,
                    duplicate,
                ) = (
                    self._publish_blocking_claude_response(result)
                    if agent == "claude"
                    else self._publish_blocking_agent_response(agent, result)
                )
                if duplicate:
                    return {"ok": True}
                # Flip the per-agent "verified" signal the first time a real
                # completion arrives from that agent. Only write when it is
                # not already True, to avoid a disk write on every completion
                # (scope.md #8 / research.md #8).
                verified_attribute = {
                    "claude": "claude_verified",
                    "codex": "codex_verified",
                }.get(agent)
                if verified_attribute is not None and getattr(
                    cfg, verified_attribute, False
                ) is not True:
                    setattr(cfg, verified_attribute, True)
                    self._app.config_manager.save(cfg)
                if agent == "claude":
                    self._queue_claude_response(
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                        acceptance_grant=grant,
                    )
                else:
                    self._queue_agent_response(
                        agent=agent,
                        result=result,
                        completion_id=completion_id,
                        completion_sequence=completion_sequence,
                        acceptance_grant=grant,
                    )

            elif kind == "working":
                self._publish_agent_working(
                    agent,
                    session_id,
                    compatibility=True,
                )

            elif (
                agent == "claude"
                and kind == "question"
                and result.get("phase") == "surfaced"
                and isinstance(session_id, str)
                and session_id
            ):
                self._surface_claude_question(
                    raw_session_id=session_id,
                    result=result,
                )

            elif kind == "error":
                # Template speech only (no HUD panel for errors); respects
                # speech focus so a background session's error stays quiet.
                if speech_controller is not None and speech_focus.should_speak(
                    agent, session_id
                ):
                    speech_controller.speak_event("error", agent)

            elif kind == "progress":
                # Template speech only; never the HUD. The controller further
                # requires conversational mode and drops it while anything
                # else is speaking.
                if speech_controller is not None and speech_focus.should_speak(
                    agent, session_id
                ):
                    speech_controller.speak_event("progress", agent, result.get("detail"))

        except Exception as exc:
            # Swallow all internal errors; log metadata only.
            self._logger.warning(
                "[AgentEvent] internal error agent-event reason=%s", type(exc).__name__
            )

        return {"ok": True}

    def _warm_speech_supervisor(self) -> None:
        """Pre-warm the local llama-server supervisor for the speech summarizer.

        Called from the ``/api/agent-event`` handler (Task 2.3, generalized in
        Phase 2) when a summary-shaped mode is active and speech is about to
        fire, so the
        model loads while the user reads the HUD response instead of paying a
        cold-start penalty inside ``speak_response``'s ``summarize()`` await.

        Response text NEVER goes to a cloud provider -- this only ever touches
        the LOCAL supervisor (scope.md LOCKED local-only floor), regardless of
        what ``enhancement_provider`` is configured for dictation cleanup.

        Mirrors ``_refresh_supervisor``'s lazy-construction pattern (the
        post-LLM-download supervisor prep above): if a supervisor instance
        already exists (the user's enhancement provider is local), just
        kick-warm it. If not (e.g. their enhancement provider is cloud, so
        ``main.py`` never constructed one), attempt to construct a lazy
        instance from the bundled binary + the configured
        ``enhancement_local_model`` GGUF on disk. If either is missing, skip
        silently -- never raises, never blocks. The summarizer's fallback
        chain (deterministic extraction -> truncated verbatim) is the
        contract; a missing local floor is never a silent drop of the
        response, only a slower/degraded spoken result.
        """
        try:
            cfg = self._app.config
            supervisor = getattr(self._app, "llama_server", None)
            if supervisor is None:
                from ..enhancement_models import (
                    get_local_bundled_model,
                    get_local_model,
                    resolve_local_model_path,
                )
                from ..llama_server import (
                    LlamaServerSupervisor,
                    find_bundled_llama_server,
                    runtime_acceleration_for_platform,
                )

                binary_path = find_bundled_llama_server()
                if binary_path is None:
                    self._logger.info(
                        "[Speech] warm kick skipped reason=binary_not_found"
                    )
                    return

                # Preserve the production lookup seam used by existing warm
                # paths/tests, then fall through to the merged catalog for a
                # development candidate.
                catalog_entry = get_local_bundled_model(
                    cfg.enhancement_local_model
                ) or get_local_model(cfg.enhancement_local_model)
                if catalog_entry is None:
                    self._logger.info(
                        "[Speech] warm kick skipped reason=unknown_model_id"
                    )
                    return

                gguf_path = resolve_local_model_path(catalog_entry)
                if (
                    gguf_path is None
                    or not gguf_path.exists()
                    or gguf_path.stat().st_size == 0
                ):
                    self._logger.info(
                        "[Speech] warm kick skipped reason=gguf_not_installed"
                    )
                    return

                from ..gpu_benchmark import ngl_for_verdict

                supervisor = LlamaServerSupervisor(
                    gguf_path,
                    binary_path=binary_path,
                    logger=self._logger,
                    idle_timeout_seconds=cfg.enhancement_local_idle_timeout,
                    on_state_change=self.broadcast,
                    n_gpu_layers_provider=lambda: ngl_for_verdict(
                        cfg.enhancement_local_gpu_verdict
                    ),
                    device_index_provider=lambda: (
                        cfg.enhancement_local_gpu_device_index
                    ),
                    model_id_provider=lambda: (
                        self._app.config.enhancement_local_model
                    ),
                    acceleration=runtime_acceleration_for_platform(),
                )
                self._app.llama_server = supervisor
                self._logger.info("[Speech] local-floor supervisor prepared")

            loop = self._loop
            if loop is not None:
                supervisor.kick_warm(loop)
            supervisor.mark_active()
        except Exception as exc:  # noqa: BLE001 -- never-raise warm-kick helper
            self._logger.warning(
                "[Speech] warm kick error reason=%s", type(exc).__name__
            )

    def run(self) -> None:
        """Start the FastAPI server (blocks). WhisperstreamApp runs in a background thread."""
        import uvicorn

        # Wire up event broadcasting BEFORE kicking off model load so
        # model.loading / model.loaded events reach connected clients.
        self._app.set_event_broadcaster(self.broadcast)

        # If the engine deferred model loading (or had no model on disk), kick
        # off the load in the background. The server begins listening below;
        # the frontend renders its UI immediately and reacts to model.loading /
        # model.loaded / model.load_failed WebSocket events.
        if self._app.stt_engine is None and not self._app.file_only:
            self._app.load_stt_engine_async()

        # Capture the event loop for broadcast()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # Hand the loop to the engine so the recording-start hook can schedule
        # llama-server lazy-warm coroutines from the hotkey-handler thread.
        self._app.set_server_loop(loop)


        # Start WhisperstreamApp engine in a background thread
        engine_thread = threading.Thread(
            target=self._run_engine, daemon=True, name="WhisperstreamEngine"
        )
        engine_thread.start()

        self._logger.info("server starting port=%d", self._port)

        # Mint the per-run capability token that every managed-session route
        # will require (Task 2.1). Rewritten on every start so a stale token
        # from a previous run never authorizes anything. Best-effort: a write
        # failure must not prevent the server from starting, matching the
        # port-file pattern immediately below. The token value itself is
        # never logged.
        try:
            managed_session_auth.mint(get_app_dir())
            self._logger.info("[ManagedSession] capability token minted")
        except OSError:
            self._logger.warning("[ManagedSession] capability token write failed")

        # Write the port-only discovery file so the Claude HUD hook can find the
        # backend when a non-default --port is used.  Best-effort: a write
        # failure must not prevent the server from starting.
        # Gated on the user's own opt-in (default off) rather than the build
        # flag, so an install that never connects the HUD writes no artifact.
        if (
            self._agent_provider_enabled("claude")
            and not self._diagnostics_locked()
        ):
            self._write_claude_discovery_file()
        if (
            self._agent_provider_enabled("codex")
            and not self._diagnostics_locked()
        ):
            self._write_codex_discovery_file()
        if self._diagnostics_locked():
            self._clear_agent_speak_state(reason="lockdown_effective")

        config = uvicorn.Config(
            self.fastapi,
            host="127.0.0.1",
            port=self._port,
            loop="asyncio",
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        def release_server_resources() -> None:
            self._imports.cancel()
            # Idempotent; the shutdown hook normally stopped it already.
            self._stop_screen_reader_monitor()
            self._loop = None
            self._uvicorn_server = None
            self._app.set_server_loop(None)
            try:
                # Drain finalization, persistence, and imports before the
                # store connection and its in-memory key are released.
                self._app.cleanup()
            finally:
                file_workflows = getattr(self, "_file_workflows", None)
                close_file_workflows = getattr(file_workflows, "close", None)
                if callable(close_file_workflows):
                    close_file_workflows()
                # The gate owns a second in-memory key reference while
                # unlocked; release it at the same shutdown boundary.
                self._lockdown_state.lock()

        run_server_loop(
            loop,
            server,
            lambda: self._serve_with_llama(server),
            logger=self._logger,
            owner_pid=getattr(self, "_owner_pid", None),
            before_loop_shutdown=(
                self._app.close_native_query_owner
                if sys.platform == "darwin" else None
            ),
            after_loop_shutdown=release_server_resources,
        )

    async def _serve_with_llama(self, server) -> None:
        """Wrap server.serve() with llama-server lifecycle management.

        Before serving: if the config calls for the local provider and the GGUF
        is already downloaded, create the supervisor INSTANCE (no subprocess
        spawn — that's lazy, deferred to the first recording).
        After serving (shutdown): stop the llama-server child process if one
        was lazily spawned.
        """
        self._maybe_prepare_llama_server()
        try:
            await server.serve()
        finally:
            if sys.platform == "darwin":
                self._app.close_native_query_owner()
            self._revoke_bakeoffs("shutdown")
            self._clear_agent_speak_state(reason="shutdown")
            self._close_agent_integration_adapters()
            self._remove_codex_discovery_file()
            self._maybe_stop_speech_controller()
            await self._maybe_stop_resident_llama_pool()
            await self._maybe_stop_llama_server()
            self._maybe_clear_managed_session_token()

    def _maybe_clear_managed_session_token(self) -> None:
        """Best-effort: invalidate and delete the capability token on clean shutdown.

        A crash or hard kill skips this (no atexit/signal trickery here by
        design -- Task 1.3/3.4 already establish that graceful-only teardown
        is the rule for this feature), so the next start's mint-and-rewrite is
        what actually guarantees a stale token never authorizes anything.
        """
        try:
            managed_session_auth.clear(get_app_dir())
        except Exception:  # noqa: BLE001 -- teardown must never raise
            self._logger.warning("[ManagedSession] capability token clear failed")

    def _maybe_stop_speech_controller(self) -> None:
        """Bounded, best-effort teardown for speech workers and model sessions."""

        controller = getattr(self._app, "speech_controller", None)
        if controller is None:
            return
        try:
            if not controller.shutdown(timeout=2.0):
                self._logger.warning("[Speech] shutdown result=timeout")
        except Exception:  # noqa: BLE001 -- process teardown is best-effort
            self._logger.warning("[Speech] shutdown result=error")

    def _resident_llama_pool(self):
        """Return the configured pool, ignoring loose legacy/mock attributes."""

        if "resident_llama_pool" not in vars(self._app):
            return None
        pool = getattr(self._app, "resident_llama_pool", None)
        # Production assigns None or ResidentLlamaPool. The callable check
        # keeps older/mocked app surfaces backward compatible without treating
        # an auto-created MagicMock attribute as an active resident ledger.
        return pool if callable(getattr(pool, "has_resident", None)) else None

    def _has_resident_models(self, model_path: Path | None = None) -> bool:
        pool = self._resident_llama_pool()
        if pool is None:
            return False
        try:
            return bool(pool.has_resident(model_path))
        except (OSError, TypeError, ValueError):
            return False

    def _ensure_resident_llama_pool(self, binary_path: Path):
        """Create the process-lifetime pool without loading any model."""

        pool = self._resident_llama_pool()
        if pool is not None:
            return pool
        from ..resident_llama_pool import ResidentLlamaPool

        pool = ResidentLlamaPool(
            binary_path=binary_path,
            logger=self._logger,
        )
        self._app.resident_llama_pool = pool
        return pool

    def _prepare_bakeoff_resident_pool(self):
        """Return a lazy local pool or one stable, content-free API error."""

        pool = self._resident_llama_pool()
        if pool is not None:
            return pool

        from ..llama_server import find_bundled_llama_server

        binary_path = find_bundled_llama_server()
        if binary_path is None:
            return JSONResponse(
                status_code=409,
                content={"error": "llama_server_unavailable"},
            )
        return self._ensure_resident_llama_pool(binary_path)

    def _new_production_llama_supervisor(
        self,
        gguf_path: Path,
        *,
        binary_path: Path,
    ):
        """Build one lazy, config-backed supervisor for normal enhancement."""

        from ..gpu_benchmark import ngl_for_verdict
        from ..llama_server import LlamaServerSupervisor, runtime_acceleration_for_platform

        cfg = self._app.config
        return LlamaServerSupervisor(
            gguf_path,
            binary_path=binary_path,
            acceleration=runtime_acceleration_for_platform(),
            logger=self._logger,
            idle_timeout_seconds=cfg.enhancement_local_idle_timeout,
            on_state_change=self.broadcast,
            n_gpu_layers_provider=lambda: ngl_for_verdict(
                get_local_gpu_verdict(
                    self._app.config,
                    self._app.config.enhancement_local_model,
                )
            ),
            device_index_provider=lambda: (
                self._app.config.enhancement_local_gpu_device_index
            ),
            model_id_provider=lambda: (
                self._app.config.enhancement_local_model
            ),
        )

    async def acquire_resident_llama(
        self,
        *,
        model_id: str,
        model_path: Path,
        catalog_ram_mb: int | float,
    ):
        """Acquire one resident model without colliding with Fixture Test.

        This is the lifecycle seam for later Experimental orchestration. It is
        intentionally not an HTTP endpoint and does not read or mutate config.
        """

        from ..llama_server import find_bundled_llama_server

        binary_path = find_bundled_llama_server()
        if binary_path is None:
            raise ResidentLlamaConflictError("llama_server_unavailable")
        pool = self._ensure_resident_llama_pool(binary_path)
        return await pool.acquire(
            model_id=model_id,
            model_path=model_path,
            catalog_ram_mb=catalog_ram_mb,
            production_supervisor=getattr(self._app, "llama_server", None),
        )

    def _request_local_warm(self, _cfg) -> bool:
        """Schedule prepare/repoint + warm for a qualifying config transition."""
        loop = self._loop or getattr(self._app, "_server_loop", None)
        if loop is None:
            self._logger.info(
                "[LlamaServer] warm request skipped reason=server_loop_not_set"
            )
            return False
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self._ensure_selected_llama_server(warm_if_enabled=True)
                )
            )
        except RuntimeError:
            self._logger.info(
                "[LlamaServer] warm request skipped reason=server_loop_closed"
            )
            return False
        return True

    async def _ensure_selected_llama_server(
        self,
        *,
        prepare_when_inactive: bool = False,
        warm_if_enabled: bool = False,
    ) -> None:
        """Ensure the supervisor targets the selected GGUF, then optionally warm.

        Construction stays centralized in ``_maybe_prepare_llama_server``.
        This async wrapper adds the repoint-before-warm invariant needed by
        config transitions and is also the post-download refresh path.
        """
        from ..enhancement_models import get_local_model, resolve_local_model_path

        cfg = self._app.config
        if not prepare_when_inactive and cfg.effective_provider() != "local":
            return
        # An endpoint override is served externally. Never repoint or warm the
        # bundled supervisor in that mode, including when a dormant supervisor
        # remains from an earlier bundled-model selection.
        if cfg.enhancement_local_endpoint.strip():
            self._logger.info(
                "[LlamaServer] ensure skipped reason=endpoint_override"
            )
            return

        catalog_entry = get_local_model(cfg.enhancement_local_model)
        if catalog_entry is None:
            self._logger.warning(
                "[LlamaServer] ensure skipped reason=unknown_model_id model=%s",
                cfg.enhancement_local_model,
            )
            return
        gguf_path = resolve_local_model_path(catalog_entry)
        if (
            gguf_path is None
            or not gguf_path.exists()
            or gguf_path.stat().st_size == 0
        ):
            self._logger.info(
                "[LlamaServer] ensure skipped reason=gguf_not_installed model=%s",
                cfg.enhancement_local_model,
            )
            return

        pool = self._resident_llama_pool()
        resident = pool.find_resident(gguf_path) if pool is not None else None
        supervisor = getattr(self._app, "llama_server", None)

        if resident is not None:
            promoted = resident.supervisor
            if supervisor is not None and supervisor is not promoted:
                # An old production supervisor that was itself borrowed by a
                # resident lineup must remain alive until app exit. Ordinary
                # unpinned production behavior still stops on model switch.
                if getattr(supervisor, "is_residency_pinned", False) is not True:
                    try:
                        await supervisor.stop()
                    except Exception as exc:
                        self._logger.warning(
                            "[LlamaServer] old supervisor stop failed reason=%s",
                            type(exc).__name__,
                        )
            supervisor = promoted
            self._app.llama_server = supervisor
            supervisor.set_idle_timeout(cfg.enhancement_local_idle_timeout)
            self._logger.info(
                "[LlamaServer] promoted resident model=%s",
                cfg.enhancement_local_model,
            )
        elif supervisor is None:
            self._maybe_prepare_llama_server(
                require_local_provider=not prepare_when_inactive
            )
            supervisor = getattr(self._app, "llama_server", None)
            if supervisor is None:
                return
        else:
            current_path = getattr(supervisor, "_gguf_path", None)
            try:
                same_model = (
                    current_path is not None
                    and Path(current_path).resolve() == gguf_path.resolve()
                )
            except (OSError, TypeError, ValueError):
                same_model = current_path == gguf_path
            if not same_model:
                if getattr(supervisor, "is_residency_pinned", False) is True:
                    from ..llama_server import find_bundled_llama_server

                    binary_path = find_bundled_llama_server()
                    if binary_path is None:
                        self._logger.error(
                            "[LlamaServer] supervisor create failed reason=binary_not_found"
                        )
                        return
                    self._ensure_resident_llama_pool(binary_path)
                    try:
                        supervisor = self._new_production_llama_supervisor(
                            gguf_path,
                            binary_path=binary_path,
                        )
                    except Exception as exc:
                        self._logger.error(
                            "[LlamaServer] supervisor create failed reason=%s",
                            type(exc).__name__,
                        )
                        return
                    self._app.llama_server = supervisor
                    self._logger.info(
                        "[LlamaServer] prepared production alias model=%s",
                        cfg.enhancement_local_model,
                    )
                else:
                    try:
                        await supervisor.restart_for_model(gguf_path)
                    except Exception as exc:
                        self._logger.error(
                            "[LlamaServer] supervisor repoint failed reason=%s",
                            type(exc).__name__,
                        )
                        return
                    self._logger.info(
                        "[LlamaServer] supervisor repointed model=%s",
                        cfg.enhancement_local_model,
                    )

        current_cfg = self._app.config
        if (
            warm_if_enabled
            and current_cfg.enhancement_enabled
            # reload_config checks acceptance before scheduling this async
            # work. Recheck at execution time so a revocation in between cannot
            # start local inference after the content-processing grant ended.
            and self._app._acceptance_is_current()
            and self._app._can_warm_local(current_cfg)
        ):
            loop = self._loop or getattr(self._app, "_server_loop", None)
            if loop is None:
                return
            try:
                supervisor.kick_warm(loop)
            except Exception as exc:
                self._logger.warning(
                    "[LlamaServer] kick_warm failed reason=%s",
                    type(exc).__name__,
                )

    def _maybe_prepare_llama_server(
        self, *, require_local_provider: bool = True
    ) -> None:
        """Create the LlamaServerSupervisor INSTANCE if local provider is configured.

        Does not spawn the subprocess — the supervisor stays dormant until
        ``kick_warm()`` is called by the recording-start hook. This keeps
        Whisperstream's RAM footprint at idle to just the Parakeet model,
        and lets the user keep the local provider configured without paying
        the ~1.6 GiB resident cost until they actually dictate.
        """
        from ..enhancement_models import get_local_model, resolve_local_model_path
        from ..llama_server import (
            LlamaServerSupervisor,
            find_bundled_llama_server,
            runtime_acceleration_for_platform,
        )

        cfg = self._app.config
        # Use effective_provider() so that under lockdown (where it returns
        # "local") the llama-server is prepared even when the stored provider
        # is cloud — on-device dispatch requires the local server (Task 4.6).
        if require_local_provider and cfg.effective_provider() != "local":
            return

        catalog_entry = get_local_model(cfg.enhancement_local_model)
        if catalog_entry is None:
            self._logger.warning(
                "[LlamaServer] skipping prepare reason=unknown_model_id model=%s",
                cfg.enhancement_local_model,
            )
            return

        # A loopback override owns its server lifecycle. Non-loopback Local
        # overrides are rejected at validation and dispatch in every mode.
        from ..config_manager import is_loopback_endpoint  # noqa: PLC0415

        _endpoint_override = cfg.enhancement_local_endpoint.strip()
        _honor_override = bool(_endpoint_override) and is_loopback_endpoint(_endpoint_override)
        if _honor_override:
            self._logger.info(
                "[LlamaServer] skipping prepare reason=endpoint_override",
            )
            return

        gguf_path = resolve_local_model_path(catalog_entry)
        if (
            gguf_path is None
            or not gguf_path.exists()
            or gguf_path.stat().st_size == 0
        ):
            self._logger.info(
                "[LlamaServer] skipping prepare reason=gguf_not_installed model=%s",
                cfg.enhancement_local_model,
            )
            return

        binary_path = find_bundled_llama_server()
        if binary_path is None:
            self._logger.warning(
                "[LlamaServer] skipping prepare reason=binary_not_found"
                "; install llama-server or stage it under src-tauri/binaries/"
            )
            return

        # Launch-time GPU validation (round 02): if the active local model holds
        # a cached runnable verdict ("viable" or "marginal"), confirm the GPU
        # set hasn't changed since the benchmark. A changed set (card added /
        # removed / swapped) invalidates the verdict AND disables enhancement
        # (toggle off → no-CPU invariant); the frontend then auto-re-benches with
        # progress. An unchanged set just refreshes the pinned device index if
        # enumeration order shifted. Best-effort: any enumeration failure leaves
        # the cached verdict intact (fail safe).
        if get_local_gpu_verdict(cfg, cfg.enhancement_local_model) in ("viable", "marginal"):
            try:
                from ..gpu_benchmark import (
                    available_acceleration_devices,
                    gpu_fingerprint,
                    list_devices_stdout,
                )

                current = available_acceleration_devices(
                    list_devices_stdout(binary_path)
                )
                current_fp = gpu_fingerprint(current)
                match = next(
                    (d for d in current
                     if d.name == cfg.enhancement_local_gpu_device),
                    None,
                )
                if current_fp != cfg.enhancement_local_gpu_fingerprint or match is None:
                    self._logger.info(
                        "[Benchmark] gpu_set_changed action=invalidate+disable"
                    )
                    # A GPU-set change invalidates EVERY model's verdict, not
                    # just the active model's -- clear the whole per-model map.
                    cfg.enhancement_local_gpu_verdicts = {}
                    cfg.enhancement_local_gpu_devices = {}
                    cfg.enhancement_local_gpu_device_names = {}
                    cfg.enhancement_local_gpu_device = ""
                    cfg.enhancement_local_gpu_device_index = -1
                    cfg.enhancement_local_gpu_fingerprint = current_fp
                    cfg.enhancement_enabled = False
                    self._app.config_manager.save(cfg)
                    self._app.reload_config(cfg)
                    cfg = self._app.config
                    self._benchmark.recheck_pending = True
                elif (
                    match.index != cfg.enhancement_local_gpu_device_index
                    or any(
                        cfg.enhancement_local_gpu_devices.get(model_id) != device.index
                        for model_id, name in cfg.enhancement_local_gpu_device_names.items()
                        for device in current if device.name == name
                    )
                ):
                    self._logger.info(
                        "[Benchmark] device_index_refreshed old=%d new=%d",
                        cfg.enhancement_local_gpu_device_index, match.index,
                    )
                    cfg.enhancement_local_gpu_device_index = match.index
                    for model_id, name in cfg.enhancement_local_gpu_device_names.items():
                        device = next((d for d in current if d.name == name), None)
                        if device is not None:
                            cfg.enhancement_local_gpu_devices[model_id] = device.index
                    self._app.config_manager.save(cfg)
                    self._app.reload_config(cfg)
                    cfg = self._app.config
            except Exception as exc:
                self._logger.error(
                    "[Benchmark] launch GPU validation failed error_type=%s "
                    "action=keep_cached_verdict",
                    type(exc).__name__,
                )

        pool = self._ensure_resident_llama_pool(binary_path)
        resident = pool.find_resident(gguf_path)
        if resident is not None:
            supervisor = resident.supervisor
            supervisor.set_idle_timeout(cfg.enhancement_local_idle_timeout)
        else:
            supervisor = self._new_production_llama_supervisor(
                gguf_path,
                binary_path=binary_path,
            )
        self._app.llama_server = supervisor
        self._logger.info(
            "[LlamaServer] prepared lazy mode model=%s idle_timeout_s=%.0f",
            cfg.enhancement_local_model,
            cfg.enhancement_local_idle_timeout,
        )

    async def _restart_local_llm_for_model(
        self, artifact_path: Path, *, catalog_id: str | None = None
    ) -> None:
        """Use the contract adapter when the wrapped app really provides it.

        Several endpoint tests intentionally wrap a lightweight ``MagicMock``
        app. Attribute lookup on that mock fabricates callable child mocks, so
        ``callable()`` or a bare ``getattr()`` cannot distinguish a real async
        app method from an absent one. Fall back to the legacy supervisor seam
        unless the resolved attribute is genuinely async.
        """

        restart_runtime = getattr(
            self._app, "restart_local_llm_for_model", None
        )
        if inspect.iscoroutinefunction(restart_runtime):
            await restart_runtime(artifact_path, catalog_id=catalog_id)
            return
        supervisor = getattr(self._app, "llama_server", None)
        if supervisor is None:
            raise RuntimeError("local llama-server unavailable")
        await supervisor.restart_for_model(artifact_path)

    async def _maybe_stop_resident_llama_pool(self) -> None:
        """Stop every resident while preserving supervisor ownership rules."""

        pool = self._resident_llama_pool()
        if pool is None:
            return
        alias = getattr(self._app, "llama_server", None)
        alias_owned = pool.owns_supervisor(alias)
        borrowed = pool.borrowed_supervisors()
        if borrowed:
            results = await asyncio.gather(
                *(supervisor.stop() for supervisor in borrowed),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    self._logger.warning(
                        "[ResidentLlamaPool] borrowed shutdown failed reason=%s",
                        type(result).__name__,
                    )
        try:
            await pool.stop_all()
        finally:
            if alias_owned or any(
                supervisor is alias for supervisor in borrowed
            ):
                self._app.llama_server = None
            self._app.resident_llama_pool = None

    async def _maybe_stop_llama_server(self) -> None:
        """Stop the llama-server supervisor if one is running."""
        supervisor = getattr(self._app, "llama_server", None)
        if supervisor is None:
            return
        runtime_supervisor = getattr(
            self._app, "_local_llm_runtime_supervisor", None
        )
        runtime_closed = False
        try:
            close_runtime = getattr(self._app, "close_local_llm_runtime", None)
            runtime_closed = (
                await close_runtime()
                if inspect.iscoroutinefunction(close_runtime)
                else False
            )
        except Exception as exc:
            self._logger.warning(
                "[LlamaServer] runtime close failed reason=%s", type(exc).__name__
            )
        try:
            # A model switch can replace the production alias before inference
            # refreshes the cached adapter. Closing that older adapter does not
            # discharge ownership of the newly warmed supervisor.
            if not runtime_closed or runtime_supervisor is not supervisor:
                await supervisor.stop()
        except Exception as exc:
            self._logger.warning(
                "[LlamaServer] stop failed reason=%s", type(exc).__name__
            )
        finally:
            self._app.llama_server = None

    def _run_engine(self) -> None:
        """Run the WhisperstreamApp push-to-talk loop in a background thread."""
        try:
            self._app.run(register_hotkeys=self._register_hotkeys)
        except Exception as exc:
            self._logger.error("engine crashed error_type=%s", type(exc).__name__)
