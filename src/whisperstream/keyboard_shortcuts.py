"""Global keyboard shortcuts and text injection via pynput.

Public API (unchanged from the keyboard-library version):
- register_push_to_talk(shortcut, on_press, on_release)
- register_toggle(shortcut, on_start, on_stop)
- unhook_all()
- capture_shortcut(on_keys_changed, on_captured)
- stop_capture()
- send(combination)
- snapshot_held_modifiers()
- wait()
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Union

from pynput.keyboard import Controller, Key, KeyCode, Listener

from .shortcut_contract import (
    CanonicalShortcut,
    ShortcutAction,
    ShortcutBinding,
    ShortcutCaptureCallback,
    ShortcutCaptureOutcome,
    ShortcutCaptureStatus,
    ShortcutFailure,
    ShortcutGesture,
    ShortcutGestureState,
    ShortcutRegistrationOutcome,
    ShortcutRegistrationStatus,
    ShortcutTransition,
)


# Grace period (seconds) that the self-event suppression guard stays active
# after `suppress_self_events()` exits. pynput's low-level keyboard hook fires
# on a separate OS thread and may lag the synthetic press/release that
# triggered it; without this grace window, our own injected keystrokes can
# re-enter the hotkey logic on the trailing edge.
_SUPPRESS_GRACE_S: float = 0.05
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_VK_ENTER = 0x0D
_VK_ESCAPE = 0x1B
_VK_GRAVE = 0xC0
# Windows reports punctuation by physical OEM virtual-key position. The
# printable ``KeyCode.char`` still depends on the active keyboard layout and
# held modifiers (AltGr is especially likely to produce characters such as
# ``½`` or ``»``), so give every standard OEM key a stable US-position
# spelling for capture, storage, and matching.
_VK_OEM_TO_ORDINAL: dict[int, int] = {
    0xBA: ord(";"),
    0xBB: ord("="),
    0xBC: ord(","),
    0xBD: ord("-"),
    0xBE: ord("."),
    0xBF: ord("/"),
    _VK_GRAVE: ord("`"),
    0xDB: ord("["),
    0xDC: ord("\\"),
    0xDD: ord("]"),
    0xDE: ord("'"),
}
_OEM_ORDINAL_TO_VK: dict[int, int] = {
    ordinal: vk for vk, ordinal in _VK_OEM_TO_ORDINAL.items()
}
_LLKHF_INJECTED_MASK = 0x12
_LISTENER_JOIN_TIMEOUT_S = 2.0

# A consumed early key normally stays hidden until its key-up. If that key-up
# never arrives (hook timeout, secure desktop), a keydown this long after the
# last one cannot be auto-repeat, so it is treated as a fresh press instead of
# being swallowed forever.
_EARLY_REPEAT_STALE_S = 1.0
_DISPATCH_STOP = object()


class _ListenerDisabledError(RuntimeError):
    """Raised when a native listener reports that it did not remain alive."""


def _new_suppress_timer(
    delay_s: float, callback: Callable[[], None],
) -> threading.Timer:
    """Construct the delayed suppression-release Timer."""
    return threading.Timer(delay_s, callback)


# ---------------------------------------------------------------------------
# Caps-lock detection + Unicode injection helpers (mockable in tests)
# ---------------------------------------------------------------------------

def _caps_lock_on() -> bool:
    """Return True if Caps Lock is currently toggled on.

    Windows only: reads VK_CAPITAL (0x14) toggle state via GetKeyState.
    Returns False on non-Windows or if the API call raises any exception.
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:
        return False


# Shift, Ctrl, Alt and both Windows keys. A modified Enter or Escape is never
# a routed reply key: Shift+Enter is a newline, Ctrl+Escape opens Start.
_ROUTED_KEY_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
# Primary language ids whose keyboard layouts are IME front ends.
_IME_PRIMARY_LANGUAGES = frozenset({0x04, 0x11, 0x12})  # Chinese, Japanese, Korean


def _routed_key_modifier_down() -> bool:
    """True when any modifier is physically held (Windows; never raises)."""
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.windll.user32.GetAsyncKeyState
        return any(state(vk) & 0x8000 for vk in _ROUTED_KEY_MODIFIER_VKS)
    except Exception:
        return False


