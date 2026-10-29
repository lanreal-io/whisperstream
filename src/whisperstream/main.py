from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import logging
import platform
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Optional, Sequence

import numpy as np

from rich.logging import RichHandler

from .access_log import log_access
from .audio_policy import PlatformRecordingAudioPolicyFactory
from .audio_policy_contract import (
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyTransaction,
    ChimeKind,
)
from .app_context import (
    UnavailableApplicationContextAdapter,
    create_application_context_adapter,
)
from .app_context_contract import (
    ContextRequirement,
    ContextSnapshot,
    ProfileRouteTier,
)
from .chunking import (
    CHUNK_DURATION_SECONDS,
    CHUNK_OVERLAP_SECONDS,
    merge_overlapping_transcripts,
)
from .cleanup_chunker import (
    PRODUCTION_FIRST_TARGET_WORDS as _CHUNK_FIRST_TARGET_WORDS,
    PRODUCTION_MIN_TAIL_WORDS as _CHUNK_MIN_TAIL_WORDS,
    PRODUCTION_TARGET_WORDS as _CHUNK_TARGET_WORDS,
    production_cleanup_chunks,
)
from .capture_contract import (
    CaptureCompletionStatus,
    CaptureFailure,
    CaptureSession,
    CaptureTerminalEvent,
)
from .email_signature import ends_with_signoff
from .enhancement_outcomes import (
    EnhancementOutcome, EnhancementResult, EnhancementProfileSnapshot,
    TranscriptEnhancementProvenance, aggregate_enhancement_outcomes,
    enhancement_provenance,
)
from .config_manager import (
    WhisperstreamConfig,
    ConfigManager,
    apply_managed_lockdown,
    get_app_dir,
    get_log_dir,
)
from .error_dialog_copy import COPY
from .enhancement_bakeoff import (
    BakeoffBusyError,
    BakeoffPairSelection,
    EnhancementBakeoffManager,
    EvaluationKind,
    EvaluationLease,
    PairExecutor,
    ProductionBakeoffPairExecutor,
)
from .eula import (
    AcceptanceGrant,
    AcceptanceGrantReason,
    acceptance_grant_is_current,
    get_acceptance_grant,
)
from .power_mode import (
    resolve_enhancement_route,
    resolve_profile_for_target,
    routing_probe_requirements,
    routing_target_application_id,
)
from .logger import get_logger, log_transcription_event, reconfigure_logging
from .model_downloader import ModelId, model_dir, model_id_for_parakeet
from .parakeet_manager import ParakeetManager
from .stt_contracts import SttEngine, SttPreviewMetadata
from .stt_errors import ModelNotPreparedError, SttFailure
from .power_mode import resolve_profile_for_target, routing_probe_requirements
from .llm_enhancer import (
    ENHANCEMENT_PROVIDERS,
    ErrorObserver,
    EnhancementProvider,
    LLMEnhancer,
    TimeoutObserver,
)
from .managed_policy import (
    ManagedLockdownPolicy, read_managed_lockdown_policy, history_disabled_by_policy,
)
from .safe_diagnostics import SafeDiagnosticSink
from ._version import __version__ as _SOFTWARE_VERSION
from .stt_factory import make_stt_manager, _resolve_backend, _selection_from_config
from .license_manager import LicenseManager
from .live_preview import (
    LivePreviewCoordinator,
    MAX_PREVIEW_WINDOW_SECONDS,
    PreviewUpdate,
)
from .review_state import ReviewState
from .text_injector import DeliveryOutcome, DeliveryStatus, TextInjector
from .temp_lifecycle import sweep_stale_owned_temp_files
from .transcript_store import (
    TranscriptStore,
    history_write_available,
    run_retention_sweep,
)
from .waveform_peaks import compute_peaks, wav_bytes_from_float32
from . import _build_flags, lockdown_keystore

if TYPE_CHECKING:
    from .foreground_window import RoutingTarget
# Native context and pin modules are imported only by their platform selectors.


def KeyboardShortcutManager(*args, **kwargs):
    """Construct the platform keyboard surface without eager native imports."""

    from .shortcuts import KeyboardShortcutManager as manager_class

    return manager_class(*args, **kwargs)


def AudioCapture(*args, **kwargs):
    """Construct microphone capture lazily for interactive app modes only."""

    from .capture import AudioCapture as capture_class

    return capture_class(*args, **kwargs)


def AudioFeedback(*args, **kwargs):
    """Construct chime playback lazily for interactive app modes only."""

    from .audio_feedback import AudioFeedback as feedback_class

    return feedback_class(*args, **kwargs)


def has_input_device() -> bool:
    """Probe microphone availability only when recording is requested."""

    from .capture import has_input_device as probe

    return probe()


def is_known_browser(process_name: str) -> bool:
    """Resolve browser identity only for interactive app-target routing."""

    from .browser_domain import is_known_browser as probe

    return probe(process_name)


def detect_foreground_routing_target(
    *, include_browser_domain: bool = False
) -> RoutingTarget | None:
    """Inspect the active app only when profile routing needs it."""

    from .foreground_window import detect_foreground_routing_target as probe

    return probe(include_browser_domain=include_browser_domain)


def _experimental_enabled() -> bool:
    """Runtime half of the agent-speech experimental gate.

    Mirrors ``server._experimental_enabled`` (strict-"1" semantics on
    ``WHISPERSTREAM_EXPERIMENTAL``). Defined locally so main.py never imports the
    heavy server module just to read one env var, and never risks the
    server<->main import cycle. The compile-time half is
    ``_build_flags.EXPERIMENTAL_AVAILABLE``; gated code checks
    ``_build_flags.EXPERIMENTAL_AVAILABLE and _experimental_enabled()``.
    """
    import os  # noqa: PLC0415 — module-local import per this file's os convention

    return os.environ.get("WHISPERSTREAM_EXPERIMENTAL", "").strip() == "1"


# Compatibility alias for tests and callers that need the default activation
# point. The live pipeline reads enhancement_words_per_chunk from config.
_CHUNK_THRESHOLD_WORDS = _CHUNK_TARGET_WORDS

_DICTATION_STATE_VERSION = 1
_DICTATION_PHASES = frozenset({"recording", "processing", "idle"})
_DICTATION_ORIGINS = frozenset({"push_to_talk", "toggle", "hud"})
_USE_CURRENT_ROUTING = object()
_EULA_BLOCK_CODE = "eula_acceptance_required"
_STT_ROUTING_FIELDS = (
    "language",
    "stt_model_selection_mode",
    "stt_backend",
    "parakeet_model",
    "qwen_force_language",
)


class SttActivationStaleError(RuntimeError):
    """A newer routing choice superseded a manager under construction."""


class SttActivationBusyError(RuntimeError):
    """Raised when a live dictation makes an STT manager swap unsafe."""


@dataclass(frozen=True, slots=True)
class SttRoutingState:
    """The complete persisted state needed to roll an STT switch back."""

    language: str | None
    stt_model_selection_mode: str
    stt_backend: str
    parakeet_model: str
    qwen_force_language: bool


def _stt_routing_state(config: WhisperstreamConfig) -> SttRoutingState:
    return SttRoutingState(
        language=config.language,
        stt_model_selection_mode=config.stt_model_selection_mode,
        stt_backend=config.stt_backend,
        parakeet_model=config.parakeet_model,
        qwen_force_language=config.qwen_force_language,
    )


def _stt_artifact_id(config: WhisperstreamConfig) -> ModelId:
    from .stt_factory import selected_model_id
    return selected_model_id(config)


async def run_transcript_evaluation(
    enhancer: LLMEnhancer,
    text: str,
    *,
    mode: str,
    profile_id: str | None,
    target_words: int = _CHUNK_TARGET_WORDS,
    limit_first_chunk: bool = True,
) -> tuple[str, int]:
    """Run one saved-transcript evaluation without app pipeline side effects.

    ``whole`` uses the same production ``enhance()`` entry point as a normal
    short/single-call dictation. ``batched`` consumes the exact production
    sentence plan and awaits ``enhance_chunk()`` in order. The returned text is
    post-model and post-guard only: no email signature, text-injector
    transforms, clipboard work, persistence, broadcast, or usage accounting.
    """
    # ============================================================================
    # TRANSCRIPT DATA â€” DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    if mode == "whole":
        return await enhancer.enhance(text, profile_id=profile_id), 1
    if mode != "batched":
        raise ValueError("mode must be whole or batched")

    chunks = production_cleanup_chunks(
        text,
        target_words=target_words,
        limit_first_chunk=limit_first_chunk,
    )
    cleaned_chunks: list[str] = []
    for chunk in chunks:
        cleaned_chunks.append(
            await enhancer.enhance_chunk(chunk, profile_id=profile_id)
        )
    return " ".join(cleaned_chunks), len(chunks)


@dataclass(frozen=True, slots=True)
class _DictationFinalizationContext:
    """Stop-time ownership boundary for one authoritative dictation.

    The container itself is immutable. Futures and the recognizer remain live
    references by design, but the tuple/object identities are fixed before the
    finalizer is queued so a later recording or model/config reload cannot
    redirect this dictation's ASR, waveform, timing, or Power Mode inputs.
    """

    recording_id: int | None
    waveform: np.ndarray | None
    chunk_futures: tuple[concurrent.futures.Future, ...]
    transcription_started_at: float
    recording_started_at: float
    stt_manager: object | None
    dictation_app: ContextSnapshot | RoutingTarget | None
    manual_profile_id: str | None
    override_anchor: str | None
    screen_reader_detected: bool
    inject_suppressed: bool
    agent_compose_lease: object | None
    persistence_ready: threading.Event = field(default_factory=threading.Event)
    acceptance_grant: AcceptanceGrant | None = None


# Screen-reader executables we detect by process name (lowercased, no extension)
# as a fallback for readers that do NOT set the SPI_GETSCREENREADER system flag.
# Narrator (Windows' built-in reader) never sets it; NVDA started at the logon
# screen can also miss it (nvaccess/nvda#462). JAWS/NVDA normally DO set the flag,
# but matching their process too keeps detection robust if the flag is missed.
_SCREEN_READER_PROCESS_NAMES = frozenset({
    "narrator",  # Windows Narrator — does not set the SPI flag
    "nvda",      # NVDA
    "jfw",       # JAWS (Freedom Scientific) main reader
    "fsreader",  # JAWS-family reader
})


def _spi_screen_reader_flag() -> bool:
    """Windows SPI_GETSCREENREADER system flag (set by NVDA/JAWS, not Narrator).
    False on non-Windows or any error."""
    try:
        import ctypes
        import ctypes.wintypes
        result = ctypes.wintypes.BOOL(0)
        ctypes.windll.user32.SystemParametersInfoW(
            0x0046,  # SPI_GETSCREENREADER
            0,
            ctypes.byref(result),
            0,
        )
        return bool(result.value)
    except Exception:
        return False


def _screen_reader_process_running() -> bool:
    """True if a known screen-reader executable is running. Fallback for readers
    that don't set the SPI flag (notably Narrator). Never raises."""
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name") or ""
            if name.rsplit(".", 1)[0].lower() in _SCREEN_READER_PROCESS_NAMES:
                return True
    except Exception:
        return False
    return False


def screen_reader_active() -> bool:
    """Return True when a screen reader is running on this machine.

    Resolution order:
    1. WHISPERSTREAM_FORCE_SCREEN_READER env var ("1"/"true" → True;
       "0"/"false" → False) — used by dev/tests/screenshots to force a state.
    2. Windows SPI_GETSCREENREADER flag — set by NVDA/JAWS.
    3. Running-process fallback — for readers that DON'T set the flag (Narrator)
       or miss it (NVDA at the logon screen).
    4. Non-Windows: both probes return False.
    Never raises — any error returns False.
    """
    from .screen_reader_monitor import forced_screen_reader_state

    forced = forced_screen_reader_state()
    if forced is not None:
        return forced
    return _spi_screen_reader_flag() or _screen_reader_process_running()


def _reconcile_envelope_lockdown_config(
    config_manager: ConfigManager,
    config: WhisperstreamConfig,
    *,
    envelope_exists: bool,
    logger: logging.Logger,
) -> None:
    """Make an on-disk password envelope authoritative after an interrupted transition.

    A crash can occur after normal-mode config is saved but before the envelope
    is removed. Runtime state is repaired before any content-capable component
    is constructed; persistence is best-effort, while the in-memory no-egress
    state remains fail closed even if the config file cannot be rewritten.
    """
    if not envelope_exists or config.lockdown_mode:
        return
    config.lockdown_mode = True
    config.enhancement_provider = "local"
    config.enhancement_enabled = False
    try:
        config_manager.save(config)
    except Exception:  # noqa: BLE001 - runtime state remains authoritative
        logger.warning("lockdown startup config reconcile status=persist_failed")


