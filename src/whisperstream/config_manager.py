from __future__ import annotations

import logging
import os
import platform
import re
import threading
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from platformdirs import user_data_dir

from .config_normalization import normalize_offer_ids, normalize_saved_models, normalize_word_list
from .config_profiles import (
    BUILTIN_PROFILE_IDS as _BUILTIN_PROFILE_IDS,
    PROFILE_ICON_KEYS as _PROFILE_ICON_KEYS,
    migrate_profiles,
    normalize_routing_fields,
    prune_profile_routing,
    validate_profiles,
)
from .config_serialization import toml_inline_array, toml_key, toml_value

logger = logging.getLogger(__name__)

# Environment variable to override the application data directory. When set,
# all helpers below resolve to this path instead of the default
# ``%LOCALAPPDATA%\Whisperstream``. Used to keep dev runs (cargo tauri dev /
# `uv run python -m whisperstream.main`) from polluting installed-app state
# (acceptance.json, license.json, config.toml, etc.).
DATA_DIR_ENV_VAR = "WHISPERSTREAM_DATA_DIR"
AGENT_SPEECH_IDLE_TIMEOUT_CHOICES = (0, 300, 600, 900, 1800, 3600)

# Keys whose values must never appear in log output (privacy / legal).
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"customer_email", "license_key", "word_overrides"}
)
_AGENT_SPEECH_KOKORO_VOICES: frozenset[str] = frozenset(
    {
        "af_heart",
        "af_bella",
        "af_nicole",
        "af_aoede",
        "af_kore",
        "af_sarah",
        "am_fenrir",
        "am_michael",
        "am_puck",
    }
)

_MACOS_INPUT_SELECTION_MODE = "opaque-id"


def _log_validation_warning(exc: ValueError) -> None:
    """Emit only the allowlisted event and exception class.

    Validation messages may contain stale routing keys, paths, endpoints, or
    other user configuration. They are useful to the caller but are not a safe
    diagnostic surface.
    """
    logger.warning(
        "config validation failed error_type=%s",
        type(exc).__name__,
    )

MAX_ALLOWED_DURATION = 7200.0  # 2 hours

# Languages selectable in the picker: the 39-language UNION of both local STT
# backends (Parakeet TDT v3's 25 + Qwen3-ASR 0.6B's 30). Must stay in sync with
# CURATED_LANGUAGES + EXTENDED_LANGUAGES in frontend/src/lib/languages.ts and
# with PARAKEET_LANGUAGES / QWEN_LANGUAGES in language_routing.py. The selected
# language is a ROUTING signal (which backend/model to run), not a per-call
# decode hint. None means auto-detect.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({
    # Parakeet TDT v3 (25 European languages)
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de",
    "el", "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk",
    "sl", "es", "sv", "ru", "uk",
    # Qwen3-ASR additions (East/SE Asian + Hindi + Arabic + Persian + Macedonian)
    "zh", "yue", "ja", "ko", "hi", "th", "vi", "id", "ms", "fil",
    "ar", "fa", "tr", "mk",
})

# Maps an enhancement provider to its dedicated key-storage field. Providers
# absent from this map (ollama, local) don't use API keys.
_PROVIDER_KEY_SLOT: Dict[str, str] = {
    "gemini": "enhancement_gemini_api_key",
    "openai": "enhancement_openai_api_key",
    "anthropic": "enhancement_anthropic_api_key",
    "custom": "enhancement_custom_api_key",
}

_API_KEY_FIELDS = (
    "enhancement_api_key",
    "enhancement_gemini_api_key",
    "enhancement_openai_api_key",
    "enhancement_anthropic_api_key",
    "enhancement_custom_api_key",
)

# Client responses expose only this fixed presence marker plus a masked
# fingerprint. The marker is never written to config.toml and is never treated
# as API-key material. Opaque provider references stay backend-side.
PROTECTED_API_KEY_MARKER = "protected:v1"

# Bundled default power-mode map. Keys are lowercased process names; values are
# bundled profile ids ("chat" / "code") that load()'s no-file branch seeds via
# enhancement_prompts.CHAT_PROMPT / CODE_PROMPT. Seeded in two places:
#   1. load()'s no-file branch (brand-new installs).
#   2. from_dict() when the "power_mode_map" key is strictly ABSENT (upgrading
#      users who predate this field) — see the seed-on-absence gate there.
# A present-but-empty {} is a deliberate user state and is NEVER re-seeded.
# Do NOT reference this constant from default_factory (the dataclass default must
# stay empty so the absence gate in from_dict can distinguish absent vs. empty).
_DEFAULT_POWER_MODE_MAP: Dict[str, str] = {
    "code.exe": "code",
    "devenv.exe": "code",
    "windowsterminal.exe": "code",
    "powershell.exe": "code",
    "pwsh.exe": "code",
    "slack.exe": "chat",
    "discord.exe": "chat",
    # Email clients — seed new installs AND upgrading users (via the
    # seed-on-absence gate in from_dict) with the email built-in profile.
    "outlook.exe": "email",
    "thunderbird.exe": "email",
}


def get_app_dir() -> Path:
    """Return the application data directory.

    Honors the ``WHISPERSTREAM_DATA_DIR`` env var when set (and non-empty),
    otherwise falls back to the platform default
    (``%LOCALAPPDATA%\\Whisperstream`` on Windows). The override is the
    seam used to isolate dev runs from installed-app state.
    """
    override = os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(user_data_dir("Whisperstream", appauthor=False))


def get_config_path() -> Path:
    return get_app_dir() / "config.toml"


def is_loopback_endpoint(endpoint: str | None) -> bool:
    """True when *endpoint* is empty or resolves to a loopback host.

    Used to enforce the Lockdown Mode "nothing leaves the device" guarantee
    (F3): under lockdown the forced-"local" dispatch must only ever reach an
    on-device server, so a client-writable ``enhancement_local_endpoint`` may
    only be empty (use the bundled loopback supervisor) or point at a loopback
    address (``127.0.0.0/8``, ``localhost``, or ``[::1]``).

    Returns ``True`` for: ``None`` / empty / whitespace (means "use the bundled
    supervisor"), any IPv4 in ``127.0.0.0/8``, IPv6 ``::1``, and the hostname
    ``localhost``. Returns ``False`` for every other host (a real off-device
    URL), and for an unparseable value (fail closed).
    """
    if endpoint is None:
        return True
    endpoint = endpoint.strip()
    if not endpoint:
        return True
    try:
        from urllib.parse import urlparse

        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        host = parsed.hostname  # lowercased, brackets/port stripped, or None
    except Exception:
        return False
    if not host:
        return False
    host = host.lower()
    if host == "localhost":
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal and not "localhost" -> a real hostname; not loopback.
        return False


def peek_lockdown_mode() -> bool:
    """Return the ``lockdown_mode`` flag read straight from ``config.toml``.

    A cheap pre-read used at startup (F4) to harden diagnostic logging BEFORE
    the full ``ConfigManager.load()`` runs — ``load()`` itself logs config
    metadata, so the flag must be known before any diagnostic line is emitted.
    Honors ``WHISPERSTREAM_DATA_DIR`` via ``get_config_path()``. Falls back to
    ``False`` on any error (missing file, TOML parse error, absent value).
    """
    try:
        config_path = get_config_path()
        if not config_path.exists():
            return False
        with open(config_path, "rb") as _f:
            _data = tomllib.load(_f)
        return bool(_data.get("lockdown_mode", False))
    except Exception:
        return False


def _model_storage_base() -> Path:
    """Return the base directory under which model subdirs are resolved.

    Reads ``model_storage_dir`` from the config.toml at ``get_config_path()``
    and returns ``Path(model_storage_dir).expanduser()`` when the value is
    non-empty.  Falls back to ``get_app_dir()`` on any error: missing file,
    TOML parse error, permission/lock issue, or an empty/absent value.  Reads
    disk on every call so it stacks naturally with the ``WHISPERSTREAM_DATA_DIR``
    override (which ``get_config_path()`` already honors) and has no
    startup-ordering dependency.
    """
    try:
        config_path = get_config_path()
        if not config_path.exists():
            return get_app_dir()
        with open(config_path, "rb") as _f:
            _data = tomllib.load(_f)
        model_storage_dir = _data.get("model_storage_dir", "")
        if model_storage_dir and isinstance(model_storage_dir, str):
            return Path(model_storage_dir).expanduser()
    except Exception:
        pass
    return get_app_dir()


def get_models_root() -> Path:
    return _model_storage_base() / "models"


def get_log_dir() -> Path:
    return get_app_dir() / "logs"


def get_local_llm_dir() -> Path:
    """Return the directory where downloaded GGUF model files are cached.

    Resolves to ``<model_storage_base>\\local-llm\\``.  When ``model_storage_dir``
    is empty/unset the base is ``get_app_dir()`` (``%LOCALAPPDATA%\\Whisperstream``
    or the ``WHISPERSTREAM_DATA_DIR`` override equivalent).  GGUFs get their own
    sibling directory alongside ``models/`` so the two caches don't collide.
    """
    return _model_storage_base() / "local-llm"


# Dictated HUD replies after which the resting reply hints stop showing
# (owner, 2026-10-24): by then the user knows how to reply.
CLAUDE_REPLY_HINT_RETIRE_AFTER = 3