def _foreground_uses_ime_layout() -> bool:
    """True when the focused app's keyboard layout is an IME language.

    A low-level hook cannot see IME composition, and an IME commits with
    Enter. Never claiming Enter while such a layout is active keeps a commit
    from submitting a reply. Non-blocking: no cross-process messages.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        window = user32.GetForegroundWindow()
        if not window:
            return False
        thread = user32.GetWindowThreadProcessId(window, None)
        layout = int(user32.GetKeyboardLayout(thread)) & 0xFFFF
        return (layout & 0x3FF) in _IME_PRIMARY_LANGUAGES
    except Exception:
        return False


def _inject_unicode_char(ch: str) -> None:
    """Inject a single character as a KEYEVENTF_UNICODE press+release pair.

    This sends the character directly as a Unicode keyboard event (VK_PACKET),
    bypassing the active keyboard layout and Caps Lock state. The character
    arrives at the focused window exactly as typed, regardless of lock state.

    Uses pynput's win32 primitives so the injected event is indistinguishable
    from pynput's own Unicode-path events (mirrors _win32.py:204-222 for
    above-BMP surrogates, adapted for single BMP codepoints).
    """
    from pynput._util.win32 import INPUT, INPUT_union, KEYBDINPUT, SendInput  # noqa: PLC0415

    scan = ord(ch)
    # Press
    SendInput(
        1,
        ctypes.byref(INPUT(
            type=INPUT.KEYBOARD,
            value=INPUT_union(ki=KEYBDINPUT(
                wVk=0,
                wScan=scan,
                dwFlags=KEYBDINPUT.UNICODE,
                time=0,
                dwExtraInfo=0,
            )),
        )),
        ctypes.sizeof(INPUT),
    )
    # Release
    SendInput(
        1,
        ctypes.byref(INPUT(
            type=INPUT.KEYBOARD,
            value=INPUT_union(ki=KEYBDINPUT(
                wVk=0,
                wScan=scan,
                dwFlags=KEYBDINPUT.UNICODE | KEYBDINPUT.KEYUP,
                time=0,
                dwExtraInfo=0,
            )),
        )),
        ctypes.sizeof(INPUT),
    )

try:
    from pynput.mouse import Button as _MouseButton
    from pynput.mouse import Listener as MouseListener
    _MOUSE_MIDDLE = getattr(_MouseButton, "middle", None)
    _MOUSE_X1 = getattr(_MouseButton, "x1", None)
    _MOUSE_X2 = getattr(_MouseButton, "x2", None)
except ImportError:
    _MouseButton = None  # type: ignore
    MouseListener = None  # type: ignore
    _MOUSE_MIDDLE = None
    _MOUSE_X1 = None
    _MOUSE_X2 = None


# ---------------------------------------------------------------------------
# Key name mapping (keyboard-library-style names ↔ pynput objects)
# ---------------------------------------------------------------------------

_NAME_TO_KEY: dict[str, Key] = {
    # Modifiers (specific side)
    "left ctrl": Key.ctrl_l,
    "right ctrl": Key.ctrl_r,
    "left shift": Key.shift,
    "right shift": Key.shift_r,
    "left alt": Key.alt_l,
    "right alt": Key.alt_r,
    "alt gr": Key.alt_gr,
    "left win": Key.cmd_l,
    "right win": Key.cmd_r,
    # Common
    "enter": Key.enter,
    "return": Key.enter,
    "space": Key.space,
    "tab": Key.tab,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "esc": Key.esc,
    "escape": Key.esc,
    "caps lock": Key.caps_lock,
    # Arrows
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    # Navigation
    "home": Key.home,
    "end": Key.end,
    "page up": Key.page_up,
    "page down": Key.page_down,
    "insert": Key.insert,
    # Function keys
    **{f"f{i}": getattr(Key, f"f{i}") for i in range(1, 25)},
    # Media
    "volume mute": Key.media_volume_mute,
    "volume up": Key.media_volume_up,
    "volume down": Key.media_volume_down,
    "play/pause": Key.media_play_pause,
    "media play pause": Key.media_play_pause,
    "media stop": Key.media_stop,
    "next track": Key.media_next,
    "previous track": Key.media_previous,
    # Misc
    "menu": Key.menu,
    "num lock": Key.num_lock,
    "pause": Key.pause,
    "print screen": Key.print_screen,
    "scroll lock": Key.scroll_lock,
}

# Generic modifiers: match either physical side
_GENERIC_MODIFIERS: dict[str, frozenset[Key]] = {
    "ctrl": frozenset({Key.ctrl_l, Key.ctrl_r}),
    "shift": frozenset({Key.shift, Key.shift_r}),
    "alt": frozenset({Key.alt_l, Key.alt_r}),
    "win": frozenset({Key.cmd, Key.cmd_l, Key.cmd_r}),
}

# For send(): generic modifier → left-side Key (used when simulating)
_GENERIC_TO_LEFT: dict[str, Key] = {
    "ctrl": Key.ctrl_l,
    "shift": Key.shift,
    "alt": Key.alt_l,
    "win": Key.cmd_l,
}

# Reverse: pynput Key → display name (for capture UI and shortcut strings)
_KEY_TO_NAME: dict[Key, str] = {
    Key.ctrl_l: "ctrl",
    Key.ctrl_r: "right ctrl",
    Key.shift: "shift",
    Key.shift_r: "right shift",
    Key.alt_l: "alt",
    Key.alt_r: "right alt",
    Key.alt_gr: "alt gr",
    Key.cmd: "win",
    Key.cmd_l: "win",
    Key.cmd_r: "right win",
    Key.enter: "enter",
    Key.space: "space",
    Key.tab: "tab",
    Key.backspace: "backspace",
    Key.delete: "delete",
    Key.esc: "esc",
    Key.caps_lock: "caps lock",
    Key.up: "up",
    Key.down: "down",
    Key.left: "left",
    Key.right: "right",
    Key.home: "home",
    Key.end: "end",
    Key.page_up: "page up",
    Key.page_down: "page down",
    Key.insert: "insert",
    Key.media_volume_mute: "volume mute",
    Key.media_volume_up: "volume up",
    Key.media_volume_down: "volume down",
    Key.media_play_pause: "play/pause",
    Key.media_stop: "media stop",
    Key.media_next: "next track",
    Key.media_previous: "previous track",
    Key.menu: "menu",
    Key.num_lock: "num lock",
    Key.pause: "pause",
    Key.print_screen: "print screen",
    Key.scroll_lock: "scroll lock",
    **{getattr(Key, f"f{i}"): f"f{i}" for i in range(1, 25)},
}

# Aliases: normalize cross-platform / community names to canonical names.
# Applied in _parse_key after lowercasing, before dict lookups.
_ALIASES: dict[str, str] = {
    "cmd": "win",
    "super": "win",
    "meta": "win",
    # Mouse button variant spellings
    "mouse 3": "mouse3",
    "mouse_3": "mouse3",
    "middle": "mouse3",
    "middle click": "mouse3",
    "middle mouse": "mouse3",
    "mouse 4": "mouse4",
    "mouse_4": "mouse4",
    "mouse 5": "mouse5",
    "mouse_5": "mouse5",
}

# Mouse button name maps (populated only when the platform backend exposes
# the relevant Button enum members. middle is cross-platform; x1/x2 are
# Windows-only and absent on macOS/Linux).
_MOUSE_NAME_TO_BUTTON: dict[str, object] = {}
if _MOUSE_MIDDLE is not None:
    _MOUSE_NAME_TO_BUTTON["mouse3"] = _MOUSE_MIDDLE
if _MOUSE_X1 is not None:
    _MOUSE_NAME_TO_BUTTON["mouse4"] = _MOUSE_X1
if _MOUSE_X2 is not None:
    _MOUSE_NAME_TO_BUTTON["mouse5"] = _MOUSE_X2

_MOUSE_BUTTON_TO_NAME: dict[object, str] = {v: k for k, v in _MOUSE_NAME_TO_BUTTON.items()}

# Every modifier Key that snapshot_held_modifiers() checks for.
_ALL_TRACKED_MODIFIERS: frozenset[Key] = frozenset({
    Key.ctrl_l, Key.ctrl_r,
    Key.shift, Key.shift_r,
    Key.alt_l, Key.alt_r, Key.alt_gr,
    Key.cmd, Key.cmd_l, Key.cmd_r,
})

# Standalone recording keys whose complete Windows event sequence may be
# consumed without stealing ordinary typing/navigation input.
_SUPPRESSIBLE_STANDALONE_KEYS: frozenset[Key] = frozenset({
    Key.caps_lock,
    Key.ctrl_l, Key.ctrl_r,
    Key.shift, Key.shift_r,
    Key.alt_l, Key.alt_r, Key.alt_gr,
    Key.cmd, Key.cmd_l, Key.cmd_r,
    *(getattr(Key, f"f{i}") for i in range(1, 25)),
})
_SUPPRESSIBLE_GRAVE_ORDINALS = frozenset({ord("`"), ord("~")})
# The grave/tilde key is one physical key on the Windows OEM-3 position. Keep
# both printable spellings as parser alternatives so a layout-generated char
# and a raw VK_OEM_3 event can satisfy the same binding.
_GRAVE_KEY_ORDINALS = _SUPPRESSIBLE_GRAVE_ORDINALS

# Low-level Windows events normally use side-specific modifier VKs. The
# generic variants are included as a defensive fallback for unusual drivers.
_SUPPRESSIBLE_VK_TO_KEY: dict[int, Key] = {
    0x10: Key.shift,
    0x11: Key.ctrl_l,
    0x12: Key.alt_l,
    **{
        int(key.value.vk): key
        for key in _SUPPRESSIBLE_STANDALONE_KEYS
        if getattr(key.value, "vk", None) is not None
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _keycode_ordinal(key: KeyCode) -> Optional[int]:
    """Stable canonical ordinal for a ``KeyCode``, identifying the *physical*
    key independent of modifier state.

    pynput derives ``key.char`` through the OS layout (``ToUnicode`` on
    Windows), so the SAME physical key reports a different ``char`` depending
    on which modifiers are held at that instant: ``c`` alone arrives as
    ``'c'``, but under Ctrl it is the control char ``'\\x03'`` and under
    Ctrl+Alt it may be ``None``. Keying off ``char`` therefore makes one key
    hash to different values on press vs release — and because the
    modifier-release guard around paste injection churns the modifier state
    mid-combo, the release of a letter can mismatch its press, leaving a
    phantom key stuck in ``_pressed`` that degrades an N-key combo into firing
    on its modifiers alone.

    ``vk`` is the physical virtual-key identity and is stable, so prefer it for
    the letter (A-Z), digit (0-9), and standard OEM punctuation rows. OEM keys
    use their stable US-position spelling so capture and stored shortcuts do
    not change with the active layout or modifier state. Fall back to ``char``
    only for other keys, then to the raw ``vk`` as a last resort.
    """
    vk = key.vk
    if vk is not None:
        if 65 <= vk <= 90:    # A-Z → lowercase ordinal ('A' and 'a' coincide)
            return vk + 32
        if 48 <= vk <= 57:    # 0-9 → ordinal
            return vk
        if vk in _VK_OEM_TO_ORDINAL:
            return _VK_OEM_TO_ORDINAL[vk]
    if key.char is not None:
        return ord(key.char.lower())
    if vk is not None:
        return vk
    return None


def _canonical(key: Union[Key, KeyCode, None]) -> Optional[Union[Key, int, object]]:
    """Normalize a key to a canonical hashable form.

    Key enum members and mouse Button members are returned as-is (already
    hashable). KeyCode objects are reduced to a stable ordinal via
    :func:`_keycode_ordinal` (vk-first for letters/digits) so the same physical
    key hashes identically on press and release regardless of held modifiers.
    """
    if _MouseButton is not None and isinstance(key, _MouseButton):
        return key
    if isinstance(key, Key):
        return key
    if isinstance(key, KeyCode):
        return _keycode_ordinal(key)
    return None


def _key_to_display(key: Union[Key, KeyCode]) -> str:
    """Convert a pynput key to a human-readable display name."""
    if _MouseButton is not None and isinstance(key, _MouseButton):
        return _MOUSE_BUTTON_TO_NAME.get(key, key.name)
    if isinstance(key, Key):
        return _KEY_TO_NAME.get(key, key.name)
    if isinstance(key, KeyCode):
        # Resolve to the physical-key ordinal (vk-first) so a Ctrl-held control
        # char like '\x01' for Ctrl+A never leaks into the display, then render
        # its printable character. Shares _keycode_ordinal with _canonical so
        # the display name and the match key can never disagree.
        ordinal = _keycode_ordinal(key)
        if ordinal is not None:
            ch = chr(ordinal)
            if ch.isprintable():
                return ch.lower()
    return str(key)


def _parse_key(name: str) -> Optional[frozenset]:
    """Parse a single key name to the set of canonical values that satisfy it."""
    name = name.strip().lower()
    name = _ALIASES.get(name, name)
    if name in _MOUSE_NAME_TO_BUTTON:
        return frozenset({_MOUSE_NAME_TO_BUTTON[name]})
    if name in _GENERIC_MODIFIERS:
        return frozenset(_GENERIC_MODIFIERS[name])
    if name in _NAME_TO_KEY:
        return frozenset({_NAME_TO_KEY[name]})
    if len(name) == 1:
        if name in {"`", "~"}:
            return _GRAVE_KEY_ORDINALS
        return frozenset({ord(name)})
    return None


def _resolve_send_key(name: str) -> Optional[Union[Key, KeyCode]]:
    """Resolve a key name to a pynput Key/KeyCode for Controller.press/release."""
    name = name.strip().lower()
    if name in _NAME_TO_KEY:
        return _NAME_TO_KEY[name]
    if name in _GENERIC_TO_LEFT:
        return _GENERIC_TO_LEFT[name]
    if len(name) == 1:
        return KeyCode.from_char(name)
    return None


def _canon_to_vk(canon: object) -> Optional[int]:
    """Best-effort map a canonical ``_pressed`` entry back to a Windows VK code.

    Mirrors how ``_canonical`` / ``_keycode_ordinal`` build the entries:
    - ``Key`` enum members carry their VK at ``key.value.vk``.
    - Letters are stored as the lowercase ordinal (``ord('a')``..``ord('z')`` =
      97..122); the Windows VK for a letter is its UPPERCASE ASCII (65..90).
    - Digits are stored as their ASCII ordinal (48..57), which equals VK_0..VK_9.
    - Standard OEM punctuation uses a stable US-position ordinal with a reverse
      lookup to its Windows virtual-key code.

    Returns None for entries with no stable VK (punctuation reached via the char
    fallback, mouse buttons); callers treat None as "can't verify, leave alone."
    """
    vk = getattr(getattr(canon, "value", None), "vk", None)
    if vk is not None:
        return int(vk)
    if isinstance(canon, int):
        if 97 <= canon <= 122:   # 'a'-'z' lowercase ordinal -> VK 'A'-'Z'
            return canon - 32
        if 48 <= canon <= 57:    # '0'-'9' -> VK_0..VK_9
            return canon
        if canon in _OEM_ORDINAL_TO_VK:
            return _OEM_ORDINAL_TO_VK[canon]
    return None


def _is_suppressible_standalone_recording_targets(
    targets: list[frozenset],
) -> bool:
    """Return whether *targets* is one eligible standalone recording key."""

    if len(targets) != 1 or not targets[0]:
        return False
    alternatives = targets[0]
    return all(target in _SUPPRESSIBLE_STANDALONE_KEYS for target in alternatives) or (
        alternatives.issubset(_SUPPRESSIBLE_GRAVE_ORDINALS)
    )


def _suppression_key_for_vk(
    targets: list[frozenset], vk: int,
) -> Optional[Union[Key, KeyCode]]:
    """Map a suppressible standalone binding's raw Windows VK to pynput."""

    if not _is_suppressible_standalone_recording_targets(targets):
        return None
    alternatives = targets[0]
    if vk == _VK_GRAVE:
        # VK_OEM_3 is the physical grave/tilde key. Preserve whichever of its
        # two printable names the stored binding uses so _on_press matches it.
        for char in ("`", "~"):
            if ord(char) in alternatives:
                return KeyCode.from_char(char)
        return None
    key = _SUPPRESSIBLE_VK_TO_KEY.get(vk)
    if key is not None and key in alternatives:
        return key
    return None


