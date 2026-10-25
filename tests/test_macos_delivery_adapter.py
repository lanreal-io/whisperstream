"""Deterministic macOS delivery, focus, and cleanup conformance tests.

Every payload, pasteboard value, target, and modifier in this module is
disposable synthetic state. The real pasteboard, focused app, owner documents,
and keyboard event stream are never read or changed.
"""

from __future__ import annotations

import logging
import platform
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from whisperstream.delivery_contract import (
    DeliveryAdapter,
    DeliveryFailure,
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus,
    DeliveryStyle,
    DeliveryWarning,
)
from whisperstream.macos_delivery_adapter import (
    MacosDeliveryAdapter,
    _NativeMacosDeliveryBridge,
    _NativeTarget,
    _SYNTHETIC_EVENT_MARKER,
)
from whisperstream.macos_shortcuts import (
    _SYNTHETIC_EVENT_MARKER as SHORTCUT_SYNTHETIC_EVENT_MARKER,
)
from whisperstream.permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native Apple Silicon delivery conformance",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_native_frontmost_query_refreshes_workspace_cache_first() -> None:
    bridge = object.__new__(_NativeMacosDeliveryBridge)
    calls: list[str] = []
    bridge._pump_run_loop = lambda: calls.append("pump")
    bridge._workspace_class = 1
    bridge._sel_shared_workspace = 2
    bridge._sel_frontmost_application = 3

    @contextmanager
    def pool():
        calls.append("pool")
        yield

    bridge._autorelease_pool = pool
    bridge._msg_id0 = lambda owner, selector: 4 if (owner, selector) == (1, 2) else 5
    expected = _NativeTarget(pid=7001, launch_token=81002)
    bridge._application_target = lambda application: expected if application == 5 else None

    assert bridge.frontmost_target() == expected
    assert calls == ["pump", "pool"]


def _permission_report(
    accessibility: PermissionState = PermissionState.GRANTED,
) -> PermissionReport:
    return PermissionReport(
        platform="macos",
        permissions=tuple(
            PermissionSnapshot(
                kind=kind,
                state=(
                    accessibility
                    if kind is PermissionKind.ACCESSIBILITY
                    else PermissionState.GRANTED
                ),
                query_supported=True,
                request_supported=True,
            )
            for kind in PermissionKind
        ),
    )


class _PermissionSequence:
    def __init__(self, *states: PermissionState) -> None:
        self.states = list(states or (PermissionState.GRANTED,))
        self.calls = 0

    def __call__(self) -> PermissionReport:
        index = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return _permission_report(self.states[index])


class _Timer:
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class _Timers:
    def __init__(self) -> None:
        self.items: list[_Timer] = []

    def __call__(self, delay: float, callback) -> _Timer:
        timer = _Timer(delay, callback)
        self.items.append(timer)
        return timer


class _Keyboard:
    def __init__(self) -> None:
        self.suppression_depth = 0
        self.suppression_entries = 0

    @contextmanager
    def suppress_self_events(self):
        self.suppression_depth += 1
        self.suppression_entries += 1
        try:
            yield
        finally:
            self.suppression_depth -= 1


class _Bridge:
    def __init__(self) -> None:
        self.target = _NativeTarget(pid=7001, launch_token=81002)
        self.frontmost_calls = 0
        self.validation_failures: list[DeliveryFailure | None] = []
        self.validation_calls = 0
        self.pasteboard = "synthetic-prior-pasteboard"
        self.pasteboard_reads = 0
        self.pasteboard_writes: list[str] = []
        self.held: tuple[int, ...] = ()
        self.physical: set[int] = set()
        self.events: list[tuple[object, ...]] = []
        self.v_down_attempts = 0
        self.fail_v_down_at: int | None = None
        self.fail_unicode_at: int | None = None
        self.unicode_attempts = 0

    def frontmost_target(self) -> _NativeTarget | None:
        self.frontmost_calls += 1
        return self.target

    def validate_target(self, target: _NativeTarget) -> DeliveryFailure | None:
        assert target == self.target
        index = self.validation_calls
        self.validation_calls += 1
        if index < len(self.validation_failures):
            return self.validation_failures[index]
        return None

    def read_pasteboard_text(self) -> str | None:
        self.pasteboard_reads += 1
        return self.pasteboard

    def write_pasteboard_text(self, value: str) -> None:
        self.pasteboard = value
        self.pasteboard_writes.append(value)

    def held_modifier_keycodes(self) -> tuple[int, ...]:
        return self.held

    def modifier_physically_down(self, keycode: int) -> bool:
        return keycode in self.physical

    def post_key(self, keycode: int, pressed: bool, *, flags: int | None = None) -> None:
        if keycode == 9 and pressed:
            self.v_down_attempts += 1
            if self.fail_v_down_at == self.v_down_attempts:
                raise RuntimeError("synthetic event rejection")
        self.events.append(("key", keycode, pressed))

    def post_unicode(self, value: str) -> None:
        self.unicode_attempts += 1
        if self.fail_unicode_at == self.unicode_attempts:
            raise RuntimeError("synthetic Unicode rejection")
        self.events.append(("unicode", value))


