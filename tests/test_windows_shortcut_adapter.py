"""Native Windows conformance for the schema-v1 shortcut adapter.

All registration uses isolated synthetic listener objects. The suite exercises
the exact Windows adapter without installing a real global hook or observing a
physical key, so it cannot mutate the owner's keyboard or mouse state.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

if sys.platform != "win32":
    pytest.skip(
        "native Windows shortcut adapter conformance",
        allow_module_level=True,
    )

from pynput.keyboard import Key, KeyCode

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.keyboard_shortcuts import WindowsShortcutAdapter
from whisperstream.shortcut_contract import (
    CanonicalShortcut,
    ShortcutAction,
    ShortcutAdapter,
    ShortcutBinding,
    ShortcutCaptureStatus,
    ShortcutFailure,
    ShortcutGesture,
    ShortcutRegistrationStatus,
)

class _SyntheticController:
    def press(self, _key: object) -> None:
        return None

    def release(self, _key: object) -> None:
        return None

    def type(self, _text: str) -> None:
        return None


@dataclass
class _SyntheticListener:
    kwargs: dict[str, object]
    fail_start: BaseException | None = None
    disabled: bool = False
    started: bool = False
    stopped: bool = False
    joined: bool = False
    suppressed: int = 0

    def start(self) -> None:
        self.started = True
        if self.fail_start is not None:
            raise self.fail_start

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None and timeout > 0
        self.joined = True

    def is_alive(self) -> bool:
        return self.started and not self.stopped and not self.disabled

    def suppress_event(self) -> None:
        self.suppressed += 1


@dataclass
class _ListenerFactory:
    failures: list[BaseException | None] = field(default_factory=list)
    disabled: list[bool] = field(default_factory=list)
    created: list[_SyntheticListener] = field(default_factory=list)

    def __call__(self, **kwargs: object) -> _SyntheticListener:
        index = len(self.created)
        listener = _SyntheticListener(
            kwargs=kwargs,
            fail_start=self.failures[index] if index < len(self.failures) else None,
            disabled=self.disabled[index] if index < len(self.disabled) else False,
        )
        self.created.append(listener)
        return listener


def _binding(
    action_id: str, gesture: ShortcutGesture, configured: str
) -> ShortcutBinding:
    aliases = {"return": "enter", "escape": "esc"}
    keys = tuple(
        aliases.get(part.strip().lower(), part.strip().lower())
        for part in configured.split("+")
        if part.strip()
    )
    return ShortcutBinding(
        action_id=action_id,
        gesture=gesture,
        configured=configured,
        shortcut=CanonicalShortcut(keys) if keys else None,
    )


def _make_adapter(
    *,
    keyboard_factory: _ListenerFactory | None = None,
    mouse_factory: _ListenerFactory | None = None,
) -> tuple[WindowsShortcutAdapter, _ListenerFactory, _ListenerFactory]:
    keyboard = keyboard_factory or _ListenerFactory()
    mouse = mouse_factory or _ListenerFactory()
    adapter = WindowsShortcutAdapter(
        logger=logging.getLogger("test.windows-shortcut-adapter"),
        keyboard_listener_factory=keyboard,
        mouse_listener_factory=mouse,
        controller_factory=_SyntheticController,
    )
    return adapter, keyboard, mouse


def _flush(adapter: WindowsShortcutAdapter) -> None:
    done = threading.Event()
    adapter._queue.put(done.set)
    assert done.wait(2.0)


def _assert_all_stopped(factory: _ListenerFactory) -> None:
    assert factory.created
    assert all(item.stopped and item.joined for item in factory.created)


def test_adapter_runtime_protocol_and_native_listener_isolation() -> None:
    adapter, keyboard, mouse = _make_adapter()
    try:
        assert isinstance(adapter, ShortcutAdapter)
        outcome = adapter.register(
            _binding("push-to-talk", ShortcutGesture.HOLD, "right shift"),
            MagicMock(),
        )
        assert outcome.status is ShortcutRegistrationStatus.REGISTERED
        assert len(keyboard.created) == len(mouse.created) == 1
        assert "win32_event_filter" in keyboard.created[0].kwargs
        assert "on_press" in keyboard.created[0].kwargs
        assert "on_release" in keyboard.created[0].kwargs
        assert set(mouse.created[0].kwargs) == {"on_click"}
    finally:
        adapter.close()
    _assert_all_stopped(keyboard)
    _assert_all_stopped(mouse)


def test_contract_hold_toggle_and_tap_reducers_preserve_co_resident_semantics() -> None:
    adapter, _keyboard, _mouse = _make_adapter()
    actions: list[tuple[str, ShortcutAction]] = []
    try:
        for binding in (
            _binding("push-to-talk", ShortcutGesture.HOLD, "right shift"),
            _binding("toggle-recording", ShortcutGesture.TOGGLE, "f8"),
            _binding("paste-last", ShortcutGesture.TAP, "ctrl+alt+v"),
            _binding("paste-last-original", ShortcutGesture.TAP, "ctrl+alt+p"),
            _binding("profile:chat", ShortcutGesture.TAP, "ctrl+alt+1"),
        ):
            result = adapter.register(binding, lambda action_id, action: actions.append((action_id, action)))
            assert result.status is ShortcutRegistrationStatus.REGISTERED

        adapter._on_press(Key.shift_r)
        adapter._on_press(Key.shift_r)
        adapter._on_release(Key.shift_r)
        adapter._on_release(Key.shift_r)

        adapter._on_press(Key.f8)
        adapter._on_press(Key.f8)
        adapter._on_release(Key.f8)
        adapter._on_press(Key.f8)
        adapter._on_release(Key.f8)

        for letter in ("v", "p", "1"):
            adapter._on_press(Key.ctrl_l)
            adapter._on_press(Key.alt_l)
            adapter._on_press(KeyCode.from_char(letter))
            adapter._on_press(KeyCode.from_char(letter))
            adapter._on_release(KeyCode.from_char(letter))
            adapter._on_release(Key.alt_l)
            adapter._on_release(Key.ctrl_l)
        _flush(adapter)

        assert actions == [
            ("push-to-talk", ShortcutAction.HOLD_BEGIN),
            ("push-to-talk", ShortcutAction.HOLD_END),
            ("toggle-recording", ShortcutAction.TOGGLE),
            ("toggle-recording", ShortcutAction.TOGGLE),
            ("paste-last", ShortcutAction.FIRE),
            ("paste-last-original", ShortcutAction.FIRE),
            ("profile:chat", ShortcutAction.FIRE),
        ]
    finally:
        adapter.close()


def test_ptt_and_toggle_may_share_one_binding_without_conflict() -> None:
    adapter, _keyboard, _mouse = _make_adapter()
    actions: list[tuple[str, ShortcutAction]] = []
    try:
        for action_id, gesture in (
            ("push-to-talk", ShortcutGesture.HOLD),
            ("toggle-recording", ShortcutGesture.TOGGLE),
        ):
            outcome = adapter.register(
                _binding(action_id, gesture, "right shift"),
                lambda aid, action: actions.append((aid, action)),
            )
            assert outcome.status is ShortcutRegistrationStatus.REGISTERED
        adapter._on_press(Key.shift_r)
        adapter._on_release(Key.shift_r)
        _flush(adapter)
        assert actions == [
            ("push-to-talk", ShortcutAction.HOLD_BEGIN),
            ("toggle-recording", ShortcutAction.TOGGLE),
            ("push-to-talk", ShortcutAction.HOLD_END),
        ]
    finally:
        adapter.close()


def test_invalid_and_conflicting_contract_registration_is_typed_and_inert() -> None:
    adapter, keyboard, _mouse = _make_adapter()
    try:
        invalid = ShortcutBinding(
            action_id="bad",
            gesture=ShortcutGesture.TAP,
            configured="ctrl+alt+v",
            shortcut=CanonicalShortcut(("ctrl", "v")),
        )
        result = adapter.register(invalid, MagicMock())
        assert result.status is ShortcutRegistrationStatus.FAILED
        assert result.failure is ShortcutFailure.INVALID_BINDING
        assert keyboard.created == []

        assert adapter.register(
            _binding("hold-a", ShortcutGesture.HOLD, "f7"), MagicMock()
        ).status is ShortcutRegistrationStatus.REGISTERED
        conflict = adapter.register(
            _binding("hold-b", ShortcutGesture.HOLD, "f8"), MagicMock()
        )
        assert conflict.status is ShortcutRegistrationStatus.FAILED
        assert conflict.failure is ShortcutFailure.CONFLICT
        assert adapter._ptt_action_id == "hold-a"
    finally:
        adapter.close()


def test_capture_suppresses_actions_completes_after_full_release_and_restores() -> None:
    adapter, keyboard, mouse = _make_adapter()
    actions: list[tuple[str, ShortcutAction]] = []
    outcomes = []
    captured = threading.Event()
    try:
        adapter.register(
            _binding("paste-last", ShortcutGesture.TAP, "ctrl+alt+v"),
            lambda action_id, action: actions.append((action_id, action)),
        )
        adapter.begin_capture(lambda outcome: (outcomes.append(outcome), captured.set()))
        assert keyboard.created[0].stopped and mouse.created[0].stopped

        adapter._on_press(Key.ctrl_l)
        adapter._on_press(Key.alt_l)
        adapter._on_press(KeyCode.from_char("v"))
        adapter._on_release(KeyCode.from_char("v"))
        assert not captured.is_set()
        adapter._on_release(Key.alt_l)
        assert not captured.is_set()
        adapter._on_release(Key.ctrl_l)
        assert captured.wait(2.0)

        assert actions == []
        assert len(outcomes) == 1
        assert outcomes[0].status is ShortcutCaptureStatus.CAPTURED
        assert outcomes[0].shortcut == CanonicalShortcut(("ctrl", "alt", "v"))
        assert adapter._mode == "none"
        assert adapter._listener is keyboard.created[-1]
        assert adapter._mouse_listener is mouse.created[-1]
    finally:
        adapter.close()
    _assert_all_stopped(keyboard)
    _assert_all_stopped(mouse)


def test_legacy_capture_ui_streams_keys_and_commits_only_after_full_release() -> None:
    adapter, _keyboard, _mouse = _make_adapter()
    changed: list[str] = []
    captured: list[str] = []
    done = threading.Event()
    try:
        adapter.capture_shortcut(
            changed.append,
            lambda hotkey: (captured.append(hotkey), done.set()),
        )
        adapter._on_press(Key.ctrl_l)
        adapter._on_press(KeyCode.from_char("v"))
        _flush(adapter)
        assert changed == ["ctrl", "ctrl+v"]
        adapter._on_release(KeyCode.from_char("v"))
        assert captured == []
        adapter._on_release(Key.ctrl_l)
        assert done.wait(2.0)
        assert captured == ["ctrl+v"]
        assert adapter._mode == "none"
    finally:
        adapter.close()


def test_capture_conflict_and_cancel_are_typed_and_restore_prior_listener() -> None:
    adapter, keyboard, mouse = _make_adapter()
    outcomes = []
    try:
        adapter.register(
            _binding("tap", ShortcutGesture.TAP, "f7"), MagicMock()
        )
        adapter.begin_capture(outcomes.append)
        adapter.begin_capture(outcomes.append)
        _flush(adapter)
        assert outcomes[0].status is ShortcutCaptureStatus.FAILED
        assert outcomes[0].failure is ShortcutFailure.CONFLICT
        adapter.cancel_capture()
        _flush(adapter)
        assert outcomes[1].status is ShortcutCaptureStatus.CANCELLED
        assert adapter._mode == "none"
        assert len(keyboard.created) == len(mouse.created) == 3
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (_ListenerFactory(failures=[OSError("synthetic start failure")]), ShortcutFailure.NATIVE_ERROR),
        (_ListenerFactory(disabled=[True]), ShortcutFailure.LISTENER_DISABLED),
        (_ListenerFactory(failures=[PermissionError("synthetic denial")]), ShortcutFailure.PERMISSION_DENIED),
    ],
)
def test_failed_registration_leaves_no_keyboard_or_mouse_hook(
    factory: _ListenerFactory, expected: ShortcutFailure
) -> None:
    adapter, keyboard, mouse = _make_adapter(keyboard_factory=factory)
    result = adapter.register(
        _binding("tap", ShortcutGesture.TAP, "f7"), MagicMock()
    )
    assert result.status is ShortcutRegistrationStatus.FAILED
    assert result.failure is expected
    assert adapter._listener is None
    assert adapter._mouse_listener is None
    assert adapter._extra_hotkeys == {}
    _assert_all_stopped(keyboard)
    assert mouse.created == []
    adapter.close()


def test_partial_registration_failure_stops_started_keyboard_and_failed_mouse() -> None:
    mouse_failure = _ListenerFactory(
        failures=[OSError("synthetic mouse start failure")]
    )
    adapter, keyboard, mouse = _make_adapter(mouse_factory=mouse_failure)
    result = adapter.register(
        _binding("tap", ShortcutGesture.TAP, "f7"), MagicMock()
    )
    assert result.status is ShortcutRegistrationStatus.FAILED
    assert result.failure is ShortcutFailure.NATIVE_ERROR
    assert adapter._listener is None
    assert adapter._mouse_listener is None
    _assert_all_stopped(keyboard)
    _assert_all_stopped(mouse)
    adapter.close()


def test_close_balances_active_hold_stops_exact_hooks_and_is_idempotent() -> None:
    adapter, keyboard, mouse = _make_adapter()
    actions: list[tuple[str, ShortcutAction]] = []
    adapter.register(
        _binding("push-to-talk", ShortcutGesture.HOLD, "right shift"),
        lambda action_id, action: actions.append((action_id, action)),
    )
    adapter._on_press(Key.shift_r)
    _flush(adapter)

    adapter.close()
    adapter.close()

    assert actions == [
        ("push-to-talk", ShortcutAction.HOLD_BEGIN),
        ("push-to-talk", ShortcutAction.HOLD_END),
    ]
    _assert_all_stopped(keyboard)
    _assert_all_stopped(mouse)
    assert adapter._listener is None
    assert adapter._mouse_listener is None
    assert not adapter._worker.is_alive()


def test_backend_cleanup_cancels_capture_before_closing_hook_after_crash() -> None:
    from whisperstream.main import WhisperstreamApp

    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._recording = True
    app._restore_system_volume = MagicMock()
    app.audio_capture = MagicMock()
    app.keyboard = MagicMock()
    app.stt_engine = engine = MagicMock()
    order: list[str] = []
    app.audio_capture.close.side_effect = lambda: order.append("capture")
    app.keyboard.close.side_effect = lambda: order.append("hotkey")
    app.stt_engine.shutdown.side_effect = lambda: order.append("stt")

    app.cleanup()
    app.cleanup()

    app.audio_capture.close.assert_called_once_with()
    app.keyboard.close.assert_called_once_with()
    engine.shutdown.assert_called_once_with()
    assert app.stt_engine is None
    assert app._recording is False
    assert order == ["capture", "hotkey", "stt"]


def test_backend_crash_finally_closes_synthetic_global_hooks() -> None:
    from whisperstream.main import WhisperstreamApp

    adapter, keyboard, mouse = _make_adapter()
    assert adapter.register(
        _binding("push-to-talk", ShortcutGesture.HOLD, "right shift"),
        MagicMock(),
    ).status is ShortcutRegistrationStatus.REGISTERED
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock()
    app._recording = False
    app.audio_capture = MagicMock()
    app.keyboard = adapter
    app.stt_engine = MagicMock()

    with pytest.raises(RuntimeError, match="synthetic backend crash"):
        try:
            raise RuntimeError("synthetic backend crash")
        finally:
            app.cleanup()

    _assert_all_stopped(keyboard)
    _assert_all_stopped(mouse)
    assert adapter._listener is None
    assert adapter._mouse_listener is None
    assert not adapter._worker.is_alive()


def test_all_shortcut_configuration_fields_round_trip_without_semantic_change() -> None:
    original = WhisperstreamConfig(
        push_to_talk_shortcut="right shift",
        toggle_shortcut="f8",
        paste_last_shortcut="ctrl+alt+v",
        paste_last_original_shortcut="ctrl+alt+o",
        claude_view_shortcut="ctrl+alt+\\",
        profile_hotkeys={"chat": "ctrl+alt+1"},
    )
    restored = WhisperstreamConfig.from_dict(original.to_dict())
    assert restored.push_to_talk_shortcut == original.push_to_talk_shortcut
    assert restored.toggle_shortcut == original.toggle_shortcut
    assert restored.paste_last_shortcut == original.paste_last_shortcut
    assert (
        restored.paste_last_original_shortcut
        == original.paste_last_original_shortcut
    )
    assert restored.claude_view_shortcut == original.claude_view_shortcut
    assert restored.profile_hotkeys == original.profile_hotkeys