class WhisperstreamApp:
    @property
    def file_only(self) -> bool:
        """Return whether OS interaction and model startup are disabled."""
        return bool(getattr(self, "_file_only", False))

    @property
    def stt_engine(self) -> SttEngine | None:
        """Return the active speech-to-text engine, independent of backend."""
        return getattr(self, "_stt_engine", None)

    @stt_engine.setter
    def stt_engine(self, value: SttEngine | None) -> None:
        self._stt_engine = value

    @property
    def parakeet(self) -> SttEngine | None:
        """Compatibility alias for older tests and tools; runtime uses stt_engine."""
        return self.stt_engine

    @parakeet.setter
    def parakeet(self, value: SttEngine | None) -> None:
        self.stt_engine = value

    def __init__(
        self,
        *,
        verbose: bool = False,
        headless: bool = False,
        defer_model_load: bool = False,
        file_only: bool = False,
        managed_policy: ManagedLockdownPolicy | None = None,
    ) -> None:
        level = logging.DEBUG if verbose else logging.INFO
        # Read machine policy before content-capable services are constructed.
        self.managed_lockdown_policy = (
            managed_policy
            if managed_policy is not None
            else read_managed_lockdown_policy()
        )
        # The envelope is an equally authoritative Lockdown marker. It may exist
        # while config.toml is still false after an interrupted transition.
        try:
            envelope_exists = lockdown_keystore.is_lockdown_enabled(get_app_dir())
        except OSError:
            envelope_exists = True
        self.logger = get_logger(level=level, log_dir=get_log_dir())
        # Retry ordinary cleanup of attributable crash residue on every launch,
        # even if this session never creates another favicon/upload temp file.
        # The helper ignores fresh files, symlinks, directories, and every path
        # outside Whisperstream's owned OS-temp namespace.
        try:
            sweep_stale_owned_temp_files()
        except Exception:  # noqa: BLE001 - temp cleanup cannot block startup
            pass
        self.config_manager = ConfigManager()
        self.config = self.config_manager.load(
            managed_lockdown=self.managed_lockdown_policy.enabled,
            managed_history_disabled=self.managed_lockdown_policy.history_disabled,
            lockdown_envelope_exists=envelope_exists,
        )
        _reconcile_envelope_lockdown_config(
            self.config_manager,
            self.config,
            envelope_exists=envelope_exists,
            logger=self.logger,
        )
        # Before history can load a key: finish or roll back a Lockdown
        # transition a crash interrupted, and remove orphaned key copies.
        lockdown_keystore.repair_interrupted_transition(
            get_app_dir(), store=self.config_manager.protected_secret_store
        )
        self.safe_diagnostics = SafeDiagnosticSink(get_log_dir())
        if (
            self.managed_lockdown_policy.enabled
            or self.config.lockdown_mode
            or envelope_exists
        ):
            self.safe_diagnostics.emit(
                "app_started",
                app_version=_SOFTWARE_VERSION,
                managed=self.managed_lockdown_policy.enabled,
                policy_state=self.managed_lockdown_policy.state,
                history_enabled=bool(self.config.history_enabled),
            )
        # Construct the content authority before the microphone, recognizer,
        # enhancer, injector, or transcript store can be used. The provider and
        # revalidator remain injectable for focused tests. Production validates
        # policy/evidence once at startup, then uses an immutable session grant.
        self._acceptance_grant_provider = get_acceptance_grant
        self._acceptance_grant_revalidator = acceptance_grant_is_current
        initial_acceptance_grant = self._current_acceptance_grant()
        self._recording_acceptance_grant: AcceptanceGrant | None = None
        self._acceptance_revoke_lock = threading.Lock()
        self._acceptance_revoke_scheduled = False
        self._transcript_store_lock = threading.Lock()
        self._transcript_store_initialized = False
        self._transcript_store_start_lock = threading.Lock()
        self._transcript_store_publish_lock = threading.Lock()
        self._transcript_store_init_thread: threading.Thread | None = None
        # Configured routing can move ahead of the loaded recognizer while an
        # artifact is being prepared. Keep both identities explicit so status,
        # rollback, and later deletion guards never infer runtime ownership
        # from the target config.
        self._loaded_stt_artifact_id: ModelId | None = None
        self._loaded_stt_routing_state: SttRoutingState | None = None
        self._stt_activation_rollback: SttRoutingState | None = None
        # One process-wide guard serializes startup construction, live model
        # activation, deletion, and model-storage relocation. The server reuses
        # this exact lock instead of creating a second lifecycle domain.
        self._stt_model_operation_lock = threading.Lock()
        # Install the runtime ownership lock before a non-deferred startup load
        # so the blocking and background startup paths use the same atomic
        # installer as later model switches.
        self._lock = threading.Lock()
        self._model_dir = model_dir(model_id_for_parakeet(self.config.parakeet_model))
        self._headless = headless
        self._file_only = bool(file_only)
        self._file_only_stop = threading.Event()
        self._context_generation = 0
        self._context_generation_lock = threading.Lock()
        self.application_context = (
            UnavailableApplicationContextAdapter()
            if self._file_only
            else create_application_context_adapter()
        )
        self.logger.debug(
            "app environment platform=%s python=%s",
            platform.platform(),
            platform.python_version(),
        )
        self.logger.debug(
            "app config summary model=%s quantization=%s threads=%s",
            self.config.parakeet_model,
            self.config.parakeet_quantization or "none",
            self.config.threads,
        )

        # One-time input-style diagnostic: was a screen reader detected, has the
        # user explicitly pinned a style (dirty flag), and what global style do
        # those two resolve to. Per-dictation resolution (incl. per-profile
        # overrides) is traced at DEBUG in _effective_input_style; this headline
        # answers "why didn't it auto-switch to keystroke" at a glance.
        _sr_startup = screen_reader_active()
        _user_set_startup = bool(getattr(self.config, "input_style_user_set", False))
        _effective_global_startup = (
            self.config.input_style
            if _user_set_startup
            else ("keystroke" if _sr_startup else "clipboard")
        )
        self.logger.info(
            "[InputStyle] startup screen_reader=%d user_set=%s configured=%s effective_global=%s",
            int(_sr_startup),
            str(_user_set_startup).lower(),
            self.config.input_style,
            _effective_global_startup,
        )

        self.keyboard: Any | None = None
        self.audio_capture: Any | None = None
        self.audio_feedback: Any | None = None
        self.audio_policy: Any | None = None
        self._audio_capture_device = self.config.audio_input_device
        self._capture_terminal_lock = threading.Lock()
        self._capture_terminal_pending: CaptureTerminalEvent | None = None
        self._capture_terminal_scheduled = False
        if self._file_only:
            self.logger.info("app OS interaction surfaces disabled mode=file_only")
        else:
            self.keyboard = KeyboardShortcutManager(logger=self.logger)
            self.audio_capture = AudioCapture(
                device=self.config.audio_input_device,
                status_callback=self._log_capture_status,
                terminal_callback=self._enqueue_capture_terminal_event,
                chunk_callback=self._on_chunk_ready,
                chunk_duration=CHUNK_DURATION_SECONDS,
                chunk_overlap=CHUNK_OVERLAP_SECONDS,
            )
            self.audio_feedback = AudioFeedback(
                logger=self.logger,
                enabled=self.config.audio_feedback,
                volume=self.config.audio_feedback_volume,
            )
            self.audio_policy = PlatformRecordingAudioPolicyFactory(
                feedback=self.audio_feedback,
                logger=self.logger,
                start_path=lambda: self.config.start_sound_path,
                stop_path=lambda: self.config.stop_sound_path,
                mode=lambda: self.config.system_volume_mode,
            ).create()

        self._console = None
        self._shortcut_recovery = None
        if sys.platform == "darwin" and self.keyboard is not None:
            from .shortcut_recovery import ShortcutRecovery

            self._shortcut_recovery = ShortcutRecovery(
                self.keyboard, self._register_hotkey_bindings,
                lambda: bool(getattr(self, "_recording", False)
                             or getattr(self, "_shutting_down", False)),
            )
        for handler in self.logger.handlers:
            if isinstance(handler, RichHandler):
                self._console = handler.console
                break

        self.stt_engine = None
        self._last_stt_failure: SttFailure | None = None
        if not defer_model_load:
            self._load_stt_engine_blocking()

        # Agent Speak ships from 1.3.0, so its collaborators are always
        # constructed. It stays dark until the user opts in via its own config
        # field (claude_hud_enabled False by default), which is the only gate.
        self._experimental = not self._file_only
        self._shutting_down = False
        self._cleanup_lock = threading.Lock()
        self._cleanup_complete = False
        # Agent-speech speech controller. Starts None so the attribute always
        # exists and the PTT barge-in stop in _start_recording stays a cheap
        # None-check. whisperstream.speech is imported lazily below to keep it
        # off the startup path, not to keep it out of the binary (it ships).
        self.speech_controller = None
        if self._experimental:
            from .speech.controller import SpeechController  # noqa: PLC0415 — lazy: keeps the TTS stack off the startup path

            # Cheap to build (no model load, no audio stream). supervisor_provider
            # is a lambda so it reads self.llama_server lazily at speak time (it is
            # assigned below and repopulated by the server when local enhancement
            # is active).
            self.speech_controller = SpeechController(
                config_provider=lambda: self.config,
                broadcast=self._broadcast,
                logger=self.logger,
                supervisor_provider=lambda: self.llama_server,
                # Barge-in outranks everything: no template utterance may
                # start while a dictation recording is live (Phase 2).
                recording_provider=lambda: self._recording,
            )

        self.text_injector = TextInjector(
            keyboard_manager=self.keyboard,
            logger=self.logger,
            word_filters=list(self.config.word_filters),
            word_replacements=dict(self.config.word_replacements),
            post_processing=self.config.post_processing,
            clipboard_behavior=self.config.clipboard_behavior,
            clipboard_clear_delay=self.config.clipboard_clear_delay,
            input_style=self.config.input_style,
            keystroke_delay_ms=self.config.keystroke_delay_ms,
            content_allowed=self._acceptance_is_current,
        )

        # Llama-server supervisor. Populated by WhisperstreamServer.run() when
        # enhancement_provider == "local" and the GGUF is installed. None means
        # the local provider is unavailable; LLMEnhancer falls back to raw text.
        # The resident pool is created lazily by WhisperstreamServer once the
        # bundled llama-server binary is available. ``llama_server`` remains
        # the compatibility alias for whichever pooled or ordinary supervisor
        # currently owns the production enhancement role.
        self.resident_llama_pool = None
        self.llama_server = None
        # Generation-bound contract adapter. It is created lazily from the
        # current supervisor so every existing supervisor replacement path is
        # observed without exposing an OS process or artifact path upstream.
        self.local_llm_runtime = None
        self._local_llm_runtime_supervisor = None
        # WhisperstreamServer installs a requester that owns supervisor
        # construction/repointing. Keeping the transition decision here while
        # delegating the async lifecycle work to the server lets a config PUT
        # create the first supervisor after a cloud/disabled startup.
        self._local_warm_requester: Optional[
            Callable[[WhisperstreamConfig], bool]
        ] = None

        # Dev-only latency simulator hook (Task 1.7 / scope.md decision 8).
        # Defaults to a no-op returning None (prod / dev tools off).
        # WhisperstreamServer overwrites this attribute right after it wraps
        # this app -- server.py owns the actual in-memory `_simulated_tps`
        # value, seeded from WHISPERSTREAM_SIMULATED_TPS -- so the optional
        # dev-tools routes (Nuitka-excluded from the production binary) never need a hard
        # import from llm_enhancer.py. LLMEnhancer is given
        # `lambda: self._simulated_tps_provider()` (NOT the provider itself)
        # below and on every reload_config() reconstruction, so the
        # indirection survives config saves recreating the enhancer.
        self._simulated_tps_provider: Callable[[], Optional[float]] = lambda: None

        self.llm_enhancer = LLMEnhancer(
            self.config, self.logger,
            llama_server_provider=lambda: self.llama_server,
            local_runtime_provider=self._resolve_local_llm_runtime,
            simulated_tps_provider=lambda: self._simulated_tps_provider(),
        )

        self.license_manager = LicenseManager(logger=self.logger)
        self.review_state = ReviewState()

        # Encrypted-at-rest transcript history store. ONE instance, reached by
        # the mic-path persistence hooks here AND by the server routes via
        # `self._app.transcript_store`. Construction is guarded so a store-init
        # failure (e.g. DPAPI/SQLite error) NEVER blocks app startup: log a
        # metadata-only warning and leave the attribute None; callers must treat
        # a None store as "history unavailable" and no-op.
        self.transcript_store: Optional[TranscriptStore] = None
        if self._file_only:
            self.logger.info("app persistence surfaces disabled mode=file_only")
        # TranscriptStore construction creates/unwraps the encryption key and
        # opens SQLite. Keep all of that, including startup retention, behind an
        # exact grant. A later durable acceptance calls the same idempotent seam.
        elif initial_acceptance_grant.allowed and not (
            sys.platform == "darwin" and headless
        ):
            self.ensure_transcript_store_initialized(initial_acceptance_grant)

        self._recording = False
        self._dictation_app: ContextSnapshot | RoutingTarget | None = None
        # Per-profile manual-switch override (issue 14). When a profile hotkey is
        # pressed, set_manual_profile() records the profile id plus the foreground
        # app's process_name at press time (the "anchor"). The next dictation uses
        # the override iff it fires while the same app is still focused; once focus
        # moves to a different app the override clears and auto-routing resumes.
        # v1 mechanism = compared at recording-start (no live focus hook/poll).
        self._manual_profile_id: Optional[str] = None
        self._override_anchor: Optional[str] = None
        # True when the in-flight recording was started via the toggle hotkey.
        # Gates the toggle-safety-timeout timer (PTT recordings never use it).
        self._started_via_toggle = False
        # Authoritative activation surface for the in-flight recording. Kept
        # separate from _started_via_toggle because HUD capture uses toggle
        # safety semantics while retaining distinct outcome controls.
        self._recording_origin: str | None = None
        # True only while a real discard is closing capture. AudioCapture.stop()
        # flushes its tail through the chunk callback, which must not enqueue
        # transcription work for a cancelled recording.
        self._discarding_recording = False
        self._active_capture_session_key: tuple[int, str, int] | None = None
        # Bakeoff runs keep their own short, atomic evaluation lease. Recording
        # code checks it while already under _lock, but awaited model work never
        # retains _lock. The manager itself is memory-only and binds no
        # production evaluator or transport at construction time.
        self.evaluation_lease = EvaluationLease()
        self.enhancement_bakeoff = EnhancementBakeoffManager(
            lease=self.evaluation_lease
        )
        # Process-local dictation generation and latest authoritative lifecycle
        # snapshot. A separate lock is required because recording callbacks are
        # already invoked while self._lock is held. The snapshot is deliberately
        # content-free and safe to replay to reconnecting WebSocket clients.
        self._dictation_state_lock = threading.Lock()
        self._recording_sequence = 0
        self._dictation_state = {
            "type": "dictation.state",
            "version": _DICTATION_STATE_VERSION,
            "recording_id": 0,
            "phase": "idle",
            "origin": None,
            "hud_enabled": False,
            "preview_enabled": False,
            "screen_reader_detected": bool(_sr_startup),
        }
        self._stop_timer: Optional[threading.Timer] = None
        self._idle_timer: Optional[threading.Timer] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="Transcriber")
        self._persistence_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="TranscriptPersistence"
        )
        self._persistence_epoch_lock = threading.Lock()
        self._persistence_epoch = 0
        self._persistence_accepting = True
        # Imported files have their own single-flight queue. Decoding or
        # transcribing a user-selected batch must never delay live-dictation
        # persistence or retain an unbounded backlog of source paths/content.
        self._import_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="AudioImport"
        )
        self._live_preview_lock = threading.Lock()
        self._live_preview_coordinator: LivePreviewCoordinator | None = None
        self._broadcaster: Optional[callable] = None
        # asyncio loop the FastAPI server runs on. Set by WhisperstreamServer.
        # Used by the recording-start hook to schedule llama-server lazy warm
        # on the right loop (the hotkey handler runs on a pynput thread, not
        # the loop thread).
        self._server_loop = None
        self._local_stop_task: Optional[asyncio.Task] = None
        self._last_level_broadcast = 0.0
        self._audio_policy_generation = 0
        self._audio_policy_transaction: AudioPolicyTransaction | None = None
        self._inject_suppressed: bool = False
        # Experimental, process-local HUD compose routing. The lease
        # store is supplied lazily by WhisperstreamServer; the frozen recording
        # token prevents later cancel/reconnect generations from redirecting an
        # already queued finalizer.
        self._agent_compose_leases = None
        self._agent_compose_pending_generation: int | None = None
        self._agent_compose_recording = None
        self._agent_reply_router = None
        self._register_escape_handler()
        self._chunk_futures: list[concurrent.futures.Future] = []
        self._chunk_futures_lock = threading.Lock()
        self._chunk_start_time: float = 0.0
        self._last_waveform: Optional[np.ndarray] = None
        self._recording_start_time: float = 0.0

        # Power Mode routing readiness (privacy-safe: counts + a boolean, never a
        # URL/domain). uia_available=False means per-site routing cannot fire (the
        # browser address bar can't be read) and routing falls back to per-app
        # /default -- the first thing to check when site_map rules never trigger.
        self.logger.info(
            "[PowerMode] routing ready uia_available=%s site_rules=%d app_rules=%d default_profile_set=%s",
            (
                False
                if self._file_only
                else bool(self.application_context.uia_available())
            ),
            len(self.config.site_map),
            len(self.config.power_mode_map),
            bool(self.config.default_profile_id),
        )

    def start_enhancement_bakeoff(
        self,
        *,
        raw_text: str,
        selections: Sequence[BakeoffPairSelection],
        execute_pair: PairExecutor | None = None,
    ) -> str:
        """Atomically start one production-faithful Bakeoff while idle.

        The default executor snapshots the current enhancement configuration,
        binds every explicit local model to the resident pool, and runs the
        selected profile through ``run_transcript_evaluation``. A supplied
        executor remains a narrow test seam for the generic session lifecycle.
        This method never holds ``self._lock`` across awaited work.
        """
        # ========================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log raw_text, evaluator output, or a Bakeoff session snapshot.
        # ========================================================================
        with self._lock:
            state = self.dictation_state_snapshot()
            if self._recording or state.get("phase") != "idle":
                raise BakeoffBusyError("dictation is active")
            if execute_pair is None:
                execute_pair = ProductionBakeoffPairExecutor(
                    config=self.config,
                    selections=selections,
                    resident_pool=self.resident_llama_pool,
                    production_supervisor_provider=lambda: self.llama_server,
                    logger=self.logger,
                    simulated_tps_provider=lambda: self._simulated_tps_provider(),
                )
            return self.enhancement_bakeoff.start(
                raw_text=raw_text,
                selections=selections,
                execute_pair=execute_pair,
            )

    def revoke_enhancement_bakeoff(self) -> None:
        """Synchronously invalidate and scrub all Bakeoff session content."""

        manager = getattr(self, "enhancement_bakeoff", None)
        if manager is not None:
            manager.revoke_all()

    def _bakeoff_evaluation_active(self) -> bool:
        lease = getattr(self, "evaluation_lease", None)
        return bool(
            lease is not None and lease.is_active(EvaluationKind.BAKEOFF)
        )

    def _recording_can_start(self) -> bool:
        """Cheap recording-thread check for an active Bakeoff evaluation."""

        return not self._bakeoff_evaluation_active()

    @contextmanager
    def _normal_evaluation_scope(self) -> Iterator[bool]:
        """Try to own ordinary enhancement without blocking a Bakeoff run."""

        lease = getattr(self, "evaluation_lease", None)
        if lease is None:
            # Preserve lightweight __new__-based test/tool callers predating
            # the app-owned lease.
            yield True
            return
        token = lease.try_acquire(EvaluationKind.NORMAL)
        try:
            yield token is not None
        finally:
            if token is not None:
                lease.release(token)

    def _current_acceptance_grant(self) -> AcceptanceGrant:
        """Read startup admission from memory, failing closed on authority errors."""
        provider = getattr(
            self, "_acceptance_grant_provider", get_acceptance_grant
        )
        try:
            grant = provider()
            if isinstance(grant, AcceptanceGrant):
                return grant
        except Exception:  # noqa: BLE001 - content authority fails closed
            pass
        return AcceptanceGrant(
            False, AcceptanceGrantReason.ACCEPTANCE_UNAVAILABLE
        )

    def _acceptance_is_current(
        self, grant: AcceptanceGrant | None = None
    ) -> bool:
        """Return whether content may proceed now for this exact grant."""
        if grant is None:
            return self._current_acceptance_grant().allowed
        if not grant.allowed:
            return False
        revalidator = getattr(
            self,
            "_acceptance_grant_revalidator",
            acceptance_grant_is_current,
        )
        try:
            return bool(revalidator(grant))
        except Exception:  # noqa: BLE001 - content authority fails closed
            return False

    def _broadcast_acceptance_blocked(self) -> None:
        """Emit the one stable, content-free denial event."""
        self.logger.info("content processing blocked reason=%s", _EULA_BLOCK_CODE)
        self._broadcast(
            {"type": "recording.blocked", "reason": _EULA_BLOCK_CODE}
        )

    def _require_acceptance(
        self,
        grant: AcceptanceGrant | None = None,
        *,
        notify: bool = True,
    ) -> bool:
        """Shared app chokepoint for current and in-flight content work."""
        allowed = self._acceptance_is_current(grant)
        if not allowed and notify:
            self._broadcast_acceptance_blocked()
        return allowed

    def ensure_transcript_store_initialized(
        self, grant: AcceptanceGrant | None = None, *, history_opt_in: bool = False
    ) -> Optional[TranscriptStore]:
        """Initialize encrypted history once, and only under an exact grant.

        This public seam is intentionally small so the acceptance endpoint can
        activate deferred history immediately after its durable reread. Every
        content entry point still revalidates independently.
        """
        if history_disabled_by_policy(self):
            return None
        grant = grant or self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return None
        if (
            sys.platform == "darwin"
            and not history_opt_in
            and getattr(self.config, "mac_history_opt_in", False) is not True
        ):
            return None
        # Lightweight __new__-based callers predate deferred initialization and
        # explicitly provide their store (including None). Preserve that seam;
        # normally constructed apps always define the initialization flag.
        if not hasattr(self, "_transcript_store_initialized"):
            return getattr(self, "transcript_store", None)
        lock = getattr(self, "_transcript_store_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._transcript_store_lock = lock
        with lock:
            if getattr(self, "_transcript_store_initialized", False):
                if history_opt_in and self.transcript_store is None:
                    self._transcript_store_initialized = False
                else:
                    return getattr(self, "transcript_store", None)
            if not self._acceptance_is_current(grant):
                return None

            store: Optional[TranscriptStore] = None
            try:
                store = TranscriptStore()
            except Exception as exc:
                # Never log transcript text or key material; metadata only.
                self.logger.warning(
                    "transcript store init failed reason=%s",
                    type(exc).__name__,
                )
                self._transcript_store_initialized = True
                self.transcript_store = None
                return None

            # Publishing must not race shutdown's final detach. Keychain work
            # itself never holds this short lock.
            with getattr(self, "_transcript_store_publish_lock", nullcontext()):
                if (
                    getattr(self, "_shutting_down", False)
                    or not self._acceptance_is_current(grant)
                ):
                    try:
                        store.close()
                    except Exception:  # noqa: BLE001 - discard stays best effort
                        pass
                    return None
                self.transcript_store = store
                self._transcript_store_initialized = True
            try:
                if self._acceptance_is_current(grant):
                    run_retention_sweep(self.config, store)
            except Exception as exc:
                self.logger.warning(
                    "startup retention sweep failed reason=%s",
                    type(exc).__name__,
                )
            return store

    def initialize_accepted_content_services(self, *, history_opt_in: bool = False) -> bool:
        """Activate services deferred until a durable exact acceptance."""
        grant = self._current_acceptance_grant()
        if not self._acceptance_is_current(grant):
            return False
        if getattr(self, "_file_only", False):
            return True
        if (
            sys.platform == "darwin"
            and not history_opt_in
            and getattr(self.config, "mac_history_opt_in", False) is not True
        ):
            return True
        if sys.platform == "darwin" and getattr(self, "_headless", False):
            # Security.framework may wait for user interaction despite a
            # noninteractive query. Never put that wait on HTTP startup or the
            # EULA acceptance request. History remains unavailable meanwhile.
            with self._transcript_store_start_lock:
                if (
                    self.history_initializing
                    or (self._transcript_store_initialized and not history_opt_in)
                    or self._shutting_down
                ):
                    return self._acceptance_is_current(grant)

                if history_opt_in and self._transcript_store_initialized:
                    # An OS denial must be retryable from a new explicit action.
                    store = self.transcript_store
                    if store is None or not store.decryptable:
                        if store is not None:
                            store.close()
                        self.transcript_store = None
                        self._transcript_store_initialized = False

                def initialize_history() -> None:
                    self.logger.info(
                        "transcript store init state=starting mode=background"
                    )
                    if history_opt_in:
                        from .macos_protected_secret import allow_keychain_interaction
                        with allow_keychain_interaction():
                            store = self.ensure_transcript_store_initialized(
                                grant, history_opt_in=True
                            )
                    else:
                        store = self.ensure_transcript_store_initialized(grant)
                    self.logger.info(
                        "transcript store init state=%s mode=background",
                        "ready" if store is not None else "unavailable",
                    )
                    if store is not None and not self._shutting_down:
                        self._broadcast({"type": "transcripts.changed"})

                self._transcript_store_init_thread = threading.Thread(
                    target=initialize_history, name="TranscriptStoreInit", daemon=True
                )
                self._transcript_store_init_thread.start()
        else:
            self.ensure_transcript_store_initialized(grant, history_opt_in=history_opt_in)
        return self._acceptance_is_current(grant)

    @property
    def history_initializing(self) -> bool:
        thread = getattr(self, "_transcript_store_init_thread", None)
        return thread is not None and thread.is_alive()

    def _schedule_acceptance_revocation_cancel(self) -> None:
        """Cancel one live capture off the audio callback thread."""
        if not getattr(self, "_recording", False):
            return
        lock = getattr(self, "_acceptance_revoke_lock", None)
        if lock is None:
            return
        with lock:
            if getattr(self, "_acceptance_revoke_scheduled", False):
                return
            self._acceptance_revoke_scheduled = True

        self._invalidate_live_preview()
        self.audio_capture.set_level_callback(None)
        self._broadcast_acceptance_blocked()

        def _cancel() -> None:
            try:
                with self._lock:
                    grant = getattr(
                        self, "_recording_acceptance_grant", None
                    )
                    if self._recording and not self._acceptance_is_current(
                        grant
                    ):
                        self._cancel_recording(
                            trigger="eula_revoked", play_feedback=False
                        )
            finally:
                with lock:
                    self._acceptance_revoke_scheduled = False

        threading.Thread(
            target=_cancel,
            daemon=True,
            name="AcceptanceRevocation",
        ).start()

    def set_inject_suppressed(self, suppressed: bool) -> None:
        """Toggle suppression of text_injector.inject() for onboarding test mode.

        When suppressed, `_finalize_transcription_and_inject` still broadcasts
        `transcription.result` over the WebSocket so the frontend can display
        the text in a React input, but skips the OS-level keyboard typing.
        """
        self._inject_suppressed = bool(suppressed)

    def set_event_broadcaster(self, callback) -> None:
        """Set a callable for broadcasting events to the UI (e.g. WebSocket server)."""
        self._broadcaster = callback

    def set_server_loop(self, loop) -> None:
        """Capture the FastAPI server's asyncio loop for cross-thread scheduling."""
        self._server_loop = loop
        if sys.platform == "darwin" and not getattr(self, "_file_only", False):
            from .macos_native_query_owner import bind_owner
            if loop is None:
                self.close_native_query_owner()
            else:
                self._native_query_owner = bind_owner(loop)

    def close_native_query_owner(self) -> None:
        """Release only the native query lifetime bound by this app."""
        owner = getattr(self, "_native_query_owner", None)
        if owner is not None:
            from .macos_native_query_owner import close_owner
            close_owner(expected_owner=owner)
            self._native_query_owner = None

    def set_local_warm_requester(self, callback) -> None:
        """Install the server-owned local supervisor prepare/warm requester."""
        self._local_warm_requester = callback

    def set_agent_compose_leases(self, leases) -> None:
        """Install the server-owned ephemeral compose lease store."""
        self._agent_compose_leases = leases

    def set_agent_reply_router(self, router) -> None:
        """Associate the server-owned routed reply owner without starting input."""

        self._agent_reply_router = router

    def _register_agent_reply_early_handlers(self) -> None:
        """Install routed Enter/Escape handling during ordinary hotkey setup."""

        self._register_escape_handler()
        router = getattr(self, "_agent_reply_router", None)
        if router is None:
            return
        register = getattr(
            self.keyboard, "register_early_key_handler", None
        )
        if not callable(register):
            return
        register(
            "enter",
            router.should_consume_response_enter,
            self._on_agent_enter_press,
        )

    def _register_escape_handler(self) -> None:
        """Install the combined routed-response and recording Escape owner."""

        register = getattr(
            self.keyboard, "register_early_key_handler", None
        )
        if callable(register):
            register(
                "escape",
                self._should_consume_escape,
                self._on_escape_press,
            )

    def _response_router_owns_escape(self) -> bool:
        router = getattr(self, "_agent_reply_router", None)
        if router is None:
            return False
        try:
            return router.should_consume_response_escape() is True
        except Exception:
            return False

    def _should_consume_escape(self) -> bool:
        """Consume Escape for a routed response or ordinary active recording.

        The router records its own claim when it says yes; the callback then
        acts on that claim and never re-runs this predicate.
        """

        claims = self.__dict__.setdefault("_escape_claims", collections.deque())
        if self._response_router_owns_escape():
            claims.append(True)
            return True
        recording = bool(
            getattr(self, "_recording", False)
            and getattr(self, "_recording_origin", None)
            in {"push_to_talk", "toggle"}
            and getattr(self, "_agent_compose_recording", None) is None
        )
        if recording:
            claims.append(False)
        return recording

    def _on_escape_press(self) -> None:
        """Honor routed Escape first, otherwise discard ordinary dictation."""

        claims = self.__dict__.setdefault("_escape_claims", collections.deque())
        try:
            routed = claims.popleft()
        except IndexError:
            routed = False
        router = getattr(self, "_agent_reply_router", None)
        if routed:
            if router is not None:
                try:
                    router.dismiss_response_routes()
                except Exception:
                    pass
            return

        with self._lock:
            if (
                not self._recording
                or self._recording_origin not in {"push_to_talk", "toggle"}
                or self._agent_compose_recording is not None
            ):
                return
            self._cancel_recording(trigger="escape")
            reset_toggle = getattr(self.keyboard, "reset_toggle_state", None)
            if callable(reset_toggle):
                reset_toggle()

    def _on_agent_enter_press(self) -> None:
        """Submit an exact ready Agent draft only while acceptance is current."""
        if not self._require_acceptance():
            return
        router = getattr(self, "_agent_reply_router", None)
        if router is None:
            return
        try:
            router.submit_ready_response_draft()
        except Exception:
            return

    def _start_routed_reply(self, mode: str) -> bool:
        if not self._require_acceptance():
            return True
        router = getattr(self, "_agent_reply_router", None)
        if router is None:
            return False
        try:
            return bool(router.start_routed_reply(mode))
        except Exception as exc:
            # The hotkey always records. A router failure falls through to
            # ordinary focused-window dictation rather than swallowing the
            # gesture; any agent response stays available for a retry.
            self.logger.warning(
                "[ClaudeHUD] reply route failed mode=%s reason=%s action=fallthrough",
                mode,
                type(exc).__name__,
            )
            return False

    def _stop_routed_reply(self, mode: str) -> bool:
        router = getattr(self, "_agent_reply_router", None)
        if router is None:
            return False
        try:
            return bool(router.stop_routed_reply(mode))
        except Exception:
            return False

    def start_agent_compose(self, generation: int) -> bool:
        """Start one lease-owned recording without stealing an active capture."""
        if getattr(self, "_shutting_down", False):
            return False
        grant = self._current_acceptance_grant()
        if not self._require_acceptance(grant):
            return False
        self.initialize_accepted_content_services()
        leases = getattr(self, "_agent_compose_leases", None)
        if leases is None or not leases.is_current(generation):
            return False
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return False
            if self._recording:
                return False
            self._recording_acceptance_grant = grant
            self._agent_compose_pending_generation = generation
            self._started_via_toggle = False
            try:
                self._try_begin_recording()
            except Exception:
                self._recording_acceptance_grant = None
                self._agent_compose_pending_generation = None
                return False
            lease = getattr(self, "_agent_compose_recording", None)
            if (
                not self._recording
                or lease is None
                or lease.generation != generation
            ):
                # _try_begin_recording opens capture before the lifecycle claims
                # the generation-scoped lease. If that claim loses a race, the
                # microphone must not survive as an ordinary recording and paste
                # the intended reply into the focused window.
                if self._recording:
                    self.logger.warning(
                        "[ClaudeHUD] reply capture aborted reason=lease_not_claimed"
                    )
                    self._cancel_recording(trigger="agent_reply")
                self._agent_compose_pending_generation = None
                self._recording_acceptance_grant = None
                return False
            return True

    def stop_agent_compose(self, generation: int) -> bool:
        """Stop capture normally so its authoritative final can become a draft."""
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return False
            lease = getattr(self, "_agent_compose_recording", None)
            if (
                not self._recording
                or lease is None
                or lease.generation != generation
            ):
                return False
            self._attempt_restore_system_volume()
            self._stop_recording()
            return True

    def cancel_agent_compose(self, generation: int, *, reason: str) -> bool:
        """Cancel only the matching compose capture, never ordinary dictation."""
        try:
            with self._lock:
                if getattr(self, "_shutting_down", False):
                    return False
                lease = getattr(self, "_agent_compose_recording", None)
                if lease is None or lease.generation != generation:
                    return False
                if self._recording:
                    self._cancel_recording(trigger=reason)
                self._agent_compose_recording = None
                self._last_waveform = None
                return True
        except Exception:
            # Blur, disconnect, and expiry cleanup must never destabilize the
            # normal hotkey path.
            self._last_waveform = None
            return False

    def _broadcast(self, event: dict) -> None:
        """Send an event to the broadcaster if one is registered."""
        if self._broadcaster is not None:
            try:
                self._broadcaster(event)
            except Exception:
                pass

    def dictation_state_snapshot(self) -> dict:
        """Return the latest content-free authoritative dictation lifecycle."""
        with self._dictation_state_lock:
            return dict(self._dictation_state)

    def _begin_dictation_state(
        self,
        *,
        origin: str,
        screen_reader_detected: bool,
    ) -> int:
        """Start a new monotonic dictation generation and broadcast its state."""
        if origin not in _DICTATION_ORIGINS:
            raise ValueError(f"invalid dictation origin: {origin}")
        hud_enabled = (
            bool(getattr(self.config, "show_recording_indicator", True))
            and not screen_reader_detected
        )
        preview_enabled = (
            hud_enabled
            and bool(getattr(self.config, "show_live_preview", False))
            and getattr(self, "stt_engine", None) is not None
        )
        with self._dictation_state_lock:
            self._recording_sequence += 1
            recording_id = self._recording_sequence
            event = {
                "type": "dictation.state",
                "version": _DICTATION_STATE_VERSION,
                "recording_id": recording_id,
                "phase": "recording",
                "origin": origin,
                "hud_enabled": hud_enabled,
                "preview_enabled": preview_enabled,
                "screen_reader_detected": bool(screen_reader_detected),
            }
            self._dictation_state = event
        pending_generation = getattr(
            self, "_agent_compose_pending_generation", None
        )
        leases = getattr(self, "_agent_compose_leases", None)
        compose_lease = None
        if leases is not None and pending_generation is not None:
            try:
                compose_lease = leases.claim(
                    pending_generation, recording_id=recording_id
                )
            except Exception:
                compose_lease = None
        self._agent_compose_pending_generation = None
        self._agent_compose_recording = compose_lease
        if compose_lease is not None:
            # The HUD must know a reply owns this recording from its first
            # event; waiting for agent.compose.state flashes the dictation pill.
            with self._dictation_state_lock:
                event["compose_generation"] = compose_lease.generation
        self.logger.debug(
            "[Dictation] state phase=recording origin=%s hud_enabled=%s preview_enabled=%s screen_reader=%s",
            origin,
            str(hud_enabled).lower(),
            str(preview_enabled).lower(),
            str(bool(screen_reader_detected)).lower(),
        )
        self._broadcast(dict(event))
        if compose_lease is not None:
            self._broadcast(
                {
                    "type": "agent.compose.state",
                    "version": 1,
                    "session_handle": compose_lease.session_handle,
                    "generation": compose_lease.generation,
                    "recording_id": recording_id,
                    "state": "recording",
                }
            )
        return recording_id

    def _transition_dictation_state(self, recording_id: int, phase: str) -> bool:
        """Compare-and-set one dictation generation to a later lifecycle phase.

        Returns False when *recording_id* is stale, so a slow finalizer can never
        clear a newer recording's state.
        """
        if phase not in _DICTATION_PHASES:
            raise ValueError(f"invalid dictation phase: {phase}")
        with self._dictation_state_lock:
            current = self._dictation_state
            if recording_id != current["recording_id"]:
                event = None
                ignored_reason = "stale"
            elif phase == current["phase"]:
                event = None
                ignored_reason = "duplicate"
            else:
                event = {**current, "phase": phase}
                self._dictation_state = event
                ignored_reason = None

        if event is None:
            self.logger.debug(
                "[Dictation] state ignored reason=%s phase=%s",
                ignored_reason,
                phase,
            )
            return False

        self.logger.debug(
            "[Dictation] state phase=%s origin=%s hud_enabled=%s preview_enabled=%s screen_reader=%s",
            phase,
            event["origin"],
            str(event["hud_enabled"]).lower(),
            str(event["preview_enabled"]).lower(),
            str(event["screen_reader_detected"]).lower(),
        )
        self._broadcast(dict(event))
        return True

    def _dispatch_error_dialog(
        self,
        code: str,
        title: str,
        message: str,
        retry_action: Optional[str] = None,
    ) -> None:
        """Broadcast an error.dialog event to the frontend popup."""
        self.logger.warning(
            "error dialog dispatched code=%s retry_action=%s",
            code,
            retry_action or "none",
        )
        payload: dict = {"type": "error.dialog", "code": code, "title": title, "message": message}
        if retry_action is not None:
            payload["retry_action"] = retry_action
        self._broadcast(payload)

    @property
    def last_stt_failure(self) -> SttFailure | None:
        """Return the current content-free model degradation, if any."""

        return getattr(self, "_last_stt_failure", None)

    def _broadcast_stt_failure(self, failure: SttFailure) -> None:
        """Publish typed STT failure metadata without diagnostic content."""

        self._last_stt_failure = failure
        metadata = failure.public_metadata()
        self.logger.error(
            "[STT] operation failed code=%s backend=%s model=%s operation=%s "
            "recovery_attempted=%s suppressed=%s",
            metadata["code"],
            metadata["backend"],
            metadata["model_id"],
            metadata["operation"],
            str(metadata["recovery_attempted"]).lower(),
            str(metadata["suppressed"]).lower(),
        )
        self._broadcast({"type": "stt.failure", **metadata})

    def _load_stt_engine_blocking(self) -> None:
        """Load the configured speech engine into ``self.stt_engine``.

        Raises ``SystemExit(1)`` in standalone mode if the model files are
        missing; in headless mode, sets ``self.stt_engine = None`` so the API
        download flow can prepare the files.
        """
        self._last_stt_failure = None
        try:
            with self._stt_model_operation_lock:
                # A model-switch worker may have won the guard after the
                # background loader was spawned. Never replace or close the
                # manager it just installed.
                with self._lock:
                    if self.parakeet is not None:
                        return

                status = (
                    self._console.status("[bold green]Initializing speech model...[/bold green]", spinner="dots")
                    if self._console is not None else nullcontext()
                )
                with status:
                    load_config = self.config
                    manager = make_stt_manager(
                        load_config,
                        logger=self.logger,
                        config_manager=self.config_manager,
                    )
                    # install_stt_manager checks the routing snapshot again
                    # under the runtime lock and closes this manager if a
                    # config write made the construction target stale.
                    self.install_stt_manager(manager, load_config)
        except ModelNotPreparedError:
            if self._headless:
                self.logger.info("stt model not found mode=headless action=wait_for_api_download")
            else:
                self.logger.error("stt model not found mode=standalone action=exit hint=run --serve for UI download")
                raise SystemExit(1)

    def _load_parakeet_blocking(self) -> None:
        """Compatibility for source callers predating the portable STT contract."""
        self._load_stt_engine_blocking()

    def load_parakeet_async(self) -> None:
        self.load_stt_engine_async()

    @property
    def configured_stt_artifact_id(self) -> str:
        """Return the exact artifact requested by current persisted config."""
        return _stt_artifact_id(self.config).value

    @property
    def loaded_stt_artifact_id(self) -> str | None:
        """Return the exact artifact owned by the live manager, if any."""
        artifact = self._loaded_stt_artifact_id
        return artifact.value if artifact is not None else None

    def _record_loaded_stt_config(self, config: WhisperstreamConfig) -> None:
        """Record exact runtime ownership after successful construction."""
        self._loaded_stt_artifact_id = _stt_artifact_id(config)
        self._loaded_stt_routing_state = _stt_routing_state(config)

    def stt_activation_busy(self) -> bool:
        """Return whether recording/finalization currently forbids a swap."""
        if bool(getattr(self, "_recording", False)):
            return True
        try:
            state = self.dictation_state_snapshot()
        except Exception:
            return False
        return state.get("phase") in {"recording", "processing"}

    def stt_activation_rollback_state(
        self,
        target_config: WhisperstreamConfig,
    ) -> SttRoutingState:
        """Return the full routing state to restore if ``target`` cannot load."""
        return (
            self._stt_activation_rollback
            or self._loaded_stt_routing_state
            or _stt_routing_state(target_config)
        )

    @contextmanager
    def stt_replacement(self):
        """Suspend the current worker while constructing its replacement.

        The server holds the model-operation lock. The short recording lock
        protects admission and detachment; native teardown/loading occurs
        outside it. A failed construction restores the previous exact owner.
        """
        with self._lock:
            if self.stt_activation_busy():
                raise SttActivationBusyError("dictation is active")
            previous = self.stt_engine
            self._stt_swap_in_progress = True
            self.stt_engine = None
        try:
            if previous is not None:
                previous.unload()
            yield
        except BaseException:
            if previous is not None:
                try:
                    previous.load()
                except BaseException:
                    previous.shutdown()
                    with self._lock:
                        self._loaded_stt_artifact_id = None
                        self._loaded_stt_routing_state = None
                    raise
            with self._lock:
                self.stt_engine = previous
            raise
        else:
            if previous is not None and previous is not self.stt_engine:
                previous.shutdown()
        finally:
            with self._lock:
                self._stt_swap_in_progress = False

    def install_stt_manager(
        self,
        manager: SttEngine,
        target_config: WhisperstreamConfig,
    ) -> None:
        """Atomically install one prepared manager and close its predecessor.

        Construction happens before this method. The app's recording lock is
        held only for the short state check and pointer swap, which prevents a
        hotkey start from racing the ownership change without blocking audio
        capture for the duration of a download or model load.
        """
        old_manager = None
        try:
            with self._lock:
                if self.stt_activation_busy():
                    raise SttActivationBusyError("dictation is active")
                if _stt_routing_state(self.config) != _stt_routing_state(target_config):
                    raise SttActivationStaleError("STT activation target is stale")
                old_manager = self.parakeet
                self.parakeet = manager
                self._record_loaded_stt_config(target_config)
                self._stt_activation_rollback = None
        except Exception:
            try:
                manager.shutdown()
            except Exception:
                self.logger.warning("stt manager close failed role=uninstalled")
            raise

        if old_manager is not None and old_manager is not manager:
            try:
                old_manager.shutdown()
            except Exception:
                # The new manager already owns the runtime. A best-effort close
                # failure must not roll the working recognizer back.
                self.logger.warning("stt manager close failed role=replaced")

    def unload_stt_manager(self) -> None:
        """Detach, close, and forget the exact live STT manager.

        Relocation uses this path before touching model files on Windows. The
        short app lock prevents a recording start from racing the detach; the
        manager closes outside the lock after runtime ownership is cleared.
        """
        with self._lock:
            if self.stt_activation_busy():
                raise SttActivationBusyError("dictation is active")
            manager = self.parakeet
            self.parakeet = None
            self._loaded_stt_artifact_id = None
            self._loaded_stt_routing_state = None
            self._stt_activation_rollback = None

        if manager is not None:
            manager.shutdown()

    def restore_stt_routing_state(
        self,
        rollback: SttRoutingState,
        target_config: WhisperstreamConfig,
    ) -> bool:
        """Persist and hot-reload a failed activation's prior routing state.

        A stale worker may finish after a newer config choice. In that case it
        must not overwrite the newer choice, so restoration is conditional on
        the current routing still matching the worker's target.
        """
        if _stt_routing_state(self.config) != _stt_routing_state(target_config):
            self.logger.info("stt routing restore skipped reason=target_changed")
            return False

        restored = replace(
            self.config,
            **{
                field_name: getattr(rollback, field_name)
                for field_name in _STT_ROUTING_FIELDS
            },
        )
        self.config_manager.save(restored)
        self.reload_config(restored)
        self._stt_activation_rollback = None
        return True

    def load_stt_engine_async(self) -> None:
        """Spawn a daemon thread that loads the Parakeet model in the background.

        Use this in --serve mode after the broadcaster is registered: the server
        starts listening immediately, the frontend can render its UI, and a
        ``model.loaded`` (or ``model.load_failed``) event is broadcast when the
        load finishes. Recording attempts before the model is ready short-circuit
        on the ``self.stt_engine is None`` guards in `_on_chunk_ready` and
        `_finalize_transcription_and_inject`.
        """
        def _run() -> None:
            self._broadcast({"type": "model.loading"})
            try:
                self._load_stt_engine_blocking()
            except SystemExit:
                self._broadcast({"type": "model.load_failed", "reason": "model_not_prepared"})
                c = COPY["model_load_failed"]
                self._dispatch_error_dialog("model_load_failed", c["title"], c["message"], retry_action="reload_model")
                return
            except SttActivationStaleError:
                # Onboarding/model selection now owns activation. The old
                # manager was closed by install_stt_manager; this is not a
                # failed model and must not open a misleading error popup.
                self.logger.info("startup model superseded action=await_selected_activation")
                return
            except SttFailure as exc:
                self.stt_engine = None
                self._broadcast({"type": "model.load_failed", "reason": exc.code})
                self._broadcast_stt_failure(exc)
                return
            except Exception as exc:
                self.logger.error("model load failed error_type=%s", type(exc).__name__)
                self._broadcast({"type": "model.load_failed", "reason": type(exc).__name__})
                c = COPY["model_load_failed"]
                self._dispatch_error_dialog("model_load_failed", c["title"], c["message"], retry_action="reload_model")
                return
            if self.stt_engine is not None:
                self._broadcast({"type": "model.loaded"})
            else:
                # Missing files are an ordinary onboarding/download state in
                # serve mode, not a failed recognizer construction.
                self._broadcast({
                    "type": "model.missing",
                    "model_name": self.configured_stt_artifact_id,
                })

        threading.Thread(target=_run, daemon=True, name="ParakeetLoader").start()

    def _profile_name(self, profile_id: str) -> str:
        """Return the display name of a profile id, falling back to the id."""
        for profile in self.config.profiles:
            if profile.get("id") == profile_id:
                return profile.get("name") or profile_id
        return profile_id

    def _recording_context_is_current(
        self,
        context: _DictationFinalizationContext | None,
    ) -> bool:
        """Whether one finalizer still owns the active HUD lifecycle."""
        if context is None or context.recording_id is None:
            return False
        state = self.dictation_state_snapshot()
        return (
            state["recording_id"] == context.recording_id
            and state["phase"] in {"recording", "processing"}
        )

    def _broadcast_effective_profile(
        self,
        context: _DictationFinalizationContext | None,
        profile_id: str | None,
    ) -> bool:
        """Publish the effective profile for one current enhanced dictation."""
        if not self._recording_context_is_current(context):
            return False
        effective_profile_id = profile_id or self.config.default_profile_id
        if not effective_profile_id:
            return False

        matched_profile = next(
            (
                profile
                for profile in self.config.profiles
                if profile.get("id") == effective_profile_id
            ),
            None,
        )
        event = {
            "type": "enhancement.profile",
            "recording_id": context.recording_id,
            "profile_id": effective_profile_id,
            "name": (
                matched_profile.get("name") or effective_profile_id
                if matched_profile is not None
                else effective_profile_id
            ),
        }
        # Built-in glyphs are derived from immutable ids. Config validation
        # bounds custom icons before Main sees them, so only custom selections
        # cross this presentation seam.
        if matched_profile is not None and effective_profile_id not in {
            "chat",
            "code",
            "email",
        }:
            icon = matched_profile.get("icon")
            if isinstance(icon, str) and icon:
                event["icon"] = icon
        self._broadcast(event)
        return True

    def _enhancement_outcome_observers(
        self,
        context: _DictationFinalizationContext | None,
    ) -> tuple[TimeoutObserver, ErrorObserver]:
        """Build recording-correlated observers with one shared outcome budget."""
        reported = False

        def _report(outcome: str, provider: EnhancementProvider) -> None:
            nonlocal reported
            if (
                reported
                or provider not in ENHANCEMENT_PROVIDERS
                or not self._recording_context_is_current(context)
                # A reply lands as a draft in the agent's own surface for
                # review; the dictation error pill would replace that surface.
                or context.agent_compose_lease is not None
            ):
                return
            self._broadcast(
                {
                    "type": f"enhancement.{outcome}",
                    "recording_id": context.recording_id,
                    "provider": provider,
                }
            )
            reported = True

        return (
            lambda provider: _report("timeout", provider),
            lambda provider: _report("error", provider),
        )

    def set_manual_profile(self, profile_id: str) -> None:
        """Hotkey callback (issue 14): sticky-switch the active profile.

        Records the CURRENT foreground app's process_name as the override anchor
        and sets *profile_id* as the manual override. The next dictation uses this
        profile as long as it fires while the same app is still focused; once focus
        moves to a different app the override clears (checked at recording-start in
        ``_enhance_then_inject``) and auto-routing resumes. Switch-only: never
        starts a recording. Broadcasts ``active_profile_override`` so the HUD chip +
        Settings status line can reflect the active override. Unknown profile ids
        are ignored (defensive: the registered hotkey set may lag a config edit).
        """
        if not any(p.get("id") == profile_id for p in self.config.profiles):
            self.logger.warning("manual profile switch ignored reason=unknown_id")
            return
        app = WhisperstreamApp._capture_application_context(
            self,
            include_browser_domain=False,
        )
        anchor = routing_target_application_id(app)
        self._manual_profile_id = profile_id
        self._override_anchor = anchor
        self.logger.info(
            "manual profile override set anchored=%s",
            "yes" if anchor else "no",
        )
        self._broadcast({
            "type": "active_profile_override",
            "profile_id": profile_id,
            "name": self._profile_name(profile_id),
            "source": "hotkey",
        })

    def _capture_application_context(
        self,
        *,
        include_browser_domain: bool,
    ) -> ContextSnapshot | RoutingTarget | None:
        """Capture one recording-start identity through the platform boundary.

        The direct probe is retained only for lightweight compatibility stubs
        that predate the adapter. Production construction always installs
        ``application_context``.
        """

        adapter = getattr(self, "application_context", None)
        if adapter is None:
            return detect_foreground_routing_target(
                include_browser_domain=include_browser_domain
            )
        lock = getattr(self, "_context_generation_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._context_generation_lock = lock
        with lock:
            self._context_generation = int(
                getattr(self, "_context_generation", 0)
            ) + 1
            generation = self._context_generation
        requirement = (
            ContextRequirement.APPLICATION_AND_BROWSER_PAGE
            if include_browser_domain
            else ContextRequirement.APPLICATION
        )
        return adapter.capture(generation, requirement)

    def clear_manual_profile(self) -> None:
        """Clear the manual override and broadcast the cleared state.

        Called when auto-routing resumes (focus changed at recording-start, or the
        override profile was deleted) and by the UI "Resume auto-routing" action.
        Idempotent: a no-op broadcast when nothing was set is harmless but skipped.
        """
        if self._manual_profile_id is None and self._override_anchor is None:
            return
        self._manual_profile_id = None
        self._override_anchor = None
        self.logger.info("manual profile override cleared")
        self._broadcast({"type": "active_profile_override", "profile_id": None})

    def _resolve_active_profile(self) -> str | None:
        """Resolve the enhancement profile for the current dictation (issue 14).

        If a manual override is active AND the recording's foreground app matches
        the anchor AND the override profile still exists, the override wins
        (overriding auto-routing). Otherwise the override is cleared (broadcasting
        the cleared state) and the result falls back to ``resolve_profile_for_target``
        (site -> app -> default). ``None`` means "use the global default profile."
        """
        # Call through the class so lightweight compatibility stubs that bind
        # only this long-standing method do not need to bind the new helper too.
        return WhisperstreamApp._resolve_profile_for_routing(
            self,
            dictation_app=self._dictation_app,
            manual_profile_id=self._manual_profile_id,
            override_anchor=self._override_anchor,
        )

    def _resolve_profile_for_routing(
        self,
        *,
        dictation_app: ContextSnapshot | RoutingTarget | None,
        manual_profile_id: str | None,
        override_anchor: str | None,
    ) -> str | None:
        """Resolve a profile from one dictation's captured routing inputs.

        A stale captured override may clear the live override only when the live
        values still match it. This compare-and-clear prevents an older
        finalizer from erasing a newer hotkey selection.
        """
        if manual_profile_id is not None:
            current = routing_target_application_id(dictation_app)
            still_exists = any(
                p.get("id") == manual_profile_id for p in self.config.profiles
            )
            if (
                still_exists
                and current is not None
                and current == override_anchor
            ):
                return manual_profile_id
            # Focus moved off the anchor (or the profile is gone): drop the
            # override and resume auto-routing, but only if this finalizer still
            # owns the live override values.
            if (
                getattr(self, "_manual_profile_id", None) == manual_profile_id
                and getattr(self, "_override_anchor", None) == override_anchor
            ):
                self.clear_manual_profile()
        route = resolve_enhancement_route(dictation_app, self.config)
        if route is None or route.tier is ProfileRouteTier.DEFAULT:
            return None
        return route.profile_id

    def _effective_input_style(
        self,
        profile_id: str | None,
        *,
        dictation_app: ContextSnapshot | RoutingTarget | None | object = _USE_CURRENT_ROUTING,
        screen_reader_detected: bool | object = _USE_CURRENT_ROUTING,
    ) -> str:
        """Resolve the effective input style for a dictation, independent of do_enhance.

        When profile_id is None (the raw/no-enhance path, where Power Mode
        routing is skipped because AI cleanup won't run), re-runs the pure
        site -> app -> default routing (resolve_profile_for_target) so a per-app
        or per-site profile's input_style override still applies even when
        enhancement is OFF, then falls back to default_profile_id. Uses
        resolve_profile_for_target (pure, no side effects) rather than
        _resolve_active_profile (which can mutate manual-override state).

        Returns "clipboard" or "keystroke". Per-profile explicit override wins
        over the effective global. The effective GLOBAL is:
        - config.input_style when the user has explicitly chosen (dirty flag
          input_style_user_set=True), OR
        - "keystroke" when a screen reader is detected (screen_reader_active()),
          "clipboard" otherwise — when the flag is False (app may auto-pick).

        The detection is live (re-evaluated on each dictation) so users who
        launch a screen reader mid-session get the right mode immediately.
        """
        _VALID_STYLES = {"clipboard", "keystroke"}

        # Detect once so the decision is loggable regardless of which branch
        # wins below (screen_reader_active is pure and never raises).
        user_set = bool(getattr(self.config, "input_style_user_set", False))
        sr_detected = (
            screen_reader_active()
            if screen_reader_detected is _USE_CURRENT_ROUTING
            else bool(screen_reader_detected)
        )
        routed_app = (
            self._dictation_app
            if dictation_app is _USE_CURRENT_ROUTING
            else dictation_app
        )

        # Compute effective global: honour user's explicit choice first, then
        # fall back to live screen-reader detection.
        if user_set:
            effective_global = self.config.input_style
        else:
            effective_global = "keystroke" if sr_detected else "clipboard"

        # Per-profile override wins over the effective global. Resolve the
        # routing-active profile even on the no-enhance path: when profile_id is
        # None (do_enhance=False, or routing found no match upstream), re-run the
        # pure site -> app -> default routing so a per-app or per-site profile's
        # input_style override applies regardless of whether AI cleanup runs.
        # resolve_profile_for_target is side-effect-free (unlike
        # _resolve_active_profile, which can mutate manual-override state), so it
        # is safe to call here purely for the input-style decision. Manual
        # hotkey overrides are intentionally NOT consulted on the no-enhance path
        # (they are an enhancement-routing affordance); focused-app routing is.
        resolved = effective_global
        per_profile = None
        pid = profile_id
        if pid is None:
            pid = resolve_profile_for_target(routed_app, self.config)
        pid = pid or self.config.default_profile_id
        if pid:
            prof = next(
                (p for p in self.config.profiles if p.get("id") == pid), None
            )
            if prof is not None:
                per_profile = prof.get("input_style")
                if per_profile in _VALID_STYLES:
                    resolved = per_profile

        # Diagnostic trace: why the app chose paste vs keystroke for this
        # dictation. No transcript content here — safe to log. When user_set
        # is True the global is the user's locked pick and screen-reader
        # detection is ignored (explicit choice wins); this line makes that
        # visible instead of silent.
        self.logger.debug(
            "[InputStyle] resolved style=%s screen_reader=%d user_set=%s "
            "effective_global=%s profile_override=%s profile_selected=%s",
            resolved,
            int(sr_detected),
            str(user_set).lower(),
            effective_global,
            per_profile if per_profile in _VALID_STYLES else "none",
            str(bool(pid)).lower(),
        )
        return resolved

    def _register_profile_hotkeys(self) -> None:
        """(Re-)register the per-profile manual-switch hotkeys from config.

        Reads ``config.profile_hotkeys`` (profile id -> shortcut) and binds each
        to ``set_manual_profile``. Never raises (the manager skips unparseable
        entries with a warning), preserving the no-admin / never-crash contract.
        """
        try:
            self.keyboard.register_profile_hotkeys(
                dict(self.config.profile_hotkeys),
                self.set_manual_profile,
            )
        except Exception:
            self.logger.warning("profile hotkey registration failed")

    def _on_paste_last_press(self) -> None:
        """Callback from the extra hotkey when the paste-last shortcut is pressed.

        Uses the global config.input_style (LOCKED decision): paste-last is a
        global re-paste affordance with no fresh routing context; the last
        dictation's profile is not reliably available here.
        """
        if not self._require_acceptance():
            return
        outcome = self.text_injector.paste_last(
            input_style=self.config.input_style
        )
        if outcome is None:
            self.logger.debug(
                "paste_last skipped reason=no_stored_transcript"
            )
        elif outcome:
            self.logger.info("paste_last injected")
        else:
            reason = getattr(getattr(outcome, "reason", None), "value", "unknown")
            self.logger.info(
                "paste_last result=failed reason=%s",
                reason,
            )

    def _on_paste_last_original_press(self) -> None:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        """Callback from the extra hotkey when the paste-last-original shortcut is pressed.

        Uses the global config.input_style, mirroring _on_paste_last_press's LOCKED
        decision: re-paste affordances have no fresh routing context, so both paste
        hotkeys honor the global clipboard/keystroke setting rather than re-resolving
        a per-app profile.
        """
        if not self._require_acceptance():
            return
        outcome = self.text_injector.paste_last_original(
            input_style=self.config.input_style
        )
        if outcome is None:
            self.logger.debug(
                "paste_last_original skipped reason=no_stored_transcript"
            )
        elif outcome:
            self.logger.info("paste_last_original injected")
        else:
            reason = getattr(getattr(outcome, "reason", None), "value", "unknown")
            self.logger.info(
                "paste_last_original result=failed reason=%s",
                reason,
            )

    def _inject_and_broadcast_route(
        self,
        text: str,
        input_style: str | None = None,
        submit: bool = False,
        *,
        already_processed: bool = False,
        request_id: str | None = None,
        generation: int | None = None,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> None:
        """Inject text and broadcast an acceptance block if delivery was denied.

        Privacy: no text or title appears in any broadcast or log line here.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._require_acceptance(acceptance_grant):
            return
        if already_processed:
            outcome = self.text_injector.inject(
                text,
                input_style=input_style,
                submit=submit,
                request_id=request_id,
                generation=generation,
                already_processed=True,
            )
        else:
            outcome = self.text_injector.inject(
                text,
                input_style=input_style,
                submit=submit,
            )
        if getattr(outcome, "status", None) is DeliveryStatus.BLOCKED:
            self._broadcast_acceptance_blocked()

    def _reserve_audio_policy_generation(self) -> int:
        """Reserve one unique audio-attempt generation before the start chime."""
        current = int(getattr(self, "_audio_policy_generation", 0))
        generation = current + 1
        self._audio_policy_generation = generation
        self._audio_policy_transaction = None
        return generation

    def _audio_policy_result(self, operation: str, result: object) -> None:
        """Record only the typed, content-free outcome of a policy operation."""
        failure = getattr(result, "failure", AudioPolicyFailure.NONE)
        stage = getattr(getattr(result, "stage", None), "value", "unknown")
        if failure is not AudioPolicyFailure.NONE:
            self.logger.warning(
                "audio policy operation=%s stage=%s failure=%s",
                operation,
                stage,
                getattr(failure, "value", "unknown"),
            )

    def _play_recording_chime(self, kind: ChimeKind) -> None:
        policy = getattr(self, "audio_policy", None)
        generation = int(getattr(self, "_audio_policy_generation", 0))
        if policy is None or generation < 1:
            # Lightweight legacy test stubs may not construct the adapter.
            feedback = getattr(self, "audio_feedback", None)
            if feedback is None:
                return
            if kind is ChimeKind.START:
                feedback.play_start_blocking(self.config.start_sound_path)
            else:
                feedback.play_stop(self.config.stop_sound_path)
            return
        result = policy.play_chime(generation, kind)
        self._audio_policy_result(f"chime-{kind.value}", result)

    def _ensure_volume_interface(self) -> bool:
        """Compatibility capability probe without exposing a native endpoint."""
        return (
            sys.platform == "win32"
            and getattr(self, "audio_policy", None) is not None
            and type(self.audio_policy).__name__
            == "WindowsRecordingAudioPolicyAdapter"
        )

    def _observe_playback_volume(self, phase: str):
        """Compatibility seam; observations now stay inside the policy adapter."""
        del phase
        return None

    def _reduce_system_volume(self) -> None:
        """Begin the frozen post-capture audio transaction."""
        policy = getattr(self, "audio_policy", None)
        generation = int(getattr(self, "_audio_policy_generation", 0))
        if policy is None or generation < 1:
            return
        try:
            mode = AudioPolicyMode(self.config.system_volume_mode)
        except ValueError:
            mode = AudioPolicyMode.OFF
        try:
            transaction, result = policy.begin(generation, mode)
            self._audio_policy_transaction = transaction
            self._audio_policy_result("begin", result)
        except Exception as exc:
            self.logger.warning("audio policy begin failed reason=%s", type(exc).__name__)

    def _attempt_restore_system_volume(self) -> None:
        try:
            self._restore_system_volume()
        except Exception as exc:
            self.logger.warning("volume restore failed reason=%s", type(exc).__name__)

    def _capture_cleanup_retryable(self) -> bool:
        capture = getattr(self, "audio_capture", None)
        if getattr(capture, "terminal_operations_owned", False) is True:
            return False
        return getattr(capture, "cleanup_retryable", True) is not False

    def _capture_unavailable(self) -> bool:
        return getattr(getattr(self, "audio_capture", None), "capture_available", True) is False

    def _report_capture_failure(self) -> None:
        unavailable = self._capture_unavailable()
        copy = COPY["audio_capture_unavailable" if unavailable else "audio_capture_failed"]
        self._dispatch_error_dialog(
            "audio_capture_failed", copy["title"], copy["message"],
            retry_action=None if unavailable else "restart_capture",
        )

    def _restore_system_volume(self) -> None:
        """Restore the exact captured endpoint before microphone close."""
        policy = getattr(self, "audio_policy", None)
        transaction = getattr(self, "_audio_policy_transaction", None)
        if policy is None or transaction is None:
            return
        result = policy.restore(transaction)
        self._audio_policy_result("restore", result)

    def _close_audio_policy(self) -> None:
        """Close one generation after capture and enforce interrupted restore."""
        policy = getattr(self, "audio_policy", None)
        generation = int(getattr(self, "_audio_policy_generation", 0))
        if policy is None or generation < 1:
            return
        try:
            result = policy.close(generation)
        except Exception as exc:
            self.logger.warning("audio policy close failed reason=%s", type(exc).__name__)
            return
        self._audio_policy_result("close", result)
        if result.failure in {AudioPolicyFailure.NONE, AudioPolicyFailure.CLOSED}:
            self._audio_policy_transaction = None

    def cleanup(self) -> None:
        """Release app-lifetime resources exactly once.

        Drain transcription/persistence work before closing its store, then
        release process-memory waveform, content, and key references. The active
        STT engine and native adapters own bounded, idempotent shutdown so no
        supervised worker, stream, hook, or platform target outlives the app.
        """
        file_only_stop = getattr(self, "_file_only_stop", None)
        if file_only_stop is not None:
            file_only_stop.set()
        cleanup_lock = getattr(self, "_cleanup_lock", None)
        if cleanup_lock is None:
            cleanup_lock = threading.Lock()
            self._cleanup_lock = cleanup_lock
        with cleanup_lock:
            if getattr(self, "_cleanup_complete", False):
                return
            recording_lock = getattr(self, "_lock", None)
            if recording_lock is None:
                recording_lock = threading.Lock()
                self._lock = recording_lock
            # If terminal settlement already owns recording state, wait for it
            # to finish before snapshotting downstream owners. If it is merely
            # queued, publishing shutdown under the same lock makes it a no-op.
            with recording_lock:
                self._dictation_app = None
                self._last_waveform = None
                self._cleanup_complete = True
                executor = getattr(self, "_executor", None)
                persistence_executor = getattr(self, "_persistence_executor", None)
                import_executor = getattr(self, "_import_executor", None)
                self._shutting_down = True
                application_context = getattr(self, "application_context", None)
                stt_engine = getattr(self, "stt_engine", None)
                audio_capture = getattr(self, "audio_capture", None)
                audio_feedback = getattr(self, "audio_feedback", None)
                keyboard = getattr(self, "keyboard", None)
                text_injector = getattr(self, "text_injector", None)
                recording = bool(getattr(self, "_recording", False))
                self._active_capture_session_key = None
                self._recording = False
                self._discarding_recording = True
                self._recording_acceptance_grant = None
                self._recording_origin = None
                self._started_via_toggle = False
                for timer_name in ("_stop_timer", "_idle_timer"):
                    timer = getattr(self, timer_name, None)
                    if timer is not None:
                        timer.cancel()
                        setattr(self, timer_name, None)
                self._invalidate_live_preview()
                chunk_lock = getattr(self, "_chunk_futures_lock", None)
                if chunk_lock is not None:
                    with chunk_lock:
                        for future in getattr(self, "_chunk_futures", ()):
                            future.cancel()
                        self._chunk_futures = []
                lease = getattr(self, "_agent_compose_recording", None)
                if lease is not None:
                    self._retire_agent_compose(lease, state="cancelled", reason="shutdown")
                if hasattr(self, "_dictation_state_lock"):
                    self._transition_dictation_state(self.dictation_state_snapshot()["recording_id"], "idle")
                enhancement_bakeoff = getattr(self, "enhancement_bakeoff", None)

        if enhancement_bakeoff is not None:
            enhancement_bakeoff.revoke_all()

        if audio_capture is not None:
            if recording:
                try:
                    self._attempt_restore_system_volume()
                except Exception:  # noqa: BLE001 - shutdown must keep closing capture
                    self.logger.warning("volume restore result=error origin=app_shutdown")
            try:
                capture_closed = audio_capture.close()
                if capture_closed is False:
                    self.logger.warning(
                        "audio capture close result=incomplete origin=app_shutdown "
                        "action=bounded_retry"
                    )
                    if self._capture_cleanup_retryable():
                        capture_closed = audio_capture.close()
                    if capture_closed is False:
                        self.logger.warning(
                            "audio capture close result=persistent_incomplete "
                            "origin=app_shutdown action=retain_until_process_exit"
                        )
            except Exception:  # noqa: BLE001 - continue with remaining owned resources
                self.logger.warning("audio capture close result=error origin=app_shutdown")
            self._recording = False

        if getattr(self, "audio_policy", None) is not None:
            try:
                self._close_audio_policy()
            except Exception:  # noqa: BLE001 - continue with remaining owned resources
                self.logger.warning(
                    "audio policy close result=error origin=app_shutdown"
                )

        if audio_feedback is not None:
            try:
                audio_feedback.close()
            except Exception:  # noqa: BLE001 - continue with remaining owned resources
                self.logger.warning(
                    "audio feedback close result=error origin=app_shutdown"
                )

        # Restore any adapter-owned clipboard/modifier state before closing the
        # keyboard owner that performs the exact synthetic releases.
        if text_injector is not None:
            try:
                text_injector.close()
            except Exception:  # noqa: BLE001 - continue owned-resource cleanup
                self.logger.warning(
                    "delivery adapter close result=error origin=app_shutdown"
                )

        # Close the global-hook owner after active capture and delivery have
        # been cancelled.
        # A held PTT reducer emits its balancing hold-end while the dispatch
        # worker drains, but it cannot finalize cancelled audio or leave a hook
        # alive after ordinary quit or an unhandled backend exception.
        if keyboard is not None:
            try:
                recovery = getattr(self, "_shortcut_recovery", None)
                if recovery is not None:
                    recovery.close()
                else:
                    keyboard.close()
            except Exception:  # noqa: BLE001 - continue with remaining owned resources
                self.logger.warning("hotkey adapter close result=error origin=app_shutdown")

        if stt_engine is not None:
            try:
                stt_engine.shutdown()
            except Exception:  # noqa: BLE001 - teardown must remain idempotent
                self.logger.warning("stt engine shutdown result=error")

        if executor is not None:
            try:
                # Finalizers may enqueue persistence while draining, so keep
                # the separate persistence executor live until this completes.
                executor.shutdown(wait=True)
            except Exception:  # noqa: BLE001 - teardown remains best effort
                self.logger.warning("transcription executor shutdown result=error")
        self._executor = None

        if import_executor is not None:
            try:
                import_executor.shutdown(wait=True, cancel_futures=True)
            except Exception:  # noqa: BLE001 - teardown remains best effort
                self.logger.warning("import executor shutdown result=error")
        self._import_executor = None

        if persistence_executor is not None:
            try:
                persistence_executor.shutdown(wait=True)
            except Exception:  # noqa: BLE001 - teardown remains best effort
                self.logger.warning("persistence executor shutdown result=error")
        self._persistence_executor = None

        self.stt_engine = None
        self._loaded_stt_artifact_id = None
        self._loaded_stt_routing_state = None

        if application_context is not None:
            try:
                application_context.close()
            except Exception:  # noqa: BLE001 - metadata cleanup remains best effort
                self.logger.warning("application context close result=error")

        with getattr(self, "_transcript_store_publish_lock", nullcontext()):
            store = getattr(self, "transcript_store", None)
            self.transcript_store = None
        if store is not None:
            try:
                store.clear_data_key()
            except Exception:  # noqa: BLE001 - key cleanup is best effort
                pass
            try:
                store.close()
            except Exception:  # noqa: BLE001 - teardown remains best effort
                self.logger.warning("transcript store close result=error")

        self._last_waveform = None
        self._chunk_futures = []
        self._agent_compose_recording = None
        self._agent_compose_pending_generation = None
        injector = getattr(self, "text_injector", None)
        if injector is not None:
            # Paste-last content is memory-only; process exit is its terminal
            # lifecycle, so release both processed and original copies.
            injector._last_transcript = None
            injector._last_original = None

    def _persistence_submission_token(self) -> int | None:
        lock = getattr(self, "_persistence_epoch_lock", None)
        if lock is None:
            if not bool(getattr(self, "_persistence_accepting", True)):
                return None
            return int(getattr(self, "_persistence_epoch", 0))
        with lock:
            if not bool(getattr(self, "_persistence_accepting", True)):
                return None
            return int(getattr(self, "_persistence_epoch", 0))

    def _persistence_token_is_current(self, token: int) -> bool:
        lock = getattr(self, "_persistence_epoch_lock", None)
        if lock is None:
            return bool(getattr(self, "_persistence_accepting", True)) and (
                token == int(getattr(self, "_persistence_epoch", 0))
            )
        with lock:
            return bool(getattr(self, "_persistence_accepting", True)) and (
                token == int(getattr(self, "_persistence_epoch", 0))
            )

    def begin_persistence_transition(self) -> None:
        """Close submission, revoke queued saves, and drain the old epoch."""
        lock = getattr(self, "_persistence_epoch_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._persistence_epoch_lock = lock
        with lock:
            self._persistence_accepting = False
            self._persistence_epoch = int(
                getattr(self, "_persistence_epoch", 0)
            ) + 1
        executor = getattr(self, "_persistence_executor", None)
        if executor is None:
            return
        # FIFO single-worker ordering makes this a boundary: earlier closures
        # either finished before Lockdown or observe the revoked epoch and drop
        # their content before this sentinel completes.
        executor.submit(lambda: None).result()

    def end_persistence_transition(self) -> None:
        """Re-open saves only after the caller has committed transition state."""
        lock = getattr(self, "_persistence_epoch_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._persistence_epoch_lock = lock
        with lock:
            self._persistence_accepting = True

    def run(self, *, register_hotkeys: bool = True) -> None:
        standalone_native = sys.platform == "darwin" and not getattr(self, "_file_only", False) and threading.current_thread() is threading.main_thread() and getattr(self, "_server_loop", None) is None
        if standalone_native:
            from .macos_native_query_owner import bind_owner
            standalone_owner = bind_owner()
        try:
            recovery = getattr(self, "_shortcut_recovery", None)
            if recovery is not None:
                recovery.enabled = register_hotkeys
            if self._file_only:
                if register_hotkeys:
                    raise RuntimeError("file-only backend cannot register hotkeys")
                self.logger.info("app ready hotkeys=disabled mode=file_only")
                self._file_only_stop.wait()
                return
            if register_hotkeys:
                self._register_hotkey()
                hotkey_state = recovery.snapshot()["state"] if recovery is not None else "ready"
                self.logger.info("app ready hotkeys=%s", hotkey_state)
            else:
                self.logger.info("app ready hotkeys=disabled")
            self.keyboard.wait()
        except KeyboardInterrupt:
            self.logger.info("app interrupted action=exit")
        finally:
            if standalone_native:
                from .macos_native_query_owner import close_owner
                close_owner(expected_owner=standalone_owner)

    def _register_hotkey(self) -> None:
        recovery = getattr(self, "_shortcut_recovery", None)
        if recovery is not None:
            recovery.reconcile(force=True)
            return
        self._register_hotkey_bindings()

    def _register_hotkey_bindings(self) -> None:
        # Server construction only associates the routed-reply owner. Install
        # its selective handlers here so --no-hotkeys cannot start or populate
        # a native listener.
        self._register_agent_reply_early_handlers()

        # PTT and toggle are co-resident, independently-bound slots. Register
        # each only when its shortcut is non-empty; an empty string means that
        # hotkey is unbound and never fires.
        ptt_shortcut = self.config.push_to_talk_shortcut
        if ptt_shortcut:
            self.logger.debug("hotkey registering mode=push_to_talk")
            try:
                self.keyboard.register_push_to_talk(
                    ptt_shortcut, self._on_hotkey_press, self._on_hotkey_release
                )
            except Exception as exc:
                if sys.platform == "darwin":
                    reason = (
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "invalid_binding"
                        if isinstance(exc, ValueError)
                        else "native_error"
                    )
                    self.logger.error(
                        "hotkey registration failed reason=%s hint=review_privacy_settings",
                        reason,
                    )
                else:
                    self.logger.error(
                        "hotkey registration failed reason=permission_denied "
                        "hint=run_as_administrator"
                    )
                raise

        toggle_shortcut = self.config.toggle_shortcut
        if toggle_shortcut:
            self.logger.debug("hotkey registering mode=toggle")
            try:
                self.keyboard.register_toggle(
                    toggle_shortcut, self._on_toggle_start, self._on_toggle_stop
                )
            except Exception as exc:
                if sys.platform == "darwin":
                    reason = (
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "invalid_binding"
                        if isinstance(exc, ValueError)
                        else "native_error"
                    )
                    self.logger.error(
                        "hotkey registration failed reason=%s hint=review_privacy_settings",
                        reason,
                    )
                else:
                    self.logger.error(
                        "hotkey registration failed reason=permission_denied "
                        "hint=run_as_administrator"
                    )
                raise

        # Register the paste-last extra hotkey (independent of the primary)
        paste_shortcut = self.config.paste_last_shortcut
        try:
            self.keyboard.register_extra_hotkey(paste_shortcut, self._on_paste_last_press)
        except Exception:
            self.logger.warning("hotkey registration failed mode=paste_last")

        # Register the paste-last-original extra hotkey (independent slot)
        try:
            self.keyboard.register_extra_hotkey(
                self.config.paste_last_original_shortcut,
                self._on_paste_last_original_press,
                key="paste_last_original",
            )
        except Exception:
            self.logger.warning("hotkey registration failed mode=paste_last_original")

        try:
            self.keyboard.register_extra_hotkey(
                self.config.claude_view_shortcut,
                self._on_claude_view_press,
                key="claude_view",
            )
        except Exception:
            self.logger.warning("hotkey registration failed mode=claude_view")

        # Register the per-profile manual-switch hotkeys (issue 14). They ride the
        # primary listener started above, so register them after the primary.
        self._register_profile_hotkeys()

    def _on_hotkey_press(self) -> None:
        """Handle hotkey press - start recording if not already recording."""
        if getattr(self, "_shutting_down", False):
            self.logger.info("hotkey press ignored mode=push_to_talk reason=shutting_down")
            return
        if not self._require_acceptance():
            self.logger.info("hotkey press ignored mode=push_to_talk reason=acceptance_blocked")
            return
        if self._start_routed_reply("push_to_talk"):
            return
        with self._lock:
            self._start_recording_for_origin("push_to_talk")

    def _on_claude_view_press(self) -> None:
        """Ask the persistent HUD to toggle its current contextual expansion."""
        if not self._require_acceptance():
            return
        try:
            self._broadcast({"type": "agent-hud-view"})
        except Exception:
            self.logger.warning("hotkey action failed mode=claude_view")

    def _on_hotkey_release(self) -> None:
        """Handle hotkey release - stop recording if currently recording."""
        if getattr(self, "_shutting_down", False):
            return
        if self._stop_routed_reply("push_to_talk"):
            return
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return
            if self._recording and getattr(
                self,
                "_recording_origin",
                None,
            ) in {
                None,
                "push_to_talk",
            }:
                self._attempt_restore_system_volume()
                self._stop_recording()

    def _on_toggle_start(self) -> None:
        """Handle toggle activation — start recording."""
        if getattr(self, "_shutting_down", False) or not self._require_acceptance():
            return
        if self._start_routed_reply("toggle"):
            return
        with self._lock:
            self._start_recording_for_origin("toggle")

    def _start_recording_for_origin(self, origin: str) -> str:
        """Start the shipping capture pipeline for one activation surface.

        Must be called while ``self._lock`` is held. The returned status is
        content-free and is used by the production REST command surface.
        """
        if getattr(self, "_shutting_down", False):
            return "recording_not_started"
        if origin not in _DICTATION_ORIGINS:
            raise ValueError(f"invalid dictation origin: {origin}")
        grant = self._current_acceptance_grant()
        if not self._require_acceptance(grant):
            return _EULA_BLOCK_CODE
        self.initialize_accepted_content_services()
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._recording:
            return "recording_active"
        if not self._recording_can_start():
            return "evaluation_active"

        self._recording_origin = origin
        self._started_via_toggle = origin in {"toggle", "hud"}
        self._recording_acceptance_grant = grant
        if self._try_begin_recording():
            return "started"

        self._recording_origin = None
        self._started_via_toggle = False
        self._recording_acceptance_grant = None
        return "recording_not_started"

    def start_hud_dictation(self) -> str:
        """Start dictation from the enabled persistent HUD control."""
        if not self._require_acceptance():
            return _EULA_BLOCK_CODE
        with self._lock:
            if not (
                bool(getattr(self.config, "show_recording_indicator", True))
                and bool(getattr(self.config, "show_recording_control", False))
            ):
                return "hud_control_disabled"
            return self._start_recording_for_origin("hud")

    def complete_hud_dictation(self) -> str:
        """Finish and deliver only the active HUD-origin recording."""
        with self._lock:
            if not self._recording or self._recording_origin != "hud":
                return "not_hud_recording"
            if not self._acceptance_is_current(
                getattr(self, "_recording_acceptance_grant", None)
            ):
                self._broadcast_acceptance_blocked()
                self._cancel_recording(
                    trigger="eula_revoked", play_feedback=False
                )
                return _EULA_BLOCK_CODE
            self._attempt_restore_system_volume()
            self._stop_recording()
            return "completed"

    def cancel_hud_dictation(self) -> str:
        """Discard only the active HUD-origin recording without finalization."""
        with self._lock:
            if not self._recording or self._recording_origin != "hud":
                return "not_hud_recording"
            self._cancel_recording()
            return "cancelled"

    def _begin_recording_startup_timing(self) -> None:
        """Start content-free, per-attempt timing for support diagnostics."""
        sequence = int(getattr(self, "_recording_startup_sequence", 0)) + 1
        now = time.monotonic()
        self._recording_startup_sequence = sequence
        self._recording_startup_started_at = now
        self._recording_startup_checkpoint_at = now
        self._recording_startup_timing_active = True
        self.logger.debug(
            "recording startup checkpoint startup_seq=%d phase=precheck_complete "
            "elapsed_ms=0 delta_ms=0",
            sequence,
        )

    def _recording_startup_checkpoint(
        self,
        phase: str,
        *,
        terminal: bool = False,
    ) -> None:
        """Log one monotonic startup checkpoint without audio-derived data."""
        if not getattr(self, "_recording_startup_timing_active", False):
            self._begin_recording_startup_timing()
        now = time.monotonic()
        started_at = float(self._recording_startup_started_at)
        checkpoint_at = float(self._recording_startup_checkpoint_at)
        self.logger.debug(
            "recording startup checkpoint startup_seq=%d phase=%s "
            "elapsed_ms=%d delta_ms=%d",
            int(self._recording_startup_sequence),
            phase,
            round((now - started_at) * 1000),
            round((now - checkpoint_at) * 1000),
        )
        self._recording_startup_checkpoint_at = now
        if terminal:
            self._recording_startup_timing_active = False

    def _try_begin_recording(self) -> bool:
        """Attempt to begin recording after license checks have already passed.

        Runs the input-device precheck BEFORE the start chime, so the chime
        never plays when recording is guaranteed to fail. On precheck failure,
        plays the stop chime, dispatches the error dialog, and returns without
        touching the system volume. On success, runs the full start sequence:
        audio capture in discard mode, start chime, capture arming, then volume
        reduction on the playback endpoint active after capture starts.

        Must be called from within self._lock and only when not self._recording.
        """
        if getattr(self, "_shutting_down", False):
            return False
        if not self._require_acceptance(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            return False
        if self._capture_unavailable():
            self._report_capture_failure()
            return False
        if not self._ensure_audio_capture_selection_locked():
            self.logger.warning(
                "audio capture start failed reason=replacement_incomplete"
            )
            self._report_capture_failure()
            return False
        if not has_input_device():
            self.logger.warning("recording aborted reason=no_input_device")
            self._reserve_audio_policy_generation()
            self._play_recording_chime(ChimeKind.STOP)
            self._close_audio_policy()
            c = COPY["no_input_device"]
            self._dispatch_error_dialog(
                "no_input_device", c["title"], c["message"], retry_action="rescan_devices"
            )
            return False
        self._begin_recording_startup_timing()
        return self._start_recording()

    def _on_toggle_stop(self) -> None:
        if self._stop_routed_reply("toggle"):
            return
        self._stop_ordinary_toggle()

    def _stop_ordinary_toggle(self) -> None:
        """Handle toggle deactivation — stop recording."""
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return
            if (
                self._recording
                and getattr(self, "_recording_origin", None) == "toggle"
            ):
                self._attempt_restore_system_volume()
                self._stop_recording()

    def _on_audio_level(self, level: float) -> None:
        """Dispatch audio level to broadcaster (~20fps throttle)."""
        if not self._acceptance_is_current(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            self._schedule_acceptance_revocation_cancel()
            return
        now = time.monotonic()
        if now - self._last_level_broadcast >= 0.05:  # ~20fps
            self._last_level_broadcast = now
            self._broadcast({"type": "audio.levels", "rms": round(level, 3), "peak": round(min(level * 1.5, 1.0), 3)})

    def _on_preview_audio(self) -> None:
        """Cheap capture-thread signal to the current preview coordinator."""
        if not self._acceptance_is_current(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            self._schedule_acceptance_revocation_cancel()
            return
        with self._live_preview_lock:
            coordinator = self._live_preview_coordinator
        if coordinator is not None:
            coordinator.notify_audio()

    def _start_live_preview(self, recording_id: int, stt_manager: object) -> None:
        """Install one isolated preview coordinator for a recording generation."""
        if not self._require_acceptance(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            return
        sample_rate = getattr(self.audio_capture, "sample_rate", 16_000)
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            sample_rate = 16_000
        capabilities = getattr(stt_manager, "capabilities", None)
        preview_support = getattr(capabilities, "preview", None)
        coordinator = LivePreviewCoordinator(
            recording_id=recording_id,
            stt_manager=stt_manager,
            snapshot_provider=self.audio_capture.preview_snapshot,
            publish=self._publish_live_preview,
            sample_rate=sample_rate,
            text_only=(
                getattr(preview_support, "metadata", None)
                is SttPreviewMetadata.TEXT
            ),
        )
        with self._live_preview_lock:
            previous = self._live_preview_coordinator
            self._live_preview_coordinator = coordinator
        if previous is not None:
            previous.invalidate()
        coordinator.start()

    def _invalidate_live_preview(self) -> None:
        """Logically cancel preview immediately without joining native ASR."""
        if not hasattr(self, "_live_preview_lock"):
            return
        with self._live_preview_lock:
            coordinator = self._live_preview_coordinator
            self._live_preview_coordinator = None
        if coordinator is not None:
            coordinator.invalidate()
        # AudioCapture drops the bounded ring; an already-running decode owns
        # its independent snapshot and can only be discarded on completion.
        if self.audio_capture is not None:
            self.audio_capture.set_preview_callback(None)

    def _freeze_live_preview(self) -> None:
        """Publish the latest complete hypothesis without starting more ASR."""
        if not hasattr(self, "_live_preview_lock"):
            return
        with self._live_preview_lock:
            coordinator = self._live_preview_coordinator
        if coordinator is not None:
            coordinator.freeze()

    def _publish_live_preview(
        self,
        recording_id: int,
        revision: int,
        update: PreviewUpdate,
    ) -> None:
        """Publish raw preview text only for the active recording generation."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._require_acceptance(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            self._invalidate_live_preview()
            return
        with self._live_preview_lock:
            coordinator = self._live_preview_coordinator
            if coordinator is None or coordinator.recording_id != recording_id:
                return
            state = self.dictation_state_snapshot()
            if (
                state["recording_id"] != recording_id
                or state["phase"] != "recording"
            ):
                return
            compose_lease = getattr(
                self, "_agent_compose_recording", None
            )
            leases = getattr(self, "_agent_compose_leases", None)
            try:
                compose_current = bool(
                    compose_lease is not None
                    and compose_lease.recording_id == recording_id
                    and leases is not None
                    and leases.is_current(
                        compose_lease.generation,
                        recording_id=recording_id,
                    )
                )
            except Exception:
                compose_current = False
            if not state["preview_enabled"] and not compose_current:
                return
            if state["preview_enabled"] and not compose_current:
                self._broadcast(
                    {
                        "type": "transcription.preview",
                        "recording_id": recording_id,
                        "revision": revision,
                        "text": update.text,
                        "stable_char_count": update.stable_char_count,
                        "presentation_version": update.presentation_version,
                        "mutable_start_char_count": (
                            update.mutable_start_char_count
                        ),
                        "freeze": update.freeze,
                    }
                )
            if compose_current:
                self._broadcast(
                    {
                        "type": "agent.compose.preview",
                        "version": 1,
                        "session_handle": compose_lease.session_handle,
                        "generation": compose_lease.generation,
                        "recording_id": recording_id,
                        "revision": revision,
                        "text": update.text,
                        "stable_char_count": update.stable_char_count,
                        "presentation_version": update.presentation_version,
                        "mutable_start_char_count": (
                            update.mutable_start_char_count
                        ),
                        "freeze": update.freeze,
                    }
                )

    def _bind_recording_language(self):
        """Resolve the profile hint even when cleanup is disabled."""
        from .language_routing import artifact_supports_language
        from .supervised_stt_manager import SupervisedSttManager

        manager = self.parakeet
        if not isinstance(manager, SupervisedSttManager):
            return manager
        profile_id = self._resolve_active_profile() or self.config.default_profile_id
        profile = next(
            (p for p in self.config.profiles if p.get("id") == profile_id), {}
        )
        artifact = (
            getattr(self, "_loaded_stt_artifact_id", None)
            or _stt_artifact_id(self.config)
        )
        override = profile.get("language")
        language = (
            override if override and artifact_supports_language(artifact, override)
            else None
        )
        # Cohere always needs a hint. Qwen also freezes its auto-detect choice.
        if language is None:
            if artifact == ModelId.COHERE_TRANSCRIBE_INT8:
                language = self.config.language
            elif artifact == ModelId.QWEN3_ASR_06B_INT8:
                selection = _selection_from_config(self.config)
                language = self.config.language if selection.qwen_force_language else ""
        return manager.for_language(language) if language is not None else manager

    def _start_recording(self) -> bool:
        if getattr(self, "_shutting_down", False):
            return False
        if getattr(self, "_stt_swap_in_progress", False):
            return False
        # A previous terminal path must never leave routing identity attached to
        # a later failed/prechecked recording attempt.
        self._dictation_app = None
        # close() retires a recording generation, not the app-lifetime adapter.
        # Every new attempt needs a fresh generation, including direct callers.
        self._reserve_audio_policy_generation()
        self._recording_stt_manager = None
        if not self._recording_can_start():
            return False
        if not self._require_acceptance(
            getattr(self, "_recording_acceptance_grant", None)
        ):
            return False
        # Barge-in: stop any agent speech the instant PTT engages. Non-blocking,
        # never raises; no-op when the experimental gate is off (controller is None).
        if self.speech_controller is not None:
            self.speech_controller.stop()

        origin = getattr(self, "_recording_origin", None)
        if origin not in _DICTATION_ORIGINS:
            # Compatibility for direct/private test and tooling callers that
            # predate the origin-aware public entry points.
            origin = (
                "toggle"
                if getattr(self, "_started_via_toggle", False)
                else "push_to_talk"
            )
            self._recording_origin = origin

        # Clean slate for the new recording's chunking state.
        self._chunk_futures = []
        self._chunk_start_time = 0.0
        self._last_waveform = None

        # Evaluate the full runtime gate before capture starts so disabled,
        # hidden-HUD, screen-reader, and missing-model paths allocate no preview
        # ring and can never submit preview inference.
        self._invalidate_live_preview()
        screen_reader_detected = screen_reader_active()
        hud_preview_enabled = (
            bool(getattr(self.config, "show_recording_indicator", True))
            and bool(getattr(self.config, "show_live_preview", False))
            and not screen_reader_detected
            and self.stt_engine is not None
        )
        leases = getattr(self, "_agent_compose_leases", None)
        pending_generation = getattr(
            self, "_agent_compose_pending_generation", None
        )
        try:
            compose_preview_enabled = bool(
                self.stt_engine is not None
                and leases is not None
                and pending_generation is not None
                and leases.is_current(pending_generation)
            )
        except Exception:
            compose_preview_enabled = False
        preview_capture_enabled = (
            hud_preview_enabled or compose_preview_enabled
        )
        self.audio_capture.set_preview_callback(
            self._on_preview_audio if preview_capture_enabled else None,
            max_duration=MAX_PREVIEW_WINDOW_SECONDS,
        )

        process_needed, site_needed = routing_probe_requirements(
            self.config,
            manual_profile_id=getattr(self, "_manual_profile_id", None),
        )
        if process_needed:
            self._dictation_app = self._capture_application_context(
                include_browser_domain=site_needed
            )
        self._recording_stt_manager = WhisperstreamApp._bind_recording_language(self)

        self.logger.debug("audio capture starting")
        self.audio_capture.set_level_callback(self._on_audio_level)
        self._broadcast(
            {
                "type": "state.arming",
                "active": True,
                "hud_enabled": bool(
                    getattr(self.config, "show_recording_indicator", True)
                ),
                "screen_reader_detected": screen_reader_detected,
                # A reply capture keeps the agent's own surface; the generic
                # arming pill would stand in for it while the chime plays.
                "agent_compose": getattr(
                    self, "_agent_compose_pending_generation", None
                ) is not None,
            }
        )
        self._recording_startup_checkpoint("capture_open_begin")
        self._active_capture_session_key = None
        try:
            self.audio_capture.start(discard_until_armed=True)
            capture_session = getattr(self.audio_capture, "active_session", None)
            owner_epoch = getattr(self.audio_capture, "owner_epoch", None)
            if (
                isinstance(capture_session, CaptureSession)
                and isinstance(owner_epoch, int)
                and not isinstance(owner_epoch, bool)
                and owner_epoch > 0
            ):
                self._active_capture_session_key = (
                    owner_epoch,
                    capture_session.request_id,
                    capture_session.generation,
                )
        except Exception as exc:
            self._broadcast({"type": "state.arming", "active": False})
            self._recording_startup_checkpoint("capture_open_failed", terminal=True)
            self.audio_capture.set_preview_callback(None)
            self._active_capture_session_key = None
            self.logger.error("audio capture start failed reason=%s", type(exc).__name__)
            self.logger.info("recording aborted reason=audio_capture_failed")
            self._play_recording_chime(ChimeKind.STOP)
            self._close_audio_policy()
            self._report_capture_failure()
            return False
        self._recording_startup_checkpoint("capture_open_complete")
        self._broadcast({"type": "state.arming", "active": False})

        # The stream is open but its callback is still discarding every input
        # block. This moves device-start latency ahead of the user-facing cue
        # without allowing the cue, preview work, chunking, or levels into the
        # recording. Arm capture before returning from the blocking cue so
        # speech begun at the audible boundary is eligible immediately.
        self._recording_startup_checkpoint("start_chime_begin")
        try:
            self._play_recording_chime(ChimeKind.START)
        finally:
            self.audio_capture.arm_capture()
        self._recording_startup_checkpoint("start_chime_complete")
        self._recording = True
        self._recording_startup_checkpoint("recording_armed")

        # Opening a Bluetooth Classic microphone can switch Windows playback
        # from the A2DP endpoint to the HFP endpoint. Drop any cached endpoint
        # only after capture has successfully opened, then let volume handling
        # reacquire and change the playback path that is active for recording.
        # On capture failure, volume is therefore left untouched.
        self._reduce_system_volume()

        self._recording_start_time = time.monotonic()
        # Kick the model load only when it exists. When the model has not been
        # prepared yet (self.stt_engine is None, e.g. a fresh install before the
        # download completes, or a data dir whose model went missing) recording
        # still proceeds: audio is captured and the HUD shows, while the
        # downstream guards in _on_chunk_ready / _finalize_transcription_and_inject
        # skip transcription. Without this guard a not-yet-ready model raised
        # AttributeError here, crashing the hotkey callback BEFORE the
        # state.recording broadcast below and suppressing the HUD entirely.
        if self.stt_engine is not None:
            self._executor.submit(self.stt_engine.ensure_loaded)
        # Lazy-warm the local LLM supervisor (if local enhancement is on).
        # kick_warm is idempotent — duplicate calls during recording are no-ops.
        # mark_active resets the idle-offload timer so an active user keeps
        # the model resident across consecutive dictations.
        if (
            self.config.enhancement_enabled
            and (self.config.effective_provider() or "").lower() == "local"
            and self.llama_server is not None
            and self._server_loop is not None
        ):
            self.llama_server.kick_warm(self._server_loop)
            self.llama_server.mark_active()
        # Carry the live screen-reader flag so the UI can suppress the HUD overlay
        # for this dictation. The HUD is a WebView2 window; when a screen reader
        # is running, that window appearing yanks NVDA/JAWS into browse mode on
        # our own window and breaks the user's typing. Evaluated per-recording so
        # a screen reader launched mid-session is honoured immediately (the
        # config snapshot the UI loads at startup would miss that).
        recording_id = self._begin_dictation_state(
            origin=origin,
            screen_reader_detected=screen_reader_detected,
        )
        if preview_capture_enabled:
            self._start_live_preview(recording_id, self._recording_stt_manager)
        self._broadcast({
            "type": "state.recording",
            "recording": True,
            "screen_reader_detected": screen_reader_detected,
        })
        self._recording_startup_checkpoint("indicator_signal_sent", terminal=True)
        self.logger.info(
            "recording started trigger=%s device_selection=%s sample_rate=%d routed_app=%s",
            origin,
            (
                "configured"
                if getattr(self.config, "audio_input_device", "")
                else "system_default"
            ),
            self.audio_capture.sample_rate,
            str(self._dictation_app is not None).lower(),
        )

        # PTT recordings NEVER start a timer. Only a recording started via the
        # toggle hotkey uses the safety timeout (a held PTT key auto-stops on
        # release, so it can't run away).
        if (
            self._started_via_toggle
            and self.config.toggle_safety_timeout > 0
        ):
            self._stop_timer = threading.Timer(
                self.config.toggle_safety_timeout,
                self._handle_timeout,
                args=(recording_id,),
            )
            self._stop_timer.start()
        return True

    def _handle_timeout(self, recording_id: int) -> None:
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return
            current = self.dictation_state_snapshot()
            if (
                self._recording
                and current["recording_id"] == recording_id
                and current["phase"] == "recording"
            ):
                self.logger.info("recording aborted reason=safety_timeout")
                self._attempt_restore_system_volume()
                self._stop_recording()

    def _on_chunk_ready(self, chunk: np.ndarray) -> None:
        """Callback invoked by AudioCapture when a full chunk is available."""
        grant = getattr(self, "_recording_acceptance_grant", None)
        if not self._acceptance_is_current(grant):
            self._schedule_acceptance_revocation_cancel()
            return
        chunk_lock = getattr(self, "_chunk_futures_lock", None)
        if chunk_lock is None:
            chunk_lock = threading.Lock()
            self._chunk_futures_lock = chunk_lock
        with chunk_lock:
            if (getattr(self, "_discarding_recording", False)
                    or getattr(self, "_shutting_down", False)
                    or getattr(self, "_recording", True) is False):
                self.logger.debug("chunk skipped reason=dictation_cancelled")
                return
            stt_manager = getattr(self, "_recording_stt_manager", None) or self.stt_engine
            if stt_manager is None:
                self.logger.warning("chunk skipped reason=model_not_loaded")
                return
            if self._chunk_start_time == 0.0:
                self._chunk_start_time = time.perf_counter()
            chunk_audio_s = chunk.size / 16_000.0
            log_transcription_event(
                self.logger, "chunk_in",
                duration_s=chunk_audio_s,
                level=logging.DEBUG,
            )
            # Capture the recognizer with the future. A model swap after this
            # callback must not redirect an already-recorded authoritative chunk.
            future = self._executor.submit(
                self._transcribe_chunk_worker, stt_manager, chunk, grant
            )
            self._chunk_futures.append(future)

    def _transcribe_chunk_worker(
        self,
        stt_manager,
        chunk: np.ndarray,
        acceptance_grant: AcceptanceGrant | None = None,
    ) -> str | None:
        """Run in the executor; transcribes a single chunk and returns the text."""
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._require_acceptance(acceptance_grant):
            return None
        try:
            t0 = time.perf_counter()
            text = stt_manager.transcribe_chunk(chunk, sample_rate=16_000)
            if not self._require_acceptance(acceptance_grant):
                return None
            elapsed = time.perf_counter() - t0
            log_transcription_event(
                self.logger, "chunk_out",
                duration_s=elapsed,
                char_count=len(text),
                word_count=len(text.split()) if text.strip() else 0,
                level=logging.DEBUG,
            )
            return text
        except Exception as exc:
            self.logger.error(
                "chunk transcription failed error_type=%s", type(exc).__name__
            )
            raise

    def _capture_finalization_context(
        self,
        *,
        recording_id: int | None,
        waveform: np.ndarray | None,
        dictation_app: ContextSnapshot | RoutingTarget | None | object = _USE_CURRENT_ROUTING,
    ) -> _DictationFinalizationContext:
        """Freeze every per-recording input before queueing finalization."""
        screen_reader_detected = False
        if recording_id is not None:
            state = self.dictation_state_snapshot()
            if state["recording_id"] == recording_id:
                screen_reader_detected = bool(state["screen_reader_detected"])
        else:
            # Direct/private callers have no lifecycle snapshot; retain their
            # historical live accessibility routing behavior.
            screen_reader_detected = screen_reader_active()
        return _DictationFinalizationContext(
            recording_id=recording_id,
            waveform=waveform,
            chunk_futures=tuple(getattr(self, "_chunk_futures", ())),
            transcription_started_at=float(
                getattr(self, "_chunk_start_time", 0.0)
            ),
            recording_started_at=float(
                getattr(self, "_recording_start_time", 0.0)
            ),
            stt_manager=(
                getattr(self, "_recording_stt_manager", None) or self.stt_engine
                if getattr(self, "stt_engine", None) is not None else None
            ),
            dictation_app=(
                getattr(self, "_dictation_app", None)
                if dictation_app is _USE_CURRENT_ROUTING
                else dictation_app
            ),
            manual_profile_id=getattr(self, "_manual_profile_id", None),
            override_anchor=getattr(self, "_override_anchor", None),
            screen_reader_detected=screen_reader_detected,
            inject_suppressed=bool(
                getattr(self, "_inject_suppressed", False)
            ),
            agent_compose_lease=(
                getattr(self, "_agent_compose_recording", None)
                if recording_id is not None
                and getattr(
                    getattr(self, "_agent_compose_recording", None),
                    "recording_id",
                    None,
                )
                == recording_id
                else None
            ),
            persistence_ready=threading.Event(),
            acceptance_grant=getattr(
                self, "_recording_acceptance_grant", None
            ) or self._current_acceptance_grant(),
        )

    def _cancel_recording(
        self, *, trigger: str = "hud", play_feedback: bool = True,
        capture_already_settled: bool = False
    ) -> None:
        """Stop capture and discard a recording without entering finalization.

        Must be called while ``self._lock`` is held. This path is intentionally
        separate from ``_stop_recording``: no waveform, transcript, stats,
        review or history work may survive a real discard.
        """
        if getattr(self, "_shutting_down", False):
            return
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

        self._dictation_app = None
        recording_id = self.dictation_state_snapshot()["recording_id"]
        compose_lease = getattr(self, "_agent_compose_recording", None)

        # Invalidate preview before closing capture so late native results can
        # no longer publish. Restore the exact recording-time playback endpoint
        # before microphone close can switch Bluetooth profiles.
        self._discarding_recording = True
        self._active_capture_session_key = None
        self._invalidate_live_preview()
        try:
            self._attempt_restore_system_volume()
        except Exception:  # noqa: BLE001 - restoration attempt cannot strand capture
            self.logger.warning(
                "volume restore result=error origin=recording_cancel"
            )
        try:
            self.audio_capture.set_level_callback(None)
        except Exception:  # noqa: BLE001 - terminal cleanup must continue
            self.logger.warning(
                "audio level callback clear result=error origin=recording_cancel"
            )

        # Cancel queued authoritative chunk work before AudioCapture.cancel joins
        # its dispatch worker. A native decode already running cannot be
        # interrupted safely, but its result is dropped with every other handle.
        chunk_lock = getattr(self, "_chunk_futures_lock", None)
        if chunk_lock is None:
            chunk_lock = threading.Lock()
            self._chunk_futures_lock = chunk_lock
        with chunk_lock:
            for future in tuple(getattr(self, "_chunk_futures", ())):
                future.cancel()
            self._chunk_futures = []

        fallback_close = False
        try:
            active_session = getattr(self.audio_capture, "active_session", None)
            typed_cancel = getattr(self.audio_capture, "cancel_capture", None)
            if capture_already_settled:
                fallback_close = self._capture_cleanup_retryable()
            elif (
                trigger == "device_lost"
                and isinstance(active_session, CaptureSession)
                and callable(typed_cancel)
            ):
                completion = typed_cancel(active_session)
                if (
                    completion.status is CaptureCompletionStatus.FAILED
                    and completion.failure is not CaptureFailure.DEVICE_LOST
                ):
                    fallback_close = True
                    self.logger.warning(
                        "recording cancel capture cleanup incomplete reason=%s "
                        "action=bounded_close_retry",
                        completion.failure.value
                        if completion.failure is not None
                        else "unknown",
                    )
            else:
                cancel_capture = getattr(self.audio_capture, "cancel", None)
                if callable(cancel_capture):
                    cancel_capture()
                else:
                    self.audio_capture.stop()
        except Exception as exc:
            fallback_close = True
            self.logger.warning(
                "recording cancel capture cleanup failed reason=%s "
                "action=bounded_close_retry",
                type(exc).__name__,
            )
        if fallback_close and self._capture_cleanup_retryable():
            try:
                close_complete = self.audio_capture.close()
                if close_complete is False:
                    self.logger.warning(
                        "recording cancel capture retry result=incomplete"
                    )
            except Exception as exc:  # noqa: BLE001 - terminal idle still wins
                self.logger.warning(
                    "recording cancel capture retry result=error reason=%s",
                    type(exc).__name__,
                )
        self._recording = False
        # AudioCapture.cancel() joins the dispatch worker. Clear again afterward
        # so even a callback that was already crossing the cancellation boundary
        # cannot leave a late native future retained by this recording.
        try:
            with chunk_lock:
                for future in tuple(getattr(self, "_chunk_futures", ())):
                    future.cancel()
                self._chunk_futures = []
        finally:
            self._discarding_recording = False

        # No result is joined, merged, delivered, or persisted. Tail flushes
        # at the cancellation boundary were blocked by _discarding_recording.
        self._chunk_start_time = 0.0
        self._last_waveform = None
        self._recording_start_time = 0.0
        self._started_via_toggle = False
        self._recording_acceptance_grant = None

        if compose_lease is not None:
            self._retire_agent_compose(
                compose_lease,
                state="cancelled",
                reason=trigger,
            )

        if trigger == "device_lost":
            reset_toggle = getattr(
                getattr(self, "keyboard", None),
                "reset_toggle_state",
                None,
            )
            if callable(reset_toggle):
                try:
                    reset_toggle()
                except Exception:
                    self.logger.warning(
                        "shortcut toggle reset result=error origin=device_loss"
                    )

        self._transition_dictation_state(recording_id, "idle")
        self._recording_origin = None
        self._broadcast({"type": "state.recording", "recording": False})
        if play_feedback:
            self._play_recording_chime(ChimeKind.STOP)
        try:
            self._close_audio_policy()
        except Exception:  # noqa: BLE001 - terminal lifecycle already owns idle
            self.logger.warning(
                "audio policy close result=error origin=recording_cancel"
            )
        self.logger.info(
            "recording cancelled trigger=%s",
            trigger,
        )

    def _stop_recording(self) -> None:
        if getattr(self, "_shutting_down", False):
            return
        grant = getattr(self, "_recording_acceptance_grant", None)
        if not self._acceptance_is_current(grant):
            self._broadcast_acceptance_blocked()
            self._cancel_recording(
                trigger="eula_revoked", play_feedback=False
            )
            return
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None

        # Freeze the reduced routing target into this stack frame and release
        # the live app reference before any terminal operation can fail. The
        # finalization context receives this exact snapshot below.
        dictation_app = getattr(self, "_dictation_app", None)
        self._dictation_app = None

        # Promote the already-decoded tail, then invalidate without joining the
        # preview worker. A native decode may finish later, but its generation
        # can no longer publish or enter the authoritative pipeline.
        self._freeze_live_preview()
        self._invalidate_live_preview()
        # Direct/private callers and future terminal surfaces must preserve the
        # same D-045 ordering as hotkey/HUD callers: restore before capture stop.
        # Repeated calls are idempotent inside the generation-bound adapter.
        self._attempt_restore_system_volume()
        try:
            self.audio_capture.set_level_callback(None)
        except Exception as exc:
            self.logger.warning("audio level detach failed reason=%s", type(exc).__name__)
        try:
            if getattr(self.audio_capture, "terminal_operations_owned", False) is True:
                session = self.audio_capture.active_session
                completion = self.audio_capture.stop_capture(session)
                if completion.status is not CaptureCompletionStatus.STOPPED:
                    self._cancel_recording(trigger="capture_failed", play_feedback=False,
                                           capture_already_settled=True)
                    if completion.status is CaptureCompletionStatus.FAILED:
                        self._report_capture_failure()
                    return
                waveform = completion.audio
            else:
                waveform = self.audio_capture.stop()
        except Exception as exc:
            self.logger.warning("recording stop failed reason=%s", type(exc).__name__)
            self._cancel_recording(trigger="capture_failed", play_feedback=False,
                                   capture_already_settled=True)
            self._report_capture_failure()
            return
        self._active_capture_session_key = None
        self._recording = False

        if not self._acceptance_is_current(grant):
            self._broadcast_acceptance_blocked()
            self._chunk_futures = []
            self._chunk_start_time = 0.0
            self._last_waveform = None
            self._recording_start_time = 0.0
            self._dictation_app = None
            self._started_via_toggle = False
            self._recording_origin = None
            self._recording_acceptance_grant = None
            if hasattr(self, "_dictation_state_lock"):
                recording_id = self.dictation_state_snapshot()["recording_id"]
                self._transition_dictation_state(recording_id, "idle")
            self._broadcast({"type": "state.recording", "recording": False})
            self._close_audio_policy()
            return

        # getattr guard keeps long-standing __new__-based unit stubs working;
        # every normally constructed app always has the lifecycle fields.
        has_dictation_state = hasattr(self, "_dictation_state_lock")
        recording_id = (
            self.dictation_state_snapshot()["recording_id"]
            if has_dictation_state
            else None
        )
        if recording_id is not None:
            self._transition_dictation_state(recording_id, "processing")
        compose_lease = getattr(self, "_agent_compose_recording", None)
        if (
            compose_lease is not None
            and compose_lease.recording_id == recording_id
        ):
            self._broadcast(
                {
                    "type": "agent.compose.state",
                    "version": 1,
                    "session_handle": compose_lease.session_handle,
                    "generation": compose_lease.generation,
                    "recording_id": recording_id,
                    "state": "processing",
                }
            )
        self._broadcast({"type": "state.recording", "recording": False})
        self._idle_timer = threading.Timer(5.0, self._check_review_prompt)
        self._idle_timer.start()
        self._play_recording_chime(ChimeKind.STOP)
        self._close_audio_policy()
        recording_start = getattr(self, "_recording_start_time", 0.0)
        duration_s = time.monotonic() - recording_start if recording_start > 0 else 0.0
        paste_target = "none" if getattr(self, "_inject_suppressed", False) else "clipboard"
        self.logger.info(
            "recording stopped duration=%.2fs frames=%d paste=%s",
            duration_s,
            waveform.size,
            paste_target,
        )

        # Keep the legacy mirror only long enough to construct the immutable
        # stop-time context. The queued worker owns its independent waveform
        # reference; release the mutable app slot before submit so even submit
        # failure leaves no app-level audio retained.
        self._last_waveform = waveform
        context = self._capture_finalization_context(
            recording_id=recording_id,
            waveform=waveform,
            dictation_app=dictation_app,
        )
        if getattr(self, "_last_waveform", None) is waveform:
            self._last_waveform = None
        self._recording_origin = None
        self._recording_acceptance_grant = None
        if recording_id is None:
            # Compatibility for lightweight __new__ stubs that predate the
            # lifecycle fields. They still get context-owned finalization.
            self._executor.submit(
                self._finalize_transcription_and_inject, context
            )
        else:
            self._executor.submit(self._finalize_dictation, context)

    def _finalize_dictation(
        self, context: _DictationFinalizationContext | int
    ) -> None:
        """Complete one dictation and terminate only its matching lifecycle."""
        # Keep direct callers that passed the Task 1.1 recording id working. The
        # production stop path always supplies the already-captured context.
        if isinstance(context, int):
            dictation_app = getattr(self, "_dictation_app", None)
            self._dictation_app = None
            context = self._capture_finalization_context(
                recording_id=context,
                waveform=getattr(self, "_last_waveform", None),
                dictation_app=dictation_app,
            )
            if getattr(self, "_last_waveform", None) is context.waveform:
                self._last_waveform = None
        completed = False
        try:
            self._finalize_transcription_and_inject(context)
            completed = True
        finally:
            if getattr(self, "_last_waveform", None) is context.waveform:
                self._last_waveform = None
            compose_lease = context.agent_compose_lease
            if compose_lease is not None:
                self._retire_agent_compose(
                    compose_lease,
                    state="idle" if completed else "cancelled",
                    reason="complete" if completed else "exception",
                )
            if context.recording_id is not None:
                self._transition_dictation_state(context.recording_id, "idle")
            # Persistence uses a separate executor so shutdown can drain
            # finalizers before saves. Preserve the established lifecycle order:
            # delivery and idle publish before the save worker begins.
            context.persistence_ready.set()

    def _retire_agent_compose(
        self,
        lease: Any,
        *,
        state: str,
        reason: str,
    ) -> None:
        """Release one exact recording lease and publish its terminal state."""
        current = getattr(self, "_agent_compose_recording", None)
        if current is not None and current.generation == lease.generation:
            self._agent_compose_recording = None
        leases = getattr(self, "_agent_compose_leases", None)
        if leases is None:
            return
        try:
            terminal = leases.release(generation=lease.generation)
        except Exception:
            return
        if terminal is None:
            return
        self._broadcast(
            {
                "type": "agent.compose.state",
                "version": 1,
                "session_handle": terminal.session_handle,
                "generation": terminal.generation,
                "recording_id": terminal.recording_id,
                "state": state,
                "reason": reason,
            }
        )

    def _finalize_transcription_and_inject(
        self, context: _DictationFinalizationContext | None = None
    ) -> None:
        """Wait on chunk futures, merge transcripts, and inject the result.

        When chunk_futures is empty (chunking disabled or audio too short),
        falls back to single-shot transcription of self._last_waveform.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if context is None:
            # Compatibility for focused tests and tooling that invoke this
            # private method directly. Queued production finalizers always pass
            # an immutable stop-time context.
            dictation_app = getattr(self, "_dictation_app", None)
            self._dictation_app = None
            context = self._capture_finalization_context(
                recording_id=None,
                waveform=getattr(self, "_last_waveform", None),
                dictation_app=dictation_app,
            )
            if getattr(self, "_last_waveform", None) is context.waveform:
                self._last_waveform = None
        grant = context.acceptance_grant
        if not self._require_acceptance(grant):
            for future in context.chunk_futures:
                future.cancel()
            return
        start_time = (
            context.transcription_started_at
            if context.transcription_started_at > 0
            else time.perf_counter()
        )
        waveform = context.waveform
        chunk_futures = context.chunk_futures
        stt_manager = context.stt_manager

        if not chunk_futures:
            # No chunks were emitted (e.g. a very short recording, or audio that
            # ended exactly on a chunk boundary). Transcribe the full waveform
            # through the chunked path (transcribe() splits and merges), so even
            # this fallback never hands a long clip to a single decode.
            if waveform is None or waveform.size == 0:
                self.logger.warning("transcription skipped reason=no_audio_samples")
                # Versioned dictation.state owns the HUD's processing exit; this
                # legacy compatibility copy still needs a matching false event
                # for rolling clients when a quick tap captures zero frames.
                self._broadcast({"type": "state.transcribing", "active": False})
                return
            if stt_manager is None:
                self.logger.warning("transcription skipped reason=model_not_loaded")
                self._broadcast({"type": "state.transcribing", "active": False})
                return
            self._broadcast({"type": "state.transcribing", "active": True})
            try:
                if not self._require_acceptance(grant):
                    return
                text = stt_manager.transcribe(waveform, sample_rate=16_000)
            except SttFailure as exc:
                self.audio_feedback.play_error(self.config.error_sound_path)
                self._broadcast({"type": "state.transcribing", "active": False})
                self._broadcast_stt_failure(exc)
                return
            except Exception as exc:
                self.logger.error(
                    "transcription failed error_type=%s", type(exc).__name__
                )
                self.audio_feedback.play_error(self.config.error_sound_path)
                self._broadcast({"type": "state.transcribing", "active": False})
                self._broadcast({"type": "error", "message": f"Transcription failed: {exc}"})
                return
            if not self._require_acceptance(grant):
                self._broadcast({"type": "state.transcribing", "active": False})
                return
            duration = time.perf_counter() - start_time
            self._last_stt_failure = None
            self._broadcast({"type": "state.transcribing", "active": False})
            log_transcription_event(
                self.logger, "final",
                duration_s=duration,
                char_count=len(text),
                word_count=len(text.split()) if text.strip() else 0,
                truncated=False,
            )
            self._enhance_then_inject(text, duration, context=context)
            return

        # Chunked path: wait for all chunk futures to resolve.
        self._broadcast({"type": "state.transcribing", "active": True})
        concurrent.futures.wait(chunk_futures)
        if not self._require_acceptance(grant):
            self._broadcast({"type": "state.transcribing", "active": False})
            return

        # Collect results in order.
        chunks: list[str] = []
        for i, future in enumerate(chunk_futures):
            try:
                result = future.result()
                if result is None or not self._acceptance_is_current(grant):
                    self._broadcast_acceptance_blocked()
                    self._broadcast({"type": "state.transcribing", "active": False})
                    return
                chunks.append(result)
            except SttFailure as exc:
                self.audio_feedback.play_error(self.config.error_sound_path)
                self._broadcast({"type": "state.transcribing", "active": False})
                self._broadcast_stt_failure(exc)
                return
            except Exception as exc:
                self.logger.error(
                    "chunk transcription failed index=%d error_type=%s",
                    i,
                    type(exc).__name__,
                )
                self.audio_feedback.play_error(self.config.error_sound_path)
                self._broadcast({"type": "state.transcribing", "active": False})
                self._broadcast({"type": "error", "message": f"Transcription failed on chunk {i}: {exc}"})
                return

        if not self._require_acceptance(grant):
            self._broadcast({"type": "state.transcribing", "active": False})
            return
        text = merge_overlapping_transcripts(chunks)
        duration = time.perf_counter() - start_time
        self._last_stt_failure = None
        self._broadcast({"type": "state.transcribing", "active": False})
        log_transcription_event(
            self.logger, "final",
            duration_s=duration,
            char_count=len(text),
            word_count=len(text.split()) if text.strip() else 0,
            truncated=False,
            num_chunks=len(chunks),
        )
        self._enhance_then_inject(text, duration, context=context)

    def _apply_email_signature(
        self, enhanced_text: str, profile_id: str | None
    ) -> str:
        """Append the built-in Email profile's signature to enhanced text.

        The value is stored on ``profile["signature"]`` for config migration
        compatibility, but it is only meaningful for the profile whose id is
        exactly ``"email"``. Pure helper (no I/O). Returns *enhanced_text*
        UNCHANGED unless ALL of:
          1. the effective profile (``profile_id`` if set, else
             ``config.default_profile_id``) is ``"email"``;
          2. that profile exists and has a non-empty ``signature`` after
             stripping (a blank signature disables the feature);
          3. the text does NOT already end in a dictated sign-off (its final
             blank-line-separated block is not already a short closing).

        When all hold, the signature is appended as its own blank-line block:
        ``f"{enhanced_text.rstrip()}\n\n{signature}"``. We do NOT use
        ``StyleGuide.append`` (single-space + global; wrong fit here).
        """
        effective_id = profile_id or self.config.default_profile_id
        if effective_id != "email":
            return enhanced_text

        profile = next(
            (p for p in self.config.profiles if p.get("id") == effective_id),
            None,
        )
        signature = str(profile.get("signature", "")).strip() if profile else ""
        if not signature:
            return enhanced_text

        if ends_with_signoff(enhanced_text):
            return enhanced_text

        return f"{enhanced_text.rstrip()}\n\n{signature}"

    def _snapshot_enhancement_profile(
        self, profile_id: str | None
    ) -> EnhancementProfileSnapshot | None:
        """Freeze the effective profile id/name before any provider call."""

        effective_id = profile_id or self.config.default_profile_id
        if not effective_id:
            return None
        profile = next(
            (
                candidate
                for candidate in self.config.profiles
                if candidate.get("id") == effective_id
            ),
            None,
        )
        if profile is None:
            return None
        profile_name = str(profile.get("name") or effective_id)
        return EnhancementProfileSnapshot(effective_id, profile_name)

    # Shared import/transcript callers use this same pure constructor.
    _enhancement_provenance = staticmethod(enhancement_provenance)

    async def _enhance_with_bounded_outcome(
        self,
        text: str,
        *,
        profile_id: str,
        on_timeout: TimeoutObserver,
        on_error: ErrorObserver,
        chunked: bool = False,
    ) -> EnhancementResult:
        """Call the whole/chunk typed API and enforce its result contract.

        ========================================================================
        TRANSCRIPT DATA - DO NOT LOG
        Never log *text* or the returned transcript text from this helper.
        ========================================================================
        """
        method = (
            self.llm_enhancer.enhance_chunk_with_outcome
            if chunked else self.llm_enhancer.enhance_with_outcome
        )
        result = await method(
            text, profile_id=profile_id, on_timeout=on_timeout, on_error=on_error,
        )
        if not isinstance(result, EnhancementResult):
            raise TypeError("typed enhancer returned an invalid result")
        return result

    def _enhance_then_inject(
        self,
        text: str,
        duration: float,
        *,
        context: _DictationFinalizationContext | None = None,
    ) -> None:
        """Enhance the merged transcript and inject it, broadcasting the result.

        Shared by both transcription paths (single-shot fallback and chunked ASR).
        Routes three ways:
          - enhancement won't run (disabled / no consent / not warm / suppressed)
            or the transcript is short → single-call path (unchanged behavior):
            one enhance() (if applicable), one process(), one inject().
          - enhancement WILL run AND the transcript is long (> threshold) AND
            injection isn't suppressed → chunked path (clean + paste per chunk).
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        persistence_waveform = (
            context.waveform
            if context is not None
            else getattr(self, "_last_waveform", None)
        )
        if getattr(self, "_last_waveform", None) is persistence_waveform:
            self._last_waveform = None
        grant = (
            getattr(context, "acceptance_grant", None)
            if context is not None
            else self._current_acceptance_grant()
        )
        grant = grant or self._current_acceptance_grant()
        if not self._require_acceptance(grant):
            return
        if not text.strip():
            self.logger.info("transcription empty action=skip_paste")
            return

        dictation_app = (
            context.dictation_app
            if context is not None
            else getattr(self, "_dictation_app", None)
        )
        inject_suppressed = (
            context.inject_suppressed
            if context is not None
            else bool(getattr(self, "_inject_suppressed", False))
        )
        compose_lease = (
            context.agent_compose_lease if context is not None else None
        )
        compose_owned = compose_lease is not None
        if compose_owned:
            leases = getattr(self, "_agent_compose_leases", None)
            try:
                compose_current = bool(
                    leases is not None
                    and leases.is_current(
                        compose_lease.generation,
                        recording_id=compose_lease.recording_id,
                    )
                )
            except Exception:
                compose_current = False
            if not compose_current:
                # A cancelled/expired/blurred generation must neither update a
                # draft nor fall back to focused injection.
                return

        # Consent gate: does enhancement run at all? (Local is on-device; cloud
        # providers need a recorded BYOK consent.)
        do_enhance = False
        if self.config.enhancement_enabled and hasattr(self, 'llm_enhancer'):
            from .enhancement_security import enhancement_is_admitted
            # effective_provider() so the consent gate honors Lockdown Mode:
            # under lockdown the effective provider is "local" (on-device), so
            # no cloud BYOK consent record is required even when the stored
            # provider is a cloud one. The stored provider is untouched.
            provider = (self.config.effective_provider() or "").lower()
            # Local provider runs fully on-device; no cloud consent record needed.
            if enhancement_is_admitted(self.config):
                do_enhance = True
            else:
                self.logger.warning(
                    "enhancement requested but no consent record for provider=%s; skipping",
                    provider,
                )

        # Power Mode: route to the profile for the app focused at recording-start.
        # A manual hotkey override (issue 14) wins while the recording's foreground
        # app still matches the anchor; otherwise the override clears and auto
        # site -> app -> default routing applies. Unmapped / stale / no-app -> None
        # -> the global default profile. Computed once and shared by both paths.
        if do_enhance and inject_suppressed and not compose_owned and any(
            p.get("id") == "chat" for p in self.config.profiles
        ):
            # Onboarding owns the mic while injection is suppressed. Its try-it
            # examples are written for Chat, and the focused window is
            # Whisperstream itself, so Power Mode routing must not apply.
            profile_id = "chat"
        elif do_enhance and context is not None:
            profile_id = self._resolve_profile_for_routing(
                dictation_app=context.dictation_app,
                manual_profile_id=context.manual_profile_id,
                override_anchor=context.override_anchor,
            )
        else:
            profile_id = self._resolve_active_profile() if do_enhance else None

        # Power Mode routing decision. Retain only the allowlisted tier and
        # booleans; process/domain/profile identities stay out of diagnostics.
        #   tier=site                        -> per-site routing fired
        #   browser=True domain_read=miss    -> address-bar read failed
        #   domain_read=hit tier=app/default -> read ok but no matching site_map rule
        if do_enhance:
            route = resolve_enhancement_route(
                dictation_app,
                self.config,
                manual_profile_id=(
                    context.manual_profile_id
                    if context is not None
                    else getattr(self, "_manual_profile_id", None)
                ),
                manual_anchor=(
                    context.override_anchor
                    if context is not None
                    else getattr(self, "_override_anchor", None)
                ),
            )
            tier = route.tier.value if route is not None else "default"
            adapter = getattr(self, "application_context", None)
            metadata = (
                adapter.safe_capture_metadata(dictation_app)
                if adapter is not None
                and isinstance(dictation_app, ContextSnapshot)
                and hasattr(adapter, "safe_capture_metadata")
                else {
                    "browser": bool(
                        is_known_browser(
                            routing_target_application_id(dictation_app)
                        )
                    ),
                    "domain_read": "hit"
                    if getattr(dictation_app, "domain", None)
                    else "miss",
                }
            )
            browser = bool(metadata.get("browser", False))
            domain_read = (
                "n/a"
                if not browser
                else str(metadata.get("domain_read", "miss"))
            )
            self.logger.info(
                "[PowerMode] routed tier=%s profile_selected=%s browser=%s domain_read=%s",
                tier,
                str(profile_id is not None).lower(),
                browser,
                domain_read,
            )

        # No-default raw inject (issue 13): when no rule matched (profile_id is
        # None) AND there is no global default profile (default_profile_id is
        # empty/falsy), the user has chosen "no enhancement for unmapped
        # targets." Skip enhancement entirely and inject the raw transcript via
        # the single-call / raw path (never chunk). A non-empty default OR a
        # matched profile keeps the existing enhance behavior.
        if do_enhance and profile_id is None and not self.config.default_profile_id:
            do_enhance = False

        # Freeze the effective profile before evaluator acquisition and provider
        # work. Config edits must not rewrite this dictation's history identity
        # or redirect later chunks to a renamed default.
        profile_snapshot = (
            self._snapshot_enhancement_profile(profile_id) if do_enhance else None
        )
        if do_enhance and profile_snapshot is None:
            do_enhance = False

        enhancement_will_run = bool(
            do_enhance and self.llm_enhancer.will_enhance(text)
        )

        # Resolve the per-dictation input style (independent of do_enhance).
        # When do_enhance is False, profile_id is None — _effective_input_style
        # re-resolves via default_profile_id so the raw path still honours any
        # profile-level input_style override without touching _resolve_active_profile.
        effective_style = self._effective_input_style(
            profile_id,
            dictation_app=(
                context.dictation_app
                if context is not None
                else _USE_CURRENT_ROUTING
            ),
            screen_reader_detected=(
                context.screen_reader_detected
                if context is not None
                else _USE_CURRENT_ROUTING
            ),
        )

        # Chunked path: enhancement will actually run, the transcript is long,
        # injection isn't suppressed, AND the user hasn't turned off chunked
        # cleanup (Advanced toggle, default on). Long enhanced dictations clean +
        # paste per chunk so the first paragraph appears fast; with the toggle
        # off, every transcript takes the single-call path regardless of length.
        if (
            do_enhance
            and not inject_suppressed
            and not compose_owned
            and self.config.enhancement_chunked_cleanup
            and enhancement_will_run
            and len(text.split()) > getattr(
                self.config, "enhancement_words_per_chunk", _CHUNK_TARGET_WORDS
            )
        ):
            with self._normal_evaluation_scope() as evaluation_acquired:
                if evaluation_acquired:
                    self._broadcast_effective_profile(context, profile_id)
                    self._enhance_then_inject_chunked(
                        text,
                        duration,
                        profile_snapshot.profile_id,
                        effective_style,
                        context=context,
                        waveform=persistence_waveform,
                        profile_snapshot=profile_snapshot,
                    )
                    return
            # The raw fallback remains deliverable while Bakeoff owns the
            # evaluator. Never race one ordinary enhancement between pairs.
            do_enhance = False

        # Single-call / raw path — unchanged from the pre-round-03 behavior.
        # `raw_text` is the merged ASR text BEFORE enhancement; `enhanced_text`
        # is the cleaned text when enhancement actually rewrote it, else None.
        raw_text = text
        enhanced_text: Optional[str] = None
        enhancement_provenance: TranscriptEnhancementProvenance | None = None
        enhance_ms = 0
        # Onboarding-only outcome label: off | applied | skipped | failed.
        onboarding_enhancement = "off"
        if do_enhance:
            with self._normal_evaluation_scope() as evaluation_acquired:
                if not evaluation_acquired:
                    do_enhance = False
                elif not self._require_acceptance(grant):
                    return
                else:
                    enhance_start = time.perf_counter()
                    on_timeout, on_error = self._enhancement_outcome_observers(context)
                    try:
                        if enhancement_will_run:
                            self._broadcast_effective_profile(
                                context, profile_snapshot.profile_id
                            )
                        result = asyncio.run(
                            self._enhance_with_bounded_outcome(
                                text,
                                profile_id=profile_snapshot.profile_id,
                                on_timeout=on_timeout,
                                on_error=on_error,
                            )
                        )
                        text = result.text
                        if not self._require_acceptance(grant):
                            return
                        enhancement_provenance = self._enhancement_provenance(
                            profile_snapshot, result.outcome
                        )
                        onboarding_enhancement = {
                            "success": "applied",
                            "not_attempted": "skipped",
                        }.get(result.outcome, "failed")
                        if result.outcome == "success":
                            # Email signatures identify successful output only.
                            text = self._apply_email_signature(
                                text, profile_snapshot.profile_id
                            )
                            enhanced_text = text
                        else:
                            # Typed fallbacks are required to carry the original
                            # text. Enforce that boundary here too so a malformed
                            # test/tool implementation cannot deliver a hidden
                            # rewritten version while history says Original.
                            text = raw_text
                    except Exception as exc:
                        self.logger.warning(
                            "LLM enhancement failed error_type=%s",
                            type(exc).__name__,
                        )
                        text = raw_text
                        onboarding_enhancement = "failed"
                        enhancement_provenance = self._enhancement_provenance(
                            profile_snapshot, "error"
                        )
                    enhance_ms = int(
                        (time.perf_counter() - enhance_start) * 1000
                    )
        if not self._require_acceptance(grant):
            return
        processed_text = self.text_injector.process(text)
        if not self._require_acceptance(grant):
            return
        if compose_owned:
            self._broadcast(
                {
                    "type": "agent.compose.final",
                    "version": 1,
                    "session_handle": compose_lease.session_handle,
                    "generation": compose_lease.generation,
                    "recording_id": compose_lease.recording_id,
                    "text": processed_text,
                }
            )
        else:
            result_event = {
                "type": "transcription.result",
                "text": processed_text,
                "duration_ms": int(duration * 1000),
                "enhance_ms": enhance_ms,
            }
            if inject_suppressed:
                # Onboarding's Enhance step compares what the user said with
                # the enhanced result. Suppressed dictations are never saved to
                # history or injected; never log either field.
                result_event["raw_text"] = (
                    processed_text
                    if text == raw_text
                    else self.text_injector.process(raw_text)
                )
                result_event["enhancement"] = onboarding_enhancement
            self._broadcast(result_event)
        if not self._require_acceptance(grant):
            return
        self.review_state.record_session()
        if inject_suppressed or compose_owned:
            reason = (
                "agent_compose"
                if compose_owned
                else "onboarding_test_mode"
            )
            self.logger.info(
                "inject suppressed reason=%s action=skip_keyboard_type",
                reason,
            )
        else:
            production_delivery = (
                context is not None and context.recording_id is not None
            )
            self._inject_and_broadcast_route(
                processed_text if production_delivery else text,
                input_style=effective_style,
                already_processed=production_delivery,
                request_id=(
                    f"dictation-{context.recording_id}"
                    if production_delivery
                    else None
                ),
                generation=(context.recording_id if production_delivery else None),
                acceptance_grant=grant,
            )
            if not self._require_acceptance(grant):
                return
            self.text_injector.remember_original(raw_text)

        # Persist AFTER broadcast + inject so saving never adds paste latency.
        self._persist_transcript(
            raw_text=raw_text,
            enhanced_text=enhanced_text,
            duration=duration,
            waveform=persistence_waveform,
            inject_suppressed=inject_suppressed,
            source="agent_reply" if compose_owned else "dictation",
            start_after=(
                context.persistence_ready
                if context is not None
                and getattr(context, "recording_id", None) is not None
                else None
            ),
            acceptance_grant=grant,
            enhancement_provenance=enhancement_provenance,
        )

    def _enhance_then_inject_chunked(
        self, text: str, duration: float, profile_id: str | None = None,
        input_style: str | None = None,
        *, context: _DictationFinalizationContext | None = None,
        waveform: np.ndarray | None | object = _USE_CURRENT_ROUTING,
        profile_snapshot: EnhancementProfileSnapshot | None = None,
    ) -> DeliveryOutcome | None:
        """Clean a long transcript chunk-by-chunk, pasting each as it completes.

        Caller guarantees: enhancement will run, injection isn't suppressed, and
        the transcript is over the chunk threshold. Each chunk is cleaned with a
        single LLM call (raw-chunk fallback on failure) and pasted via the
        incremental batch-paste path (clipboard saved once, restored after the
        last chunk). One final transcription.result broadcast carries the full
        assembled text and the total cleanup time.

        *profile_id* is the Power Mode profile resolved for the focused app; it is
        threaded into every chunk's cleanup so long dictations honor per-app
        routing. ``None`` uses the global default profile.

        *input_style* is the effective style resolved by the caller via
        ``_effective_input_style``; forwarded to each ``paste_chunk`` call.
        ``None`` falls back to the injector's stored global default.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if waveform is _USE_CURRENT_ROUTING:
            waveform = (
                context.waveform
                if context is not None
                else getattr(self, "_last_waveform", None)
            )
        if getattr(self, "_last_waveform", None) is waveform:
            self._last_waveform = None
        grant = (
            context.acceptance_grant
            if context is not None
            else self._current_acceptance_grant()
        )
        if not self._require_acceptance(grant):
            return DeliveryOutcome.blocked()
        profile_snapshot = profile_snapshot or self._snapshot_enhancement_profile(
            profile_id
        )
        if profile_snapshot is None:
            return DeliveryOutcome.blocked()
        chunks = production_cleanup_chunks(
            text,
            target_words=getattr(
                self.config, "enhancement_words_per_chunk", _CHUNK_TARGET_WORDS
            ),
            limit_first_chunk=getattr(
                self.config, "enhancement_limit_first_chunk", True
            ),
        )
        self.logger.info(
            "chunked cleanup start chunks=%d words_in=%d", len(chunks), len(text.split())
        )

        enhance_start = time.perf_counter()
        last_idx = len(chunks) - 1
        batch_outcome: DeliveryOutcome | None = None
        on_timeout, on_error = self._enhancement_outcome_observers(context)
        enhancement_outcomes: list[EnhancementOutcome] = []
        try:
            for i, chunk in enumerate(chunks):
                if not self._require_acceptance(grant):
                    return DeliveryOutcome.blocked()
                result = asyncio.run(
                    self._enhance_with_bounded_outcome(
                        chunk,
                        profile_id=profile_snapshot.profile_id,
                        on_timeout=on_timeout,
                        on_error=on_error,
                        chunked=True,
                    )
                )
                cleaned = result.text if result.outcome == "success" else chunk
                enhancement_outcomes.append(result.outcome)
                if not self._require_acceptance(grant):
                    return DeliveryOutcome.blocked()
                # Append an Email signature exactly once, and only when at least
                # one chunk was successfully enhanced. Complete fallback/skip
                # must remain Original at this stage.
                if i == last_idx and "success" in enhancement_outcomes:
                    cleaned = self._apply_email_signature(
                        cleaned, profile_snapshot.profile_id
                    )
                processed = self.text_injector.process(
                    cleaned, do_prepend=(i == 0), do_append=(i == last_idx)
                )
                if not self._require_acceptance(grant):
                    return DeliveryOutcome.blocked()
                if context is not None and context.recording_id is not None:
                    batch_outcome = self.text_injector.paste_chunk(
                        processed,
                        first=(i == 0),
                        last=(i == last_idx),
                        input_style=input_style,
                        request_id=f"dictation-{context.recording_id}",
                        generation=context.recording_id,
                    )
                else:
                    batch_outcome = self.text_injector.paste_chunk(
                        processed,
                        first=(i == 0),
                        last=(i == last_idx),
                        input_style=input_style,
                    )
                if batch_outcome.status in {
                    DeliveryStatus.FAILED,
                    DeliveryStatus.BLOCKED,
                }:
                    break
        finally:
            # Normal completion has already ended the batch on last=True, so
            # this is an idempotent no-op. An unexpected cleanup/process/paste
            # failure instead restores the user's clipboard and clears cached
            # pin/batch state before the lifecycle terminal runs.
            self.text_injector.abort_chunked_paste()
        enhance_ms = int((time.perf_counter() - enhance_start) * 1000)
        if (
            batch_outcome is None
            or batch_outcome.status
            in {DeliveryStatus.FAILED, DeliveryStatus.BLOCKED}
        ):
            reason = (
                batch_outcome.reason.value
                if batch_outcome is not None
                and batch_outcome.reason is not None
                else "internal_error"
            )
            self.logger.info(
                "chunked cleanup stopped result=failed reason=%s",
                reason,
            )
            if batch_outcome is not None and batch_outcome.status is DeliveryStatus.BLOCKED:
                self._broadcast_acceptance_blocked()
            return batch_outcome
        self.logger.info(
            "chunked cleanup done chunks=%d enhance_ms=%d", len(chunks), enhance_ms
        )

        # Capture the pre-enhancement original now that the chunk loop is done.
        # Mirrors when _last_transcript is set (chunked path always injects, so
        # no _inject_suppressed guard needed — the caller checks before routing here).
        if not self._require_acceptance(grant):
            return DeliveryOutcome.blocked()
        self.text_injector.remember_original(text)

        # last_transcript is the full pasted text (set by paste_chunk on the last
        # chunk); use it for the broadcast so the result panel matches what landed.
        processed_text = self.text_injector.last_transcript or ""
        if not self._require_acceptance(grant):
            return DeliveryOutcome.blocked()
        self._broadcast({
            "type": "transcription.result",
            "text": processed_text,
            "duration_ms": int(duration * 1000),
            "enhance_ms": enhance_ms,
        })
        if not self._require_acceptance(grant):
            return DeliveryOutcome.blocked()
        self.review_state.record_session()

        aggregate_outcome = aggregate_enhancement_outcomes(
            enhancement_outcomes
        )
        enhancement_provenance = self._enhancement_provenance(
            profile_snapshot, aggregate_outcome
        )
        has_enhanced_output = "success" in enhancement_outcomes

        # Persist AFTER broadcast + paste so saving never adds paste latency. In
        # the chunked path raw = the input ASR text and enhanced = the full
        # cleaned-and-pasted text (text_injector.last_transcript).
        self._persist_transcript(
            raw_text=text,
            enhanced_text=(
                (self.text_injector.last_transcript or None)
                if has_enhanced_output
                else None
            ),
            duration=duration,
            waveform=waveform,
            inject_suppressed=(
                context.inject_suppressed
                if context is not None
                else bool(getattr(self, "_inject_suppressed", False))
            ),
            start_after=(
                context.persistence_ready
                if context is not None
                and getattr(context, "recording_id", None) is not None
                else None
            ),
            acceptance_grant=grant,
            enhancement_provenance=enhancement_provenance,
        )
        return batch_outcome

    def _persist_transcript(
        self,
        *,
        raw_text: str,
        enhanced_text: Optional[str],
        duration: float,
        waveform: np.ndarray | None | object = _USE_CURRENT_ROUTING,
        inject_suppressed: bool | None = None,
        source: str = "dictation",
        start_after: threading.Event | None = None,
        acceptance_grant: AcceptanceGrant | None = None,
        enhancement_provenance: TranscriptEnhancementProvenance | None = None,
    ) -> None:
        """Save a completed dictation to the encrypted TranscriptStore.

        Called at the END of both mic paths (single-call and chunked), AFTER the
        result is broadcast and injected, so persistence never adds paste
        latency. The actual save (encrypt + WAV write) is offloaded to the
        dedicated persistence executor. A persistence failure must NEVER break
        dictation.

        ``enhancement_provenance`` is already frozen at this boundary, then the
        store validates and encrypts it with the transcript content key so a
        later profile edit cannot rewrite history.

        Capture rules (Task 2.2):
          * Skip when history is disabled, the text is empty, injection is
            suppressed (onboarding test mode), or the store is unavailable.
          * Peaks + audio bytes are computed ONLY when save_audio is True; with
            save_audio False the record is text-only (peaks=None, audio=None).
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if waveform is _USE_CURRENT_ROUTING:
            waveform = getattr(self, "_last_waveform", None)
        # The local worker closure owns its independent reference from here.
        # Release the mutable app slot before all early returns and submit,
        # protecting any newer recording via identity comparison.
        if getattr(self, "_last_waveform", None) is waveform:
            self._last_waveform = None
        acceptance_grant = (
            acceptance_grant or self._current_acceptance_grant()
        )
        if not self._require_acceptance(acceptance_grant):
            return
        if history_disabled_by_policy(self) or not getattr(self.config, "history_enabled", False):
            return
        if not raw_text or not raw_text.strip():
            return
        # Never persist onboarding test recordings.
        if inject_suppressed is None:
            inject_suppressed = bool(
                getattr(self, "_inject_suppressed", False)
            )
        if inject_suppressed:
            return
        # transcript_store is None when its init failed (Task 2.1); a partially
        # constructed app may not have the attribute at all. Either way, no-op.
        store = self.ensure_transcript_store_initialized(acceptance_grant)
        if store is None:
            return
        if not history_write_available(store):
            return
        executor = getattr(self, "_persistence_executor", None)
        if executor is None and not hasattr(self, "_persistence_executor"):
            # Compatibility for narrowly constructed test/dev app stubs. Real
            # app instances always own the dedicated persistence executor.
            executor = getattr(self, "_executor", None)
        if executor is None:
            return

        model_name = self.config.parakeet_model
        persistence_epoch = self._persistence_submission_token()
        if persistence_epoch is None:
            return

        def _do_save() -> None:
            # Persistence must not break dictation: swallow + log metadata only.
            try:
                if start_after is not None:
                    start_after.wait()
                if not self._require_acceptance(acceptance_grant):
                    return
                if not self._persistence_token_is_current(persistence_epoch):
                    return
                if history_disabled_by_policy(self) or not bool(getattr(self.config, "history_enabled", False)):
                    return
                if not history_write_available(store):
                    return
                save_audio = bool(getattr(self.config, "save_audio", False))
                peaks = None
                audio_bytes = None
                # Peaks are saved only when audio is saved.
                if save_audio and waveform is not None and getattr(waveform, "size", 0) > 0:
                    peaks = compute_peaks(waveform)
                    audio_bytes = wav_bytes_from_float32(waveform, 16000)
                if not self._require_acceptance(acceptance_grant):
                    return
                if not self._persistence_token_is_current(persistence_epoch):
                    return
                if history_disabled_by_policy(self) or not bool(getattr(self.config, "history_enabled", False)):
                    return
                if not history_write_available(store):
                    return
                rid = store.save(
                    raw_text=raw_text,
                    enhanced_text=enhanced_text,
                    duration_ms=int(duration * 1000),
                    model=model_name,
                    source=source,
                    audio_bytes=audio_bytes,
                    peaks=peaks,
                    enhancement_provenance=enhancement_provenance,
                )
                if rid is None:
                    return
                # Under lockdown, emit a content-free access-log entry for the
                # save so the required audit trail (45 CFR 164.312(b)) is
                # complete for every write path (not just the REST surface).
                # NEVER log key material or transcript content here.
                lockdown_mode = bool(
                    getattr(self.config, "lockdown_mode", False)
                )
                if lockdown_mode and rid is not None:
                    log_access("save", rid, enabled=True)
                # A new record landed: tell any open Transcripts list to refresh,
                # the same way the import path does. Without this, a live dictation
                # only shows up after the tab is remounted (navigate away + back).
                # The event carries no transcript text, just the type. _broadcast is
                # thread-safe (the server marshals it onto the asyncio loop), so
                # firing it from this executor thread is fine.
                if not self._require_acceptance(acceptance_grant):
                    return
                self._broadcast({"type": "transcripts.changed"})
                # Cheap per-transcription sweep keeps the store bounded without a
                # background timer (Forever skips the age purge but still sweeps
                # orphans). Runs on the executor thread, so it never adds paste
                # latency. Metadata-only logging inside the helper.
                if self._acceptance_is_current(acceptance_grant):
                    run_retention_sweep(self.config, store)
            except Exception as exc:
                # Metadata only — never log transcript text or key material.
                self.logger.warning(
                    "transcript persist failed reason=%s", type(exc).__name__
                )
            finally:
                # Audio lifecycle hardening (Task 4.5 / research Q5):
                # Null _last_waveform immediately after persist completes — whether
                # the save succeeded or failed — so raw audio does not sit in memory
                # between recordings.  This is unconditional (not lockdown-only)
                # because the waveform is no longer needed once the persist attempt
                # has finished: if save_audio=True it has been encrypted and written
                # to disk; if save_audio=False it was never written.  The next
                # recording will overwrite _last_waveform in _stop_recording before
                # re-reading it, so clearing here is safe.
                if getattr(self, "_last_waveform", None) is waveform:
                    self._last_waveform = None

        try:
            executor.submit(_do_save)
        except Exception as exc:
            # Even the submit failing must not break dictation.
            self.logger.warning(
                "transcript persist submit failed reason=%s", type(exc).__name__
            )

    def _check_review_prompt(self) -> None:
        """Fire-once timer callback: broadcast the GitHub star prompt if eligible.

        Runs on the Timer thread. Thread-safe: reads Lockdown and review state
        under their own locks; no shared mutable state is accessed here.
        """
        if not self._require_acceptance():
            return
        # The prompt only opens GitHub, which Lockdown blocks; never ask then.
        if (
            getattr(self.managed_lockdown_policy, "enabled", False) is True
            or getattr(self.config, "lockdown_mode", False) is True
            or lockdown_keystore.is_lockdown_enabled(get_app_dir())
        ):
            return
        if self.review_state.should_show_prompt():
            self._broadcast({"type": "review.prompt"})
            # Each shown prompt counts toward the ask limit and interval.
            self.review_state.record_prompt_shown()

    def _ensure_audio_capture_selection_locked(self) -> bool:
        """Make the idle capture adapter match the latest saved selection."""
        if getattr(self, "_shutting_down", False) or self._capture_unavailable():
            return False
        target = getattr(self.config, "audio_input_device", "")
        if getattr(self, "_recording", False):
            return False
        previous = getattr(self, "audio_capture", None)
        if previous is None:
            return True
        needs_replacement = target != getattr(
            self, "_audio_capture_device", target
        )
        retained_session = isinstance(
            getattr(previous, "active_session", None), CaptureSession
        )
        if not needs_replacement and not retained_session:
            return True
        try:
            close_complete = previous.close()
        except Exception as exc:  # noqa: BLE001 - retry remains pending
            self.logger.warning(
                "audio capture config replacement deferred reason=%s",
                type(exc).__name__,
            )
            return False
        if close_complete is False:
            self.logger.warning(
                "audio capture config replacement deferred "
                "reason=prior_capture_close_incomplete"
            )
            return False
        if not needs_replacement:
            return True
        try:
            replacement = AudioCapture(
                device=target,
                status_callback=self._log_capture_status,
                terminal_callback=self._enqueue_capture_terminal_event,
                chunk_callback=self._on_chunk_ready,
                chunk_duration=CHUNK_DURATION_SECONDS,
                chunk_overlap=CHUNK_OVERLAP_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - closed owner stays retryable
            self.logger.warning(
                "audio capture config replacement deferred reason=%s",
                type(exc).__name__,
            )
            return False
        self.audio_capture = replacement
        self._audio_capture_device = target
        return True

    def reload_config(self, new_config: WhisperstreamConfig) -> None:
        """Hot-reload configuration (called from Settings GUI save)."""
        recording_lock = getattr(self, "_lock", None)
        if recording_lock is None:
            recording_lock = threading.Lock()
            self._lock = recording_lock
        with recording_lock:
            self._reload_config_locked(new_config)

    def _reload_config_locked(self, new_config: WhisperstreamConfig) -> None:
        """Apply one config snapshot while holding recording ownership."""
        managed_policy = getattr(self, "managed_lockdown_policy", None)
        if managed_policy is not None and managed_policy.enabled:
            try:
                envelope_exists = lockdown_keystore.is_lockdown_enabled(get_app_dir())
            except OSError:
                # An unreadable key location cannot authorize a history-on
                # no-envelope state. Treat it as envelope-backed so an existing
                # password path is not destructively rewritten in memory.
                envelope_exists = True
            apply_managed_lockdown(
                new_config,
                envelope_exists=envelope_exists,
            )
        if history_disabled_by_policy(self):
            new_config.history_enabled = False
            new_config.save_audio = False
        old_ptt = self.config.push_to_talk_shortcut
        old_toggle = self.config.toggle_shortcut
        old_paste_shortcut = self.config.paste_last_shortcut
        old_paste_original = self.config.paste_last_original_shortcut
        old_claude_view = self.config.claude_view_shortcut
        old_device = self.config.audio_input_device
        old_clipboard_behavior = self.config.clipboard_behavior
        old_clipboard_delay = self.config.clipboard_clear_delay
        old_filters = list(self.config.word_filters)
        old_replacements = dict(self.config.word_replacements)
        old_model = self.config.parakeet_model
        old_quant = self.config.parakeet_quantization
        old_local_endpoint = self.config.enhancement_local_endpoint
        old_enhancement_enabled = self.config.enhancement_enabled
        # effective_provider() so the warm-up transition detection honors
        # Lockdown Mode (forces "local"). Comparing effective providers keeps
        # the cloud->local auto-warm working when lockdown flips the provider,
        # without ever reading/mutating the stored enhancement_provider.
        old_enhancement_provider = self.config.effective_provider()
        old_profile_hotkeys = dict(self.config.profile_hotkeys)
        old_stt_routing = _stt_routing_state(self.config)
        new_stt_routing = _stt_routing_state(new_config)
        if new_stt_routing != old_stt_routing:
            # Preserve the route that owns the current recognizer, rather than
            # merely the last configured target. Repeated target saves during
            # one operation must not erase the only usable rollback state.
            self._stt_activation_rollback = (
                self._loaded_stt_routing_state or old_stt_routing
            )
        self.config = new_config
        speech_controller = getattr(self, "speech_controller", None)
        if speech_controller is not None:
            speech_controller.reconcile_residency()

        # Model-family swap: keep the existing ParakeetManager loaded and
        # serving the OLD model until the new model has finished downloading
        # and been instantiated. The atomic swap happens in
        # _run_model_download (server.py) once the new ParakeetManager is
        # ready. This means recording during a download produces results from
        # the previous language (a near-miss) rather than crashing because
        # self.stt_engine is None, and Cancel mid-download is a no-op for the
        # in-memory model.
        if (
            new_config.parakeet_model != old_model
            or new_config.parakeet_quantization != old_quant
        ):
            self.logger.info(
                "parakeet model changed old_model=%s old_quantization=%s new_model=%s new_quantization=%s action=defer_swap_until_download_complete",
                old_model, old_quant or "none",
                new_config.parakeet_model, new_config.parakeet_quantization or "none",
            )

        # Update audio feedback settings in place. Tearing down + re-initing
        # the SDL mixer here while the hotkey thread is calling sound.play()
        # on a cached Sound is a native abort (no Python traceback). Volume
        # is read on every play, so just update the live instance.
        if self.audio_feedback is not None:
            self.audio_feedback.update_settings(
                enabled=new_config.audio_feedback,
                volume=new_config.audio_feedback_volume,
            )

        # Recreate AudioCapture if the input device changed while idle. An open
        # recording keeps its exact native stream; the next start compares the
        # adapter with this latest config snapshot and applies the saved choice.
        capture_device = getattr(
            self, "_audio_capture_device", new_config.audio_input_device
        )
        if (
            self.audio_capture is not None
            and new_config.audio_input_device != capture_device
        ):
            self.logger.info(
                "audio capture config changed old_selection=%s new_selection=%s action=%s",
                "configured" if capture_device else "system_default",
                "configured" if new_config.audio_input_device else "system_default",
                "defer_until_idle"
                if getattr(self, "_recording", False)
                else "recreate",
            )
            if not getattr(self, "_recording", False):
                self._ensure_audio_capture_selection_locked()

        # Re-register hotkeys if any recording or paste shortcut changed,
        # OR if the per-profile hotkeys changed. The re-registration path
        # re-applies the per-profile hotkeys too (issue 14).
        if self.keyboard is not None and (
            new_config.push_to_talk_shortcut != old_ptt
            or new_config.toggle_shortcut != old_toggle
            or new_config.paste_last_shortcut != old_paste_shortcut
            or new_config.paste_last_original_shortcut != old_paste_original
            or new_config.claude_view_shortcut != old_claude_view
        ):
            self.logger.info("hotkey config changed action=re_register")
            recovery = getattr(self, "_shortcut_recovery", None)
            if recovery is not None:
                recovery.reconcile(force=True)
            else:
                self.keyboard.unhook_all()
                self._register_hotkey()
        elif self.keyboard is not None and new_config.profile_hotkeys != old_profile_hotkeys:
            # Only the profile hotkeys changed: re-register them in place (they
            # ride the existing primary listener, so no full unhook is needed).
            self.logger.info("profile hotkeys changed action=re_register")
            recovery = getattr(self, "_shortcut_recovery", None)
            if recovery is not None:
                recovery.reconcile(force=True)
            else:
                self._register_profile_hotkeys()

        # If a manual override is active but its profile was deleted in this save,
        # drop the override so a stale id never sticks (clears + broadcasts).
        if (
            self._manual_profile_id is not None
            and not any(p.get("id") == self._manual_profile_id for p in new_config.profiles)
        ):
            self.clear_manual_profile()

        # Dictionary Boost belongs to enhancement. Never reload the speech
        # recognizer or re-enable the abandoned decoder-hotword path here.

        # Apply mutable decode-language state without rebuilding the loaded
        # artifact. Qwen receives its optional forced language; Cohere always
        # receives the selected ISO language; Parakeet has no corresponding
        # method because it auto-detects.
        if (
            self.parakeet is not None
            and hasattr(self.parakeet, "set_language_hint")
            and _stt_artifact_id(new_config) == self._loaded_stt_artifact_id
        ):
            # A different configured artifact is only a pending replacement.
            # Keep the current worker's language intact until that swap settles.
            selection = _selection_from_config(new_config)
            if self._loaded_stt_artifact_id is ModelId.COHERE_TRANSCRIBE_INT8:
                language_hint = new_config.language
            else:
                from .language_routing import qwen_forced_language_value

                language_hint = qwen_forced_language_value(
                    new_config.language,
                    selection.qwen_force_language if selection.backend == "qwen" else False,
                )
            self.parakeet.set_language_hint(language_hint)

        # A routing save that still resolves to the exact loaded artifact is
        # complete now: Parakeet needs no in-place update, while Qwen/Cohere's
        # mutable routing behavior (the language hint) was applied above. Move
        # the live snapshot forward so a later failed cross-artifact activation
        # restores this immediate prior state instead of the load-time state.
        if (
            self.parakeet is not None
            and self._loaded_stt_artifact_id is not None
            and _stt_artifact_id(new_config) == self._loaded_stt_artifact_id
        ):
            self._loaded_stt_routing_state = new_stt_routing
            self._stt_activation_rollback = None

        if (
            new_config.clipboard_behavior != old_clipboard_behavior
            or new_config.clipboard_clear_delay != old_clipboard_delay
        ):
            self.text_injector.update_clipboard_settings(
                behavior=new_config.clipboard_behavior,
                clear_delay=new_config.clipboard_clear_delay,
            )

        # Recreate TextInjector if word_filters or word_replacements changed.
        # IMPORTANT: pass the pin collaborators here too — pinning must survive
        # a dictionary edit (same wiring as the initial construction in __init__).
        if (
            list(new_config.word_filters) != old_filters
            or dict(new_config.word_replacements) != old_replacements
        ):
            self.logger.info("dictionary changed action=recreate_text_injector")
            replacement_injector = TextInjector(
                keyboard_manager=self.keyboard,
                logger=self.logger,
                word_filters=list(new_config.word_filters),
                word_replacements=dict(new_config.word_replacements),
                post_processing=new_config.post_processing,
                clipboard_behavior=new_config.clipboard_behavior,
                clipboard_clear_delay=new_config.clipboard_clear_delay,
                input_style=new_config.input_style,
                keystroke_delay_ms=new_config.keystroke_delay_ms,
                content_allowed=self._acceptance_is_current,
            )
            previous_injector = self.text_injector
            try:
                previous_injector.close()
            except Exception:  # noqa: BLE001 - replacement remains usable
                self.logger.warning(
                    "delivery adapter close result=error origin=config_reload"
                )
            self.text_injector = replacement_injector

        # Recreate LLMEnhancer with the new config. The supervisor handle is
        # resolved live via the provider lambda, so this is safe to call
        # before the supervisor has finished spawning. simulated_tps_provider
        # re-reads via the SAME indirection as __init__ above, so the Task 1.7
        # dev latency harness survives this reconstruction.
        self.llm_enhancer = LLMEnhancer(
            new_config, self.logger,
            llama_server_provider=lambda: self.llama_server,
            local_runtime_provider=self._resolve_local_llm_runtime,
            simulated_tps_provider=lambda: self._simulated_tps_provider(),
        )

        # Reconcile ordinary production residency with the effective config.
        # Explicit Bakeoff pins remain owned by the pool until backend exit.
        local_active = (
            new_config.enhancement_enabled
            and new_config.effective_provider() == "local"
            and not new_config.enhancement_local_endpoint.strip()
        )
        should_warm = local_active and not (
            old_enhancement_enabled
            and old_enhancement_provider == "local"
            and not old_local_endpoint.strip()
        )
        if not local_active and self.llama_server is not None:
            loop = self._server_loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(
                        self._schedule_inactive_local_llm_stop
                    )
                except RuntimeError:
                    self.logger.warning(
                        "[LlamaServer] stop request skipped reason=server_loop_closed"
                    )

        if should_warm and self._acceptance_is_current():
            # In server mode this requester first creates or repoints the
            # supervisor for the selected GGUF, then kicks warm. That ordering
            # is required when startup occurred on cloud/disabled and no
            # supervisor exists yet. Keep the direct fallback for standalone
            # callers and focused unit harnesses that do not wrap the app in a
            # WhisperstreamServer.
            warm_requested = False
            requester = self._local_warm_requester
            if requester is not None:
                try:
                    warm_requested = bool(requester(new_config))
                except Exception as exc:
                    self.logger.warning(
                        "[LlamaServer] warm request failed reason=%s",
                        type(exc).__name__,
                    )
            if not warm_requested and self._can_warm_local(new_config):
                try:
                    self.llama_server.kick_warm(self._server_loop)
                except Exception as exc:
                    self.logger.warning(
                        "[LlamaServer] kick_warm failed reason=%s",
                        type(exc).__name__,
                    )

        # Propagate the idle-offload timeout to a live supervisor if present.
        # The user can toggle "Keep LLM in memory" at any time; this keeps
        # the existing supervisor in sync without a restart.
        if self.llama_server is not None:
            self.llama_server.set_idle_timeout(new_config.enhancement_local_idle_timeout)
        resident_pool = getattr(self, "resident_llama_pool", None)
        if resident_pool is not None:
            resident_pool.set_idle_timeout(
                new_config.enhancement_local_idle_timeout
            )

        self.logger.info("config reloaded")

    def _schedule_inactive_local_llm_stop(self) -> None:
        """Coalesce config saves on the supervisor's event loop."""
        task = self._local_stop_task
        if task is None or task.done():
            self._local_stop_task = asyncio.create_task(self._stop_inactive_local_llm())

    async def _stop_inactive_local_llm(self) -> None:
        """Recheck queued config work and release only ordinary residency."""
        supervisor = self.llama_server
        cfg = self.config
        if (
            supervisor is None
            or getattr(supervisor, "is_residency_pinned", False) is True
            or (
                cfg.enhancement_enabled
                and cfg.effective_provider() == "local"
                and not cfg.enhancement_local_endpoint.strip()
            )
        ):
            return
        try:
            await supervisor.stop()
            # A re-enable can arrive while stop awaits cancellation/termination.
            # Restore lazy warmup on that transition after cleanup completes.
            cfg = self.config
            if (
                self.llama_server is supervisor
                and cfg.enhancement_enabled
                and self._acceptance_is_current()
                and self._can_warm_local(cfg)
            ):
                supervisor.kick_warm(self._server_loop)
        except Exception as exc:
            self.logger.warning(
                "[LlamaServer] inactive stop failed reason=%s", type(exc).__name__
            )

    def _can_warm_local(self, cfg: "WhisperstreamConfig") -> bool:
        """Return True iff all guards for kick_warm are satisfied.

        Guard set (research.md line 18):
          - provider is "local"
          - no endpoint-override (user is using bundled llama-server,
            not pointing at an external server)
          - GGUF exists on disk and is non-empty
          - supervisor instance exists on the app (set by
            _maybe_prepare_llama_server or _refresh_supervisor)
          - server loop has been captured (set_server_loop has been called)
        """
        from .enhancement_models import get_local_model, resolve_local_model_path

        # effective_provider() so Lockdown Mode (forces "local") passes this
        # warm guard even when the stored provider is a cloud one. Log the
        # effective provider that drove the decision; the stored value is
        # untouched.
        effective = cfg.effective_provider()
        if effective != "local":
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=provider_not_local provider=%s",
                effective,
            )
            return False

        if cfg.enhancement_local_endpoint.strip():
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=endpoint_override",
            )
            return False

        catalog_entry = get_local_model(cfg.enhancement_local_model)
        if catalog_entry is None:
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=unknown_model_id model=%s",
                cfg.enhancement_local_model,
            )
            return False

        gguf_path = resolve_local_model_path(catalog_entry)
        if (
            gguf_path is None
            or not gguf_path.exists()
            or gguf_path.stat().st_size == 0
        ):
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=gguf_not_installed model=%s",
                cfg.enhancement_local_model,
            )
            return False

        if self.llama_server is None:
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=supervisor_not_initialized"
            )
            return False

        if self._server_loop is None:
            self.logger.debug(
                "[LlamaServer] _can_warm_local=False reason=server_loop_not_set"
            )
            return False

        return True

    def _resolve_local_llm_runtime(self):
        """Resolve a Windows adapter for the current raw supervisor identity."""

        supervisor = getattr(self, "llama_server", None)
        if supervisor is None:
            self.local_llm_runtime = None
            self._local_llm_runtime_supervisor = None
            return None
        if self._local_llm_runtime_supervisor is supervisor:
            return self.local_llm_runtime

        from .local_llm_runtime import create_managed_runtime

        artifact_path = getattr(supervisor, "_gguf_path", None)
        if not isinstance(artifact_path, Path):
            return None
        runtime = create_managed_runtime(
            supervisor,
            catalog_id=str(self.config.enhancement_local_model),
            artifact_path=artifact_path,
        )
        self.local_llm_runtime = runtime
        self._local_llm_runtime_supervisor = supervisor if runtime is not None else None
        return runtime

    async def close_local_llm_runtime(self) -> bool:
        """Close the owned adapter and clear its generation state."""

        runtime = getattr(self, "local_llm_runtime", None)
        if runtime is None:
            return False
        try:
            await runtime.close()
        finally:
            self.local_llm_runtime = None
            self._local_llm_runtime_supervisor = None
        return True

    async def restart_local_llm_for_model(
        self, artifact_path: Path, *, catalog_id: str | None = None
    ) -> None:
        """Repoint the supervisor while advancing the contract generation."""

        runtime = self._resolve_local_llm_runtime()
        if runtime is not None:
            await runtime.restart_for_model(
                artifact_path,
                catalog_id=catalog_id or str(self.config.enhancement_local_model),
            )
            return
        supervisor = getattr(self, "llama_server", None)
        if supervisor is None:
            raise RuntimeError("local llama-server unavailable")
        await supervisor.restart_for_model(artifact_path)

    def _enqueue_capture_terminal_event(self, event: CaptureTerminalEvent) -> None:
        """Native callback seam: enqueue only, never settle or close capture."""
        if not isinstance(event, CaptureTerminalEvent) or getattr(
            self, "_shutting_down", False
        ):
            return
        terminal_lock = self._capture_terminal_lock
        with terminal_lock:
            pending = self._capture_terminal_pending
            if pending is None or (
                event.owner_epoch,
                event.generation,
            ) > (
                pending.owner_epoch,
                pending.generation,
            ):
                self._capture_terminal_pending = event
            if self._capture_terminal_scheduled:
                return
            self._capture_terminal_scheduled = True
        try:
            threading.Thread(
                target=self._drain_capture_terminal_events,
                daemon=True,
                name="CaptureTerminal",
            ).start()
        except Exception as exc:  # noqa: BLE001 - callback cannot own cleanup
            with terminal_lock:
                self._capture_terminal_pending = None
                self._capture_terminal_scheduled = False
            self.logger.warning(
                "capture terminal scheduling failed reason=%s",
                type(exc).__name__,
            )

    def _drain_capture_terminal_events(self) -> None:
        """Settle the latest queued terminal event until the bounded slot is empty."""
        while True:
            with self._capture_terminal_lock:
                event = self._capture_terminal_pending
                self._capture_terminal_pending = None
                if event is None:
                    self._capture_terminal_scheduled = False
                    return
            try:
                self._settle_capture_terminal_event(event)
            except Exception as exc:  # noqa: BLE001 - keep scheduler available
                self.logger.warning(
                    "capture terminal settlement failed reason=%s",
                    type(exc).__name__,
                )

    def _settle_capture_terminal_event(self, event: CaptureTerminalEvent) -> None:
        """Claim and discard one exact active capture generation off callback."""
        if event.failure not in (CaptureFailure.DEVICE_LOST, CaptureFailure.STREAM_FAILED,
                                 CaptureFailure.TIMEOUT, CaptureFailure.CLEANUP_FAILED):
            return
        with self._lock:
            if getattr(self, "_shutting_down", False):
                return
            if not getattr(self, "_recording", False):
                return
            if getattr(self, "_active_capture_session_key", None) != (
                event.owner_epoch,
                event.request_id,
                event.generation,
            ):
                return
            self.logger.warning(
                "recording terminal claim failure=%s",
                event.failure.value,
            )
            self._active_capture_session_key = None
            self._cancel_recording(trigger="device_lost")
            self._report_capture_failure()

    def _log_capture_status(self, message: str) -> None:
        self.logger.debug("audio capture status event=updated")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whisperstream",
        description="Whisperstream – Windows dictation app using local Parakeet STT (CPU-only).",
        epilog=(
            "Usage:\n"
            "  uv run python -m whisperstream.setup   # one-time: download the Parakeet model files\n"
            "  uv run python -m whisperstream.main    # daily: start Whisperstream and use the configured hotkey\n\n"
            "While Whisperstream is running, hold down your primary shortcut (default: right shift)\n"
            "to record. Release to stop and transcribe."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Smoke-test the pipeline without registering hotkeys or capturing audio",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run as headless API server (for Tauri frontend)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=38420,
        help="API server port (default: 38420)",
    )
    parser.add_argument(
        "--no-hotkeys",
        action="store_true",
        help="Skip global hotkey registration (useful during development)",
    )
    parser.add_argument(
        "--file-only",
        action="store_true",
        help=(
            "Run the API with microphone, hotkey, audio-output, pin-routing, "
            "and text-delivery surfaces disabled (requires --serve)"
        ),
    )
    parser.add_argument(
        "--lifecycle-probe-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fixture-probe-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--owner-pid",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--engine-generation",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--audio",
        type=str,
        default=None,
        metavar="FILE",
        help="Audio file to transcribe (only with --check). Supports wav/flac/ogg and m4a (via ffmpeg).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the application data directory (config, logs, models, "
            "license, acceptance). Equivalent to setting WHISPERSTREAM_DATA_DIR. "
            "Use a separate path during development to avoid polluting "
            "installed-app state."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.file_only and not args.serve:
        parser.error("--file-only requires --serve")
    if args.lifecycle_probe_worker and not (args.serve and args.file_only):
        parser.error("--lifecycle-probe-worker requires --serve --file-only")
    if args.lifecycle_probe_worker and sys.platform != "darwin":
        parser.error("--lifecycle-probe-worker requires Darwin")
    if args.fixture_probe_worker and not (args.serve and args.file_only):
        parser.error("--fixture-probe-worker requires --serve --file-only")
    if args.fixture_probe_worker and sys.platform != "darwin":
        parser.error("--fixture-probe-worker requires Darwin")
    if args.fixture_probe_worker and args.lifecycle_probe_worker:
        parser.error("worker probe modes are mutually exclusive")
    if args.owner_pid is not None and (not args.serve or args.owner_pid <= 0):
        parser.error("--owner-pid requires --serve and a positive PID")

    # Apply --data-dir before any helper that resolves paths runs (logger,
    # ConfigManager). Set the env var so child code paths that re-import
    # config_manager helpers also see the override.
    data_dir_arg = getattr(args, "data_dir", None)
    if data_dir_arg:
        import os as _os
        from .config_manager import DATA_DIR_ENV_VAR as _DATA_DIR_ENV_VAR
        _os.environ[_DATA_DIR_ENV_VAR] = data_dir_arg

    # Machine policy must be evaluated before content-capable services start.
    startup_policy = read_managed_lockdown_policy()
    # Install a safe crash logger even in voluntary or Managed Lockdown so users
    # can produce a useful, user-controlled support report.
    _logger = get_logger(
        level=logging.DEBUG if args.verbose else logging.INFO,
        log_dir=get_log_dir(),
    )

    app: WhisperstreamApp | None = None
    try:
        if args.check:
            _run_smoke_check(verbose=args.verbose, audio_path=args.audio)
            return

        # One-release best-effort migration: remove the exact external Codex
        # hook entries and generated forwarders written by older Whisperstream
        # builds. This is independent of the retired config boolean.
        from .codex_retirement import retire_codex_integration  # noqa: PLC0415

        retire_codex_integration()

        if args.serve:
            if sys.platform == "darwin":
                from .macos_process_policy import make_backend_accessory  # noqa: PLC0415

                make_backend_accessory(_logger)
            app = WhisperstreamApp(
                verbose=args.verbose,
                headless=True,
                defer_model_load=True,
                managed_policy=startup_policy,
                file_only=args.file_only,
            )
            if args.lifecycle_probe_worker:
                from .macos_stt_manager import MacosFluidAudioManager  # noqa: PLC0415

                lifecycle_manager = MacosFluidAudioManager(
                    logger=app.logger,
                    timeout=app.config.model_timeout,
                    lifecycle_probe_only=True,
                )
                lifecycle_manager.start_worker_for_lifecycle_probe()
                app.stt_engine = lifecycle_manager
            elif args.fixture_probe_worker:
                # Internal package regression seam. File-only construction keeps
                # every OS interaction and persistence surface disabled while the
                # ordinary factory loads the real bundled worker and prewarmed
                # model for fixture-only API calls.
                app._load_stt_engine_blocking()
                if app.stt_engine is None:
                    raise RuntimeError("fixture probe speech engine unavailable")
            from .server import WhisperstreamServer
            no_hotkeys = getattr(args, 'no_hotkeys', False) or args.file_only
            server = WhisperstreamServer(
                app,
                port=args.port,
                logger=app.logger,
                register_hotkeys=not no_hotkeys,
                owner_pid=args.owner_pid,
                engine_generation=args.engine_generation,
            )
            server.run()
            return

        app = WhisperstreamApp(verbose=args.verbose, managed_policy=startup_policy)
        app.run()
    except SystemExit:
        raise
    except Exception as exc:
        _logger.error("app crashed error_type=%s", type(exc).__name__)
        raise SystemExit(1)
    finally:
        if app is not None:
            app.cleanup()


def _load_audio_file(path: str, logger: logging.Logger) -> tuple[np.ndarray, int]:
    """Load an audio file and return (float32 mono waveform, sample_rate).

    PCM WAV uses wave/numpy. Other formats use bounded ffmpeg conversion.
    """
    import subprocess
    import wave
    from pathlib import Path
    from .audio_limits import (
        MAX_AUDIO_FILE_BYTES, MAX_AUDIO_SECONDS, MAX_DECODED_SAMPLES,
        FFMPEG_TIMEOUT_SECONDS, AudioLimitExceeded,
    )
    from .temp_lifecycle import create_owned_temp_file, close_then_unlink

    audio_path = Path(path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")
    if audio_path.stat().st_size > MAX_AUDIO_FILE_BYTES:
        raise AudioLimitExceeded("audio_limit_exceeded")

    suffix = audio_path.suffix.lower()

    # Try native wave module for .wav files
    if suffix == ".wav":
        with wave.open(str(audio_path), "r") as wf:
            sr = wf.getframerate()
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            frame_count = wf.getnframes()
            if (
                sr <= 0 or sr > 192000 or not 1 <= channels <= 8
                or frame_count > sr * MAX_AUDIO_SECONDS
                or frame_count * channels > MAX_DECODED_SAMPLES
            ):
                raise AudioLimitExceeded("audio_limit_exceeded")
            if sampwidth not in {2, 4}:
                raise ValueError("unsupported WAV sample width")
            frames = wf.readframes(frame_count)
            if len(frames) != frame_count * channels * sampwidth:
                raise ValueError("incomplete WAV audio")
        if sampwidth == 2:
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 4:
            data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"Unsupported WAV sample width: {sampwidth}")
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return data, sr

    # For all other formats, use ffmpeg to convert to 16kHz mono PCM
    logger.info("audio convert start tool=ffmpeg suffix=%s", suffix)
    output, output_path = create_owned_temp_file(purpose="transcribe-decode", suffix=".pcm")
    try:
        maximum = min(MAX_DECODED_SAMPLES, MAX_AUDIO_SECONDS * 16000) * 2
        try:
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                 "-protocol_whitelist", "file,pipe",
                 "-format_whitelist", "aac,mp3,mov,flac,ogg,wav,matroska",
                 "-i", str(audio_path), "-t", str(MAX_AUDIO_SECONDS + 1),
                 "-fs", str(maximum + 2), "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", "-"],
                stdout=output, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            raise RuntimeError("ffmpeg is required. Install ffmpeg or convert to .wav first.") from None
        except subprocess.TimeoutExpired:
            raise AudioLimitExceeded("audio_decode_timeout") from None
        if result.returncode != 0:
            raise RuntimeError("audio_decode_failed")
        output.seek(0)
        pcm = output.read(maximum + 2)
        if len(pcm) > maximum:
            raise AudioLimitExceeded("audio_limit_exceeded")
        if len(pcm) % 2:
            raise ValueError("incomplete decoded audio")
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0, 16000
    finally:
        if not close_then_unlink(output_path, (output,)):
            logger.warning("audio decode temp cleanup status=retry_exhausted")