# ---------------------------------------------------------------------------
# Windows adapter
# ---------------------------------------------------------------------------

class WindowsShortcutAdapter:
    """Windows pynput adapter for the native-free shortcut contract.

    ``KeyboardShortcutManager`` remains an alias below so existing callers keep
    their shipped API. Native key objects, listener instances, configured-name
    parsing, low-level filtering, and physical modifier reconciliation all stay
    inside this adapter.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger,
        keyboard_listener_factory: Optional[Callable[..., object]] = None,
        mouse_listener_factory: Optional[Callable[..., object]] = None,
        controller_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._logger = logger
        self._controller = (controller_factory or Controller)()
        self._keyboard_listener_factory = keyboard_listener_factory or Listener
        self._mouse_listener_factory = (
            mouse_listener_factory
            if mouse_listener_factory is not None
            else MouseListener
        )
        self._listener: Optional[object] = None
        self._mouse_listener: Optional[object] = None
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._pressed: set[Union[Key, int]] = set()
        self._wait_event = threading.Event()
        self._closed = False
        self._last_cleanup_failure: Optional[ShortcutFailure] = None

        # Self-event suppression: depth counter incremented by
        # suppress_self_events(). While > 0, _on_press / _on_release ignore
        # incoming events so synthetic keystrokes from send() or direct
        # Controller.press calls (e.g. text_injector's modifier-release
        # guard) don't re-enter the hotkey logic. Re-entrant: nested
        # suppress() blocks stack their depths.
        self._suppress_depth: int = 0
        self._suppress_lock = threading.Lock()

        # Dedicated early system-key interception. Unlike secondary hotkeys,
        # Enter/Escape must be suppressed before they reach the focused app.
        # These slots are fixed and never mutate _extra_hotkeys while the
        # listener iterates it.
        self._early_key_lock = threading.Lock()
        self._early_key_handlers: dict[
            int, tuple[Callable[[], bool], Callable[[], None]]
        ] = {}
        self._early_suppressed_vks: set[int] = set()
        self._early_suppressed_at: dict[int, float] = {}
        self._recording_suppression_lock = threading.Lock()
        self._recording_suppressed_keys: dict[
            int, Union[Key, KeyCode]
        ] = {}

        # Callback dispatch queue + daemon worker thread.
        # pynput callbacks run on the OS hook thread — blocking there freezes
        # all system input.  Heavy user callbacks are dispatched here instead.
        self._queue: queue.Queue[object] = queue.Queue()
        self._worker = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="KbdDispatch",
        )
        self._worker.start()

        # Mode: "capture" | "none". This is ONLY the capture discriminator —
        # push-to-talk and toggle are no longer mutually-exclusive modes; they
        # are co-resident slots (below), each evaluated on every key event.
        # While ``_mode == "capture"`` both recording slots short-circuit so a
        # shortcut being captured does not also trigger a recording.
        self._mode = "none"

        # Push-to-talk slot. Has its OWN target list (independent of toggle).
        # Each element is a frozenset of canonical key values; at least one
        # from each set must be in ``_pressed`` for the hotkey to be active.
        # When any target is a modifier, no unconfigured modifier may be held.
        # Empty target list = unbound (never fires).
        self._ptt_targets: list[frozenset] = []
        self._ptt_on_press: Optional[Callable] = None
        self._ptt_on_release: Optional[Callable] = None
        self._ptt_contract_callback: Optional[Callable] = None
        self._ptt_action_id: Optional[str] = None
        self._ptt_state = ShortcutGestureState(ShortcutGesture.HOLD)
        self._ptt_pressed = False
        self._ptt_shortcut_name: str = ""

        # Toggle slot. Has its OWN target list (independent of PTT). Empty
        # target list = unbound (never fires).
        self._toggle_targets: list[frozenset] = []
        self._toggle_on_start: Optional[Callable] = None
        self._toggle_on_stop: Optional[Callable] = None
        self._toggle_contract_callback: Optional[Callable] = None
        self._toggle_action_id: Optional[str] = None
        self._toggle_state = ShortcutGestureState(ShortcutGesture.TOGGLE)
        self._toggle_active = False
        self._toggle_debounce = False
        self._toggle_shortcut_name: str = ""

        # Extra hotkeys (secondary, independent of primary PTT/toggle hotkey).
        # Keyed by a stable label (e.g. "paste_last", "paste_last_original").
        # Each entry: {"targets": list[frozenset], "on_press": Callable, "pressed": bool}.
        # Mirrors the _profile_hotkeys registry pattern, generalized to N hotkeys.
        self._extra_hotkeys: dict[str, dict] = {}

        # Pin-toggle hotkey (dedicated parallel tap slot — do NOT reuse the
        # _extra_hotkeys registry above, which the paste hotkeys occupy).
        # Fires once per press (tap-style), rearms on release, never
        # push-to-talk.

        # Per-profile manual-switch hotkeys (tap-style, fire once on press).
        # Each entry is a dict keyed by profile id with the parsed key-target
        # list, the on_press callback, and a debounce flag so a held combo fires
        # exactly once. Checked alongside the primary + extra hotkeys in
        # _on_press / _on_release; no dedicated listener (they ride the primary
        # listener like the extra hotkey). Mirrors the extra-hotkey machinery,
        # generalized to N hotkeys.
        self._profile_hotkeys: dict[str, dict] = {}

        # Capture
        self._capture_active = False
        self._capture_names: list[str] = []
        self._capture_canonical: set = set()
        self._capture_on_changed: Optional[Callable] = None
        self._capture_on_captured: Optional[Callable] = None
        self._capture_contract_callback: Optional[ShortcutCaptureCallback] = None

    # ------------------------------------------------------------------ dispatch

    def _dispatch_loop(self) -> None:
        while True:
            cb = self._queue.get()
            if cb is _DISPATCH_STOP:
                return
            try:
                assert callable(cb)
                cb()
            except Exception:
                self._logger.exception("hotkey callback raised")

    def _queue_action_callback(
        self, callback: Callable[[], None], *, allow_when_closed: bool = False
    ) -> None:
        """Queue one action that cannot begin new work after adapter close."""

        def guarded() -> None:
            if self._closed and not allow_when_closed:
                return
            callback()

        self._queue.put(guarded)

    # ------------------------------------------------------------------ listener lifecycle

    def _start_listener(self) -> None:
        if self._closed:
            raise _ListenerDisabledError("shortcut adapter is closed")
        self._stop_listener()
        listener_kwargs = {
            "on_press": self._on_press,
            "on_release": self._on_release,
        }
        if sys.platform == "win32":
            listener_kwargs["win32_event_filter"] = self._win32_event_filter
        keyboard_listener: Optional[object] = None
        mouse_listener: Optional[object] = None
        try:
            keyboard_listener = self._keyboard_listener_factory(**listener_kwargs)
            keyboard_listener.start()
            is_alive = getattr(keyboard_listener, "is_alive", None)
            if callable(is_alive) and is_alive() is False:
                raise _ListenerDisabledError("keyboard listener disabled")
            if self._mouse_listener_factory is not None:
                mouse_listener = self._mouse_listener_factory(on_click=self._on_click)
                mouse_listener.start()
                is_alive = getattr(mouse_listener, "is_alive", None)
                if callable(is_alive) and is_alive() is False:
                    raise _ListenerDisabledError("mouse listener disabled")
        except Exception:
            self._stop_native_listener(mouse_listener)
            self._stop_native_listener(keyboard_listener)
            with self._lock:
                self._listener = None
                self._mouse_listener = None
                self._pressed.clear()
            raise
        with self._lock:
            self._listener = keyboard_listener
            self._mouse_listener = mouse_listener
            self._pressed.clear()

    @staticmethod
    def _stop_native_listener(listener: Optional[object]) -> bool:
        """Stop and join one exact listener; return whether cleanup succeeded."""

        if listener is None:
            return True
        ok = True
        try:
            listener.stop()
        except Exception:
            ok = False
        join = getattr(listener, "join", None)
        if callable(join) and listener is not threading.current_thread():
            try:
                join(timeout=_LISTENER_JOIN_TIMEOUT_S)
            except (RuntimeError, TypeError):
                ok = False
        is_alive = getattr(listener, "is_alive", None)
        if callable(is_alive):
            try:
                alive = is_alive()
                if isinstance(alive, bool) and alive:
                    ok = False
            except Exception:
                ok = False
        return ok

    def _stop_listener(self) -> Optional[ShortcutFailure]:
        with self._lock:
            mouse_listener = self._mouse_listener
            keyboard_listener = self._listener
            self._mouse_listener = None
            self._listener = None
            self._pressed.clear()
            with self._early_key_lock:
                self._early_suppressed_vks.clear()
                self._early_suppressed_at.clear()
            with self._recording_suppression_lock:
                self._recording_suppressed_keys.clear()
        ok = self._stop_native_listener(mouse_listener)
        ok = self._stop_native_listener(keyboard_listener) and ok
        failure = None if ok else ShortcutFailure.CLEANUP_FAILED
        self._last_cleanup_failure = failure
        if failure is not None:
            self._logger.warning("hotkey listener cleanup failed")
        return failure

    def _has_bindings(self) -> bool:
        with self._early_key_lock:
            has_early = bool(self._early_key_handlers)
        return bool(
            self._ptt_targets
            or self._toggle_targets
            or self._extra_hotkeys
            or self._profile_hotkeys
            or has_early
        )

    def _ensure_listener(self) -> None:
        if self._listener is None and self._has_bindings():
            self._start_listener()

    def _win32_event_filter(self, msg, data) -> bool:
        """Selectively consume routed system keys and standalone recording keys."""

        if getattr(data, "flags", 0) & _LLKHF_INJECTED_MASK:
            return True
        vk = int(getattr(data, "vkCode", -1))
        if msg in {_WM_KEYUP, _WM_SYSKEYUP}:
            with self._recording_suppression_lock:
                recording_key = self._recording_suppressed_keys.pop(vk, None)
            if recording_key is not None:
                # Low-level hooks run before Windows updates its async state.
                # A down that timed out (or was injected) may have reached the
                # OS despite our suppression latch. Preserve its balancing up.
                reached_windows = self._physical_key_down(vk)
                # Returning False prevents pynput's normal callback dispatch,
                # so forward the release manually before suppress_event raises.
                self._on_release(recording_key)
                if reached_windows:
                    return False
                listener = self._listener
                if listener is not None:
                    listener.suppress_event()
                return False
            with self._early_key_lock:
                suppress = vk in self._early_suppressed_vks
                self._early_suppressed_vks.discard(vk)
                self._early_suppressed_at.pop(vk, None)
            if suppress:
                listener = self._listener
                if listener is not None:
                    listener.suppress_event()
                return False
            return True
        if msg not in {_WM_KEYDOWN, _WM_SYSKEYDOWN}:
            return True

        now = time.monotonic()
        with self._early_key_lock:
            handler = self._early_key_handlers.get(vk)
            repeated = vk in self._early_suppressed_vks
            if repeated:
                last = self._early_suppressed_at.get(vk, now)
                if now - last > _EARLY_REPEAT_STALE_S:
                    # Lost key-up: this is a new press, so re-run the predicate.
                    self._early_suppressed_vks.discard(vk)
                    self._early_suppressed_at.pop(vk, None)
                    repeated = False
                else:
                    self._early_suppressed_at[vk] = now
        if handler is not None:
            if repeated:
                listener = self._listener
                if listener is not None:
                    listener.suppress_event()
                return False
            predicate, callback = handler
            try:
                if _routed_key_modifier_down():
                    self._logger.debug("routed key pass vk=%d reason=modifier_down", vk)
                    should_consume = False
                elif vk == _VK_ENTER and _foreground_uses_ime_layout():
                    self._logger.debug("routed key pass vk=%d reason=ime_layout", vk)
                    should_consume = False
                else:
                    should_consume = predicate() is True
            except Exception:
                should_consume = False
            if should_consume:
                with self._early_key_lock:
                    self._early_suppressed_vks.add(vk)
                    self._early_suppressed_at[vk] = now
                self._queue.put(callback)
                listener = self._listener
                if listener is not None:
                    listener.suppress_event()
                return False

        with self._recording_suppression_lock:
            recording_key = self._recording_suppressed_keys.get(vk)
        if recording_key is None:
            recording_key = self._recording_suppression_key(vk)
            if recording_key is None:
                return True
            with self._recording_suppression_lock:
                self._recording_suppressed_keys[vk] = recording_key
        # Forward every keydown, including repeats, to retain pynput's existing
        # debounce behavior while the physical event stays hidden from apps.
        self._on_press(recording_key)
        listener = self._listener
        if listener is not None:
            listener.suppress_event()
        return False

    def _recording_suppression_key(
        self, vk: int,
    ) -> Optional[Union[Key, KeyCode]]:
        """Return the pynput key for an eligible standalone PTT/toggle VK."""

        if self._mode != "none":
            return None
        return (
            _suppression_key_for_vk(self._ptt_targets, vk)
            or _suppression_key_for_vk(self._toggle_targets, vk)
        )

    # ------------------------------------------------------------------ pynput callbacks

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        # Only middle and side buttons are registerable as shortcuts. Ignore
        # left/right so normal UI interaction is never intercepted.
        if (
            button is not _MOUSE_MIDDLE
            and button is not _MOUSE_X1
            and button is not _MOUSE_X2
        ):
            return
        if pressed:
            self._on_press(button)
        else:
            self._on_release(button)

    def _on_press(self, key: Union[Key, KeyCode, "_MouseButton", None]) -> None:
        # Skip self-injected events to prevent synthetic keystrokes from
        # send() / Controller.press from re-entering the hotkey logic
        # (would otherwise produce phantom recordings on shortcuts whose
        # set includes a key we synthesize, e.g. left shift / insert).
        if self._suppress_depth > 0:
            return
        canon = _canonical(key)
        if canon is None:
            return
        self._pressed.add(canon)

        # Capture mode short-circuits both recording slots: a shortcut being
        # captured must not also trigger a recording.
        if self._mode != "none":
            if self._mode != "capture":
                return
            display = _key_to_display(key)
            if display not in self._capture_names:
                self._capture_names.append(display)
                self._capture_canonical.add(canon)
                combo = "+".join(self._capture_names)
                cb = self._capture_on_changed
                if cb:
                    self._queue.put(lambda c=combo, f=cb: f(c))
            return

        # The remaining slots are co-resident: each is checked independently on
        # every event (NOT mutually exclusive), gated only on its own target
        # list being non-empty (= bound).

        # Extra hotkeys (paste-last, paste-last-original, …). Each fires once per press.
        if self._extra_hotkeys:
            for action_id, entry in self._extra_hotkeys.items():
                if self._check_targets(entry["targets"]):
                    actions = entry["state"].apply(ShortcutTransition.PRESSED)
                    entry["pressed"] = entry["state"].active
                    for action in actions:
                        contract_cb = entry.get("contract_callback")
                        cb = entry.get("on_press")
                        if contract_cb is not None:
                            self._queue_action_callback(
                                lambda aid=action_id, a=action, f=contract_cb: f(aid, a)
                            )
                        elif cb is not None:
                            self._queue_action_callback(cb)

        # Profile hotkeys (issue 14). Tap-style: fire once per press, on the
        # dispatch worker so a heavy callback never blocks the OS hook thread.
        if self._profile_hotkeys:
            for action_id, entry in self._profile_hotkeys.items():
                if self._check_targets(entry["targets"]):
                    actions = entry["state"].apply(ShortcutTransition.PRESSED)
                    entry["pressed"] = entry["state"].active
                    for action in actions:
                        contract_cb = entry.get("contract_callback")
                        cb = entry.get("on_press")
                        if contract_cb is not None:
                            self._queue_action_callback(
                                lambda aid=action_id, a=action, f=contract_cb: f(aid, a)
                            )
                        elif cb is not None:
                            self._queue_action_callback(cb)

        # Push-to-talk slot.
        if self._ptt_targets:
            if self._check_ptt_hotkey():
                actions = self._ptt_state.apply(ShortcutTransition.PRESSED)
                self._ptt_pressed = self._ptt_state.active
                for action in actions:
                    self._logger.info("hotkey pressed key=%s mode=push_to_talk", self._ptt_shortcut_name)
                    if self._ptt_contract_callback is not None:
                        self._queue_action_callback(
                            lambda aid=self._ptt_action_id or "push-to-talk", a=action,
                            f=self._ptt_contract_callback: f(aid, a)
                        )
                    elif self._ptt_on_press is not None:
                        self._queue_action_callback(self._ptt_on_press)

        # Toggle slot.
        if self._toggle_targets:
            if self._check_toggle_hotkey():
                actions = self._toggle_state.apply(ShortcutTransition.PRESSED)
                self._toggle_debounce = self._toggle_state.active
                for action in actions:
                    self._logger.info("hotkey pressed key=%s mode=toggle", self._toggle_shortcut_name)
                    if self._toggle_contract_callback is not None:
                        self._queue_action_callback(
                            lambda aid=self._toggle_action_id or "toggle-recording", a=action,
                            f=self._toggle_contract_callback: f(aid, a)
                        )
                    elif self._toggle_active:
                        self._toggle_active = False
                        if self._toggle_on_stop:
                            self._queue_action_callback(self._toggle_on_stop)
                    else:
                        self._toggle_active = True
                        if self._toggle_on_start:
                            self._queue_action_callback(self._toggle_on_start)

    def _on_release(self, key: Union[Key, KeyCode, "_MouseButton", None]) -> None:
        # See _on_press — drop self-injected releases for the same reason.
        if self._suppress_depth > 0:
            return
        canon = _canonical(key)
        if canon is None:
            return
        self._pressed.discard(canon)

        # Capture mode short-circuits both recording slots (see _on_press).
        if self._mode == "capture":
            if self._capture_names and not (self._capture_canonical & self._pressed):
                hotkey = "+".join(self._capture_names)
                self._capture_active = False
                self._capture_names = []
                self._capture_canonical = set()
                self._mode = "capture-pending"
                self._logger.info("capture mode exited key=%s", hotkey)
                self._queue.put(lambda h=hotkey: self._complete_capture(h))
            return
        if self._mode != "none":
            return

        # Co-resident slots: each reset independently on every event, gated on
        # its own target list being non-empty (= bound).

        # Reset each extra hotkey's pressed state when its combo is no longer held.
        if self._extra_hotkeys:
            for entry in self._extra_hotkeys.values():
                if not self._check_targets(entry["targets"]):
                    entry["state"].apply(ShortcutTransition.RELEASED)
                    entry["pressed"] = entry["state"].active

        # Reset each profile hotkey's pressed flag when its combo is released
        # (issue 14) so it can fire again on the next press (tap-style debounce).
        if self._profile_hotkeys:
            for entry in self._profile_hotkeys.values():
                if not self._check_targets(entry["targets"]):
                    entry["state"].apply(ShortcutTransition.RELEASED)
                    entry["pressed"] = entry["state"].active

        # Push-to-talk slot: release when its combo is no longer fully held.
        if self._ptt_targets:
            if not self._check_ptt_hotkey():
                actions = self._ptt_state.apply(ShortcutTransition.RELEASED)
                self._ptt_pressed = self._ptt_state.active
                for action in actions:
                    self._logger.info("hotkey released key=%s mode=push_to_talk", self._ptt_shortcut_name)
                    if self._ptt_contract_callback is not None:
                        self._queue_action_callback(
                            lambda aid=self._ptt_action_id or "push-to-talk", a=action,
                            f=self._ptt_contract_callback: f(aid, a)
                        )
                    elif self._ptt_on_release is not None:
                        self._queue_action_callback(self._ptt_on_release)

        # Toggle slot: clear the debounce once its combo is released.
        if self._toggle_targets:
            if not self._check_toggle_hotkey():
                self._toggle_state.apply(ShortcutTransition.RELEASED)
                self._toggle_debounce = self._toggle_state.active

    def _check_targets(self, targets: list[frozenset]) -> bool:
        """Return True iff every alternative-set in *targets* is satisfied by a
        currently-pressed key and no unconfigured modifier is held.

        Shortcuts without a modifier target retain their prior behavior: held
        modifiers do not prevent an ordinary key or mouse button from firing.
        An empty target list is never satisfied.
        """
        for alts in targets:
            if not (alts & self._pressed):
                return False

        allowed_modifiers = frozenset().union(
            *(alts & _ALL_TRACKED_MODIFIERS for alts in targets)
        )
        if allowed_modifiers:
            held_modifiers = self._pressed & _ALL_TRACKED_MODIFIERS
            if not held_modifiers <= allowed_modifiers:
                return False
        return bool(targets)

    def _check_ptt_hotkey(self) -> bool:
        return self._check_targets(self._ptt_targets)

    def _check_toggle_hotkey(self) -> bool:
        return self._check_targets(self._toggle_targets)

    # ------------------------------------------------------------------ public API

    def register_early_key_handler(
        self,
        key: str,
        predicate: Callable[[], bool],
        on_press: Callable[[], None],
    ) -> None:
        """Register one fixed Enter/Escape handler for selective suppression."""

        normalized = key.strip().lower()
        vk = {
            "enter": _VK_ENTER,
            "return": _VK_ENTER,
            "esc": _VK_ESCAPE,
            "escape": _VK_ESCAPE,
        }.get(normalized)
        if vk is None:
            raise ValueError("early key must be Enter or Escape")
        if not callable(predicate) or not callable(on_press):
            raise TypeError("early key handlers must be callable")
        with self._early_key_lock:
            self._early_key_handlers[vk] = (predicate, on_press)

    def reset_toggle_state(self) -> None:
        """Reset routed-toggle ownership without touching pressed-key debounce."""

        self._toggle_active = False

    @staticmethod
    def _logical_key_name(name: str) -> Optional[str]:
        """Return the adapter-private configured name as one logical key."""

        normalized = name.strip().lower()
        normalized = _ALIASES.get(normalized, normalized)
        normalized = {"return": "enter", "escape": "esc"}.get(
            normalized, normalized
        )
        if (
            normalized in _MOUSE_NAME_TO_BUTTON
            or normalized in _GENERIC_MODIFIERS
            or normalized in _NAME_TO_KEY
            or len(normalized) == 1
        ):
            return normalized
        return None

    def _binding_from_config(
        self,
        action_id: str,
        gesture: ShortcutGesture,
        configured: str,
    ) -> ShortcutBinding:
        logical: list[str] = []
        if configured.strip():
            for part in configured.split("+"):
                name = self._logical_key_name(part)
                if name is not None and name not in logical:
                    logical.append(name)
        return ShortcutBinding(
            action_id=action_id,
            gesture=gesture,
            configured=configured,
            shortcut=CanonicalShortcut(tuple(logical)) if logical else None,
        )

    def _remove_binding(self, action_id: str) -> None:
        if action_id == self._ptt_action_id:
            for action in self._ptt_state.reset():
                if self._ptt_contract_callback is not None:
                    self._queue_action_callback(
                        lambda aid=action_id, a=action,
                        f=self._ptt_contract_callback: f(aid, a)
                    )
                elif self._ptt_on_release is not None:
                    self._queue_action_callback(self._ptt_on_release)
            self._ptt_targets = []
            self._ptt_action_id = None
            self._ptt_contract_callback = None
            self._ptt_on_press = None
            self._ptt_on_release = None
            self._ptt_pressed = False
            self._ptt_shortcut_name = ""
            return
        if action_id == self._toggle_action_id:
            self._toggle_state.reset()
            self._toggle_targets = []
            self._toggle_action_id = None
            self._toggle_contract_callback = None
            self._toggle_on_start = None
            self._toggle_on_stop = None
            self._toggle_active = False
            self._toggle_debounce = False
            self._toggle_shortcut_name = ""
            return
        self._extra_hotkeys.pop(action_id, None)
        self._profile_hotkeys.pop(action_id, None)

    def _install_binding(
        self,
        binding: ShortcutBinding,
        callback: Optional[Callable],
        targets: list[frozenset],
    ) -> Optional[ShortcutFailure]:
        if binding.gesture is ShortcutGesture.HOLD:
            if self._ptt_action_id not in (None, binding.action_id):
                return ShortcutFailure.CONFLICT
            self._ptt_action_id = binding.action_id
            self._ptt_targets = targets
            self._ptt_contract_callback = callback
            self._ptt_on_press = None
            self._ptt_on_release = None
            self._ptt_state = ShortcutGestureState(ShortcutGesture.HOLD)
            self._ptt_pressed = False
            self._ptt_shortcut_name = binding.configured
            return None
        if binding.gesture is ShortcutGesture.TOGGLE:
            if self._toggle_action_id not in (None, binding.action_id):
                return ShortcutFailure.CONFLICT
            self._toggle_action_id = binding.action_id
            self._toggle_targets = targets
            self._toggle_contract_callback = callback
            self._toggle_on_start = None
            self._toggle_on_stop = None
            self._toggle_state = ShortcutGestureState(ShortcutGesture.TOGGLE)
            self._toggle_debounce = False
            self._toggle_shortcut_name = binding.configured
            return None
        entry = {
            "targets": targets,
            "on_press": None,
            "contract_callback": callback,
            "state": ShortcutGestureState(ShortcutGesture.TAP),
            "pressed": False,
        }
        if binding.action_id.startswith("profile:"):
            self._profile_hotkeys[binding.action_id] = entry
        else:
            self._extra_hotkeys[binding.action_id] = entry
        return None

    @staticmethod
    def _registration_failure(exc: BaseException) -> ShortcutFailure:
        if isinstance(exc, PermissionError):
            return ShortcutFailure.PERMISSION_DENIED
        if isinstance(exc, _ListenerDisabledError):
            return ShortcutFailure.LISTENER_DISABLED
        return ShortcutFailure.NATIVE_ERROR

    def register(
        self,
        binding: ShortcutBinding,
        callback: Callable[[str, ShortcutAction], None],
    ) -> ShortcutRegistrationOutcome:
        """Register one schema-v1 binding with typed, content-free outcomes."""

        if not isinstance(binding, ShortcutBinding) or not callable(callback):
            action_id = getattr(binding, "action_id", "invalid")
            return ShortcutRegistrationOutcome(
                action_id=action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.INVALID_BINDING,
            )
        if self._closed:
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.LISTENER_DISABLED,
            )
        if not binding.configured.strip():
            self._remove_binding(binding.action_id)
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.UNBOUND,
            )
        targets = self._parse_shortcut(binding.configured)
        parsed = self._binding_from_config(
            binding.action_id, binding.gesture, binding.configured
        ).shortcut
        if not targets or binding.shortcut is None or parsed != binding.shortcut:
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=ShortcutFailure.INVALID_BINDING,
            )
        failure = self._install_binding(binding, callback, targets)
        if failure is not None:
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=failure,
            )
        try:
            self._ensure_listener()
        except Exception as exc:
            self._remove_binding(binding.action_id)
            self._stop_listener()
            return ShortcutRegistrationOutcome(
                action_id=binding.action_id,
                status=ShortcutRegistrationStatus.FAILED,
                failure=self._registration_failure(exc),
            )
        return ShortcutRegistrationOutcome(
            action_id=binding.action_id,
            status=ShortcutRegistrationStatus.REGISTERED,
        )

    @staticmethod
    def _raise_legacy_registration_failure(
        shortcut: str, outcome: ShortcutRegistrationOutcome
    ) -> None:
        if outcome.status is not ShortcutRegistrationStatus.FAILED:
            return
        if outcome.failure is ShortcutFailure.INVALID_BINDING:
            raise ValueError(f"Could not parse shortcut: {shortcut!r}")
        if outcome.failure is ShortcutFailure.PERMISSION_DENIED:
            raise PermissionError("global shortcut registration denied")
        raise RuntimeError(f"global shortcut registration failed: {outcome.failure}")

    def register_push_to_talk(
        self, shortcut: str, on_press: Callable[[], None], on_release: Callable[[], None],
    ) -> None:
        """Register the push-to-talk slot (hold-to-record, release to stop).

        This is a co-resident slot: registering it does NOT clear the toggle
        slot — both can be bound at once and the key the user presses picks the
        behavior. An empty ``shortcut`` string leaves the slot unbound (target
        list empty, never fires) but still stores the handlers.
        """
        binding = self._binding_from_config(
            "push-to-talk", ShortcutGesture.HOLD, shortcut
        )
        outcome = self.register(binding, lambda _action_id, _action: None)
        self._raise_legacy_registration_failure(shortcut, outcome)
        self._ptt_contract_callback = None
        self._ptt_on_press = on_press
        self._ptt_on_release = on_release
        self._logger.info(
            "hotkey registered key=%s mode=push_to_talk keys=%d",
            shortcut,
            len(self._ptt_targets),
        )

    def register_toggle(
        self, shortcut: str, on_start: Callable[[], None], on_stop: Callable[[], None],
    ) -> None:
        """Register the toggle slot: first press starts, second press stops.

        This is a co-resident slot: registering it does NOT clear the
        push-to-talk slot — both can be bound at once. An empty ``shortcut``
        string leaves the slot unbound (target list empty, never fires) but
        still stores the handlers.
        """
        binding = self._binding_from_config(
            "toggle-recording", ShortcutGesture.TOGGLE, shortcut
        )
        outcome = self.register(binding, lambda _action_id, _action: None)
        self._raise_legacy_registration_failure(shortcut, outcome)
        self._toggle_contract_callback = None
        self._toggle_on_start = on_start
        self._toggle_on_stop = on_stop
        self._toggle_active = False
        self._logger.info(
            "hotkey registered key=%s mode=toggle keys=%d",
            shortcut,
            len(self._toggle_targets),
        )

    def register_extra_hotkey(
        self, shortcut: str, on_press: Callable[[], None], *, key: str = "paste_last",
    ) -> None:
        """Register a secondary hotkey that fires *on_press* when the combo is detected.

        The extra hotkey is independent of the primary PTT/toggle hotkey. It
        fires once per press (not push-to-talk). It is checked alongside the
        primary hotkey in ``_on_press`` / ``_on_release`` and does not require
        its own listener.

        *key* is a stable label used to identify this slot in the registry
        (e.g. ``"paste_last"``, ``"paste_last_original"``). Registering a second
        time with the same *key* replaces the existing entry (no duplication).

        An empty or whitespace-only *shortcut* unbinds the slot for *key* without
        raising. A non-empty but unparseable shortcut raises ``ValueError``.

        If no listener is running, registration starts it. This keeps secondary
        actions usable when both recording shortcut slots are intentionally
        unbound.
        """
        if not shortcut or not shortcut.strip():
            # Empty/blank: unbind this key gracefully.
            self._extra_hotkeys.pop(key, None)
            return
        targets = self._parse_shortcut(shortcut)
        if not targets:
            raise ValueError(f"Could not parse extra shortcut: {shortcut!r}")
        self._extra_hotkeys[key] = {
            "targets": targets,
            "on_press": on_press,
            "contract_callback": None,
            "state": ShortcutGestureState(ShortcutGesture.TAP),
            "pressed": False,
        }
        try:
            self._ensure_listener()
        except Exception:
            self._extra_hotkeys.pop(key, None)
            self._stop_listener()
            raise
        self._logger.info("hotkey registered key=%s mode=extra label=%s keys=%d", shortcut, key, len(targets))

    def unregister_extra_hotkey(self, *, key: Optional[str] = None) -> None:
        """Remove one or all extra hotkey registrations.

        If *key* is ``None``, clears the entire extra-hotkey registry.
        Otherwise removes only the entry for *key* (a no-op if that key is not
        registered).
        """
        if key is None:
            self._extra_hotkeys = {}
        else:
            entry = self._extra_hotkeys.pop(key, None)
            if entry is not None:
                entry["state"].reset()

    def register_profile_hotkeys(
        self, hotkeys: dict[str, str], on_press: Callable[[str], None],
    ) -> None:
        """Register N tap-style per-profile hotkeys (issue 14).

        *hotkeys* maps a profile id to a shortcut string (e.g.
        ``{"chat": "ctrl+alt+1"}``). Each registered combo fires *on_press*
        ONCE per press (tap-style, like the extra hotkey, not push-to-talk),
        passing the profile id. The hotkeys are checked alongside the primary +
        extra hotkeys in ``_on_press`` / ``_on_release`` and do not need their
        own listener — they ride the primary listener started by
        ``register_push_to_talk`` / ``register_toggle``.

        Fully replaces any prior profile-hotkey registration. Unparseable
        shortcuts are skipped with a warning (never raises) so one bad entry
        can't break the others or crash hotkey setup ("never-crash" contract).
        Call this AFTER the primary hotkey is registered; re-call on every
        config save to re-register the current set.
        """
        registry: dict[str, dict] = {}
        for profile_id, shortcut in hotkeys.items():
            if not shortcut or not shortcut.strip():
                continue
            targets = self._parse_shortcut(shortcut)
            if not targets:
                self._logger.warning(
                    "profile hotkey registration skipped reason=unparseable profile=%s shortcut=%r",
                    profile_id, shortcut,
                )
                continue
            registry[profile_id] = {
                "targets": targets,
                "on_press": lambda pid=profile_id: on_press(pid),
                "contract_callback": None,
                "state": ShortcutGestureState(ShortcutGesture.TAP),
                "pressed": False,
            }
        self._profile_hotkeys = registry
        try:
            self._ensure_listener()
        except Exception:
            self.unregister_profile_hotkeys()
            self._stop_listener()
            raise
        self._logger.info(
            "profile hotkeys registered count=%d", len(registry)
        )

    def unregister_profile_hotkeys(self) -> None:
        """Remove all per-profile hotkey registrations."""
        for entry in self._profile_hotkeys.values():
            entry["state"].reset()
        self._profile_hotkeys = {}

    def unhook_all(self) -> None:
        """Remove all registered keyboard hooks and reset every slot.

        Clears BOTH recording slots (targets + flags) plus the extra hotkey, so
        no stale ``_toggle_active`` / ``_ptt_pressed`` state leaks across a
        re-registration after an unhook.
        """
        self._stop_listener()
        self._mode = "none"
        # Push-to-talk slot
        ptt_actions = self._ptt_state.reset()
        for action in ptt_actions:
            if self._ptt_contract_callback is not None:
                self._queue_action_callback(
                    lambda aid=self._ptt_action_id or "push-to-talk", a=action,
                    f=self._ptt_contract_callback: f(aid, a),
                    allow_when_closed=self._closed,
                )
            elif self._ptt_on_release is not None:
                self._queue_action_callback(
                    self._ptt_on_release, allow_when_closed=self._closed
                )
        self._ptt_targets = []
        self._ptt_action_id = None
        self._ptt_contract_callback = None
        self._ptt_on_press = None
        self._ptt_on_release = None
        self._ptt_pressed = False
        self._ptt_shortcut_name = ""
        # Toggle slot
        self._toggle_state.reset()
        self._toggle_targets = []
        self._toggle_action_id = None
        self._toggle_contract_callback = None
        self._toggle_on_start = None
        self._toggle_on_stop = None
        self._toggle_active = False
        self._toggle_debounce = False
        self._toggle_shortcut_name = ""
        # Extra hotkeys (keyed registry)
        for entry in self._extra_hotkeys.values():
            entry["state"].reset()
        self._extra_hotkeys = {}
        for entry in self._profile_hotkeys.values():
            entry["state"].reset()
        self._profile_hotkeys = {}
        self._capture_active = False
        self._capture_names = []
        self._capture_canonical = set()
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None

    def capture_shortcut(
        self,
        on_keys_changed: Callable[[str], None],
        on_captured: Callable[[str], None],
    ) -> None:
        """Capture a keyboard shortcut combo.

        Stops any active listener, hooks a capture handler that tracks
        pressed keys, calls *on_keys_changed* with the current combo string
        as keys are pressed, and calls *on_captured* with the final hotkey
        string when all keys are released.
        """
        if self._capture_active:
            return
        self._begin_capture(on_keys_changed, on_captured, None)

    def begin_capture(self, callback: ShortcutCaptureCallback) -> None:
        """Begin contract capture while preserving the existing live UI seam."""

        if not callable(callback):
            raise TypeError("capture callback must be callable")
        if self._capture_active or self._mode != "none":
            self._queue.put(
                lambda f=callback: f(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.CONFLICT,
                    )
                )
            )
            return
        self._begin_capture(None, None, callback)

    def _begin_capture(
        self,
        on_keys_changed: Optional[Callable[[str], None]],
        on_captured: Optional[Callable[[str], None]],
        contract_callback: Optional[ShortcutCaptureCallback],
    ) -> None:
        self._stop_listener()
        self._capture_active = True
        self._capture_names = []
        self._capture_canonical = set()
        self._capture_on_changed = on_keys_changed
        self._capture_on_captured = on_captured
        self._capture_contract_callback = contract_callback
        self._mode = "capture"
        self._logger.info("capture mode entered")
        try:
            self._start_listener()
        except Exception as exc:
            self._capture_active = False
            self._mode = "none"
            self._stop_listener()
            callback = self._capture_contract_callback
            self._capture_contract_callback = None
            if callback is not None:
                failure = self._registration_failure(exc)
                self._queue.put(
                    lambda f=callback, reason=failure: f(
                        ShortcutCaptureOutcome(
                            status=ShortcutCaptureStatus.FAILED,
                            failure=reason,
                        )
                    )
                )
                return
            raise

    def _complete_capture(self, hotkey: str) -> None:
        legacy_callback = self._capture_on_captured
        contract_callback = self._capture_contract_callback
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None
        self._stop_listener()
        self._mode = "none"
        try:
            self._ensure_listener()
        except Exception:
            self._stop_listener()
            if contract_callback is not None:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.CLEANUP_FAILED,
                    )
                )
            return
        if contract_callback is not None:
            binding = self._binding_from_config(
                "captured", ShortcutGesture.TAP, hotkey
            )
            if binding.shortcut is None:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.FAILED,
                        failure=ShortcutFailure.INVALID_BINDING,
                    )
                )
            else:
                contract_callback(
                    ShortcutCaptureOutcome(
                        status=ShortcutCaptureStatus.CAPTURED,
                        shortcut=binding.shortcut,
                    )
                )
        if legacy_callback is not None:
            legacy_callback(hotkey)

    def stop_capture(self, *, timeout: bool = False) -> None:
        """Cancel any in-progress shortcut capture.

        Pass ``timeout=True`` when the capture was ended by a timeout rather
        than an explicit user cancellation, so the correct lifecycle event is
        emitted in the log.
        """
        was_capturing = self._capture_active or self._mode.startswith("capture")
        if self._capture_active:
            if timeout:
                self._logger.info("capture mode timeout")
        callback = self._capture_contract_callback
        self._capture_active = False
        self._capture_names = []
        self._capture_canonical = set()
        self._mode = "none"
        self._capture_on_changed = None
        self._capture_on_captured = None
        self._capture_contract_callback = None
        self._stop_listener()
        try:
            self._ensure_listener()
        except Exception:
            self._stop_listener()
            if callback is not None:
                self._queue.put(
                    lambda f=callback: f(
                        ShortcutCaptureOutcome(
                            status=ShortcutCaptureStatus.FAILED,
                            failure=ShortcutFailure.CLEANUP_FAILED,
                        )
                    )
                )
            return
        if was_capturing and callback is not None:
            self._queue.put(
                lambda f=callback: f(
                    ShortcutCaptureOutcome(status=ShortcutCaptureStatus.CANCELLED)
                )
            )

    def cancel_capture(self) -> None:
        self.stop_capture()

    def close(self) -> None:
        """Idempotently stop exact listeners and drain queued hold teardown."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.unhook_all()
        self._queue.put(_DISPATCH_STOP)
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=_LISTENER_JOIN_TIMEOUT_S)
            if self._worker.is_alive():
                self._last_cleanup_failure = ShortcutFailure.CLEANUP_FAILED
                self._logger.warning("hotkey dispatch cleanup failed")
        self._wait_event.set()

    @contextmanager
    def suppress_self_events(self) -> Iterator[None]:
        """Context manager that drops self-injected key events.

        While the guard is active (``_suppress_depth > 0``),
        ``_on_press`` and ``_on_release`` early-return without touching
        ``_pressed`` or firing hotkey callbacks. This prevents synthetic
        keystrokes from ``send()`` or direct ``Controller.press`` /
        ``Controller.release`` calls (e.g. text_injector's
        modifier-release guard around the paste combo) from re-entering
        the global hotkey listener and triggering phantom recordings on
        shortcuts whose key set overlaps a synthesized key (the original
        bug case: PTT bound to ``shift`` / ``left shift`` / ``insert``,
        all of which we synthesize during paste injection).

        Re-entrant: nested ``with`` blocks stack their depths so an
        outer ``text_injector`` block that itself calls ``send()``
        (which also enters this guard) Just Works.

        On exit, the depth decrement is delayed by ``_SUPPRESS_GRACE_S``
        seconds via a daemon Timer. This grace window absorbs the lag
        between when we issue a synthetic release and when pynput's
        low-level keyboard hook (running on its own OS thread) delivers
        the corresponding event to ``_on_release``. Without it, the
        trailing edge of an injection can leak through.
        """
        with self._suppress_lock:
            self._suppress_depth += 1
        try:
            yield
        finally:
            timer = _new_suppress_timer(
                _SUPPRESS_GRACE_S, self._suppress_release_one,
            )
            timer.daemon = True
            timer.start()

    def _suppress_release_one(self) -> None:
        """Decrement the suppression depth by 1. Called from the grace Timer.

        When the decrement closes the suppression window (depth returns to 0),
        reconcile the ``_pressed`` mirror with the real OS keyboard state: the
        window may have swallowed genuine user press/release events, leaving the
        mirror out of sync (phantom-stuck keys that spuriously satisfy a hotkey
        combo, or missing modifiers). See ``_reconcile_pressed_with_os``.
        """
        reached_zero = False
        with self._suppress_lock:
            if self._suppress_depth > 0:
                self._suppress_depth -= 1
                reached_zero = self._suppress_depth == 0
        # Reconcile OUTSIDE _suppress_lock to avoid nesting it with _lock.
        if reached_zero:
            self._reconcile_pressed_with_os()

    def send(self, combination: str) -> None:
        """Simulate a key combination (e.g. ``'ctrl+v'``, ``'volume mute'``)."""
        parts = [p.strip() for p in combination.split("+")]
        keys: list[Union[Key, KeyCode]] = []
        for part in parts:
            resolved = _resolve_send_key(part)
            if resolved is None:
                self._logger.warning("hotkey send failed reason=unknown_key key=%r combination=%r", part, combination)
                return
            keys.append(resolved)
        # Wrap inside the suppress guard so the synthetic press/release
        # cannot re-enter the global hotkey listener as a real keystroke.
        # Re-entrant: callers may nest this inside their own
        # suppress_self_events() block (text_injector does this).
        with self.suppress_self_events():
            for k in keys:
                self._controller.press(k)
            for k in reversed(keys):
                self._controller.release(k)

    def type_text(self, text: str, *, delay_ms: int = 0) -> None:
        """Type *text* char-by-char via the pynput Controller, inside the
        self-event-suppression guard so synthetic keystrokes can't re-enter the
        hotkey listener.  When delay_ms > 0, sleep delay_ms/1000 between chars
        (paced path for slow remote sessions); when 0, use Controller.type() for
        the fast path.  NEVER touches the clipboard.

        When Caps Lock is on (Windows only), each character is injected as a
        pure KEYEVENTF_UNICODE event via _inject_unicode_char(), which is
        immune to Caps Lock state.  When Caps Lock is off, the existing
        Controller.type() hybrid is used (VK/scancode for base keys, already
        the Citrix/RDP-friendly path).

        # TRANSCRIPT-DATA-DO-NOT-LOG: this method receives transcript text.
        # Never log `text` or any substring of it.
        """
        if not text:
            return
        with self.suppress_self_events():
            if _caps_lock_on():
                # Unicode-inject path: case-exact, Caps Lock state-immune.
                for ch in text:
                    _inject_unicode_char(ch)
                    if delay_ms > 0:
                        time.sleep(delay_ms / 1000.0)
            elif delay_ms <= 0:
                self._controller.type(text)
            else:
                for ch in text:
                    self._controller.type(ch)
                    time.sleep(delay_ms / 1000.0)

    def snapshot_held_modifiers(self) -> list[Key]:
        """Return the modifier Keys currently held down.

        Authoritative on the **real OS key state** (``GetAsyncKeyState``), unioned
        with the tracked ``self._pressed`` mirror, intersected with
        ``_ALL_TRACKED_MODIFIERS``.  The returned ``Key`` values are side-specific
        (e.g. ``Key.shift_r``) so they can be passed directly to
        ``Controller.release()`` / ``Controller.press()``.

        Why the OS state and not just the mirror: the mirror can MISS a
        physically-held modifier when its press was swallowed inside a
        ``suppress_self_events`` window (and our own synthetic injection churns
        modifier state mid-combo). The text-injector modifier-release guard reads
        this snapshot to decide which modifiers to release before typing/pasting;
        if it misses a held Ctrl/Alt, keystroke-mode typing runs with that
        modifier down and every typed char becomes a chord (e.g. VSCode opens /
        creates files instead of receiving text). Unioning with the OS state
        closes that gap. The mirror is still unioned in (never subtracted) so the
        existing stale-mirror re-press gate is preserved: a key the mirror lists
        but the OS reports up is released here yet NOT re-pressed (the re-press in
        ``_release_send_repress`` gates on ``modifier_physically_down``).

        Off-Windows there is no async-key oracle, so ``_physical_key_down`` returns
        False and this degrades to the mirror-only intersection (prior behavior).
        """
        with self._lock:
            held = {k for k in _ALL_TRACKED_MODIFIERS if k in self._pressed}
        for k in _ALL_TRACKED_MODIFIERS:
            vk = getattr(getattr(k, "value", None), "vk", None)
            if vk is not None and self._physical_key_down(int(vk)):
                held.add(k)
        return list(held)

    def _physical_key_down(self, vk: int) -> bool:
        """Return True iff the key with virtual-key code *vk* is physically down.

        Reads the real hardware state via ``GetAsyncKeyState`` (high-order bit
        0x8000 set => currently down). Windows only; returns False on other
        platforms or on any error (never raises -- preserves the no-admin /
        never-crash contract). The OS async key state never goes stale the way the
        ``_pressed`` mirror can while ``suppress_self_events`` is active.
        A low-level hook runs before that state updates: a suppressed down may
        still report up, while an accepted down stays set until its up passes.
        """
        if sys.platform != "win32":
            return False
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(int(vk)) & 0x8000)
        except Exception:
            return False

    def modifier_physically_down(self, key: Key) -> bool:
        """Return True only if *key* is genuinely held down right now, per the OS.

        Gating the modifier-guard re-press on the OS state rather than the stale
        ``_pressed`` mirror avoids re-pressing a key the user released *during* an
        injection (which would leave it stuck down: Windows dings, typing breaks,
        and screen readers read everything because a phantom Shift/Ctrl is held).

        Returns False for keys with no VK.
        """
        vk = getattr(getattr(key, "value", None), "vk", None)
        if vk is None:
            return False
        return self._physical_key_down(int(vk))

    def _reconcile_pressed_with_os(self) -> None:
        """Resync the tracked ``_pressed`` mirror to the real OS keyboard state.

        Called when the self-event suppression window closes. While suppression is
        active ``_on_press`` / ``_on_release`` early-return WITHOUT touching
        ``_pressed`` (so synthetic injection keystrokes can't re-enter the hotkey
        logic) -- but that also swallows genuine user key events that land inside
        the window + its grace tail. The mirror then drifts from physical reality
        in two ways:

        - Keys the user RELEASED stay stuck "down" -- phantom keys that keep a
          hotkey combo falsely satisfied. This is the live bug where, after a
          ``paste_last`` (ctrl+alt+v), the alt+v releases are swallowed, so a later
          bare Left Ctrl press (or any Ctrl-combo like Ctrl+Tab) completes the
          stuck ctrl+alt+v and re-fires paste_last "every other time."
        - Keys the user PRESSED during the window go missing.

        Reconciling against ``GetAsyncKeyState`` repairs both: drop any tracked key
        the OS reports up, and add any tracked modifier the OS reports down. Hotkey
        debounce flags (``entry["pressed"]`` etc.) are intentionally left untouched
        -- they self-heal on the next genuine ``_on_release`` once the phantom keys
        are gone.

        Windows only; a no-op elsewhere, where there is no physical-state oracle to
        reconcile against (and dropping on a False result would wrongly clear the
        mirror).
        """
        if sys.platform != "win32":
            return
        with self._lock:
            # Drop any tracked key the OS reports physically up (clears phantoms).
            for canon in list(self._pressed):
                vk = _canon_to_vk(canon)
                if vk is not None and not self._physical_key_down(vk):
                    self._pressed.discard(canon)
            # Add any tracked modifier the OS reports physically down (restores a
            # held modifier whose press was swallowed during suppression).
            for k in _ALL_TRACKED_MODIFIERS:
                vk = getattr(getattr(k, "value", None), "vk", None)
                if vk is not None and self._physical_key_down(int(vk)):
                    self._pressed.add(k)

    def wait(self) -> None:
        """Block the calling thread indefinitely (until the process exits)."""
        self._wait_event.wait()

    # ------------------------------------------------------------------ internal

    def _parse_shortcut(self, shortcut: str) -> list[frozenset]:
        """Parse ``'ctrl+shift'`` or ``'right shift'`` into a list of key-alternative sets."""
        parts = [p.strip().lower() for p in shortcut.split("+")]
        targets: list[frozenset] = []
        for part in parts:
            result = _parse_key(part)
            if result is not None:
                targets.append(result)
            else:
                self._logger.warning("hotkey parse warning reason=unknown_key shortcut=%r key=%r", shortcut, part)
        return targets


# Backward-compatible production name. New platform work should name the
# adapter explicitly while current callers keep their stable import/API.
KeyboardShortcutManager = WindowsShortcutAdapter


__all__ = ["KeyboardShortcutManager", "WindowsShortcutAdapter"]
