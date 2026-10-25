"""Native Mac shortcut adapter tests using injected event-tap sources only."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

import whisperstream.macos_shortcuts as macos_shortcuts_module
from whisperstream.macos_shortcuts import (
    MacKeyEvent,
    MacosShortcutAdapter,
    macos_shortcut_glyphs,
)
from whisperstream.permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)
from whisperstream.shortcut_contract import (
    CanonicalShortcut,
    ShortcutAction,
    ShortcutAdapter,
    ShortcutBinding,
    ShortcutCaptureStatus,
    ShortcutFailure,
    ShortcutGesture,
    ShortcutRegistrationStatus,
    ShortcutTransition,
)


def _permission_report(
    accessibility: PermissionState = PermissionState.GRANTED,
    input_monitoring: PermissionState = PermissionState.GRANTED,
) -> PermissionReport:
    return PermissionReport(
        platform="macos",
        permissions=(
            PermissionSnapshot(
                kind=PermissionKind.MICROPHONE,
                state=PermissionState.NOT_DETERMINED,
                query_supported=True,
                request_supported=True,
            ),
            PermissionSnapshot(
                kind=PermissionKind.ACCESSIBILITY,
                state=accessibility,
                query_supported=True,
                request_supported=True,
            ),
            PermissionSnapshot(
                kind=PermissionKind.INPUT_MONITORING,
                state=input_monitoring,
                query_supported=True,
                request_supported=True,
            ),
        ),
    )


class FakeListener:
    def __init__(
        self,
        callback: Callable[[MacKeyEvent], bool],
        *,
        start_error: BaseException | None = None,
        alive_after_start: bool = True,
        alive_after_stop: bool = False,
    ) -> None:
        self.callback = callback
        self.start_error = start_error
        self.alive_after_start = alive_after_start
        self.alive_after_stop = alive_after_stop
        self.started = False
        self.stopped = False
        self.joined = False

    def start(self) -> None:
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None
        self.joined = True

    def is_alive(self) -> bool:
        return (
            self.started
            and self.alive_after_start
            and (not self.stopped or self.alive_after_stop)
        )

    def emit_key(
        self,
        keycode: int,
        transition: ShortcutTransition,
        *,
        modifiers: frozenset[str] = frozenset(),
        synthetic: bool = False,
    ) -> bool:
        return self.callback(
            MacKeyEvent(
                identity=("key", keycode),
                transition=transition,
                modifiers=modifiers,
                synthetic=synthetic,
            )
        )

    def emit_mouse(self, button: int, transition: ShortcutTransition) -> bool:
        return self.callback(
            MacKeyEvent(identity=("mouse", button), transition=transition)
        )

    def emit_disabled(self, *, reenabled: bool) -> bool:
        return self.callback(
            MacKeyEvent(
                disabled_reason=ShortcutFailure.LISTENER_DISABLED,
                reenabled=reenabled,
            )
        )


class FakeListenerFactory:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        alive_after_start: bool = True,
        alive_after_stop: bool = False,
    ) -> None:
        self.start_error = start_error
        self.alive_after_start = alive_after_start
        self.alive_after_stop = alive_after_stop
        self.listeners: list[FakeListener] = []

    def __call__(self, callback: Callable[[MacKeyEvent], bool]) -> FakeListener:
        listener = FakeListener(
            callback,
            start_error=self.start_error,
            alive_after_start=self.alive_after_start,
            alive_after_stop=self.alive_after_stop,
        )
        self.listeners.append(listener)
        return listener


class ManualTimer:
    def __init__(self, _delay: float, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


def _manager(
    factory: FakeListenerFactory | None = None,
    *,
    report: PermissionReport | None = None,
    physical: Callable[[int], bool] | None = None,
    timer_factory: Callable[[float, Callable[[], None]], ManualTimer] | None = None,
) -> tuple[MacosShortcutAdapter, FakeListenerFactory]:
    selected = factory or FakeListenerFactory()
    return (
        MacosShortcutAdapter(
            logger=logging.getLogger("test.macos-shortcuts"),
            listener_factory=selected,
            permission_reporter=lambda: report or _permission_report(),
            physical_key_state=physical or (lambda _keycode: False),
            suppress_timer_factory=timer_factory,
        ),
        selected,
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for shortcut callback")


def _native_listener(
    callback: Callable[[MacKeyEvent], bool],
    event_state: dict[int, tuple[int, int, int]],
) -> tuple[macos_shortcuts_module._CoreGraphicsEventListener, MagicMock]:
    listener = macos_shortcuts_module._CoreGraphicsEventListener(callback)
    api = MagicMock()

    def integer_field(event: int, field: int) -> int:
        keycode, _flags, marker = event_state[event]
        if field == macos_shortcuts_module._CG_KEYBOARD_EVENT_KEYCODE:
            return keycode
        if field == macos_shortcuts_module._CG_EVENT_SOURCE_USER_DATA:
            return marker
        raise AssertionError(f"unexpected Core Graphics field: {field}")

    api.cg.CGEventGetIntegerValueField.side_effect = integer_field
    api.cg.CGEventGetFlags.side_effect = lambda event: event_state[event][1]
    listener._api = api
    return listener, api


def _binding(
    action_id: str,
    gesture: ShortcutGesture,
    configured: str,
    *keys: str,
) -> ShortcutBinding:
    return ShortcutBinding(
        action_id=action_id,
        gesture=gesture,
        configured=configured,
        shortcut=CanonicalShortcut(tuple(keys)) if keys else None,
    )


def test_selector_and_real_native_symbols_are_lazy_and_host_truthful() -> None:
    from whisperstream import shortcuts

    sys.modules.pop("whisperstream.keyboard_shortcuts", None)
    assert shortcuts._adapter_module("Darwin").MacosShortcutAdapter is MacosShortcutAdapter
    assert "whisperstream.keyboard_shortcuts" not in sys.modules
    assert MacosShortcutAdapter.native_symbols_available() is (sys.platform == "darwin")


def test_native_flags_changed_uses_event_state_for_modifier_capture() -> None:
    emitted: list[MacKeyEvent] = []
    listener, api = _native_listener(
        lambda event: emitted.append(event) or False,
        {
            101: (55, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            102: (17, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            103: (17, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            104: (55, 0, 0),
        },
    )
    # Reproduce the stale accumulated state seen at a head-insert event tap.
    api.key_down.return_value = False

    assert listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 101, 0
    ) == 101
    assert listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_KEY_DOWN, 102, 0
    ) == 102
    assert listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_KEY_UP, 103, 0
    ) == 103
    assert listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 104, 0
    ) == 104

    assert [event.transition for event in emitted] == [
        ShortcutTransition.PRESSED,
        ShortcutTransition.PRESSED,
        ShortcutTransition.RELEASED,
        ShortcutTransition.RELEASED,
    ]
    assert [event.identity for event in emitted] == [
        ("key", 55),
        ("key", 17),
        ("key", 17),
        ("key", 55),
    ]
    assert [event.modifiers for event in emitted] == [
        frozenset({"cmd"}),
        frozenset({"cmd"}),
        frozenset({"cmd"}),
        frozenset(),
    ]
    api.key_down.assert_not_called()


@pytest.mark.parametrize(
    ("keycode", "flag", "name"),
    [
        (55, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, "cmd"),
        (56, macos_shortcuts_module._CG_EVENT_FLAG_MASK_SHIFT, "shift"),
        (58, macos_shortcuts_module._CG_EVENT_FLAG_MASK_ALTERNATE, "option"),
        (59, macos_shortcuts_module._CG_EVENT_FLAG_MASK_CONTROL, "ctrl"),
    ],
)
def test_native_modifier_only_transitions_use_event_flags(
    keycode: int,
    flag: int,
    name: str,
) -> None:
    emitted: list[MacKeyEvent] = []
    listener, _api = _native_listener(
        lambda event: emitted.append(event) or False,
        {201: (keycode, flag, 0), 202: (keycode, 0, 0)},
    )

    listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 201, 0
    )
    listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 202, 0
    )

    assert [(event.transition, event.modifiers) for event in emitted] == [
        (ShortcutTransition.PRESSED, frozenset({name})),
        (ShortcutTransition.RELEASED, frozenset()),
    ]


def test_native_modifier_release_tracks_left_and_right_keys_separately() -> None:
    emitted: list[MacKeyEvent] = []
    command = macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND
    listener, _api = _native_listener(
        lambda event: emitted.append(event) or False,
        {
            211: (55, command, 0),
            212: (54, command, 0),
            213: (55, command, 0),
            214: (54, 0, 0),
        },
    )

    for event in (211, 212, 213, 214):
        listener._on_native_event(
            0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, event, 0
        )

    assert [(event.identity, event.transition) for event in emitted] == [
        (("key", 55), ShortcutTransition.PRESSED),
        (("key", 54), ShortcutTransition.PRESSED),
        (("key", 55), ShortcutTransition.RELEASED),
        (("key", 54), ShortcutTransition.RELEASED),
    ]


def test_native_modifier_flags_activate_registered_runtime_chord() -> None:
    manager, _factory = _manager()
    calls: list[str] = []
    manager.register_extra_hotkey("cmd+t", lambda: calls.append("fired"))
    listener, _api = _native_listener(
        manager._handle_event,
        {
            301: (55, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            302: (17, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            303: (17, macos_shortcuts_module._CG_EVENT_FLAG_MASK_COMMAND, 0),
            304: (55, 0, 0),
        },
    )

    listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 301, 0
    )
    listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_KEY_DOWN, 302, 0)
    listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_KEY_UP, 303, 0)
    listener._on_native_event(
        0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 304, 0
    )

    _wait_for(lambda: calls == ["fired"])
    manager.close()


def test_adapter_runtime_protocol_keeps_native_event_objects_private() -> None:
    manager, factory = _manager()
    try:
        assert isinstance(manager, ShortcutAdapter)
        outcome = manager.register(
            _binding(
                "push-to-talk",
                ShortcutGesture.HOLD,
                "right shift",
                "right shift",
            ),
            lambda _action_id, _action: None,
        )
        assert outcome.status is ShortcutRegistrationStatus.REGISTERED
        assert len(factory.listeners) == 1
        assert isinstance(factory.listeners[0].callback, Callable)
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("right shift", "⇧"),
        ("cmd+option+shift+k", "⌥⇧⌘K"),
        ("ctrl+cmd+space", "⌃⌘Space"),
        ("win+alt+left", "⌥⌘←"),
        ("caps lock", "⇪"),
    ],
)
def test_canonical_mac_mapping_and_glyphs(configured: str, expected: str) -> None:
    assert macos_shortcut_glyphs(configured) == expected


def test_contract_hold_toggle_taps_same_binding_mouse_and_repeat_semantics() -> None:
    manager, factory = _manager()
    actions: list[tuple[str, ShortcutAction]] = []
    tap_actions: list[str] = []

    assert manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda action_id, action: actions.append((action_id, action)),
    ).status is ShortcutRegistrationStatus.REGISTERED
    assert manager.register(
        _binding("toggle", ShortcutGesture.TOGGLE, "right shift", "right shift"),
        lambda action_id, action: actions.append((action_id, action)),
    ).status is ShortcutRegistrationStatus.REGISTERED
    manager.register_extra_hotkey("cmd+k", lambda: tap_actions.append("extra"))
    manager.register_extra_hotkey(
        "mouse3", lambda: tap_actions.append("mouse"), key="paste_last_original"
    )
    manager.register_profile_hotkeys(
        {"chat": "option+1"}, lambda profile: tap_actions.append(profile)
    )
    listener = factory.listeners[0]

    assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
    assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
    assert listener.emit_key(60, ShortcutTransition.RELEASED) is True
    listener.emit_key(60, ShortcutTransition.PRESSED)
    listener.emit_key(60, ShortcutTransition.RELEASED)

    listener.emit_key(55, ShortcutTransition.PRESSED)
    listener.emit_key(40, ShortcutTransition.PRESSED)
    listener.emit_key(40, ShortcutTransition.PRESSED)
    listener.emit_key(40, ShortcutTransition.RELEASED)
    listener.emit_key(55, ShortcutTransition.RELEASED)
    listener.emit_mouse(2, ShortcutTransition.PRESSED)
    listener.emit_mouse(2, ShortcutTransition.PRESSED)
    listener.emit_mouse(2, ShortcutTransition.RELEASED)
    listener.emit_key(58, ShortcutTransition.PRESSED)
    listener.emit_key(18, ShortcutTransition.PRESSED)
    listener.emit_key(18, ShortcutTransition.RELEASED)
    listener.emit_key(58, ShortcutTransition.RELEASED)

    _wait_for(lambda: len(actions) == 6 and len(tap_actions) == 3)
    assert actions == [
        ("hold", ShortcutAction.HOLD_BEGIN),
        ("toggle", ShortcutAction.TOGGLE),
        ("hold", ShortcutAction.HOLD_END),
        ("hold", ShortcutAction.HOLD_BEGIN),
        ("toggle", ShortcutAction.TOGGLE),
        ("hold", ShortcutAction.HOLD_END),
    ]
    assert tap_actions == ["extra", "mouse", "chat"]
    manager.close()


def test_legacy_extra_hotkey_registry_mutations_match_windows_semantics() -> None:
    manager, factory = _manager()
    old_calls: list[str] = []
    replacement_calls: list[str] = []
    second_calls: list[str] = []
    try:
        manager.register_extra_hotkey(
            "cmd+k", lambda: old_calls.append("old"), key="paste_last"
        )
        manager.register_extra_hotkey(
            "option+o",
            lambda: second_calls.append("second"),
            key="paste_last_original",
        )
        assert {"paste_last", "paste_last_original"}.issubset(manager._bindings)

        manager.register_extra_hotkey(
            "cmd+j",
            lambda: replacement_calls.append("replacement"),
            key="paste_last",
        )
        assert tuple(
            action_id
            for action_id in manager._bindings
            if action_id == "paste_last"
        ) == ("paste_last",)

        listener = factory.listeners[0]
        listener.emit_key(55, ShortcutTransition.PRESSED)
        listener.emit_key(40, ShortcutTransition.PRESSED)
        listener.emit_key(40, ShortcutTransition.RELEASED)
        listener.emit_key(55, ShortcutTransition.RELEASED)
        listener.emit_key(55, ShortcutTransition.PRESSED)
        listener.emit_key(38, ShortcutTransition.PRESSED)
        listener.emit_key(38, ShortcutTransition.RELEASED)
        listener.emit_key(55, ShortcutTransition.RELEASED)
        _wait_for(lambda: replacement_calls == ["replacement"])
        assert old_calls == []

        manager.register_extra_hotkey(
            "", lambda: None, key="paste_last_original"
        )
        assert "paste_last_original" not in manager._bindings

        manager.register_profile_hotkeys({"chat": "option+1"}, lambda _profile: None)
        manager.unregister_extra_hotkey()
        assert "paste_last" not in manager._bindings
        assert "profile:chat" in manager._bindings

        manager.unhook_all()
        assert manager._bindings == {}
        assert second_calls == []
    finally:
        manager.close()


def test_invalid_and_conflicting_contract_registration_is_typed_and_inert() -> None:
    manager, factory = _manager()
    try:
        invalid = ShortcutBinding(
            action_id="bad",
            gesture=ShortcutGesture.TAP,
            configured="cmd+k",
            shortcut=CanonicalShortcut(("cmd",)),
        )
        invalid_outcome = manager.register(invalid, lambda _action_id, _action: None)
        assert invalid_outcome.status is ShortcutRegistrationStatus.FAILED
        assert invalid_outcome.failure is ShortcutFailure.INVALID_BINDING
        assert factory.listeners == []

        first = manager.register(
            _binding("hold-a", ShortcutGesture.HOLD, "f7", "f7"),
            lambda _action_id, _action: None,
        )
        conflict = manager.register(
            _binding("hold-b", ShortcutGesture.HOLD, "f8", "f8"),
            lambda _action_id, _action: None,
        )
        assert first.status is ShortcutRegistrationStatus.REGISTERED
        assert conflict.status is ShortcutRegistrationStatus.FAILED
        assert conflict.failure is ShortcutFailure.CONFLICT
        assert set(manager._bindings) == {"hold-a"}
    finally:
        manager.close()


def test_legacy_toggle_alternates_start_stop_and_rearms_after_release() -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_toggle(
        "cmd+space",
        lambda: calls.append("start"),
        lambda: calls.append("stop"),
    )
    listener = factory.listeners[0]

    for _ in range(2):
        listener.emit_key(55, ShortcutTransition.PRESSED)
        listener.emit_key(49, ShortcutTransition.PRESSED)
        listener.emit_key(49, ShortcutTransition.RELEASED)
        listener.emit_key(55, ShortcutTransition.RELEASED)
    _wait_for(lambda: calls == ["start", "stop"])
    manager.close()


def test_legacy_toggle_reset_makes_next_press_start_again() -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_toggle(
        "cmd+space",
        lambda: calls.append("start"),
        lambda: calls.append("stop"),
    )
    listener = factory.listeners[0]

    listener.emit_key(55, ShortcutTransition.PRESSED)
    listener.emit_key(49, ShortcutTransition.PRESSED)
    listener.emit_key(49, ShortcutTransition.RELEASED)
    listener.emit_key(55, ShortcutTransition.RELEASED)
    _wait_for(lambda: calls == ["start"])
    manager.reset_toggle_state()
    listener.emit_key(55, ShortcutTransition.PRESSED)
    listener.emit_key(49, ShortcutTransition.PRESSED)
    listener.emit_key(49, ShortcutTransition.RELEASED)
    listener.emit_key(55, ShortcutTransition.RELEASED)

    _wait_for(lambda: calls == ["start", "start"])
    manager.close()


def test_capture_streams_canonical_names_commits_after_full_release_and_restores() -> None:
    manager, factory = _manager()
    manager.register_push_to_talk("right shift", lambda: None, lambda: None)
    live: list[str] = []
    captured: list[str] = []

    manager.capture_shortcut(live.append, captured.append)
    assert factory.listeners[0].stopped and factory.listeners[0].joined
    capture_listener = factory.listeners[1]
    capture_listener.emit_key(40, ShortcutTransition.PRESSED)
    capture_listener.emit_key(55, ShortcutTransition.PRESSED)
    capture_listener.emit_key(56, ShortcutTransition.PRESSED)
    capture_listener.emit_key(40, ShortcutTransition.RELEASED)
    capture_listener.emit_key(55, ShortcutTransition.RELEASED)
    assert captured == []
    capture_listener.emit_key(56, ShortcutTransition.RELEASED)

    _wait_for(lambda: captured == ["shift+cmd+k"] and len(factory.listeners) == 3)
    assert live[-1] == "shift+cmd+k"
    assert capture_listener.stopped and capture_listener.joined
    assert factory.listeners[2].is_alive()
    manager.close()


def test_capture_recovers_chord_modifiers_from_the_key_event_flags() -> None:
    manager, factory = _manager()
    captured: list[str] = []
    manager.capture_shortcut(lambda _keys: None, captured.append)
    listener = factory.listeners[0]

    # Reproduce a missed Command flagsChanged press. The T event still carries
    # the authoritative modifier snapshot, as real Core Graphics events do.
    listener.emit_key(
        17,
        ShortcutTransition.PRESSED,
        modifiers=frozenset({"cmd"}),
    )
    listener.emit_key(
        17,
        ShortcutTransition.RELEASED,
        modifiers=frozenset({"cmd"}),
    )
    assert captured == []
    listener.emit_key(55, ShortcutTransition.RELEASED)

    _wait_for(lambda: captured == ["cmd+t"])
    manager.close()


def test_capture_keeps_right_modifier_identity_without_generic_duplicate() -> None:
    manager, factory = _manager()
    captured: list[str] = []
    manager.capture_shortcut(lambda _keys: None, captured.append)
    listener = factory.listeners[0]

    listener.emit_key(
        54,
        ShortcutTransition.PRESSED,
        modifiers=frozenset({"cmd"}),
    )
    listener.emit_key(
        17,
        ShortcutTransition.PRESSED,
        modifiers=frozenset({"cmd"}),
    )
    listener.emit_key(
        17,
        ShortcutTransition.RELEASED,
        modifiers=frozenset({"cmd"}),
    )
    listener.emit_key(54, ShortcutTransition.RELEASED)

    _wait_for(lambda: captured == ["right cmd+t"])
    manager.close()


def test_contract_capture_conflict_cancel_and_listener_restore_failure_are_typed() -> None:
    manager, factory = _manager()
    outcomes = []
    manager.begin_capture(outcomes.append)
    manager.begin_capture(outcomes.append)
    _wait_for(lambda: len(outcomes) == 1)
    assert outcomes[0].status is ShortcutCaptureStatus.FAILED
    assert outcomes[0].failure is ShortcutFailure.CONFLICT
    manager.cancel_capture()
    _wait_for(lambda: len(outcomes) == 2)
    assert outcomes[1].status is ShortcutCaptureStatus.CANCELLED
    assert factory.listeners[0].stopped and factory.listeners[0].joined
    manager.close()


def test_capture_restore_failure_is_typed_and_leaves_no_listener() -> None:
    manager, factory = _manager()
    manager.register(
        _binding("tap", ShortcutGesture.TAP, "f7", "f7"),
        lambda _action_id, _action: None,
    )
    outcomes = []
    manager.begin_capture(outcomes.append)
    capture_listener = factory.listeners[1]
    factory.start_error = RuntimeError("synthetic restore failure")
    capture_listener.emit_key(40, ShortcutTransition.PRESSED)
    capture_listener.emit_key(40, ShortcutTransition.RELEASED)

    _wait_for(lambda: len(outcomes) == 1)
    assert outcomes[0].status is ShortcutCaptureStatus.FAILED
    assert outcomes[0].failure is ShortcutFailure.CLEANUP_FAILED
    assert all(listener.stopped and listener.joined for listener in factory.listeners)
    assert manager._listener is None
    manager.close()


@pytest.mark.parametrize(
    ("accessibility", "input_monitoring", "expected"),
    [
        (PermissionState.DENIED, PermissionState.GRANTED, ShortcutFailure.PERMISSION_DENIED),
        (PermissionState.RESTRICTED, PermissionState.GRANTED, ShortcutFailure.PERMISSION_DENIED),
        (PermissionState.NOT_DETERMINED, PermissionState.GRANTED, ShortcutFailure.PERMISSION_DENIED),
        (PermissionState.UNKNOWN, PermissionState.GRANTED, ShortcutFailure.NATIVE_ERROR),
        (PermissionState.UNSUPPORTED, PermissionState.GRANTED, ShortcutFailure.NATIVE_ERROR),
    ],
)
def test_permission_denied_and_unavailable_registration_are_truthful(
    accessibility: PermissionState,
    input_monitoring: PermissionState,
    expected: ShortcutFailure,
) -> None:
    manager, factory = _manager(
        report=_permission_report(accessibility, input_monitoring)
    )
    outcome = manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, _action: None,
    )

    assert outcome.status is ShortcutRegistrationStatus.FAILED
    assert outcome.failure is expected
    assert factory.listeners == []
    manager.close()


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (FakeListenerFactory(start_error=PermissionError("denied")), ShortcutFailure.PERMISSION_DENIED),
        (FakeListenerFactory(start_error=RuntimeError("native")), ShortcutFailure.NATIVE_ERROR),
        (FakeListenerFactory(alive_after_start=False), ShortcutFailure.LISTENER_DISABLED),
    ],
)
def test_partial_registration_failure_stops_exact_listener(
    factory: FakeListenerFactory,
    expected: ShortcutFailure,
) -> None:
    manager, _ = _manager(factory)
    outcome = manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, _action: None,
    )

    assert outcome.failure is expected
    listener = factory.listeners[0]
    assert listener.stopped and listener.joined
    manager.close()


def test_partial_registration_cleanup_failure_is_typed_and_not_hidden() -> None:
    factory = FakeListenerFactory(
        start_error=RuntimeError("synthetic start failure"),
        alive_after_stop=True,
    )
    manager, _ = _manager(factory)
    outcome = manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, _action: None,
    )

    assert outcome.status is ShortcutRegistrationStatus.FAILED
    assert outcome.failure is ShortcutFailure.CLEANUP_FAILED
    assert manager.last_cleanup_failure is ShortcutFailure.CLEANUP_FAILED
    assert manager._listener is factory.listeners[0]
    factory.listeners[0].alive_after_stop = False
    manager.close()


def test_disabled_event_tap_reenables_or_fails_typed_and_balances_hold() -> None:
    manager, factory = _manager()
    actions: list[ShortcutAction] = []
    manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, action: actions.append(action),
    )
    listener = factory.listeners[0]
    listener.emit_key(60, ShortcutTransition.PRESSED)
    listener.emit_disabled(reenabled=True)
    assert manager.listener_reenable_count == 1
    assert manager.last_listener_failure is None
    listener.emit_disabled(reenabled=False)

    _wait_for(lambda: listener.stopped and actions == [ShortcutAction.HOLD_BEGIN, ShortcutAction.HOLD_END])
    assert manager.last_listener_failure is ShortcutFailure.LISTENER_DISABLED
    assert listener.joined
    manager.close()


def test_self_event_fence_standalone_suppression_and_modifier_reconciliation() -> None:
    timers: list[ManualTimer] = []
    physical_down = {55}

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(
        physical=lambda code: code in physical_down,
        timer_factory=timer_factory,
    )
    calls: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: calls.append("begin"), lambda: calls.append("end")
    )
    listener = factory.listeners[0]

    with manager.suppress_self_events():
        assert listener.emit_key(60, ShortcutTransition.PRESSED) is False
    assert timers[0].started
    assert listener.emit_key(60, ShortcutTransition.PRESSED) is False
    timers[0].fire()
    assert manager.snapshot_held_modifiers() == ["cmd"]
    assert manager.modifier_physically_down("cmd") is True

    # An unrelated held modifier must be released before a new PTT gesture.
    assert listener.emit_key(55, ShortcutTransition.RELEASED) is False
    assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
    assert listener.emit_key(60, ShortcutTransition.RELEASED) is True
    assert listener.emit_key(60, ShortcutTransition.PRESSED, synthetic=True) is False
    assert listener.emit_key(60, ShortcutTransition.RELEASED, synthetic=True) is False
    _wait_for(lambda: calls == ["begin", "end"])
    manager.close()


@pytest.mark.parametrize("key", ["right shift", "f8", "`"])
def test_tap_timeout_passes_the_escaped_gesture_until_release_and_rearms(key: str) -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_push_to_talk(key, lambda: calls.append("begin"), lambda: calls.append("end"))
    listener = factory.listeners[0]
    code = next(iter(macos_shortcuts_module._NAME_TO_CODES[key]))
    try:
        assert listener.emit_key(code, ShortcutTransition.PRESSED) is True
        # The tap exceeded its deadline; macOS may have delivered the down.
        listener.emit_disabled(reenabled=True)
        assert listener.emit_key(code, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(code, ShortcutTransition.RELEASED, synthetic=True) is False
        assert listener.emit_key(code, ShortcutTransition.RELEASED) is False
        _wait_for(lambda: calls == ["begin", "end"])
        assert listener.emit_key(code, ShortcutTransition.PRESSED) is True
        assert listener.emit_key(code, ShortcutTransition.RELEASED) is True
        _wait_for(lambda: calls == ["begin", "end", "begin", "end"])
    finally:
        manager.close()


def test_tap_timeout_does_not_reclaim_an_escaped_early_key_repeat() -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_push_to_talk("right shift", lambda: None, lambda: None)
    manager.register_early_key_handler("enter", lambda: True, lambda: calls.append("enter"))
    listener = factory.listeners[0]
    try:
        assert listener.emit_key(36, ShortcutTransition.PRESSED) is True
        listener.emit_disabled(reenabled=True)
        assert listener.emit_key(36, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(36, ShortcutTransition.RELEASED) is False
        _wait_for(lambda: calls == ["enter"])
        assert listener.emit_key(36, ShortcutTransition.PRESSED) is True
        assert listener.emit_key(36, ShortcutTransition.RELEASED) is True
        _wait_for(lambda: calls == ["enter", "enter"])
    finally:
        manager.close()


def test_native_tap_timeout_returns_the_balancing_modifier_event() -> None:
    manager, _factory = _manager()
    calls: list[str] = []
    manager.register_push_to_talk("right shift", lambda: calls.append("begin"), lambda: calls.append("end"))
    listener, api = _native_listener(
        manager._handle_event,
        {
            401: (60, macos_shortcuts_module._CG_EVENT_FLAG_MASK_SHIFT, 0),
            402: (60, 0, 0),
            403: (60, macos_shortcuts_module._CG_EVENT_FLAG_MASK_SHIFT, 0),
            404: (60, 0, 0),
        },
    )
    listener._tap = 7
    api.cg.CGEventTapIsEnabled.return_value = True
    try:
        assert listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 401, 0) is None
        listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_TAP_DISABLED_BY_TIMEOUT, 0, 0)
        assert listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 402, 0) == 402
        _wait_for(lambda: calls == ["begin", "end"])
        assert listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 403, 0) is None
        assert listener._on_native_event(0, macos_shortcuts_module._CG_EVENT_FLAGS_CHANGED, 404, 0) is None
        _wait_for(lambda: calls == ["begin", "end", "begin", "end"])
    finally:
        manager.close()


@pytest.mark.parametrize("letter", list("abcdefghijklmnopqrstuvwxyz"))
def test_standalone_letter_recording_binding_does_not_consume_typing(letter) -> None:
    manager, factory = _manager()
    calls = []
    manager.register_push_to_talk(letter, lambda: calls.append("begin"), lambda: calls.append("end"))
    listener = factory.listeners[0]
    code = next(iter(macos_shortcuts_module._NAME_TO_CODES[letter]))
    try:
        assert listener.emit_key(code, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(code, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(code, ShortcutTransition.RELEASED) is False
        _wait_for(lambda: calls == ["begin", "end"])
    finally:
        manager.close()


@pytest.mark.parametrize("gesture", ["push_to_talk", "toggle"])
def test_extra_modifier_rejects_activation_without_swallowing_chord(gesture) -> None:
    manager, factory = _manager()
    calls = []
    register = getattr(manager, f"register_{gesture}")
    register("right shift", lambda: calls.append("begin"), lambda: calls.append("end"))
    listener = factory.listeners[0]
    try:
        assert listener.emit_key(55, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(60, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(60, ShortcutTransition.RELEASED) is False
        assert listener.emit_key(55, ShortcutTransition.RELEASED) is False
        assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
        assert listener.emit_key(60, ShortcutTransition.RELEASED) is True
        if gesture == "toggle":
            assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
            assert listener.emit_key(60, ShortcutTransition.RELEASED) is True
        _wait_for(lambda: calls == ["begin", "end"])
    finally:
        manager.close()


def test_extra_modifier_after_activation_keeps_exact_hold_until_bound_key_release() -> None:
    manager, factory = _manager()
    calls = []
    manager.register_push_to_talk("right shift", lambda: calls.append("begin"), lambda: calls.append("end"))
    listener = factory.listeners[0]
    try:
        assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
        assert listener.emit_key(55, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(40, ShortcutTransition.PRESSED) is False
        assert listener.emit_key(40, ShortcutTransition.RELEASED) is False
        assert listener.emit_key(60, ShortcutTransition.PRESSED) is True
        # A repeated bound key must still belong to the same hold.
        assert listener.emit_key(60, ShortcutTransition.RELEASED) is True
        assert listener.emit_key(55, ShortcutTransition.RELEASED) is False
        _wait_for(lambda: calls == ["begin", "end"])
    finally:
        manager.close()


def test_combo_is_not_suppressed_and_early_enter_is_selectively_consumed() -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_push_to_talk(
        "right shift+k", lambda: calls.append("begin"), lambda: calls.append("end")
    )
    manager.register_early_key_handler("enter", lambda: True, lambda: calls.append("enter"))
    listener = factory.listeners[0]

    assert listener.emit_key(60, ShortcutTransition.PRESSED) is False
    assert listener.emit_key(40, ShortcutTransition.PRESSED) is False
    assert listener.emit_key(40, ShortcutTransition.RELEASED) is False
    assert listener.emit_key(60, ShortcutTransition.RELEASED) is False
    assert listener.emit_key(36, ShortcutTransition.PRESSED) is True
    assert listener.emit_key(36, ShortcutTransition.PRESSED) is True
    assert listener.emit_key(36, ShortcutTransition.RELEASED) is True
    _wait_for(lambda: calls == ["begin", "end", "enter"])
    manager.close()


def test_modified_return_is_never_a_routed_reply_key() -> None:
    manager, factory = _manager()
    calls: list[str] = []
    manager.register_push_to_talk("right shift+k", lambda: None, lambda: None)
    manager.register_early_key_handler("enter", lambda: True, lambda: calls.append("enter"))
    listener = factory.listeners[0]
    try:
        # Shift+Return is a newline in the focused app or the HUD's text box.
        assert listener.emit_key(36, ShortcutTransition.PRESSED, modifiers=frozenset({"shift"})) is False
        assert listener.emit_key(36, ShortcutTransition.RELEASED, modifiers=frozenset({"shift"})) is False
        assert listener.emit_key(36, ShortcutTransition.PRESSED) is True
        assert listener.emit_key(36, ShortcutTransition.RELEASED) is True
        _wait_for(lambda: calls == ["enter"])
    finally:
        manager.close()


def test_close_and_crash_cleanup_balance_hold_cancel_capture_and_join_every_owner() -> None:
    manager, factory = _manager()
    actions: list[ShortcutAction] = []
    manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, action: actions.append(action),
    )
    factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
    _wait_for(lambda: actions == [ShortcutAction.HOLD_BEGIN])

    try:
        raise RuntimeError("synthetic backend failure")
    except RuntimeError:
        manager.close()

    assert actions == [ShortcutAction.HOLD_BEGIN, ShortcutAction.HOLD_END]
    assert all(listener.stopped and listener.joined for listener in factory.listeners)
    assert manager._pressed == set()
    assert not manager._worker.is_alive()
    manager.close()


def test_backend_crash_finally_closes_injected_macos_event_source() -> None:
    from whisperstream.main import WhisperstreamApp

    manager, factory = _manager()
    assert manager.register(
        _binding("push-to-talk", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, _action: None,
    ).status is ShortcutRegistrationStatus.REGISTERED
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._recording = False
    app.audio_capture = MagicMock()
    app.keyboard = manager
    app.stt_engine = MagicMock()

    with pytest.raises(RuntimeError, match="synthetic backend crash"):
        try:
            raise RuntimeError("synthetic backend crash")
        finally:
            app.cleanup()

    assert all(listener.stopped and listener.joined for listener in factory.listeners)
    assert manager._listener is None
    assert manager._pressed == set()
    assert not manager._worker.is_alive()


def test_shortcut_adapter_refuses_to_silently_own_delivery() -> None:
    manager, _factory = _manager()
    with pytest.raises(RuntimeError, match="does not own text delivery"):
        manager.send("cmd+v")
    with pytest.raises(RuntimeError, match="does not own text delivery"):
        manager.type_text("synthetic")
    manager.close()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (PermissionError("synthetic denial"), "permission_denied"),
        (ValueError("synthetic binding"), "invalid_binding"),
        (RuntimeError("synthetic native failure"), "native_error"),
    ],
)
def test_main_logs_macos_registration_failures_truthfully(
    failure: BaseException,
    reason: str,
) -> None:
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app.config = MagicMock(
        push_to_talk_shortcut="right shift",
        toggle_shortcut="",
    )
    app.keyboard = MagicMock()
    app.keyboard.register_push_to_talk.side_effect = failure

    with patch("whisperstream.main.sys.platform", "darwin"):
        with pytest.raises(type(failure), match="synthetic"):
            app._register_hotkey()

    app.logger.error.assert_called_once_with(
        "hotkey registration failed reason=%s hint=review_privacy_settings",
        reason,
    )


@pytest.mark.parametrize("input_monitoring", list(PermissionState))
def test_active_tap_only_requires_accessibility(input_monitoring) -> None:
    manager, factory = _manager(report=_permission_report(PermissionState.GRANTED, input_monitoring))
    actions = []
    outcome = manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda action_id, action: actions.append((action_id, action)),
    )
    assert outcome.status is ShortcutRegistrationStatus.REGISTERED
    factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
    factory.listeners[0].emit_key(60, ShortcutTransition.RELEASED)
    _wait_for(lambda: len(actions) == 2)
    manager.close()


def test_ensure_ready_without_bindings_reuses_and_recovers_listener() -> None:
    manager, factory = _manager(report=_permission_report(PermissionState.GRANTED, PermissionState.UNKNOWN))
    assert not manager.listener_ready
    assert manager.ensure_ready()
    assert manager.listener_ready
    assert manager.ensure_ready()
    assert len(factory.listeners) == 1
    factory.listeners[0].alive_after_start = False
    assert not manager.listener_ready
    assert manager.ensure_ready()
    assert len(factory.listeners) == 2
    assert factory.listeners[0].stopped
    manager.close()
    assert not manager.listener_ready
    with pytest.raises(RuntimeError):
        manager.ensure_ready()


def test_ensure_ready_does_not_replace_capture_listener() -> None:
    manager, factory = _manager()
    manager.begin_capture(lambda outcome: None)
    assert manager.capture_active
    assert not manager.ensure_ready()
    assert len(factory.listeners) == 1
    manager.cancel_capture()
    assert not manager.capture_active
    manager.close()


def test_ensure_ready_rechecks_revoked_access() -> None:
    manager, factory = _manager()
    assert manager.ensure_ready()
    manager._permission_reporter = lambda: _permission_report(PermissionState.DENIED)
    with pytest.raises(PermissionError):
        manager.ensure_ready()
    assert not manager.listener_ready
    assert factory.listeners[0].stopped
    manager.close()


def test_ensure_ready_defers_held_gesture_without_duplicate_action() -> None:
    manager, factory = _manager()
    actions = []
    manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda action_id, action: actions.append((action_id, action)),
    )
    factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
    assert not manager.ensure_ready()
    assert len(factory.listeners) == 1
    factory.listeners[0].emit_key(60, ShortcutTransition.RELEASED)
    assert manager.ensure_ready()
    _wait_for(lambda: len(actions) == 2)
    manager.close()


def test_dead_listener_recovers_missed_release_and_balances_hold_once() -> None:
    held = {60}
    manager, factory = _manager(physical=lambda code: code in held)
    actions: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: actions.append("begin"), lambda: actions.append("end")
    )
    try:
        stale = factory.listeners[0]
        stale.emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: actions == ["begin"])
        stale.alive_after_start = False
        held.clear()

        manager.reconcile_lost_releases()
        _wait_for(lambda: actions == ["begin", "end"])
        assert len(factory.listeners) == 1  # Release settlement does not create a tap.
        assert manager.ensure_ready()
        assert len(factory.listeners) == 2
        assert manager._pressed == set()
        assert manager.ensure_ready()
        current = factory.listeners[-1]
        held.add(60)
        current.emit_key(60, ShortcutTransition.PRESSED)
        held.clear()
        current.emit_key(60, ShortcutTransition.RELEASED)
        _wait_for(lambda: actions == ["begin", "end", "begin", "end"])
    finally:
        manager.close()


def test_dead_listener_defers_until_physical_hold_is_released() -> None:
    held = {60}
    manager, factory = _manager(physical=lambda code: code in held)
    actions: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: actions.append("begin"), lambda: actions.append("end")
    )
    try:
        stale = factory.listeners[0]
        stale.emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: actions == ["begin"])
        stale.alive_after_start = False
        assert not manager.ensure_ready()
        assert len(factory.listeners) == 1
        assert not stale.stopped
        assert actions == ["begin"]
        held.clear()
        assert manager.ensure_ready()
        _wait_for(lambda: actions == ["begin", "end"])
    finally:
        manager.close()


def test_healthy_listener_does_not_physically_reconcile_a_live_gesture() -> None:
    physical = MagicMock(return_value=False)
    manager, factory = _manager(physical=physical)
    actions: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: actions.append("begin"), lambda: actions.append("end")
    )
    try:
        factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: actions == ["begin"])
        manager.reconcile_lost_releases()
        assert not manager.ensure_ready()
        physical.assert_not_called()
        assert actions == ["begin"]
    finally:
        manager.close()


def test_dead_listener_preserves_hold_when_physical_state_is_unavailable() -> None:
    physical = MagicMock(side_effect=RuntimeError("synthetic query unavailable"))
    manager, factory = _manager(physical=physical)
    actions: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: actions.append("begin"), lambda: actions.append("end")
    )
    try:
        factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: actions == ["begin"])
        factory.listeners[0].alive_after_start = False
        assert not manager.ensure_ready()
        assert actions == ["begin"]
        assert len(factory.listeners) == 1
        physical.side_effect = None
        physical.return_value = False
        assert manager.ensure_ready()
        _wait_for(lambda: actions == ["begin", "end"])
    finally:
        manager.close()


def test_dead_listener_release_rearms_toggle_without_toggling_it() -> None:
    manager, factory = _manager()
    actions: list[str] = []
    manager.register_toggle(
        "right shift", lambda: actions.append("start"), lambda: actions.append("stop")
    )
    try:
        factory.listeners[0].emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: actions == ["start"])
        factory.listeners[0].alive_after_start = False
        assert manager.ensure_ready()
        assert actions == ["start"]
        factory.listeners[-1].emit_key(60, ShortcutTransition.PRESSED)
        factory.listeners[-1].emit_key(60, ShortcutTransition.RELEASED)
        _wait_for(lambda: actions == ["start", "stop"])
    finally:
        manager.close()


def test_queued_disabled_cleanup_cannot_stop_a_recovered_listener() -> None:
    manager, factory = _manager()
    entered = threading.Event()
    release = threading.Event()
    drained = threading.Event()

    def hold_dispatch() -> None:
        entered.set()
        assert release.wait(2.0)

    try:
        assert manager.ensure_ready()
        manager._queue_callback(hold_dispatch)
        assert entered.wait(1.0)
        factory.listeners[0].emit_disabled(reenabled=False)
        assert manager.ensure_ready()
        replacement = factory.listeners[-1]
        manager._queue_callback(drained.set)
        release.set()
        assert drained.wait(1.0)
        assert manager.listener_ready
        assert manager._listener is replacement
        assert not replacement.stopped
    finally:
        release.set()
        manager.close()


def test_retired_listener_cannot_dispatch_into_recovered_bindings() -> None:
    manager, factory = _manager()
    actions: list[str] = []
    manager.register_push_to_talk(
        "right shift", lambda: actions.append("begin"), lambda: actions.append("end")
    )
    try:
        stale = factory.listeners[0]
        stale.alive_after_start = False
        assert manager.ensure_ready()
        assert not stale.emit_key(60, ShortcutTransition.PRESSED)
        assert not stale.emit_key(60, ShortcutTransition.RELEASED)
        stale.emit_disabled(reenabled=False)
        assert manager.listener_ready
        current = factory.listeners[-1]
        current.emit_key(60, ShortcutTransition.PRESSED)
        current.emit_key(60, ShortcutTransition.RELEASED)
        _wait_for(lambda: actions == ["begin", "end"])
    finally:
        manager.close()


def test_failed_teardown_listener_cannot_dispatch_early_handlers() -> None:
    factory = FakeListenerFactory(alive_after_stop=True)
    manager, _ = _manager(factory)
    actions: list[str] = []
    manager.register_early_key_handler("enter", lambda: True, lambda: actions.append("enter"))
    try:
        assert manager.ensure_ready()
        survivor = factory.listeners[0]
        manager.unhook_all()
        assert manager._listener is survivor
        assert survivor.is_alive()
        assert not survivor.emit_key(36, ShortcutTransition.PRESSED)
        assert not survivor.emit_key(36, ShortcutTransition.RELEASED)
        assert actions == []
    finally:
        for listener in factory.listeners:
            listener.alive_after_stop = False
        manager.close()


def test_ensure_ready_failure_can_be_retried_and_is_observable() -> None:
    factory = FakeListenerFactory(start_error=RuntimeError("synthetic failure"))
    manager, _ = _manager(factory)
    with pytest.raises(RuntimeError):
        manager.ensure_ready()
    assert manager.last_listener_failure is ShortcutFailure.NATIVE_ERROR
    assert not manager.listener_ready
    factory.start_error = None
    assert manager.ensure_ready()
    assert manager.last_listener_failure is None
    assert len(factory.listeners) == 2
    manager.close()


def test_recovery_retains_failed_teardown_before_creating_another_listener() -> None:
    from whisperstream.shortcut_recovery import ShortcutRecovery

    factory = FakeListenerFactory(alive_after_stop=True)
    manager, _ = _manager(factory)
    recovery = ShortcutRecovery(
        manager,
        lambda: manager.register_push_to_talk("right shift", lambda: None, lambda: None),
        lambda: False,
    )
    try:
        assert recovery.reconcile() == {"state": "failed", "failure": "native_error"}
        assert len(factory.listeners) == 1
        survivor = factory.listeners[0]
        assert manager._listener is survivor
        assert survivor.is_alive()
        assert not manager.listener_ready
        assert manager.last_cleanup_failure is ShortcutFailure.CLEANUP_FAILED

        assert recovery.reconcile()["state"] == "failed"
        assert len(factory.listeners) == 1
        assert manager._listener is survivor

        survivor.alive_after_stop = False
        factory.alive_after_stop = False
        assert recovery.reconcile()["state"] == "ready"
        assert not survivor.is_alive()
        assert sum(listener.is_alive() for listener in factory.listeners) == 1
        assert manager.last_cleanup_failure is None
    finally:
        for listener in factory.listeners:
            listener.alive_after_stop = False
        recovery.close()


def test_failed_start_retains_partial_listener_until_cleanup_succeeds() -> None:
    factory = FakeListenerFactory(
        start_error=RuntimeError("synthetic start failure"), alive_after_stop=True
    )
    manager, _ = _manager(factory)
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            manager.ensure_ready()
        survivor = factory.listeners[0]
        assert manager._listener is survivor
        assert not manager.listener_ready
        factory.start_error = None
        with pytest.raises(RuntimeError, match="cleanup failed"):
            manager.ensure_ready()
        assert len(factory.listeners) == 1

        survivor.alive_after_stop = False
        factory.alive_after_stop = False
        assert manager.ensure_ready()
        assert len(factory.listeners) == 2
        assert not survivor.is_alive()
    finally:
        for listener in factory.listeners:
            listener.alive_after_stop = False
        manager.close()


def test_close_can_retry_exact_listener_after_failed_teardown() -> None:
    factory = FakeListenerFactory(alive_after_stop=True)
    manager, _ = _manager(factory)
    assert manager.ensure_ready()
    survivor = factory.listeners[0]
    manager.close()
    try:
        assert manager._listener is survivor
        assert manager.last_cleanup_failure is ShortcutFailure.CLEANUP_FAILED
    finally:
        survivor.alive_after_stop = False
        manager.close()
    assert manager._listener is None
    assert not survivor.is_alive()
    assert manager.last_cleanup_failure is None


def test_empty_stop_preserves_an_unresolved_cleanup_failure() -> None:
    manager, _ = _manager()
    try:
        manager._last_cleanup_failure = ShortcutFailure.CLEANUP_FAILED
        assert manager._stop_listener() is ShortcutFailure.CLEANUP_FAILED
        assert manager.last_cleanup_failure is ShortcutFailure.CLEANUP_FAILED
        with pytest.raises(RuntimeError, match="cleanup failed"):
            manager.ensure_ready()
    finally:
        manager.close()


def test_legacy_capture_delivers_value_when_listener_restore_fails() -> None:
    manager, factory = _manager()
    manager.register(
        _binding("hold", ShortcutGesture.HOLD, "right shift", "right shift"),
        lambda _action_id, _action: None,
    )
    captured = []
    manager.capture_shortcut(lambda keys: None, captured.append)
    factory.start_error = RuntimeError("synthetic restore failure")
    factory.listeners[-1].emit_key(40, ShortcutTransition.PRESSED)
    factory.listeners[-1].emit_key(40, ShortcutTransition.RELEASED)
    _wait_for(lambda: len(captured) == 1)
    assert captured == ["k"]
    assert not manager.capture_active
    assert not manager.listener_ready
    assert manager.last_listener_failure is ShortcutFailure.NATIVE_ERROR
    manager.close()


def test_closed_adapter_rejects_capture_without_listener_or_callback() -> None:
    manager, factory = _manager()
    manager.close()
    with pytest.raises(RuntimeError):
        manager.capture_shortcut(lambda keys: None, lambda keys: None)
    assert not manager.capture_active
    assert factory.listeners == []
    assert manager.last_listener_failure is ShortcutFailure.LISTENER_DISABLED


def test_suppressed_physical_tap_release_rearms_paste_last() -> None:
    timers: list[ManualTimer] = []
    physical_down = {79}

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(
        physical=lambda code: code in physical_down, timer_factory=timer_factory
    )
    calls: list[str] = []
    try:
        manager.register_extra_hotkey("f18", lambda: calls.append("paste"))
        listener = factory.listeners[0]
        listener.emit_key(79, ShortcutTransition.PRESSED)
        _wait_for(lambda: calls == ["paste"])
        with manager.suppress_self_events():
            physical_down.clear()
            listener.emit_key(79, ShortcutTransition.RELEASED)
        timers[0].fire()
        physical_down.add(79)
        listener.emit_key(79, ShortcutTransition.PRESSED)
        listener.emit_key(79, ShortcutTransition.RELEASED)
        _wait_for(lambda: calls == ["paste", "paste"])
    finally:
        manager.close()


@pytest.mark.parametrize("held", [False, True])
def test_suppressed_hold_release_balances_only_when_physically_released(held: bool) -> None:
    timers: list[ManualTimer] = []
    physical_down = {98}

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(
        physical=lambda code: code in physical_down, timer_factory=timer_factory
    )
    calls: list[str] = []
    try:
        manager.register_push_to_talk("f7", lambda: calls.append("begin"), lambda: calls.append("end"))
        listener = factory.listeners[0]
        listener.emit_key(98, ShortcutTransition.PRESSED)
        _wait_for(lambda: calls == ["begin"])
        with manager.suppress_self_events():
            if not held:
                physical_down.clear()
                listener.emit_key(98, ShortcutTransition.RELEASED)
        timers[0].fire()
        if held:
            time.sleep(.02)
            assert calls == ["begin"]
        else:
            _wait_for(lambda: calls == ["begin", "end"])
        physical_down.clear()
        listener.emit_key(98, ShortcutTransition.RELEASED)
        _wait_for(lambda: calls == ["begin", "end"])
        assert calls.count("end") == 1
    finally:
        manager.close()


def test_suppression_does_not_release_key_when_physical_state_is_unknown() -> None:
    timers: list[ManualTimer] = []

    def physical(_code: int) -> bool:
        raise RuntimeError("physical state unavailable")

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(physical=physical, timer_factory=timer_factory)
    calls: list[str] = []
    try:
        manager.register_push_to_talk("f7", lambda: calls.append("begin"), lambda: calls.append("end"))
        listener = factory.listeners[0]
        listener.emit_key(98, ShortcutTransition.PRESSED)
        _wait_for(lambda: calls == ["begin"])
        with manager.suppress_self_events():
            listener.emit_key(98, ShortcutTransition.RELEASED)
        timers[0].fire()
        time.sleep(.02)
        assert calls == ["begin"]
        listener.emit_key(98, ShortcutTransition.RELEASED)
        _wait_for(lambda: calls == ["begin", "end"])
    finally:
        manager.close()


def test_nested_suppression_reconciles_modifier_release_only_at_final_expiry() -> None:
    timers: list[ManualTimer] = []
    physical_down = {60}
    manager = None

    def physical(code: int) -> bool:
        assert manager is not None
        assert manager._suppress_lock.locked()
        return code in physical_down

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(physical=physical, timer_factory=timer_factory)
    calls: list[str] = []
    try:
        manager.register_push_to_talk("right shift", lambda: calls.append("begin"), lambda: calls.append("end"))
        listener = factory.listeners[0]
        listener.emit_key(60, ShortcutTransition.PRESSED)
        _wait_for(lambda: calls == ["begin"])
        with manager.suppress_self_events():
            with manager.suppress_self_events():
                physical_down.clear()
                listener.emit_key(60, ShortcutTransition.RELEASED)
        timers[0].fire()
        time.sleep(.02)
        assert calls == ["begin"]
        timers[1].fire()
        _wait_for(lambda: calls == ["begin", "end"])
        listener.emit_key(60, ShortcutTransition.RELEASED)
        assert calls.count("end") == 1
    finally:
        manager.close()


def test_suppression_expiry_and_native_event_share_lock_order() -> None:
    timers: list[ManualTimer] = []

    def timer_factory(delay: float, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    manager, factory = _manager(timer_factory=timer_factory)
    manager.register_extra_hotkey("f18", lambda: None)
    listener = factory.listeners[0]
    with manager.suppress_self_events():
        pass
    event_owns_lock = threading.Event()
    expiry_started = threading.Event()
    event_done = threading.Event()
    failures: list[Exception] = []

    def native_event() -> None:
        try:
            with manager._lock:
                event_owns_lock.set()
                assert expiry_started.wait(1)
                # Let expiry try to acquire its first lock while the listener
                # retains the lock held by _handle_listener_event.
                time.sleep(.03)
                listener.emit_key(79, ShortcutTransition.RELEASED)
                event_done.set()
        except Exception as exc:
            failures.append(exc)

    def expiry() -> None:
        assert event_owns_lock.wait(1)
        expiry_started.set()
        timers[0].fire()

    event_thread = threading.Thread(target=native_event, daemon=True)
    expiry_thread = threading.Thread(target=expiry, daemon=True)
    try:
        event_thread.start()
        expiry_thread.start()
        event_thread.join(timeout=1)
        expiry_thread.join(timeout=1)
        assert event_done.is_set(), "native event deadlocked with suppression expiry"
        assert not event_thread.is_alive() and not expiry_thread.is_alive()
        assert not failures
    finally:
        if not event_thread.is_alive() and not expiry_thread.is_alive():
            manager.close()