def _run_smoke_check(*, verbose: bool = False, audio_path: str | None = None) -> None:
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    logger = get_logger(level=logging.DEBUG if verbose else logging.INFO, log_dir=get_log_dir())
    logger.info("smoke check start")
    config_manager = ConfigManager()
    config = config_manager.load()
    _backend, _backend_source = _resolve_backend(config)
    if _backend == "qwen":
        logger.info(
            "smoke check config backend=qwen source=%s",
            _backend_source,
        )
    elif _backend == "cohere":
        logger.info(
            "smoke check config backend=cohere source=%s language=%s",
            _backend_source,
            config.language,
        )
    else:
        logger.info(
            "smoke check config backend=parakeet source=%s model=%s quantization=%s",
            _backend_source,
            config.parakeet_model,
            config.parakeet_quantization or "none",
        )
    try:
        parakeet = make_stt_manager(config, logger=logger, config_manager=config_manager)
    except ModelNotPreparedError as exc:
        logger.error("smoke check model_not_found reason=%s", type(exc).__name__)
        logger.info("smoke check finished result=fail reason=model_missing")
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.error(
            "smoke check model_load_failed error_type=%s", type(exc).__name__
        )
        logger.info("smoke check finished result=fail reason=model_load_error")
        raise SystemExit(2) from exc

    text_injector = TextInjector(
        keyboard_manager=KeyboardShortcutManager(logger=logger),
        logger=logger,
        word_filters=list(config.word_filters),
        word_replacements=dict(config.word_replacements),
        post_processing=config.post_processing,
        clipboard_behavior=False,
        clipboard_clear_delay=config.clipboard_clear_delay,
        input_style=config.input_style,
        keystroke_delay_ms=config.keystroke_delay_ms,
    )

    try:
        if audio_path:
            # Audio file mode: load, transcribe, process, compare
            from pathlib import Path

            audio_data, sr = _load_audio_file(audio_path, logger)
            audio_duration = audio_data.size / sr
            logger.info("smoke check audio loaded samples=%d sample_rate=%d duration=%.1fs", audio_data.size, sr, audio_duration)
            _t0 = time.monotonic()
            transcription = parakeet.transcribe(audio_data, sample_rate=sr, language=config.language)
            transcribe_time = time.monotonic() - _t0
            rtf = transcribe_time / audio_duration if audio_duration > 0 else float("inf")
            rtf_line = (
                f"backend={_backend}"
                f" audio={audio_duration:.2f}s"
                f" transcribe={transcribe_time:.3f}s"
                f" rtf={rtf:.4f}"
            )
            logger.info("smoke check latency %s", rtf_line)
            print(rtf_line)
            processed = text_injector.process(transcription)
            logger.info("smoke check transcription complete chars=%d", len(processed))
            print(processed)

            # Auto-compare with companion .txt file if it exists
            txt_path = Path(audio_path).with_suffix(".txt")
            if txt_path.exists():
                expected = txt_path.read_text(encoding="utf-8").strip()
                if processed.strip().lower() == expected.lower():
                    logger.info("smoke check fixture compare result=match")
                else:
                    logger.warning(
                        "smoke check fixture compare result=mismatch expected_chars=%d got_chars=%d",
                        len(expected),
                        len(processed.strip()),
                    )
        else:
            # Synthetic waveform mode (existing behavior)
            dummy_audio = np.zeros(16_000, dtype=np.float32)
            transcription = parakeet.transcribe(dummy_audio, sample_rate=16_000, language=config.language)
            processed = text_injector.process(transcription or "test")
            logger.info("smoke check synthetic transcription complete raw_chars=%d processed_chars=%d", len(transcription or ""), len(processed))
    except Exception as exc:
        logger.error(
            "smoke check transcription failed error_type=%s", type(exc).__name__
        )
        logger.info("smoke check finished result=fail reason=inference_error")
        raise SystemExit(3) from exc
    finally:
        try:
            parakeet.shutdown()
        except Exception:
            logger.warning("smoke check worker shutdown result=error")

    logger.info("smoke check finished result=pass")


if __name__ == "__main__":
    main()