def _adapter(
    *,
    bridge: _Bridge | None = None,
    keyboard: _Keyboard | None = None,
    timers: _Timers | None = None,
    permissions: _PermissionSequence | None = None,
    clipboard_behavior: bool = True,
    keystroke_delay_ms: int = 0,
    sleep=None,
) -> tuple[
    MacosDeliveryAdapter,
    _Bridge,
    _Keyboard,
    _Timers,
    _PermissionSequence,
]:
    bridge = bridge or _Bridge()
    keyboard = keyboard or _Keyboard()
    timers = timers or _Timers()
    permissions = permissions or _PermissionSequence(PermissionState.GRANTED)
    adapter = MacosDeliveryAdapter(
        keyboard_manager=keyboard,
        logger=logging.getLogger("test.macos-delivery"),
        timer_factory=timers,
        sleep=sleep or (lambda _seconds: None),
        clipboard_behavior=clipboard_behavior,
        clipboard_clear_delay=0.4,
        keystroke_delay_ms=keystroke_delay_ms,
        permission_reporter=permissions,
        bridge_factory=lambda: bridge,
    )
    return adapter, bridge, keyboard, timers, permissions


def _request(
    adapter: MacosDeliveryAdapter,
    *,
    request_id: str,
    generation: int,
    style: DeliveryStyle,
    payload: str,
    submit: bool = False,
) -> DeliveryRequest:
    outcome = adapter.resolve_target(request_id, generation, DeliveryRoute.FOCUSED)
    assert outcome.failure is None
    assert outcome.target is not None
    return DeliveryRequest(
        request_id=request_id,
        generation=generation,
        target=outcome.target,
        style=style,
        processed_text=payload,
        submit=submit,
    )


def test_adapter_is_contract_shaped_opaque_and_native_symbols_resolve() -> None:
    adapter, bridge, _, _, _ = _adapter()

    outcome = adapter.resolve_target("opaque-mac-target", 7, DeliveryRoute.FOCUSED)

    assert isinstance(adapter, DeliveryAdapter)
    assert outcome.target is not None
    assert outcome.target.route is DeliveryRoute.FOCUSED
    retained = repr(outcome)
    assert str(bridge.target.pid) not in retained
    assert str(bridge.target.launch_token) not in retained
    assert "opaque_id" not in repr(outcome.target)
    assert MacosDeliveryAdapter.native_symbols_available() is True
    assert _SYNTHETIC_EVENT_MARKER == SHORTCUT_SYNTHETIC_EVENT_MARKER


def test_text_injector_selects_mac_lazily_without_windows_adapter() -> None:
    from whisperstream.text_injector import TextInjector

    injector_source = (REPO_ROOT / "src/whisperstream/text_injector.py").read_text(
        encoding="utf-8"
    )
    assert "from .windows_delivery_adapter" not in injector_source
    assert "from .macos_delivery_adapter" not in injector_source

    with patch("whisperstream.text_injector.platform.system", return_value="Darwin"):
        injector = TextInjector(
            keyboard_manager=_Keyboard(),
            logger=logging.getLogger("test.macos-delivery"),
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=True,
            clipboard_clear_delay=0.4,
        )
    assert isinstance(injector._delivery, MacosDeliveryAdapter)
    assert injector._delivery._bridge is None
    injector.close()