@dataclass(kw_only=True, slots=True)
class WhisperstreamConfig:
    primary_shortcut: str = "right shift"
    push_to_talk_shortcut: str = "right shift"
    toggle_shortcut: str = ""
    paste_last_shortcut: str = "ctrl+alt+v"
    paste_last_original_shortcut: str = ""
    claude_view_shortcut: str = "ctrl+alt+`"
    # Automatic follows Whisperstream's exact recommendation for `language`.
    # Manual pins the exact artifact encoded by stt_backend + parakeet_model
    # while that artifact supports the selected language.
    stt_model_selection_mode: str = "automatic"
    stt_backend: str = "parakeet"
    parakeet_model: str = "nemo-parakeet-tdt-0.6b-v3"
    parakeet_quantization: Optional[str] = "int8"
    # Qwen-only advanced toggle. Effective only in Manual mode with Qwen pinned;
    # each decode is then biased toward `language` (passed as the English
    # language name) instead of auto-detecting. Automatic, Manual Parakeet, and
    # incompatible fallback all clear it so hidden stale hints cannot survive.
    qwen_force_language: bool = False
    # Override directory for on-device model storage. Empty string = use the
    # default data dir (get_app_dir()). When set, both get_models_root() and
    # get_local_llm_dir() re-base under this path (Task 1.2). Any non-empty
    # string is valid; writability and free-space checks happen at relocation
    # time, not at load time. The scalar emit loop in save() serializes this
    # as a plain TOML string with no special handling needed.
    model_storage_dir: str = ""
    onnx_providers: str = "cpu"
    threads: Optional[int] = None
    language: Optional[str] = None
    word_boosts: List[str] = field(default_factory=list)
    word_filters: List[str] = field(default_factory=list)
    word_replacements: Dict[str, str] = field(default_factory=dict)
    post_processing: str = ""
    # True = save the user's prior clipboard before our paste and restore it
    # after clipboard_clear_delay seconds. False = leave clipboard alone (our
    # text persists).
    clipboard_behavior: bool = True
    # Seconds to wait before restoring the prior clipboard contents (when
    # clipboard_behavior is True).
    clipboard_clear_delay: float = 0.75
    # How transcribed text is delivered to the focused window.
    # "clipboard" (default) = paste via pyperclip + Shift+Insert.
    # "keystroke" = type each character via pynput (useful in Citrix/RDP
    # sessions where clipboard redirection is disabled). Keystroke mode never
    # touches the clipboard, so clipboard_behavior is a no-op while active.
    input_style: str = "clipboard"
    # Dirty flag: True means the user has explicitly chosen input_style and the
    # app must honour it as-is. False (default) means the app may auto-pick
    # (e.g. fall back to "keystroke" when a screen reader is detected). Once
    # the user makes a selection in the UI, the frontend sets this to True and
    # it persists forever. The scalar emit loop auto-serializes it; no custom
    # TOML emit line is needed. No validate() constraint: both True and False
    # are always valid.
    input_style_user_set: bool = False
    # Inter-key delay in milliseconds for keystroke mode (0 = no pacing,
    # fast path). Raise for high-latency remote sessions that drop fast
    # keystrokes. Valid values the UI exposes: 0, 5, 10, 15, 25.
    keystroke_delay_ms: int = 0
    # 0.0 = keep model in memory (no idle unload); >0 = unload after N seconds idle.
    # Default ON because the user-perceived cost is "brief delay on first recording
    # after inactivity" which is worse than the memory cost on a desktop with the
    # model already downloaded.
    model_timeout: float = 0.0
    audio_feedback: bool = True
    audio_feedback_volume: float = 1.0
    start_sound_path: Optional[str] = None
    stop_sound_path: Optional[str] = None
    error_sound_path: Optional[str] = None
    # Legacy field, kept for config migration; PTT mode ignores it entirely,
    # toggle mode uses toggle_safety_timeout instead.
    max_recording_duration: float = 0.0
    # Audio chunking is LOCKED (not user-configurable) at the constants in
    # chunking.py (CHUNK_DURATION_SECONDS / CHUNK_OVERLAP_SECONDS). The former
    # chunk_duration / chunk_overlap config fields were removed: a disabled or
    # oversized chunk window silently truncates the Qwen backend (~38s decode
    # cap), so the value is no longer exposed. Legacy keys in config.toml are
    # ignored (from_dict strips unknown keys).
    toggle_safety_timeout: float = 1800.0  # max toggle-mode recording length in seconds (0 disables)
    system_volume_mode: str = "off"
    recording_mode: str = "push_to_talk"
    run_on_startup: bool = False
    audio_input_device: str = ""
    # Presentation-only companion for Mac's opaque UID-derived input identity.
    # Windows continues to persist and display ``audio_input_device`` by name.
    audio_input_device_label: str = ""
    show_recording_indicator: bool = True
    # Opt-in persistent recording control in the existing HUD. The recording
    # indicator remains the runtime master gate; this value is persisted even
    # while the master is disabled.
    show_recording_control: bool = False
    # Opt-in raw dictation preview inside the recording HUD. The recording
    # indicator remains the runtime master gate; this value is persisted even
    # while the master is disabled.
    show_live_preview: bool = False
    claude_hud_enabled: bool = False
    # Independent Codex Agent Speak connection. Claude Code remains additive;
    # neither provider's lifecycle may overwrite the other's persisted state.
    codex_hud_enabled: bool = False
    # Master Agents switch. Off pauses every remembered provider without
    # uninstalling it: the per-provider flags above keep remembering which
    # agents to resume, and Codex hook trust is preserved.
    agents_enabled: bool = True
    # Server-authored: the bundled Claude plugin version that last completed a
    # verified mod handshake. Lets Claude read as connected with no session
    # open; a fresh install or update clears it until a session handshakes.
    claude_connected_plugin_version: str = ""
    # Legacy installation-surface preference. Retained for compatibility only;
    # one connection now covers every surface and nothing gates on it.
    claude_agent_surface: str = "auto"
    codex_agent_surface: str = "auto"
    # HUD text display (Experimental tab, seed-only): whether the HUD overlay
    # renders the agent's reply text (speech is independent of this flag).
    claude_hud_show_text: bool = True
    # Agent Speak setup flow (seed-only): whether the four-step setup flow has
    # been completed. Re-enabling Agent Speak after this is True goes straight
    # to Configure and never repeats the flow. Seed-only default; existing
    # installs are never migrated to True.
    agent_speak_setup_completed: bool = False
    # Per-agent "verified" signal (independent of the shared setup-completed
    # flag above): flips True the first time a real completion arrives from
    # that agent (kind=="response" in server.py._handle_agent_event) OR when
    # that agent's onboarding exercise completes. Lets the UI retire the
    # stale per-agent "run /hooks" / "run /reload-plugins" hint once the
    # agent has actually produced a message. Never migrated for existing
    # installs; only ever flipped forward, never reset.
    codex_verified: bool = False
    claude_verified: bool = False
    # Agent speech (-Experimental gated). The normalized contract separates the
    # master toggle, session activation, and summary ownership. Only a marked
    # agent-authored summary is currently speakable; Whisperstream-authored
    # summaries are a deferred source. Existing config files may still carry the
    # legacy mode below, which from_dict maps into the master toggle without
    # rewriting or discarding the stored legacy value.
    agent_speech_enabled: bool = False
    # Show written summaries for coding agent replies in the HUD overlay.
    # Independent from agent_speech_enabled; defaults to False.
    agent_summary_text_enabled: bool = False
    agent_speech_content: str = "summary"  # summary | short | full
    agent_speech_short_seconds: int = 20
    agent_speech_activation: str = "always_on"  # "per_session" | "always_on"
    agent_speech_summary_source: str = "agent"  # "agent" | "whisperstream"
    # Legacy compatibility field. New code must use the normalized fields above.
    agent_speech_mode: str = "off"  # "off" | "verbatim" | "summary" | "conversational"
    # Voice engine selection. New preferences default to Supertonic; complete
    # cached Kokoro permits preservation of existing Kokoro/Automatic choices.
    agent_speech_engine: str = field(default_factory=lambda: "auto" if platform.system() == "Darwin" else "supertonic")
    agent_speech_voice: int = 1  # speaker id; Supertonic 0..9 (default Voice 1)
    agent_speech_kokoro_voice: str = "af_heart"  # default Heart
    agent_speech_speed: float = 1.0  # 0.5..2.0
    agent_speech_volume: float = 1.0  # 0.0..1.0
    agent_speech_idle_timeout_seconds: int = 1800
    # Claude interaction controls. Stop replies remain passively routable for
    # the configured whole-second grace; zero disables passive reply routing.
    # The separate engaged ceiling remains fixed at two minutes.
    claude_reply_grace_seconds: int = 6
    # Visually silent interval after one response route is terminal and before
    # the next queued Claude response is promoted.
    claude_reply_queue_delay_seconds: int = 2
    # Routed hotkey replies remain display-first unless the user explicitly
    # opts into sending the authoritative final as soon as it is ready.
    claude_reply_auto_submit: bool = False
    # Server-authored: dictated replies delivered from the HUD, counted up to
    # CLAUDE_REPLY_HINT_RETIRE_AFTER. The resting reply hints retire there.
    claude_reply_hint_dictated_replies: int = 0
    claude_permission_decisions_enabled: bool = False
    setup_completed: bool = False
    hotword_score: float = 1.5
    beam_width: int = 4
    # Enhancement (AI post-processing) fields
    enhancement_enabled: bool = False
    enhancement_provider: str = "gemini"
    # Active-provider mirror of the per-provider slot, kept in sync by
    # from_dict so legacy code paths (older tests, older API consumers
    # reading the field directly) keep working. Per-provider slots below
    # are the source of truth.
    enhancement_api_key: str = ""
    # Per-provider key slots — preserve credentials across provider
    # switches. Frontend writes the slot for the active provider; from_dict
    # mirrors that value into enhancement_api_key.
    enhancement_gemini_api_key: str = ""
    enhancement_openai_api_key: str = ""
    enhancement_anthropic_api_key: str = ""
    enhancement_anthropic_workspace_id: str = ""
    enhancement_thinking_levels: Dict[str, str] = field(default_factory=dict)
    # Custom OpenAI-compatible provider fields (base URL + model + key).
    enhancement_custom_base_url: str = ""
    enhancement_custom_model: str = ""
    # User-entered IDs, grouped by provider; saved with ordinary settings.
    enhancement_saved_models: Dict[str, List[str]] = field(default_factory=dict)
    enhancement_custom_api_key: str = ""
    enhancement_ollama_endpoint: str = "http://localhost:11434"
    enhancement_ollama_model: str = "llama3.2"
    enhancement_gemini_model: str = "gemini-3.5-flash-lite"
    enhancement_openai_model: str = "gpt-5.6-luna"
    enhancement_anthropic_model: str = "claude-haiku-4-5-20251001"
    enhancement_mode: str = "default"
    enhancement_custom_prompt: str = ""
    enhancement_local_model: str = "stream-1-mini"
    enhancement_local_port: int = 0
    enhancement_local_endpoint: str = ""
    # Master output postprocessing toggle for all enhancement providers.
    # Empty responses and request failures still fall back to the source.
    enhancement_safeguards_enabled: bool = True
    # Per-request enhancement latency budget. Applies independently to each
    # whole-text, preview, connection-check, or chunk request.
    enhancement_timeout_seconds: float = 5.0
    # Legacy Stage 2 preference retained for config/API round trips only.
    # Semantic and structure rejection are retired regardless of this value.
    enhancement_output_guard_mode: str = "off"
    # 0.0 = keep LLM in memory (no idle unload); >0 = unload after N seconds idle.
    # Mirrors model_timeout (Parakeet) but defaults to ON-by-default because the
    # local LLM is ~1.6 GiB resident vs Parakeet's ~600 MB, and is invoked less
    # often. The user-perceived cost is "first dictation after the idle window
    # ships raw text (no LLM wait) while the model spawns + warms in the
    # background; subsequent dictations get the ~1s cleaned output."
    enhancement_local_idle_timeout: float = 600.0
    # Enhancement profiles: each is a {"id", "name", "prompt"} cleanup style.
    # Custom profiles may also carry one bounded optional `icon` key. Built-in
    # glyphs are derived from their immutable ids and never persisted here.
    # Empty by default — new installs seed bundled Chat/Code in load()'s no-file
    # branch (Task 1.4); existing users get migrated from the legacy
    # enhancement_mode/enhancement_custom_prompt pair in from_dict (Task 1.2).
    # Keeping the default empty is load-bearing for migration idempotency.
    profiles: List[Dict[str, str]] = field(default_factory=list)
    # Id of the profile that runs by default (the global default pointer that
    # replaces the role of the legacy enhancement_mode).
    default_profile_id: str = ""
    # Per-app profile routing: lowercased process name -> profile id. Empty by
    # default (must stay empty so from_dict's absence gate can distinguish an
    # absent key from a deliberately-cleared {}). New installs seed
    # _DEFAULT_POWER_MODE_MAP in load()'s no-file branch; upgrading users whose
    # config predates this field get the same defaults via from_dict's
    # seed-on-absence gate. A present-but-empty map is preserved.
    power_mode_map: Dict[str, str] = field(default_factory=dict)
    # Recorded exe path per power_mode_map entry: lowercased process basename ->
    # full exe path, captured when a rule is added (from the running app or the
    # Browse picker). Lets install detection check path existence for apps no
    # registry source lists (Steam games, portable exes) and detect a real
    # uninstall (the recorded path is gone). Empty by default (NO seeding);
    # validate() prunes entries whose basename is no longer in power_mode_map.
    # Never logged.
    power_mode_paths: Dict[str, str] = field(default_factory=dict)
    # Per-site profile routing: lowercased registrable domain -> profile id.
    # Empty by default for ALL installs (no seeding, unlike power_mode_map).
    # Keys are registrable-domain strings already normalized by the frontend or
    # server before writing (e.g. "github.com", "gmail.com"). from_dict
    # lowercases keys and drops blanks; validate() prunes stale profile ids via
    # the same seen_profile_ids set used for power_mode_map so a profile delete
    # prunes both app and site rules in one pass.
    site_map: Dict[str, str] = field(default_factory=dict)
    # Per-profile manual-switch hotkey: profile id -> shortcut string (e.g.
    # "ctrl+alt+1"). Pressing the hotkey sets a sticky manual override that the
    # next dictation(s) use, until focus moves to a different app (issue 14).
    # Empty by default for ALL installs (NO seeding, mirroring site_map). from_dict
    # coerces values to lowercase strings and drops blanks; validate() prunes
    # entries whose profile id is gone (reusing the seen_profile_ids set), and
    # drops any hotkey that collides with primary_shortcut / paste_last_shortcut
    # or is duplicated across profiles (drop the offender + warn, never raise).
    profile_hotkeys: Dict[str, str] = field(default_factory=dict)
    # Per-model GPU benchmark verdict cache (tiered-benchmark). Maps a
    # LOCAL_BUNDLED_MODELS id -> tri-state verdict ("not_run"/"viable"/
    # "marginal"/"unviable"). This is the single source of truth for GPU
    # gating/runtime; a model id absent from the map has never been
    # benchmarked, i.e. reads as "not_run" -- see the module-level
    # ``get_local_gpu_verdict()`` helper below, the canonical read site.
    # from_dict migrates the pre-tiered-benchmark single global verdict onto
    # the measured model's entry the first time a config without this map
    # loads (see that migration block); GPU-fingerprint changes clear the
    # WHOLE map, not just one entry (server.py). Defaults to {} for both new
    # installs and configs that already migrated -- never auto-seeded beyond
    # the one-time migration.
    enhancement_local_gpu_verdicts: Dict[str, str] = field(default_factory=dict)
    enhancement_local_gpu_artifacts: Dict[str, str] = field(default_factory=dict)
    # Device identity string recorded when the benchmark ran (e.g. the Vulkan
    # device name). Used to detect hardware changes that warrant a re-check.
    # Empty string means no benchmark has been run or the device is unknown.
    enhancement_local_gpu_device: str = ""
    # Whether the one-time "try on-device cleanup" first-visit nudge has been
    # shown/dismissed. The nudge (Feature #2) fires once on the first
    # Enhancement-tab visit for a user who is on a cloud provider and has not
    # run the GPU benchmark yet (see Enhancement.tsx). Persists across sessions
    # so the nudge fires at most once.
    enhancement_local_nudge_seen: bool = False
    # Vulkan device index of the GPU the benchmark selected as best, used to pin
    # the supervisor via ``--device VulkanN``. ``-1`` means none selected (not
    # benched, or unviable). Refreshed at launch if device ordering shifts.
    enhancement_local_gpu_device_index: int = -1
    # Benchmark-owned model -> winning device identity. Never infer across models.
    enhancement_local_gpu_devices: Dict[str, int] = field(default_factory=dict)
    enhancement_local_gpu_device_names: Dict[str, str] = field(default_factory=dict)
    # Order-independent signature of ALL real GPUs present when the benchmark ran
    # (name + total VRAM per device). On each launch the current set is compared
    # to this; any add/remove triggers a re-benchmark. Empty = never benched.
    enhancement_local_gpu_fingerprint: str = ""
    # When True (default), long transcripts are cleaned in sentence-aligned
    # chunks so text starts appearing sooner (round 03 progressive paste). When
    # False, every transcript takes the single-call path regardless of length.
    # User-facing toggle under Advanced; client-writable (not server-authoritative).
    enhancement_chunked_cleanup: bool = True
    # Transcripts above this word count enter progressive cleanup. Sentence
    # alignment means planned chunks can vary around the target.
    enhancement_words_per_chunk: int = 120
    # Cap only the first target at 60 words for a faster first cleaned paste.
    # When false, the first target matches enhancement_words_per_chunk.
    enhancement_limit_first_chunk: bool = True
    # (email_signature is no longer a global field. The default email sign-off is
    # now a PER-PROFILE field -- profile["signature"] -- applied by main.py's
    # _apply_email_signature for whichever profile ran the cleanup. from_dict
    # migrates any legacy top-level email_signature onto the "email" profile.)
    # Transcripts tab (history persistence). Plain scalars, not secrets.
    # history_enabled is the master save switch ("Save transcripts"); when False
    # no new dictations are persisted. save_audio (nested under history) also
    # stores the WAV when history is on; False = text-only history.
    # transcript_retention_days: how long to keep transcripts before the
    # retention sweep purges them. 0 = Forever (never age-purge). Allowed UI
    # values are 0/1/7/30. New installs get these defaults; existing configs
    # simply gain them on next load (no auto-migration).
    history_enabled: bool = field(default_factory=lambda: platform.system() != "Darwin")
    # Separate consent from an old install's inherited history_enabled=True.
    # Server-owned through the explicit Mac history/Lockdown opt-in endpoints.
    mac_history_opt_in: bool = False
    save_audio: bool = True
    transcript_retention_days: int = 7
    # Lockdown Mode (HIPAA-conscious workflows). When True, diagnostic/telemetry
    # logging is disabled, cloud AI providers are hard-blocked (only the on-device
    # bundled LLM runs), transcript access requires a password, and an idle
    # auto-lock timer is active. These safeguards align with the Security Rule's
    # access-control, auto-logoff, and encryption requirements WITHOUT constituting
    # a HIPAA certification or unqualified "HIPAA compliant" claim.
    #
    # lockdown_idle_lock_seconds: seconds of inactivity before the transcript
    # surface auto-locks. 0 = Never (auto-lock disabled). Default 300 (5 min).
    #
    # Lock/backoff counters are NEVER stored here (plaintext-editable config.toml
    # would defeat the lockout). Only these two scalar flags belong in config.
    lockdown_mode: bool = False
    lockdown_idle_lock_seconds: int = 300
    # UI theme. Valid values: light / dark.
    # New field — existing configs load the default "light" (fresh default). The
    # normalizer below aliases the pre-rename names (glacier -> light,
    # glacier-dark -> dark) so an in-flight config keeps its choice.
    theme: str = "light"
    # Feature-spotlight / upgrade-offer overlay: which offers the user has already
    # dismissed or been shown, so the launch overlay never re-nags. Both empty by
    # default for ALL installs (NO seeding); from_dict drops non-strings/blanks and
    # dedups order-preserving. Offer ids are case-sensitive and stored verbatim.
    dismissed_offer_ids: List[str] = field(default_factory=list)
    shown_offer_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        protected_secret_store: Any = None,
    ) -> "WhisperstreamConfig":
        merged: Dict[str, Any] = dict(data)
        merged["mac_history_opt_in"] = merged.get("mac_history_opt_in") is True
        if platform.system() == "Darwin" and not merged["mac_history_opt_in"]:
            merged["history_enabled"] = False
        merged["enhancement_saved_models"] = normalize_saved_models(data)
        from .enhancement_thinking import normalize_thinking_levels, normalize_workspace_id
        merged["enhancement_thinking_levels"] = normalize_thinking_levels(merged.get("enhancement_thinking_levels"))
        merged["enhancement_anthropic_workspace_id"] = normalize_workspace_id(merged.get("enhancement_anthropic_workspace_id", ""))
        # Legacy: silently drop word_overrides; no migration (no production users).
        merged.pop("word_overrides", None)

        merged["word_boosts"] = normalize_word_list(merged.get("word_boosts", []), preserve_case=True)
        merged["word_filters"] = normalize_word_list(merged.get("word_filters", []))

        merged["dismissed_offer_ids"] = normalize_offer_ids(merged.get("dismissed_offer_ids", []))
        merged["shown_offer_ids"] = normalize_offer_ids(merged.get("shown_offer_ids", []))
        replacements_raw = merged.get("word_replacements", {}) or {}
        merged["word_replacements"] = {
            str(k).lower(): str(v)
            for k, v in replacements_raw.items()
            if str(k).strip() and str(v).strip()
        }
        merged.update(normalize_routing_fields(data, default_app_rules=_DEFAULT_POWER_MODE_MAP))

        # Migration: primary_shortcut + recording_mode -> push_to_talk_shortcut / toggle_shortcut
        # Gated on BOTH new fields being absent (frontend PUTs the full config on every
        # save; an ungated copy would clobber user edits on every round-trip).
        if "push_to_talk_shortcut" not in merged and "toggle_shortcut" not in merged:
            legacy = str(merged.get("primary_shortcut", "right shift"))
            mode = str(merged.get("recording_mode", "push_to_talk")).lower()
            if mode == "toggle":
                merged["toggle_shortcut"] = legacy
                merged["push_to_talk_shortcut"] = ""
            else:
                merged["push_to_talk_shortcut"] = legacy
                merged["toggle_shortcut"] = ""

        if "primary_shortcut" in merged:
            merged["primary_shortcut"] = str(merged["primary_shortcut"]).lower()
        if "push_to_talk_shortcut" in merged:
            merged["push_to_talk_shortcut"] = str(merged["push_to_talk_shortcut"]).lower()
        if "toggle_shortcut" in merged:
            merged["toggle_shortcut"] = str(merged["toggle_shortcut"]).lower()
        if "paste_last_shortcut" in merged:
            merged["paste_last_shortcut"] = str(merged["paste_last_shortcut"]).lower()
        if "paste_last_original_shortcut" in merged:
            merged["paste_last_original_shortcut"] = str(merged["paste_last_original_shortcut"]).lower()
        if "claude_view_shortcut" in merged:
            merged["claude_view_shortcut"] = str(merged["claude_view_shortcut"]).lower()
        # Kitten was the former voice engine. Preserve the stored TOML while
        # mapping legacy installs to the lightweight successor in memory.
        default_speech_engine = "auto" if platform.system() == "Darwin" else "supertonic"
        idle_timeout = merged.get("agent_speech_idle_timeout_seconds")
        if type(idle_timeout) is not int or idle_timeout not in AGENT_SPEECH_IDLE_TIMEOUT_CHOICES:
            merged["agent_speech_idle_timeout_seconds"] = 1800
        agent_speech_engine = merged.get("agent_speech_engine")
        if isinstance(agent_speech_engine, str):
            normalized_engine = agent_speech_engine.strip().casefold()
            if normalized_engine in {"kitten", "kittentts"}:
                logger.warning(
                    "config legacy agent speech engine fallback "
                    "engine=%s fallback=%s",
                    normalized_engine, default_speech_engine,
                )
                merged["agent_speech_engine"] = default_speech_engine
            elif normalized_engine in {"auto", "kokoro", "supertonic"}:
                merged["agent_speech_engine"] = normalized_engine
            else:
                merged["agent_speech_engine"] = default_speech_engine
        else:
            merged["agent_speech_engine"] = default_speech_engine

        supertonic_voice = merged.get("agent_speech_voice", 1)
        merged["agent_speech_voice"] = (
            supertonic_voice
            if (
                isinstance(supertonic_voice, int)
                and not isinstance(supertonic_voice, bool)
                and 0 <= supertonic_voice <= 9
            )
            else 1
        )
        from .speech.availability import kokoro_available
        _storage = merged.get("model_storage_dir")
        _voice_root = Path(_storage).expanduser() / "models" if isinstance(_storage, str) and _storage else None
        if not kokoro_available(models_root=_voice_root):
            merged["agent_speech_engine"] = default_speech_engine

        kokoro_voice = merged.get("agent_speech_kokoro_voice")
        if isinstance(kokoro_voice, str):
            kokoro_voice = kokoro_voice.strip().casefold()
        merged["agent_speech_kokoro_voice"] = (
            kokoro_voice
            if kokoro_voice in _AGENT_SPEECH_KOKORO_VOICES
            else "af_heart"
        )
        # Claude HUD Complete: normalize the new speech/interaction contract
        # without destructively migrating the legacy agent_speech_mode field.
        # Old non-off modes mean "the user had speech enabled"; every legacy
        # summary strategy safely converges on the marked agent-summary source.
        if "agent_speech_enabled" not in data:
            legacy_mode = merged.get("agent_speech_mode", "off")
            merged["agent_speech_enabled"] = (
                isinstance(legacy_mode, str)
                and legacy_mode in {"verbatim", "summary", "conversational"}
            )

        def _normalized_bool(key: str, default: bool) -> bool:
            raw = merged.get(key, default)
            return raw if isinstance(raw, bool) else default

        merged["agent_speech_enabled"] = _normalized_bool(
            "agent_speech_enabled", False
        )
        merged["agent_summary_text_enabled"] = _normalized_bool(
            "agent_summary_text_enabled", False
        )
        # Replace the legacy reply-parking toggle without changing an existing
        # user's choice: enabled becomes the six-second default, disabled
        # becomes Off. The numeric field wins when both appear in a client PUT.
        if "claude_reply_grace_seconds" not in data:
            legacy_parking = data.get("claude_reply_parking_enabled")
            if isinstance(legacy_parking, bool):
                merged["claude_reply_grace_seconds"] = (
                    6 if legacy_parking else 0
                )
        reply_grace = merged.get("claude_reply_grace_seconds", 6)
        merged["claude_reply_grace_seconds"] = (
            reply_grace
            if (
                isinstance(reply_grace, int)
                and not isinstance(reply_grace, bool)
                and 0 <= reply_grace <= 30
            )
            else 6
        )
        reply_queue_delay = merged.get(
            "claude_reply_queue_delay_seconds", 2
        )
        merged["claude_reply_queue_delay_seconds"] = (
            reply_queue_delay
            if (
                isinstance(reply_queue_delay, int)
                and not isinstance(reply_queue_delay, bool)
                and 0 <= reply_queue_delay <= 10
            )
            else 2
        )
        merged["claude_reply_auto_submit"] = _normalized_bool(
            "claude_reply_auto_submit", False
        )
        hint_replies = merged.get("claude_reply_hint_dictated_replies", 0)
        merged["claude_reply_hint_dictated_replies"] = (
            min(hint_replies, CLAUDE_REPLY_HINT_RETIRE_AFTER)
            if isinstance(hint_replies, int)
            and not isinstance(hint_replies, bool)
            and hint_replies >= 0
            else 0
        )
        merged["claude_permission_decisions_enabled"] = _normalized_bool(
            "claude_permission_decisions_enabled", False
        )

        content = merged.get("agent_speech_content", "summary")
        merged["agent_speech_content"] = content if content in ("summary", "short", "full") else "summary"
        short_seconds = merged.get("agent_speech_short_seconds", 20)
        merged["agent_speech_short_seconds"] = (
            short_seconds if type(short_seconds) is int and 5 <= short_seconds <= 120 else 20
        )

        for surface_field in ("claude_agent_surface", "codex_agent_surface"):
            value = merged.get(surface_field, "auto")
            merged[surface_field] = value if isinstance(value, str) and value in {"auto", "cli", "desktop", "both"} else "auto"

        activation = merged.get("agent_speech_activation", "always_on")
        if isinstance(activation, str):
            activation = activation.lower()
        merged["agent_speech_activation"] = (
            activation
            if activation in {"per_session", "always_on"}
            else "always_on"
        )

        summary_source = merged.get("agent_speech_summary_source", "agent")
        if isinstance(summary_source, str):
            summary_source = summary_source.lower()
        merged["agent_speech_summary_source"] = (
            summary_source
            if summary_source in {"agent", "whisperstream"}
            else "agent"
        )

        if "onnx_providers" in merged:
            merged["onnx_providers"] = str(merged["onnx_providers"]).lower()
        if "recording_mode" in merged:
            merged["recording_mode"] = str(merged["recording_mode"]).lower()
        if "system_volume_mode" in merged:
            merged["system_volume_mode"] = str(merged["system_volume_mode"]).lower()
        if "enhancement_provider" in merged:
            merged["enhancement_provider"] = str(merged["enhancement_provider"]).lower()
        if "enhancement_mode" in merged:
            merged["enhancement_mode"] = str(merged["enhancement_mode"]).lower()
        if "enhancement_output_guard_mode" in merged:
            merged["enhancement_output_guard_mode"] = str(
                merged["enhancement_output_guard_mode"]
            ).lower()
        if "enhancement_local_model" in merged:
            _requested_local_model = str(merged["enhancement_local_model"]).lower()
            # Catalog-membership validation includes the fail-closed
            # development overlay when it is enabled. Resolve the requested
            # case-insensitive value back to the catalog's canonical exact ID
            # so a future registry entry is not rewritten merely because its
            # experiment identifier contains uppercase characters.
            from whisperstream.enhancement_models import get_local_model_catalog

            _catalog_model = next(
                (
                    model
                    for model in get_local_model_catalog()
                    if str(model["id"]).casefold()
                    == _requested_local_model.casefold()
                ),
                None,
            )
            if _catalog_model is None:
                logger.warning(
                    "config validation failed field=enhancement_local_model "
                    "reason=unknown_id action=use_default",
                )
                merged["enhancement_local_model"] = "stream-1-mini"
            else:
                merged["enhancement_local_model"] = str(_catalog_model["id"])

        # Migrate the pre-tiered-benchmark single global GPU verdict (the old
        # on-disk scalar key, read below) onto the new per-model map (a
        # verdict per LOCAL_BUNDLED_MODELS id). Reads the RAW legacy value
        # from ``merged`` before the map coercion below runs, so a legacy
        # value that predates the "marginal" tier still migrates correctly.
        # The scalar dataclass field is gone; the old on-disk key survives
        # only in configs written by pre-tiered-benchmark versions, and
        # from_dict strips it at construction (unknown-key filter) once this
        # migration has consumed it.
        #
        # Gate: runs ONLY when the map key is strictly ABSENT from the
        # incoming data. A present key (even {}) means this config already
        # migrated, or is a fresh post-migration config with no benchmark run
        # yet -- re-seeding would resurrect a verdict a later
        # benchmark/reset may have since cleared. Mirrors the power_mode_map
        # / profiles seed-on-absence gate style used elsewhere in this method.
        #
        # The legacy value was recorded for whichever model was actually
        # measured, so it seeds THAT model's entry, not a fixed one:
        # enhancement_local_model if it names a known LOCAL_BUNDLED_MODELS
        # id, else Gemma (the pre-tiered-benchmark default local model, so an upgrading
        # user who never touched enhancement_local_model migrates onto the
        # entry that matches what was actually benched). Never rewrites the
        # user's enhancement_local_model / enhancement_provider.
        if "enhancement_local_gpu_verdicts" not in merged:
            legacy_verdict = merged.get("enhancement_local_gpu_verdict")
            if isinstance(legacy_verdict, str) and legacy_verdict not in ("", "not_run"):
                from whisperstream.enhancement_models import LOCAL_BUNDLED_MODELS  # noqa: PLC0415

                _known_local_ids = {m["id"] for m in LOCAL_BUNDLED_MODELS}
                # Only an explicitly saved model counts; the current default is
                # Stream-1 Mini, which no pre-tiered-benchmark version measured.
                _current_local_model = data.get("enhancement_local_model")
                measured_id = (
                    _current_local_model
                    if isinstance(_current_local_model, str)
                    and _current_local_model in _known_local_ids
                    else "gemma4-e2b-instruct"
                )
                merged["enhancement_local_gpu_verdicts"] = {measured_id: legacy_verdict}

        # Validate/coerce the per-model verdict map: a non-dict value (hand
        # edit) normalizes to {}; entries with a non-string key OR a
        # non-string value are dropped entirely; entries that survive but
        # whose value isn't one of the four known tri-state verdicts are
        # coerced to "not_run" rather than left as an unknown string a caller
        # might treat as usable.
        _VALID_MAP_VERDICTS = frozenset({"not_run", "viable", "marginal", "unviable"})
        _raw_verdict_map = merged.get("enhancement_local_gpu_verdicts")
        if not isinstance(_raw_verdict_map, dict):
            merged["enhancement_local_gpu_verdicts"] = {}
        else:
            merged["enhancement_local_gpu_verdicts"] = {
                k: (v if v in _VALID_MAP_VERDICTS else "not_run")
                for k, v in _raw_verdict_map.items()
                if isinstance(k, str) and isinstance(v, str)
            }

        # Coerce the selected-device index to int (manual edits / the wire may
        # send a string); fall back to -1 (no pinned device).
        if "enhancement_local_gpu_device_index" in merged:
            try:
                merged["enhancement_local_gpu_device_index"] = int(
                    merged["enhancement_local_gpu_device_index"]
                )
            except (TypeError, ValueError):
                merged["enhancement_local_gpu_device_index"] = -1
        # The fingerprint is a free-form signature string; coerce non-strings.
        if "enhancement_local_gpu_fingerprint" in merged and not isinstance(
            merged["enhancement_local_gpu_fingerprint"], str
        ):
            merged["enhancement_local_gpu_fingerprint"] = ""

        # Coerce transcript_retention_days to a non-negative int. Manual edits /
        # the wire may send a string; a negative value is clamped to 0 (Forever)
        # rather than left to mean an impossible "purge everything" window.
        if "transcript_retention_days" in merged:
            try:
                days = int(merged["transcript_retention_days"])
            except (TypeError, ValueError):
                days = 7
            merged["transcript_retention_days"] = max(0, days)

        # Coerce lockdown_idle_lock_seconds to a non-negative int. Mirrors the
        # transcript_retention_days coercion above. Invalid/missing -> 300 (default);
        # negative -> clamp to 0 (Never). This field is only present in configs
        # written by lockdown-aware versions; older configs simply use the default.
        if "lockdown_idle_lock_seconds" in merged:
            try:
                idle_secs = int(merged["lockdown_idle_lock_seconds"])
            except (TypeError, ValueError):
                idle_secs = 300
            merged["lockdown_idle_lock_seconds"] = max(0, idle_secs)

        # Normalize input_style: coerce unrecognized values (bad strings, ints,
        # None) to "clipboard" so hand-edited configs can't poison injection
        # routing. Only the two valid styles pass through unchanged.
        _VALID_INPUT_STYLES = frozenset({"clipboard", "keystroke"})
        raw_input_style = merged.get("input_style", "clipboard")
        if not isinstance(raw_input_style, str) or raw_input_style not in _VALID_INPUT_STYLES:
            merged["input_style"] = "clipboard"

        # Normalize theme: alias the pre-rename names (Classic was cut; Glacier
        # became "light" and Glacier Dark became "dark") so an in-flight config
        # keeps its choice, then coerce anything unrecognized to "light" so a
        # hand-edited bad value doesn't crash startup.
        _THEME_ALIASES = {"glacier": "light", "glacier-dark": "dark", "classic": "light"}
        _VALID_THEMES = frozenset({"light", "dark"})
        raw_theme = merged.get("theme", "light")
        if isinstance(raw_theme, str) and raw_theme in _THEME_ALIASES:
            raw_theme = _THEME_ALIASES[raw_theme]
        if not isinstance(raw_theme, str) or raw_theme not in _VALID_THEMES:
            merged["theme"] = "light"
        else:
            merged["theme"] = raw_theme

        # Coerce input_style_user_set to bool. Truthy/falsy ints and other
        # bool-coercible types are accepted; the field must always be a bool.
        if "input_style_user_set" in merged:
            merged["input_style_user_set"] = bool(merged["input_style_user_set"])

        # Coerce keystroke_delay_ms to a non-negative int. Manual edits / the
        # wire may send a string; a negative value is clamped to 0 (no pacing).
        if "keystroke_delay_ms" in merged:
            try:
                delay = int(merged["keystroke_delay_ms"])
            except (TypeError, ValueError):
                delay = 0
            merged["keystroke_delay_ms"] = max(0, delay)

        # Decrypt API key if it looks like an encrypted value. Applies to
        # the legacy active-mirror field plus each per-provider slot.
        from whisperstream.key_encryption import decrypt_api_key  # noqa: PLC0415
        for key_field in _API_KEY_FIELDS:
            value = merged.get(key_field, "")
            if isinstance(value, str) and value.startswith(("enc:", "keychain:v1:")):
                merged[key_field] = decrypt_api_key(
                    value,
                    store=protected_secret_store,
                )

        # Per-provider key migration + reconciliation. The legacy
        # enhancement_api_key field used to be the only storage; per-provider
        # slots now hold credentials so users can switch providers without
        # re-entering keys. On first load (no slot populated yet), seed the
        # active provider's slot from the legacy field. After migration,
        # mirror the active slot back into the legacy field so callers
        # reading enhancement_api_key still see the current key.
        #
        # The migration MUST gate on "no slot anywhere has a value." On every
        # PUT the frontend re-sends the full config, so the legacy mirror
        # arrives carrying the *previous* provider's key. Without the guard,
        # switching from a configured provider to an unconfigured one would
        # copy the prior provider's key into the new provider's empty slot.
        provider = (merged.get("enhancement_provider") or "").lower()
        slot_attr = _PROVIDER_KEY_SLOT.get(provider)
        if slot_attr is not None:
            slot_value = merged.get(slot_attr, "") or ""
            legacy_value = merged.get("enhancement_api_key", "") or ""
            any_slot_populated = any(
                (merged.get(field, "") or "") for field in _PROVIDER_KEY_SLOT.values()
            )
            if not slot_value and legacy_value and not any_slot_populated:
                merged[slot_attr] = legacy_value
                slot_value = legacy_value
            # Reconcile so disk + wire forms agree on the active key.
            merged["enhancement_api_key"] = slot_value

        # Legacy migration: mute_system_audio -> system_volume_mode
        if "mute_system_audio" in merged:
            merged["system_volume_mode"] = "mute" if merged["mute_system_audio"] else "off"
            del merged["mute_system_audio"]

        merged.update(migrate_profiles(merged))

        # Normalize nullable fields: empty string -> None
        for nullable_field in ("parakeet_quantization", "language", "threads",
                               "start_sound_path", "stop_sound_path", "error_sound_path"):
            val = merged.get(nullable_field)
            if val is not None and isinstance(val, str) and val.strip() == "":
                merged[nullable_field] = None

        quant = merged.get("parakeet_quantization")
        if quant is not None:
            merged["parakeet_quantization"] = str(quant).lower()

        lang = merged.get("language")
        if lang is not None:
            code = str(lang).lower()
            if code not in SUPPORTED_LANGUAGES:
                exc = ValueError(
                    f"language must be one of the 39 supported codes or None, got {code!r}"
                )
                _log_validation_warning(exc)
                merged["language"] = None
            else:
                merged["language"] = code

        # Normalize Automatic/Manual routing after language normalization. The
        # key-presence bit is load-bearing migration state: an old config that
        # already points at a compatible non-recommended artifact is inferred
        # as Manual so upgrading never silently changes its working recognizer.
        from .language_routing import normalize_stt_selection

        selection = normalize_stt_selection(
            language=merged.get("language"),
            selection_mode=merged.get("stt_model_selection_mode"),
            selection_mode_present="stt_model_selection_mode" in data,
            stt_backend=merged.get("stt_backend"),
            parakeet_model=merged.get("parakeet_model"),
            qwen_force_language=bool(merged.get("qwen_force_language", False)),
        )
        merged["stt_model_selection_mode"] = selection.mode
        merged["stt_backend"] = selection.backend
        merged["parakeet_model"] = selection.parakeet_model
        merged["qwen_force_language"] = selection.qwen_force_language

        # Strip unknown keys so stale config fields don't crash the app.
        # Fold the retired optional Google row into the one public Gemma model.
        from . import gemma_artifacts as artifacts
        raw_bindings = merged.get("enhancement_local_gpu_artifacts", {})
        merged["enhancement_local_gpu_artifacts"] = {
            k: v for k, v in raw_bindings.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw_bindings, dict) else {}
        if data.get("enhancement_local_model") == artifacts.GOOGLE_ID:
            merged["enhancement_local_model"] = artifacts.PUBLIC_ID
            directory = get_local_llm_dir()
            google = artifacts.artifact_path(directory, artifacts.GOOGLE_ID)
            if artifacts.present(google):
                artifacts.select(directory, artifacts.GOOGLE_ID)
                for field_name in ("enhancement_local_gpu_verdicts", "enhancement_local_gpu_devices", "enhancement_local_gpu_device_names"):
                    mapping = dict(merged.get(field_name) or {})
                    if artifacts.GOOGLE_ID in mapping:
                        mapping[artifacts.PUBLIC_ID] = mapping.pop(artifacts.GOOGLE_ID)
                    elif field_name == "enhancement_local_gpu_verdicts":
                        mapping.pop(artifacts.PUBLIC_ID, None)
                    merged[field_name] = mapping
                merged["enhancement_local_gpu_artifacts"][artifacts.PUBLIC_ID] = artifacts.receipt(directory)
            else:
                merged["enhancement_local_gpu_verdicts"].pop(artifacts.PUBLIC_ID, None)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in merged.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the in-memory config shape without persistence side effects.

        ``ConfigManager.save`` is the sole owner of protecting API keys and
        managing their provider references. Client responses must use
        ``ConfigManager.client_dict`` so plaintext never crosses the backend
        boundary.
        """

        payload = asdict(self)
        payload["enhancement_local_gpu_verdicts"] = {
            key: get_local_gpu_verdict(self, key)
            for key in self.enhancement_local_gpu_verdicts
            if key != "gemma4-e2b-google-qat"
        }
        payload["enhancement_local_gpu_artifacts"] = dict(self.enhancement_local_gpu_artifacts)
        return payload

    def active_api_key(self) -> str:
        """Return the API key for the currently selected provider.

        Reads from the per-provider slot for ``self.enhancement_provider``,
        falling back to ``self.enhancement_api_key`` when the slot is empty
        so callers that constructed the dataclass directly (older tests,
        direct ``WhisperstreamConfig(...)`` instantiation) still resolve a
        key. Providers without a slot (ollama, local) always return ``""``.
        """
        provider = (self.enhancement_provider or "").lower()
        slot_attr = _PROVIDER_KEY_SLOT.get(provider)
        if slot_attr is None:
            return ""
        slot_value = getattr(self, slot_attr, "") or ""
        if slot_value:
            return slot_value
        return self.enhancement_api_key or ""

    def effective_provider(self) -> str:
        """Return the provider that should be used for LLM enhancement RIGHT NOW.

        This is the SINGLE provider-resolution chokepoint for the entire
        application. All dispatch, consent-gate, and warm-up logic must read
        ``effective_provider()`` rather than ``enhancement_provider`` directly.

        When ``lockdown_mode`` is True this always returns ``"local"``
        (the on-device bundled LLM) regardless of the stored
        ``enhancement_provider`` value. The stored ``enhancement_provider`` is
        intentionally left untouched — so that when the user turns Lockdown
        Mode back off their original cloud-provider selection is fully restored
        without any extra migration or save step.

        When ``lockdown_mode`` is False this returns ``self.enhancement_provider``
        verbatim (including ``"local"`` when the user has explicitly selected it).
        """
        if self.lockdown_mode:
            return "local"
        return self.enhancement_provider

    def validate(self) -> None:
        from .enhancement_security import validate_config_endpoints
        validate_config_endpoints(self)
        from .enhancement_thinking import normalize_thinking_levels, normalize_workspace_id
        self.enhancement_thinking_levels = normalize_thinking_levels(self.enhancement_thinking_levels)
        self.enhancement_anthropic_workspace_id = normalize_workspace_id(self.enhancement_anthropic_workspace_id)
        if self.stt_model_selection_mode not in ("automatic", "manual"):
            raise ValueError(
                "stt_model_selection_mode must be 'automatic' or 'manual', "
                f"got {self.stt_model_selection_mode!r}"
            )

        if (
            type(self.agent_speech_idle_timeout_seconds) is not int
            or self.agent_speech_idle_timeout_seconds not in AGENT_SPEECH_IDLE_TIMEOUT_CHOICES
        ):
            raise ValueError("agent_speech_idle_timeout_seconds must be 0, 300, 600, 900, 1800, or 3600")
        if self.agent_speech_engine not in ("auto", "kokoro", "supertonic"):
            raise ValueError(
                "agent_speech_engine must be 'auto', 'kokoro', or 'supertonic', "
                f"got {self.agent_speech_engine!r}"
            )

        if (
            not isinstance(self.agent_speech_voice, int)
            or isinstance(self.agent_speech_voice, bool)
            or not 0 <= self.agent_speech_voice <= 9
        ):
            raise ValueError(
                "agent_speech_voice must be an integer from 0 to 9, "
                f"got {self.agent_speech_voice!r}"
            )

        if (
            self.agent_speech_kokoro_voice
            not in _AGENT_SPEECH_KOKORO_VOICES
        ):
            raise ValueError(
                "agent_speech_kokoro_voice must be a bundled Kokoro voice, "
                f"got {self.agent_speech_kokoro_voice!r}"
            )

        if not isinstance(self.agent_speech_enabled, bool):
            raise ValueError(
                "agent_speech_enabled must be a boolean, "
                f"got {self.agent_speech_enabled!r}"
            )

        if not isinstance(self.agent_summary_text_enabled, bool):
            raise ValueError(
                "agent_summary_text_enabled must be a boolean, "
                f"got {self.agent_summary_text_enabled!r}"
            )

        if not isinstance(self.agent_speak_setup_completed, bool):
            raise ValueError(
                "agent_speak_setup_completed must be a boolean, "
                f"got {self.agent_speak_setup_completed!r}"
            )

        if not isinstance(self.codex_verified, bool):
            raise ValueError(
                f"codex_verified must be a boolean, got {self.codex_verified!r}"
            )

        if not isinstance(self.claude_verified, bool):
            raise ValueError(
                f"claude_verified must be a boolean, got {self.claude_verified!r}"
            )

        if not isinstance(self.agents_enabled, bool):
            raise ValueError(
                f"agents_enabled must be a boolean, got {self.agents_enabled!r}"
            )

        if not isinstance(self.claude_connected_plugin_version, str) or len(
            self.claude_connected_plugin_version
        ) > 64:
            raise ValueError("claude_connected_plugin_version must be a short string")

        if self.agent_speech_content not in ("summary", "short", "full"):
            raise ValueError("agent_speech_content must be summary, short, or full")
        if type(self.agent_speech_short_seconds) is not int or not 5 <= self.agent_speech_short_seconds <= 120:
            raise ValueError("agent_speech_short_seconds must be an integer from 5 to 120")

        if self.agent_speech_activation not in ("per_session", "always_on"):
            raise ValueError(
                "agent_speech_activation must be 'per_session' or 'always_on', "
                f"got {self.agent_speech_activation!r}"
            )

        if self.agent_speech_summary_source not in ("agent", "whisperstream"):
            raise ValueError(
                "agent_speech_summary_source must be 'agent' or 'whisperstream', "
                f"got {self.agent_speech_summary_source!r}"
            )

        if (
            not isinstance(self.claude_reply_grace_seconds, int)
            or isinstance(self.claude_reply_grace_seconds, bool)
            or not 0 <= self.claude_reply_grace_seconds <= 30
        ):
            raise ValueError(
                "claude_reply_grace_seconds must be an integer from 0 to 30, "
                f"got {self.claude_reply_grace_seconds!r}"
            )

        if (
            not isinstance(self.claude_reply_hint_dictated_replies, int)
            or isinstance(self.claude_reply_hint_dictated_replies, bool)
            or not 0
            <= self.claude_reply_hint_dictated_replies
            <= CLAUDE_REPLY_HINT_RETIRE_AFTER
        ):
            raise ValueError(
                "claude_reply_hint_dictated_replies must be an integer from "
                f"0 to {CLAUDE_REPLY_HINT_RETIRE_AFTER}, "
                f"got {self.claude_reply_hint_dictated_replies!r}"
            )

        if (
            not isinstance(self.claude_reply_queue_delay_seconds, int)
            or isinstance(self.claude_reply_queue_delay_seconds, bool)
            or not 0 <= self.claude_reply_queue_delay_seconds <= 10
        ):
            raise ValueError(
                "claude_reply_queue_delay_seconds must be an integer from "
                f"0 to 10, got {self.claude_reply_queue_delay_seconds!r}"
            )

        if not isinstance(self.claude_reply_auto_submit, bool):
            raise ValueError(
                "claude_reply_auto_submit must be a boolean, "
                f"got {self.claude_reply_auto_submit!r}"
            )

        if not isinstance(self.claude_permission_decisions_enabled, bool):
            raise ValueError(
                "claude_permission_decisions_enabled must be a boolean, "
                f"got {self.claude_permission_decisions_enabled!r}"
            )

        view_shortcut = (self.claude_view_shortcut or "").strip().lower()
        if view_shortcut:
            reserved_view_conflicts = {
                (self.primary_shortcut or "").strip().lower(),
                (self.push_to_talk_shortcut or "").strip().lower(),
                (self.toggle_shortcut or "").strip().lower(),
                (self.paste_last_shortcut or "").strip().lower(),
                (self.paste_last_original_shortcut or "").strip().lower(),
                "enter",
                "esc",
                "escape",
            }
            reserved_view_conflicts.update(
                (shortcut or "").strip().lower()
                for shortcut in self.profile_hotkeys.values()
            )
            reserved_view_conflicts.discard("")
            if view_shortcut in reserved_view_conflicts:
                _log_validation_warning(
                    ValueError(
                        "claude_view_shortcut conflicts with another action; "
                        "clearing the view shortcut"
                    )
                )
                self.claude_view_shortcut = ""

        if self.threads is not None and self.threads < 0:
            raise ValueError(f"threads must be non-negative, got {self.threads}")

        if self.clipboard_clear_delay <= 0:
            raise ValueError(
                f"clipboard_clear_delay must be positive, got {self.clipboard_clear_delay}"
            )

        if self.model_timeout < 0:
            raise ValueError(f"model_timeout must be non-negative, got {self.model_timeout}")

        if self.max_recording_duration < 0:
            raise ValueError(
                f"max_recording_duration must be non-negative, got {self.max_recording_duration}"
            )

        if self.max_recording_duration > MAX_ALLOWED_DURATION:
            raise ValueError(
                f"max_recording_duration must be <= {MAX_ALLOWED_DURATION}, got {self.max_recording_duration}"
            )

        if self.toggle_safety_timeout < 0:
            raise ValueError(
                f"toggle_safety_timeout must be >= 0.0, got {self.toggle_safety_timeout}"
            )
        if self.toggle_safety_timeout > MAX_ALLOWED_DURATION:
            raise ValueError(
                f"toggle_safety_timeout must be <= {MAX_ALLOWED_DURATION}, got {self.toggle_safety_timeout}"
            )

        if self.start_sound_path:
            path = Path(self.start_sound_path)
            if not path.is_file():
                raise ValueError(f"start_sound_path does not exist: {path}")

        if self.stop_sound_path:
            path = Path(self.stop_sound_path)
            if not path.is_file():
                raise ValueError(f"stop_sound_path does not exist: {path}")

        if self.error_sound_path:
            path = Path(self.error_sound_path)
            if not path.is_file():
                raise ValueError(f"error_sound_path does not exist: {path}")

        if not (0.0 <= self.audio_feedback_volume <= 1.0):
            raise ValueError(
                f"audio_feedback_volume must be between 0.0 and 1.0, got {self.audio_feedback_volume}"
            )

        if self.recording_mode not in ("push_to_talk", "toggle"):
            raise ValueError(
                f"recording_mode must be 'push_to_talk' or 'toggle', got {self.recording_mode!r}"
            )

        if self.system_volume_mode not in ("off", "reduce", "mute"):
            raise ValueError(
                f"system_volume_mode must be 'off', 'reduce', or 'mute', got {self.system_volume_mode!r}"
            )

        if self.language is not None and self.language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language must be one of the 39 supported codes or None, got {self.language!r}"
            )

        if not (0.0 <= self.hotword_score <= 10.0):
            raise ValueError(
                f"hotword_score must be between 0.0 and 10.0, got {self.hotword_score}"
            )

        if self.beam_width < 1 or self.beam_width > 32:
            raise ValueError(
                f"beam_width must be between 1 and 32, got {self.beam_width}"
            )

        if self.enhancement_provider not in ("local", "gemini", "openai", "anthropic", "ollama", "custom"):
            raise ValueError(
                f"enhancement_provider must be one of 'local', 'gemini', 'openai', 'anthropic', 'ollama', 'custom', "
                f"got {self.enhancement_provider!r}"
            )

        # Lockdown Mode constraints (defense in depth — normal UI routes through
        # effective_provider(), so this guards only direct/manual config edits).
        if (
            not isinstance(self.enhancement_timeout_seconds, (int, float))
            or isinstance(self.enhancement_timeout_seconds, bool)
            or not 1 <= self.enhancement_timeout_seconds <= 300
        ):
            raise ValueError(
                "enhancement_timeout_seconds must be a number from 1 to 300, "
                f"got {self.enhancement_timeout_seconds!r}"
            )

        if not isinstance(self.enhancement_chunked_cleanup, bool):
            raise ValueError(
                "enhancement_chunked_cleanup must be a boolean, "
                f"got {self.enhancement_chunked_cleanup!r}"
            )

        if (
            not isinstance(self.enhancement_words_per_chunk, int)
            or isinstance(self.enhancement_words_per_chunk, bool)
            or not 20 <= self.enhancement_words_per_chunk <= 500
        ):
            raise ValueError(
                "enhancement_words_per_chunk must be an integer from 20 to 500, "
                f"got {self.enhancement_words_per_chunk!r}"
            )

        if not isinstance(self.enhancement_limit_first_chunk, bool):
            raise ValueError(
                "enhancement_limit_first_chunk must be a boolean, "
                f"got {self.enhancement_limit_first_chunk!r}"
            )

        if self.lockdown_idle_lock_seconds < 0:
            raise ValueError(
                f"lockdown_idle_lock_seconds must be >= 0, got {self.lockdown_idle_lock_seconds}"
            )

        if self.lockdown_mode and self.enhancement_provider != "local":
            raise ValueError(
                f"lockdown_mode requires enhancement_provider to be 'local' "
                f"(cloud providers and Ollama are blocked under lockdown), "
                f"got {self.enhancement_provider!r}"
            )

        # F3 (nothing leaves the device): under lockdown the forced-"local"
        # dispatch still honors enhancement_local_endpoint, so a client-writable
        # off-device URL would exfiltrate transcript text while lockdown_mode
        # stays true. Reject any non-loopback endpoint under lockdown (empty /
        # 127.0.0.1 / localhost / [::1] are allowed). The dispatch + warmup also
        # ignore a non-loopback override under lockdown (defense in depth), but
        # this makes a PUT /api/config carrying such an endpoint a hard 400.
        if self.lockdown_mode and not is_loopback_endpoint(self.enhancement_local_endpoint):
            raise ValueError(
                "lockdown_local_endpoint_not_loopback: enhancement_local_endpoint "
                "must be empty or a loopback address (127.0.0.1 / localhost / "
                "[::1]) under lockdown"
            )

        # F5 (no unprotected history under lockdown): a lockdown that SAVES
        # history must be backed by a password envelope. If lockdown is on and
        # history is enabled but there is NO lockdown.key.json on disk, the
        # transcript surface would save + serve new transcripts with no password
        # and no lock screen while the app still reports "Lockdown: On". Reject
        # that combination so "start saving under lockdown" is forced through the
        # password/envelope setup instead of a bare history_enabled toggle. The
        # envelope is checked at get_app_dir() (honors WHISPERSTREAM_DATA_DIR),
        # the same dir the keystore + transcript store use. Local import keeps
        # this off the module import graph.
        if self.lockdown_mode and self.history_enabled:
            from .lockdown_keystore import is_lockdown_enabled

            if not is_lockdown_enabled(get_app_dir()):
                raise ValueError(
                    "lockdown_history_no_envelope: saving transcript history "
                    "under lockdown requires a password envelope (set a lockdown "
                    "password before enabling history)"
                )

        if self.enhancement_mode not in ("default", "chat", "code", "custom", "email"):
            raise ValueError(
                f"enhancement_mode must be one of 'default', 'chat', 'code', 'custom', 'email', "
                f"got {self.enhancement_mode!r}"
            )

        if self.enhancement_output_guard_mode not in ("enforce", "log", "off"):
            raise ValueError(
                "enhancement_output_guard_mode must be one of 'enforce', 'log', or 'off', "
                f"got {self.enhancement_output_guard_mode!r}"
            )

        if self.input_style not in ("clipboard", "keystroke"):
            raise ValueError(
                f"input_style must be 'clipboard' or 'keystroke', got {self.input_style!r}"
            )

        if self.keystroke_delay_ms < 0:
            raise ValueError(
                f"keystroke_delay_ms must be non-negative, got {self.keystroke_delay_ms}"
            )

        profile_ids = validate_profiles(
            self.profiles, self.default_profile_id, supported_languages=SUPPORTED_LANGUAGES,
        )
        routing, warnings = prune_profile_routing(
            power_mode_map=self.power_mode_map,
            power_mode_paths=self.power_mode_paths,
            site_map=self.site_map,
            profile_hotkeys=self.profile_hotkeys,
            profile_ids=profile_ids,
            reserved_shortcuts=(
                self.primary_shortcut, self.push_to_talk_shortcut, self.toggle_shortcut,
                self.paste_last_shortcut,
                self.paste_last_original_shortcut, self.claude_view_shortcut,
            ),
        )
        self.power_mode_map = routing["power_mode_map"]
        self.power_mode_paths = routing["power_mode_paths"]
        self.site_map = routing["site_map"]
        self.profile_hotkeys = routing["profile_hotkeys"]
        for warning in warnings:
            _log_validation_warning(warning)


def get_local_gpu_verdict(config: "WhisperstreamConfig", model_id: str) -> str:
    """Return the cached GPU benchmark verdict for *model_id*.

    Reads ``config.enhancement_local_gpu_verdicts`` (the per-model map added
    in Task 1.3 of tiered-benchmark). A model that has never been
    benchmarked is absent from the map and reads as ``"not_run"``. This is
    the canonical read site for that map -- Task 1.4's callers (server.py's
    supervisor gates, the launch usability check, etc.) use this instead of
    re-deriving the ``.get(..., "not_run")`` lookup inline.
    """
    from . import gemma_artifacts as artifacts
    if model_id == artifacts.PUBLIC_ID:
        directory = get_local_llm_dir()
        actual = artifacts.receipt(directory)
        expected = config.enhancement_local_gpu_artifacts.get(model_id)
        if not actual or (expected and expected != actual):
            return "not_run"
        # Historical primary-only receipts are compatible only with the primary.
        if not expected and artifacts.resolve(directory)[0] != artifacts.PUBLIC_ID:
            return "not_run"
        if not expected:
            # Adopt the legacy primary result for this installed generation;
            # subsequent serialization persists the binding, not an unbound pass.
            config.enhancement_local_gpu_artifacts[model_id] = actual
    return config.enhancement_local_gpu_verdicts.get(model_id, "not_run")


def _config_diff_keys(
    old: WhisperstreamConfig, new: WhisperstreamConfig
) -> list[str]:
    """Return a sorted list of field names whose values differ between *old* and *new*."""
    old_dict = asdict(old)
    new_dict = asdict(new)
    return sorted(k for k in old_dict if old_dict[k] != new_dict.get(k))


def _new_install_config() -> WhisperstreamConfig:
    """Build the existing fresh-install defaults without reading user state."""
    config = WhisperstreamConfig()
    from whisperstream.enhancement_prompts import CHAT_PROMPT, CODE_PROMPT, EMAIL_PROMPT  # noqa: PLC0415

    config.profiles = [
        {"id": "chat", "name": "Chat", "prompt": CHAT_PROMPT},
        {"id": "code", "name": "Code", "prompt": CODE_PROMPT},
        {"id": "email", "name": "Email", "prompt": EMAIL_PROMPT},
    ]
    config.default_profile_id = "chat"
    config.power_mode_map = dict(_DEFAULT_POWER_MODE_MAP)
    config.site_map = {
        "mail.google.com": "email",
        "outlook.office.com": "email",
        "outlook.live.com": "email",
    }
    return config


def apply_managed_lockdown(
    config: WhisperstreamConfig,
    *,
    envelope_exists: bool,
) -> bool:
    """Reconcile plaintext config beneath authoritative machine policy.

    Returns whether persistence is required. Runtime state is mutated first so
    a read-only or damaged config file cannot weaken the effective boundary.
    """
    before = (
        config.lockdown_mode,
        config.history_enabled,
        config.enhancement_enabled,
        config.enhancement_provider,
        config.enhancement_local_endpoint,
    )
    config.lockdown_mode = True
    if not envelope_exists:
        config.history_enabled = False
    if config.enhancement_enabled and config.enhancement_provider != "local":
        config.enhancement_enabled = False
    config.enhancement_provider = "local"
    if not is_loopback_endpoint(config.enhancement_local_endpoint):
        config.enhancement_local_endpoint = ""
    after = (
        config.lockdown_mode,
        config.history_enabled,
        config.enhancement_enabled,
        config.enhancement_provider,
        config.enhancement_local_endpoint,
    )
    return before != after


class ConfigManager:
    def __init__(
        self,
        config_path: Optional[Path] = None,
        models_root: Optional[Path] = None,
        protected_secret_store: Any = None,
    ) -> None:
        self._config_path = config_path or get_config_path()
        self._models_root = models_root or get_models_root()
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._models_root.mkdir(parents=True, exist_ok=True)
        # Tracks the last known in-memory config for computing save diffs.
        self._current_config: Optional[WhisperstreamConfig] = None
        # A test or private packaged probe may inject an isolated native store.
        # Production leaves this as None so selection stays lazy.
        self._protected_secret_store = protected_secret_store
        self._api_key_references: Dict[str, str] = {}
        self._save_lock = threading.RLock()

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def models_root(self) -> Path:
        return self._models_root

    @property
    def protected_secret_store(self) -> Any:
        """Return the optional injected store without selecting a provider."""

        return self._protected_secret_store

    @staticmethod
    def _reference_fields(data: Dict[str, Any]) -> Dict[str, str]:
        """Return valid opaque API-key references from a parsed config."""

        from .protected_secret_contract import (  # noqa: PLC0415
            ProtectedSecretError,
            parse_protected_secret_reference,
        )

        references: Dict[str, str] = {}
        for field_name in _API_KEY_FIELDS:
            value = data.get(field_name)
            if not isinstance(value, str) or not value:
                continue
            try:
                parse_protected_secret_reference(value)
            except ProtectedSecretError:
                continue
            references[field_name] = value
        return references

    def _references_on_disk(self) -> Dict[str, str]:
        if not self._config_path.exists():
            return {}
        try:
            with self._config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return dict(self._api_key_references)
        return self._reference_fields(data)

    def config_from_client_dict(
        self,
        data: Dict[str, Any],
        *,
        current: WhisperstreamConfig,
    ) -> WhisperstreamConfig:
        """Decode a client config while resolving fixed key-presence markers.

        A marker means "keep the current server-side secret". New plaintext
        values remain accepted as replacement input and are protected only by
        :meth:`save`.
        """

        merged = dict(data)
        if platform.system() != "Darwin":
            # Keep the Windows wire/in-memory compatibility path name-only.
            # The companion label is a Darwin persistence concern and must not
            # become part of a Windows config merely because a client supplied
            # a newer cross-platform payload.
            merged.pop("audio_input_device_label", None)
        for field_name in _API_KEY_FIELDS:
            if (
                field_name not in merged
                or merged.get(field_name) == PROTECTED_API_KEY_MARKER
            ):
                merged[field_name] = getattr(current, field_name, "")
        return WhisperstreamConfig.from_dict(
            merged,
            protected_secret_store=self._protected_secret_store,
        )

    @staticmethod
    def client_dict(config: WhisperstreamConfig) -> Dict[str, Any]:
        """Return a plaintext-free client payload with fixed presence markers."""

        payload = config.to_dict()
        if platform.system() == "Darwin":
            # This response-only discriminator is authoritative even while
            # capability discovery or device enumeration is unavailable.
            payload["audio_input_device_selection_mode"] = (
                _MACOS_INPUT_SELECTION_MODE
            )
        else:
            # Windows API shape remains byte-for-byte name-only compatible.
            payload.pop("audio_input_device_label", None)
        for field_name in _API_KEY_FIELDS:
            payload[field_name] = (
                PROTECTED_API_KEY_MARKER
                if bool(getattr(config, field_name, ""))
                else ""
            )
        return payload

    def _delete_references(self, references: set[str], *, context: str) -> None:
        if not references:
            return
        from .key_encryption import delete_api_key  # noqa: PLC0415
        from .protected_secret_contract import (  # noqa: PLC0415
            ProtectedSecretError,
            ProtectedSecretFailure,
        )

        for reference in sorted(references):
            try:
                delete_api_key(
                    reference,
                    store=self._protected_secret_store,
                )
            except ProtectedSecretError as error:
                if error.failure is ProtectedSecretFailure.NOT_FOUND:
                    continue
                logger.warning(
                    "API key reference cleanup failed context=%s operation=%s failure=%s",
                    context,
                    error.operation.value,
                    error.failure.value,
                )

    def _protect_api_key_fields(
        self,
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, str], set[str], set[str]]:
        """Replace plaintext key fields with stable provider references.

        Existing references are reused when their loaded value is unchanged.
        Equal mirror/slot values share one reference. Any newly-created items
        are returned so a failed atomic config write can delete them exactly.
        """

        from .key_encryption import decrypt_api_key, encrypt_api_key  # noqa: PLC0415

        old_fields = self._references_on_disk()
        old_references = set(old_fields.values())
        new_fields: Dict[str, str] = {}
        created: set[str] = set()
        protected_values: list[tuple[str, str]] = []
        try:
            # Resolve each distinct old reference once. This lets the active
            # mirror reuse a provider slot's item when the selected provider
            # changes, rather than churning an equivalent Keychain item.
            for old_reference in dict.fromkeys(old_fields.values()):
                loaded = decrypt_api_key(
                    old_reference,
                    store=self._protected_secret_store,
                )
                if loaded:
                    protected_values.append((loaded, old_reference))

            for field_name in _API_KEY_FIELDS:
                plaintext = payload.get(field_name)
                if not isinstance(plaintext, str) or not plaintext:
                    payload[field_name] = ""
                    continue

                reference: str | None = None
                for seen_plaintext, seen_reference in protected_values:
                    if seen_plaintext == plaintext:
                        reference = seen_reference
                        break

                if reference is None:
                    reference = encrypt_api_key(
                        plaintext,
                        store=self._protected_secret_store,
                    )
                    if reference not in old_references:
                        created.add(reference)

                protected_values.append((plaintext, reference))
                new_fields[field_name] = reference
                payload[field_name] = reference
        except Exception:
            self._delete_references(created, context="rollback")
            raise
        return new_fields, created, old_references

    def refresh_models_root(self) -> Path:
        """Re-read ``get_models_root()``, update the cached ``_models_root``,
        create the directory if it does not exist, and return the new path.

        The relocation worker calls this after writing the new
        ``model_storage_dir`` to ``config.toml`` so that a subsequent
        ``model_dir(...)`` call targets the new location instead of the old
        cached one.  The constructor's caching behaviour is unchanged.
        """
        new_root = get_models_root()
        self._models_root = new_root
        new_root.mkdir(parents=True, exist_ok=True)
        return new_root

    def model_dir(self, model_name: str, quantization: Optional[str] = None) -> Path:
        """Resolve a Parakeet model's directory under the cached models root.

        Compatibility entry point for the relocation path (``server.py``) and
        its tests, which address Parakeet by ``(name, quantization)``. The
        directory NAME is sourced from the single registry in
        ``model_downloader`` (``DOWNLOAD_REGISTRY[...].dir_name``, the round-03
        unification) so it can never drift from the canonical
        ``model_downloader.model_dir`` resolver; only the ROOT differs. This
        uses the instance-cached ``_models_root`` (switched explicitly by
        ``refresh_models_root``) so a relocation flips the root atomically,
        whereas ``model_downloader.model_dir`` reads ``get_models_root()``
        live. Both yield the same path once ``config.toml`` carries the new
        ``model_storage_dir``. STT is int8-only post round-03, so
        ``quantization`` is accepted for signature compatibility but the
        registry entry (always int8) determines the directory name.
        """
        from . import model_downloader

        dir_name = model_downloader.DOWNLOAD_REGISTRY[
            model_downloader.model_id_for_parakeet(model_name)
        ].dir_name
        return self._models_root / dir_name

    def load(
        self,
        *,
        managed_lockdown: bool = False,
        managed_history_disabled: bool = False,
        lockdown_envelope_exists: bool = False,
    ) -> WhisperstreamConfig:
        """Load config, applying machine Lockdown before validation when set.

        Machine policy is supplied by the startup authority rather than read
        here, keeping registry evaluation ahead of diagnostic initialization.
        Under policy, missing, malformed, or inaccessible user config falls
        back to fresh defaults and is reconciled fail closed.
        """
        use_fresh_config = not self._config_path.exists()
        data: Dict[str, Any] | None = None
        if not use_fresh_config:
            try:
                with self._config_path.open("rb") as handle:
                    data = tomllib.load(handle)
            except Exception:
                if not (managed_lockdown or managed_history_disabled):
                    raise
                use_fresh_config = True

        self._api_key_references = self._reference_fields(data or {})
        plaintext_key_fields = tuple(
            field for field in _API_KEY_FIELDS
            if isinstance((data or {}).get(field), str)
            and bool(data[field])
            and not data[field].startswith(("enc:", "keychain:v1:"))
        )
        try:
            config = (
                _new_install_config()
                if use_fresh_config
                else WhisperstreamConfig.from_dict(data or {}, protected_secret_store=self._protected_secret_store)
            )
        except Exception:
            if not (managed_lockdown or managed_history_disabled):
                raise
            # A standard user can edit config.toml. If its values cannot be
            # normalized, discard that untrusted document and build a safe
            # managed baseline rather than leaving startup unavailable.
            use_fresh_config = True
            config = _new_install_config()
        reconciliation_needed = False
        if managed_lockdown:
            reconciliation_needed = apply_managed_lockdown(
                config,
                envelope_exists=lockdown_envelope_exists,
            ) or use_fresh_config
        if managed_history_disabled:
            reconciliation_needed = config.history_enabled or config.save_audio or reconciliation_needed
            config.history_enabled = False
            config.save_audio = False
        # Older releases accepted remote HTTP and off-device Local overrides.
        # Keep startup/recovery usable without admitting those routes or
        # rewriting the source file. The user can configure a safe destination.
        from .enhancement_security import normalize_endpoint, EnhancementAdmissionError
        for endpoint_field in (
            "enhancement_custom_base_url", "enhancement_ollama_endpoint", "enhancement_local_endpoint",
        ):
            endpoint_value = getattr(config, endpoint_field)
            if not endpoint_value:
                continue
            try:
                normalize_endpoint(endpoint_value, local_only=endpoint_field == "enhancement_local_endpoint")
            except EnhancementAdmissionError:
                config.enhancement_enabled = False
                setattr(config, endpoint_field, "")
                logger.warning("enhancement endpoint recovery action=disable_and_clear_invalid_override")
        try:
            config.validate()
        except Exception as exc:
            if (managed_lockdown or managed_history_disabled) and not use_fresh_config:
                # Semantically invalid user values are another config-tamper
                # path. Recover to the safe managed baseline and overwrite the
                # invalid document best-effort below.
                use_fresh_config = True
                config = _new_install_config()
                if managed_lockdown:
                    apply_managed_lockdown(
                        config,
                        envelope_exists=lockdown_envelope_exists,
                    )
                if managed_history_disabled:
                    config.history_enabled = False
                    config.save_audio = False
                reconciliation_needed = True
                config.validate()
            else:
                if isinstance(exc, ValueError):
                    _log_validation_warning(exc)
                raise
        if plaintext_key_fields:
            # Migrate legacy plaintext through the selected provider, or scrub
            # credentials if protection is unavailable. Never retain plaintext.
            from .protected_secret_contract import ProtectedSecretError
            try:
                self.save(config)
            except ProtectedSecretError as error:
                for field in _API_KEY_FIELDS:
                    setattr(config, field, "")
                self.save(config)
                logger.warning(
                    "plaintext API key migration failed operation=%s failure=%s; credentials removed",
                    error.operation.value, error.failure.value,
                )
        variant = config.parakeet_quantization or "fp16"
        logger.info(
            "config loaded model_variant=%s system_volume_mode=%s",
            variant,
            config.system_volume_mode,
        )
        self._current_config = config
        if reconciliation_needed:
            try:
                self.save(config)
            except Exception:  # noqa: BLE001 - in-memory policy remains authoritative
                logger.warning("managed lockdown config reconcile status=persist_failed")
                self._current_config = config
        return config

    def save(self, config: WhisperstreamConfig) -> None:
        """Serialize *config* atomically and reconcile protected references."""

        with self._save_lock:
            self._save_locked(config)

    def _save_locked(self, config: WhisperstreamConfig) -> None:
        """Serialize *config* to TOML and write it to the config file."""
        try:
            config.validate()
        except ValueError as exc:
            _log_validation_warning(exc)
            raise
        lines: list[str] = []
        data = config.to_dict()
        if platform.system() != "Darwin":
            # The in-memory companion lets shared code carry a migrated Mac
            # config, but it is never serialized into Windows config.toml.
            data.pop("audio_input_device_label", None)
        new_key_fields, created_references, old_references = (
            self._protect_api_key_fields(data)
        )
        word_boosts = data.pop("word_boosts", [])
        word_filters = data.pop("word_filters", [])
        # Offer-id lists: pop so the scalar loop below does not hand a list to
        # toml_value (which raises TypeError on a list); emitted as inline arrays.
        dismissed_offer_ids = data.pop("dismissed_offer_ids", [])
        shown_offer_ids = data.pop("shown_offer_ids", [])
        word_replacements = data.pop("word_replacements", {})
        saved_models = data.pop("enhancement_saved_models", {})
        thinking_levels = data.pop("enhancement_thinking_levels", {})
        power_mode_map = data.pop("power_mode_map", {})
        power_mode_paths = data.pop("power_mode_paths", {})
        site_map = data.pop("site_map", {})
        profile_hotkeys = data.pop("profile_hotkeys", {})
        # Task 1.3 (tiered-benchmark): pop the per-model verdict map so the
        # scalar loop below doesn't hand a dict to toml_value (which raises
        # TypeError on any dict, including {}) -- mirrors every other
        # Dict[str, str] field's table-emission treatment above.
        enhancement_local_gpu_verdicts = data.pop("enhancement_local_gpu_verdicts", {})
        # Pop the profile structures so the scalar loop below doesn't try to
        # emit them as inline scalars (profiles are an array-of-tables).
        profiles = data.pop("profiles", [])
        default_profile_id = data.pop("default_profile_id", "")

        model_device_maps = {
            key: data.pop(key, {}) for key in
            ("enhancement_local_gpu_devices", "enhancement_local_gpu_device_names", "enhancement_local_gpu_artifacts")
        }
        for key, value in data.items():
            lines.append(f"{key} = {toml_value(value)}")

        lines.append(f"default_profile_id = {toml_value(default_profile_id)}")
        lines.append(f"word_boosts = {toml_inline_array(word_boosts)}")
        lines.append(f"word_filters = {toml_inline_array(word_filters)}")
        lines.append(f"dismissed_offer_ids = {toml_inline_array(dismissed_offer_ids)}")
        lines.append(f"shown_offer_ids = {toml_inline_array(shown_offer_ids)}")

        # Emit even when empty so deletion never retriggers upgrade seeding.
        lines.append("\n[enhancement_saved_models]")
        for provider, models in saved_models.items():
            lines.append(f"{toml_key(provider)} = {toml_inline_array(models)}")

        lines.append("\n[enhancement_thinking_levels]")
        from .enhancement_thinking import normalize_thinking_levels
        for key, value in normalize_thinking_levels(thinking_levels).items():
            lines.append(f"{toml_key(key)} = {toml_value(value)}")

        for key, mapping in model_device_maps.items():
            if mapping:
                lines.append(f"\n[{key}]")
                for model_id, value in mapping.items():
                    lines.append(f"{toml_key(model_id)} = {toml_value(value)}")

        if word_replacements:
            lines.append("")
            lines.append("[word_replacements]")
            for k, v in word_replacements.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        if power_mode_map:
            lines.append("")
            lines.append("[power_mode_map]")
            for k, v in power_mode_map.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        if power_mode_paths:
            lines.append("")
            lines.append("[power_mode_paths]")
            for k, v in power_mode_paths.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        if site_map:
            lines.append("")
            lines.append("[site_map]")
            for k, v in site_map.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        if profile_hotkeys:
            lines.append("")
            lines.append("[profile_hotkeys]")
            for k, v in profile_hotkeys.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        if enhancement_local_gpu_verdicts:
            lines.append("")
            lines.append("[enhancement_local_gpu_verdicts]")
            for k, v in enhancement_local_gpu_verdicts.items():
                lines.append(f"{toml_key(k)} = {toml_value(v)}")

        # Enhancement profiles as a [[profiles]] array-of-tables. Only the
        # on-disk shape is allowlisted (drop any frontend-only key like
        # `builtin`). toml_value handles multi-line prompts via
        # \"\"\"...\"\"\", so no special-casing is needed.
        # `input_style` is conditionally emitted: present in the profile dict
        # means the profile overrides the global default; absent means inherit.
        for profile in profiles:
            lines.append("")
            lines.append("[[profiles]]")
            for k in ("id", "name", "prompt"):
                lines.append(f"{toml_key(k)} = {toml_value(profile.get(k, ''))}")
            if (
                profile.get("id") not in _BUILTIN_PROFILE_IDS
                and isinstance(profile.get("icon"), str)
                and profile.get("icon") in _PROFILE_ICON_KEYS
            ):
                lines.append(f"{toml_key('icon')} = {toml_value(profile['icon'])}")
            if profile.get("language"):
                lines.append(f"{toml_key('language')} = {toml_value(profile['language'])}")
            if profile.get("input_style"):
                lines.append(f"{toml_key('input_style')} = {toml_value(profile['input_style'])}")
            # `signature` is conditionally emitted (like input_style): the default
            # email sign-off for this profile. toml_value handles multi-line
            # values via triple-quoted strings, so no special-casing is needed.
            if profile.get("signature"):
                lines.append(f"{toml_key('signature')} = {toml_value(profile['signature'])}")
            # `mode` is conditionally emitted (like input_style): the fine-tune
            # cleanup mode ("chat"|"code") this profile routes to when a fine-tuned
            # model is active. Only these two trained modes are valid; anything
            # else is dropped so the backend's default (chat) applies. Absent =
            # inherit the id-derived default. Ignored by Gemma/cloud.
            if profile.get("mode") in ("chat", "code", "email"):
                lines.append(f"{toml_key('mode')} = {toml_value(profile['mode'])}")

        lines.append("")  # trailing newline
        from whisperstream.atomic_io import atomic_write_text

        try:
            atomic_write_text(self._config_path, "\n".join(lines))
        except OSError as exc:
            self._delete_references(created_references, context="write-rollback")
            logger.error("config save failed error_type=%s", type(exc).__name__)
            raise

        self._api_key_references = new_key_fields
        self._delete_references(
            old_references - set(new_key_fields.values()),
            context="replaced",
        )

        # Compute which keys changed vs the previous in-memory snapshot.
        if self._current_config is not None:
            changed = _config_diff_keys(self._current_config, config)
        else:
            # No previous snapshot — report all non-default fields as changed.
            defaults = WhisperstreamConfig()
            changed = _config_diff_keys(defaults, config)

        logger.info("config saved keys_changed=%s", ",".join(changed) if changed else "(none)")
        self._current_config = config