@pytest.mark.parametrize(
    ("state", "failure"),
    (
        (PermissionState.NOT_DETERMINED, DeliveryFailure.PERMISSION_DENIED),
        (PermissionState.DENIED, DeliveryFailure.PERMISSION_DENIED),
        (PermissionState.RESTRICTED, DeliveryFailure.PERMISSION_DENIED),
        (PermissionState.UNKNOWN, DeliveryFailure.NATIVE_ERROR),
        (PermissionState.UNSUPPORTED, DeliveryFailure.NATIVE_ERROR),
    ),
)
def test_permission_denial_and_unknown_fail_before_target_or_input(
    state: PermissionState,
    failure: DeliveryFailure,
) -> None:
    adapter, bridge, _, _, _ = _adapter(
        permissions=_PermissionSequence(state)
    )

    outcome = adapter.resolve_target("permission-edge", 1, DeliveryRoute.FOCUSED)

    assert outcome.failure is failure
    assert bridge.frontmost_calls == 0
    assert bridge.pasteboard_reads == 0
    assert bridge.events == []


def test_permission_revocation_after_resolution_fails_without_pasteboard_access() -> None:
    permissions = _PermissionSequence(
        PermissionState.GRANTED,
        PermissionState.DENIED,
    )
    adapter, bridge, _, _, _ = _adapter(permissions=permissions)
    request = _request(
        adapter,
        request_id="revoked-after-target",
        generation=2,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-revoked-payload",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.failure is DeliveryFailure.PERMISSION_DENIED
    assert receipt.units_attempted == 0
    assert bridge.pasteboard_reads == 0
    assert bridge.pasteboard_writes == []
    assert bridge.events == []


def test_clipboard_command_v_restores_and_balances_physical_modifier() -> None:
    adapter, bridge, keyboard, timers, _ = _adapter()
    bridge.held = (56,)
    bridge.physical = {56}
    request = _request(
        adapter,
        request_id="mac-command-v",
        generation=3,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-command-v-payload",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert (receipt.units_attempted, receipt.units_completed) == (1, 1)
    assert bridge.pasteboard == "synthetic-command-v-payload"
    assert bridge.events == [
        ("key", 56, False),
        ("key", 55, True),
        ("key", 9, True),
        ("key", 9, False),
        ("key", 55, False),
        ("key", 56, True),
    ]
    assert keyboard.suppression_entries == 1
    assert adapter.pending_restore_count == 1
    assert adapter.synthetic_modifier_count == 1
    assert timers.items[0].started is True

    timers.items[0].fire()
    timers.items[0].fire()
    assert bridge.pasteboard == "synthetic-prior-pasteboard"
    assert adapter.pending_restore_count == 0
    adapter.close()
    assert bridge.events[-1] == ("key", 56, False)
    assert adapter.synthetic_modifier_count == 0


def test_direct_typing_preserves_unicode_submits_and_never_reads_pasteboard() -> None:
    sleep_calls: list[float] = []
    adapter, bridge, keyboard, timers, _ = _adapter(
        keystroke_delay_ms=3,
        sleep=sleep_calls.append,
    )
    request = _request(
        adapter,
        request_id="mac-direct-type",
        generation=4,
        style=DeliveryStyle.KEYSTROKE,
        payload="A\nβ🚀",
        submit=True,
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.units_completed == 1
    assert bridge.events == [
        ("unicode", "A"),
        ("unicode", "\n"),
        ("unicode", "β"),
        ("unicode", "🚀"),
        ("key", 36, True),
        ("key", 36, False),
    ]
    assert bridge.pasteboard_reads == 0
    assert bridge.pasteboard_writes == []
    assert sleep_calls == [0.003, 0.003, 0.003, 0.003]
    assert timers.items == []
    assert keyboard.suppression_entries == 1


def test_target_loss_before_delivery_and_focus_loss_after_copy_are_explicit() -> None:
    dead_bridge = _Bridge()
    dead_bridge.validation_failures = [DeliveryFailure.DEAD_TARGET]
    dead, dead_bridge, _, _, _ = _adapter(bridge=dead_bridge)
    dead_request = _request(
        dead,
        request_id="dead-target",
        generation=5,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-dead-target",
    )

    dead_receipt = dead.deliver(dead_request)
    assert dead_receipt.failure is DeliveryFailure.DEAD_TARGET
    assert dead_receipt.units_attempted == 0
    assert dead_bridge.pasteboard_reads == 0
    assert dead_bridge.events == []

    focus_bridge = _Bridge()
    focus_bridge.validation_failures = [
        None,
        None,
        DeliveryFailure.FOCUS_MISMATCH,
    ]
    focus, focus_bridge, _, timers, _ = _adapter(bridge=focus_bridge)
    focus_request = _request(
        focus,
        request_id="focus-loss-after-copy",
        generation=6,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-focus-loss",
    )

    focus_receipt = focus.deliver(focus_request)
    assert focus_receipt.status is DeliveryStatus.FAILED
    assert focus_receipt.failure is DeliveryFailure.FOCUS_MISMATCH
    assert focus_receipt.units_completed == 0
    assert focus_bridge.events == []
    assert focus_bridge.pasteboard == "synthetic-focus-loss"
    timers.items[0].fire()
    assert focus_bridge.pasteboard == "synthetic-prior-pasteboard"


def test_process_identity_mismatch_is_typed_before_clipboard_or_input() -> None:
    bridge = _Bridge()
    bridge.validation_failures = [DeliveryFailure.PROCESS_MISMATCH]
    adapter, bridge, _, _, _ = _adapter(bridge=bridge)
    request = _request(
        adapter,
        request_id="process-identity-mismatch",
        generation=16,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-process-mismatch",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.failure is DeliveryFailure.PROCESS_MISMATCH
    assert receipt.units_attempted == 0
    assert bridge.pasteboard_reads == 0
    assert bridge.pasteboard_writes == []
    assert bridge.events == []


def test_input_failure_releases_command_and_balances_owned_modifiers() -> None:
    bridge = _Bridge()
    bridge.held = (56,)
    bridge.physical = {56}
    bridge.fail_v_down_at = 1
    adapter, bridge, _, timers, _ = _adapter(bridge=bridge)
    request = _request(
        adapter,
        request_id="mac-input-cleanup",
        generation=17,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-input-cleanup",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.units_completed == 0
    assert bridge.events == [
        ("key", 56, False),
        ("key", 55, True),
        ("key", 55, False),
        ("key", 56, True),
    ]
    assert adapter.synthetic_modifier_count == 1
    adapter.close()
    assert bridge.events[-1] == ("key", 56, False)
    assert adapter.synthetic_modifier_count == 0
    assert all(timer.cancelled for timer in timers.items)
    assert bridge.pasteboard == "synthetic-prior-pasteboard"


def test_disabled_clipboard_restore_never_reads_or_schedules() -> None:
    adapter, bridge, _, timers, _ = _adapter(clipboard_behavior=False)
    request = _request(
        adapter,
        request_id="mac-no-clipboard-restore",
        generation=18,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-no-restore",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.clipboard_restored is True
    assert bridge.pasteboard_reads == 0
    assert bridge.pasteboard == "synthetic-no-restore"
    assert timers.items == []


def test_duplicate_finalization_and_precancel_never_redeliver() -> None:
    adapter, bridge, _, timers, _ = _adapter()
    first = _request(
        adapter,
        request_id="same-mac-finalizer",
        generation=7,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-first-mac-payload",
    )
    first_receipt = adapter.deliver(first)
    duplicate = _request(
        adapter,
        request_id="same-mac-finalizer",
        generation=7,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-must-not-redeliver",
    )

    assert adapter.deliver(duplicate) == first_receipt
    assert bridge.v_down_attempts == 1
    assert "synthetic-must-not-redeliver" not in bridge.pasteboard_writes

    cancelled = adapter.cancel("cancelled-mac-finalizer", 8)
    cancelled_request = _request(
        adapter,
        request_id="cancelled-mac-finalizer",
        generation=8,
        style=DeliveryStyle.KEYSTROKE,
        payload="synthetic-cancelled-payload",
    )
    assert adapter.deliver(cancelled_request) == cancelled
    assert cancelled.status is DeliveryStatus.CANCELLED
    assert not any(event[0] == "unicode" for event in bridge.events)
    timers.items[0].fire()


def test_partial_batch_failure_is_terminal_never_retried_and_not_promoted() -> None:
    from whisperstream.text_injector import (
        DeliveryStatus as PublicDeliveryStatus,
        TextInjector,
    )

    bridge = _Bridge()
    bridge.fail_v_down_at = 2
    adapter, bridge, keyboard, _, _ = _adapter(bridge=bridge)
    injector = TextInjector(
        keyboard_manager=keyboard,
        logger=logging.getLogger("test.macos-delivery"),
        word_filters=[],
        word_replacements={},
        post_processing="",
        clipboard_behavior=True,
        clipboard_clear_delay=0.4,
        delivery_adapter=adapter,
    )
    injector._last_transcript = "synthetic-prior-complete"

    first = injector.paste_chunk(
        "synthetic-first-unit",
        first=True,
        last=False,
        request_id="mac-focused-partial",
        generation=9,
    )
    failed = injector.paste_chunk(
        "synthetic-second-unit",
        first=False,
        last=False,
        request_id="mac-focused-partial",
        generation=9,
    )
    latched = injector.paste_chunk(
        "synthetic-third-unit",
        first=False,
        last=True,
        request_id="mac-focused-partial",
        generation=9,
    )

    assert first.status is PublicDeliveryStatus.FOCUSED
    assert failed.status is PublicDeliveryStatus.FAILED
    assert failed.receipt is not None and failed.receipt.partial is True
    assert latched is failed
    assert bridge.v_down_attempts == 2
    assert injector.last_transcript == "synthetic-prior-complete"
    injector.close()
    assert bridge.pasteboard == "synthetic-prior-pasteboard"
    assert adapter.active_batch_count == 0
    assert adapter.pending_restore_count == 0


def test_snapshot_failure_selects_direct_typing_before_input() -> None:
    class _ReadFailureBridge(_Bridge):
        def read_pasteboard_text(self) -> str | None:
            raise RuntimeError("synthetic read rejection")

    read_adapter, read_bridge, _, _, _ = _adapter(bridge=_ReadFailureBridge())
    read_receipt = read_adapter.deliver(
        _request(
            read_adapter,
            request_id="mac-read-warning",
            generation=10,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-read-warning",
        )
    )
    assert read_receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert read_receipt.units_completed == 1
    assert read_receipt.warnings == (
        DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,
    )
    assert read_bridge.v_down_attempts == 0
    assert read_bridge.unicode_attempts == len("synthetic-read-warning")
    assert read_bridge.pasteboard_writes == []

    no_string_bridge = _Bridge()
    no_string_bridge.pasteboard = None
    no_string_adapter, _, _, timers, _ = _adapter(bridge=no_string_bridge)
    no_string_receipt = no_string_adapter.deliver(
        _request(
            no_string_adapter,
            request_id="mac-no-string-fallback",
            generation=11,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-no-string",
        )
    )
    assert no_string_receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert no_string_receipt.warnings == (
        DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,
    )
    assert no_string_bridge.pasteboard is None
    assert no_string_bridge.pasteboard_writes == []
    assert no_string_bridge.v_down_attempts == 0
    assert timers.items == []


def test_snapshot_fallback_typing_failure_is_terminal_without_paste() -> None:
    class _ReadFailureBridge(_Bridge):
        def read_pasteboard_text(self) -> str | None:
            raise RuntimeError("synthetic read rejection")

    bridge = _ReadFailureBridge()
    bridge.fail_unicode_at = 1
    adapter, _, _, timers, _ = _adapter(bridge=bridge)
    receipt = adapter.deliver(
        _request(
            adapter,
            request_id="mac-fallback-failure",
            generation=12,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-fallback-failure",
        )
    )

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.failure is DeliveryFailure.INPUT_FAILED
    assert receipt.units_completed == 0
    assert receipt.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)
    assert bridge.unicode_attempts == 1
    assert bridge.v_down_attempts == 0
    assert bridge.pasteboard_writes == []
    assert timers.items == []


def test_snapshot_fallback_is_selected_once_for_the_whole_batch() -> None:
    class _ReadFailureBridge(_Bridge):
        def __init__(self) -> None:
            super().__init__()
            self.read_attempts = 0

        def read_pasteboard_text(self) -> str | None:
            self.read_attempts += 1
            raise RuntimeError("synthetic read rejection")

    bridge = _ReadFailureBridge()
    adapter, _, _, timers, _ = _adapter(
        bridge=bridge,
        keystroke_delay_ms=2,
    )
    started = adapter.begin_batch(
        _request(
            adapter,
            request_id="mac-fallback-batch",
            generation=13,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-first",
        )
    )
    assert started.token is not None

    first = adapter.deliver_batch_unit(
        started.token, "synthetic-first", last=False
    )
    final = adapter.deliver_batch_unit(
        started.token, " synthetic-second", last=True
    )

    assert first.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert final.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert bridge.read_attempts == 1
    assert bridge.pasteboard_writes == []
    assert bridge.v_down_attempts == 0
    assert bridge.unicode_attempts == len("synthetic-first synthetic-second")
    assert timers.items == []
    assert final.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)


def test_clipboard_copy_failure_remains_best_effort() -> None:

    class _WriteFailureBridge(_Bridge):
        def write_pasteboard_text(self, value: str) -> None:
            raise RuntimeError("synthetic write rejection")

    write_adapter, write_bridge, _, _, _ = _adapter(bridge=_WriteFailureBridge())
    write_receipt = write_adapter.deliver(
        _request(
            write_adapter,
            request_id="mac-write-warning",
            generation=14,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-write-warning",
        )
    )
    assert write_receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert write_receipt.units_completed == 0
    assert write_receipt.warnings == (DeliveryWarning.CLIPBOARD_COPY_FAILED,)
    assert write_bridge.events == []


def test_close_cancels_batch_timer_targets_and_owned_modifiers() -> None:
    adapter, bridge, _, timers, _ = _adapter()
    bridge.held = (59,)
    bridge.physical = {59}
    first = _request(
        adapter,
        request_id="mac-pending-restore",
        generation=12,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-pending-restore",
    )
    adapter.deliver(first)
    batch_request = _request(
        adapter,
        request_id="mac-active-batch",
        generation=13,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-batch-start",
    )
    started = adapter.begin_batch(batch_request)
    assert started.token is not None
    unresolved = adapter.resolve_target(
        "mac-unused-target", 14, DeliveryRoute.FOCUSED
    )
    assert unresolved.target is not None

    adapter.close()

    assert all(timer.cancelled for timer in timers.items)
    assert bridge.pasteboard == "synthetic-prior-pasteboard"
    assert adapter.pending_restore_count == 0
    assert adapter.active_batch_count == 0
    assert adapter.target_count == 0
    assert adapter.synthetic_modifier_count == 0
    assert adapter.close() is None
    assert (
        adapter.resolve_target("after-close", 1, DeliveryRoute.FOCUSED).failure
        is DeliveryFailure.CLEANUP_FAILED
    )


def test_receipts_logs_and_source_do_not_retain_content_or_native_identity(caplog) -> None:
    payload = "SYNTHETIC_PRIVATE_MAC_PAYLOAD_51af"
    adapter, bridge, _, timers, _ = _adapter()
    request = _request(
        adapter,
        request_id="mac-privacy-receipt",
        generation=15,
        style=DeliveryStyle.CLIPBOARD,
        payload=payload,
    )
    with caplog.at_level(logging.INFO, logger="test.macos-delivery"):
        receipt = adapter.deliver(request)

    retained = "\n".join(
        (repr(request), repr(receipt), str(receipt.as_dict()), caplog.text)
    )
    assert payload not in retained
    assert str(bridge.target.pid) not in retained
    assert str(bridge.target.launch_token) not in retained
    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    timers.items[0].fire()

    source = (
        REPO_ROOT / "src/whisperstream/macos_delivery_adapter.py"
    ).read_text(encoding="utf-8")
    contract = (REPO_ROOT / "src/whisperstream/delivery_contract.py").read_text(
        encoding="utf-8"
    )
    assert "requestAccess" not in source
    assert "openSystemSettings" not in source
    assert "tccutil" not in source
    assert "AppKit" not in contract
    assert "CoreGraphics" not in contract


@pytest.mark.parametrize("reject_v_down", [False, True])
def test_native_command_v_sets_flags_before_post_without_global_state_dependency(reject_v_down):
    events = []
    class CG:
        def CGEventCreateKeyboardEvent(self, source, key, down):
            events.append({"key": key, "down": down, "flags": None, "marker": None})
            return len(events)
        def CGEventSetIntegerValueField(self, event, field, value):
            events[event - 1]["marker"] = value
        def CGEventSetFlags(self, event, flags):
            events[event - 1]["flags"] = flags
        def CGEventPost(self, tap, event):
            assert events[event - 1]["flags"] is not None
            if reject_v_down and events[event - 1]["key"] == 9 and events[event - 1]["down"]:
                raise RuntimeError("synthetic post failure")
    bridge = object.__new__(_NativeMacosDeliveryBridge)
    bridge._cg = CG()
    bridge._cf = SimpleNamespace(CFRelease=lambda event: None)
    adapter = object.__new__(MacosDeliveryAdapter)
    adapter._native_bridge = lambda: bridge
    if reject_v_down:
        with pytest.raises(RuntimeError, match="synthetic post failure"):
            adapter._send_command_v()
    else:
        adapter._send_command_v()
    expected = [(55, True, 1 << 20), (9, True, 1 << 20)]
    if not reject_v_down:
        expected.append((9, False, 1 << 20))
    expected.append((55, False, 0))
    assert [(e["key"], e["down"], e["flags"]) for e in events] == expected
    assert all(e["marker"] == _SYNTHETIC_EVENT_MARKER for e in events)


def test_live_clipboard_settings_preserve_restore_and_apply_next_delivery() -> None:
    adapter, bridge, _, timers, _ = _adapter()
    adapter.deliver(_request(adapter, request_id="live-first", generation=1, style=DeliveryStyle.CLIPBOARD, payload="synthetic-first"))
    pending = timers.items[0]
    assert pending.delay == 0.4
    adapter.update_clipboard_settings(behavior=False, clear_delay=0.2)
    assert not pending.cancelled
    pending.fire()
    assert bridge.pasteboard == "synthetic-prior-pasteboard"
    adapter.deliver(_request(adapter, request_id="live-second", generation=2, style=DeliveryStyle.CLIPBOARD, payload="synthetic-second"))
    assert len(timers.items) == 1
    adapter.update_clipboard_settings(behavior=True, clear_delay=0.2)
    adapter.deliver(_request(adapter, request_id="live-third", generation=3, style=DeliveryStyle.CLIPBOARD, payload="synthetic-third"))
    assert timers.items[-1].delay == 0.2
    adapter.close()


def test_disabling_clipboard_restore_preserves_admitted_snapshot_obligation() -> None:
    adapter, bridge, _, timers, _ = _adapter()
    preparation = adapter._prepare_delivery(_request(adapter, request_id="admitted", generation=1, style=DeliveryStyle.CLIPBOARD, payload="synthetic-output"))
    adapter.update_clipboard_settings(behavior=False, clear_delay=0.2)
    bridge.pasteboard = "synthetic-admitted-output"
    adapter._schedule_clipboard_restore(preparation.saved_clipboard)
    timers.items[0].fire()
    assert bridge.pasteboard == "synthetic-prior-pasteboard"
    adapter.close()


@pytest.mark.parametrize("character", ["A", "β", "🚀"])
def test_unicode_events_clear_inherited_flags_for_down_and_up(character):
    from unittest.mock import Mock, call
    bridge = object.__new__(_NativeMacosDeliveryBridge)
    bridge._post_event = Mock()
    bridge.post_unicode(character)
    assert bridge._post_event.call_args_list == [
        call(0, True, character, flags=0),
        call(0, False, character, flags=0),
    ]


def test_unicode_down_failure_attempts_zero_flag_release():
    from unittest.mock import Mock, call
    bridge = object.__new__(_NativeMacosDeliveryBridge)
    bridge._post_event = Mock(side_effect=[RuntimeError("synthetic post failure"), None])
    with pytest.raises(RuntimeError, match="synthetic post failure"):
        bridge.post_unicode("A")
    assert bridge._post_event.call_args_list == [
        call(0, True, "A", flags=0),
        call(0, False, "A", flags=0),
    ]
