"""Deterministic Windows delivery, focus, and cleanup conformance tests.

Every payload, clipboard, target, and modifier in this module is disposable
synthetic state. The real Windows clipboard, focus, hooks, and user input are
never touched.
"""

from __future__ import annotations

import logging
import platform
from contextlib import nullcontext
from pathlib import Path
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
from whisperstream.windows_delivery_adapter import WindowsDeliveryAdapter


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="native Windows delivery conformance",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Clipboard:
    def __init__(self, value: str = "synthetic-owner-clipboard") -> None:
        self.value = value
        self.writes: list[str] = []
        self.reads = 0
        self.fail_read = False
        self.fail_write = False

    def paste(self) -> str:
        self.reads += 1
        if self.fail_read:
            raise RuntimeError("synthetic clipboard read failure")
        return self.value

    def copy(self, value: str) -> None:
        if self.fail_write:
            raise RuntimeError("synthetic clipboard write failure")
        self.value = value
        self.writes.append(value)


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


class _Controller:
    def __init__(self) -> None:
        self.released: list[object] = []
        self.pressed: list[object] = []

    def release(self, key: object) -> None:
        self.released.append(key)

    def press(self, key: object) -> None:
        self.pressed.append(key)


class _Keyboard:
    def __init__(self) -> None:
        self._controller = _Controller()
        self.held: tuple[object, ...] = ()
        self.physical: set[object] = set()
        self.sent: list[str] = []
        self.typed: list[tuple[str, int]] = []
        self.type_attempts = 0
        self.fail_type_at: int | None = None
        self.fail_send_at: int | None = None

    def snapshot_held_modifiers(self) -> tuple[object, ...]:
        return self.held

    def suppress_self_events(self):
        return nullcontext()

    def modifier_physically_down(self, key: object) -> bool:
        return key in self.physical

    def send(self, combo: str) -> None:
        if self.fail_send_at == len(self.sent) + 1:
            raise RuntimeError("synthetic send failure")
        self.sent.append(combo)

    def type_text(self, text: str, *, delay_ms: int) -> None:
        self.type_attempts += 1
        if self.fail_type_at == self.type_attempts:
            raise RuntimeError("synthetic typing failure")
        self.typed.append((text, delay_ms))


def _adapter(
    *,
    clipboard: _Clipboard | None = None,
    keyboard: _Keyboard | None = None,
    timers: _Timers | None = None,
    keystroke_delay_ms: int = 0,
) -> tuple[WindowsDeliveryAdapter, _Clipboard, _Keyboard, _Timers]:
    clipboard = clipboard or _Clipboard()
    keyboard = keyboard or _Keyboard()
    timers = timers or _Timers()
    adapter = WindowsDeliveryAdapter(
        keyboard_manager=keyboard,
        logger=logging.getLogger("test.windows-delivery"),
        clipboard=clipboard,
        timer_factory=timers,
        sleep=lambda _seconds: None,
        clipboard_behavior=True,
        clipboard_clear_delay=0.4,
        keystroke_delay_ms=keystroke_delay_ms,
    )
    return adapter, clipboard, keyboard, timers


def _request(
    adapter: WindowsDeliveryAdapter,
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


def test_adapter_is_contract_shaped_and_hides_opaque_target_identity() -> None:
    adapter, _, _, _ = _adapter()

    outcome = adapter.resolve_target("opaque-target", 7, DeliveryRoute.FOCUSED)

    assert isinstance(adapter, DeliveryAdapter)
    assert outcome.target is not None
    assert outcome.target.route is DeliveryRoute.FOCUSED
    assert "opaque_id" not in repr(outcome.target)


def test_shared_processing_and_main_do_not_own_windows_input_apis() -> None:
    injector_source = (REPO_ROOT / "src/whisperstream/text_injector.py").read_text(
        encoding="utf-8"
    )
    main_source = (REPO_ROOT / "src/whisperstream/main.py").read_text(
        encoding="utf-8"
    )

    for private_token in (
        "pyperclip.copy(",
        "pyperclip.paste(",
        "._controller",
    ):
        assert private_token not in injector_source
        assert private_token not in main_source
    assert "WindowsDeliveryAdapter" not in main_source
    assert "windows_delivery_adapter" not in main_source


def test_text_injector_selects_the_macos_adapter_on_macos() -> None:
    from whisperstream.macos_delivery_adapter import MacosDeliveryAdapter
    from whisperstream.text_injector import TextInjector

    with patch("whisperstream.text_injector.platform.system", return_value="Darwin"):
        injector = TextInjector(
            keyboard_manager=_Keyboard(),
            logger=logging.getLogger("test.windows-delivery"),
            word_filters=[],
            word_replacements={},
            post_processing="",
            clipboard_behavior=True,
            clipboard_clear_delay=0.4,
        )
    assert isinstance(injector._delivery, MacosDeliveryAdapter)
    assert injector._delivery._bridge is None
    injector.close()


def test_clipboard_mode_saves_shift_inserts_and_restores_with_modifier_guard() -> None:
    adapter, clipboard, keyboard, timers = _adapter()
    keyboard.held = ("right-shift",)
    keyboard.physical = {"right-shift"}
    request = _request(
        adapter,
        request_id="clipboard-success",
        generation=1,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-delivery-payload",
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert (receipt.units_attempted, receipt.units_completed) == (1, 1)
    assert clipboard.value == "synthetic-delivery-payload"
    assert keyboard.sent == ["shift+insert"]
    assert keyboard._controller.released == ["right-shift"]
    assert keyboard._controller.pressed == ["right-shift"]
    assert adapter.pending_restore_count == 1
    assert timers.items[0].started is True

    timers.items[0].fire()
    assert clipboard.value == "synthetic-owner-clipboard"
    assert adapter.pending_restore_count == 0
    adapter.close()
    assert keyboard._controller.released[-1] == "right-shift"


def test_keystroke_mode_never_touches_clipboard_and_can_submit() -> None:
    adapter, clipboard, keyboard, timers = _adapter(keystroke_delay_ms=3)
    request = _request(
        adapter,
        request_id="keystroke-success",
        generation=2,
        style=DeliveryStyle.KEYSTROKE,
        payload="synthetic typed payload",
        submit=True,
    )

    receipt = adapter.deliver(request)

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.units_completed == 1
    assert keyboard.typed == [("synthetic typed payload", 3)]
    assert keyboard.sent == ["enter"]
    assert clipboard.value == "synthetic-owner-clipboard"
    assert clipboard.writes == []
    assert timers.items == []


def test_duplicate_finalization_and_precancel_never_redeliver() -> None:
    adapter, clipboard, keyboard, timers = _adapter()
    first = _request(
        adapter,
        request_id="same-finalizer",
        generation=9,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-first-payload",
    )
    first_receipt = adapter.deliver(first)
    duplicate = _request(
        adapter,
        request_id="same-finalizer",
        generation=9,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-must-not-deliver",
    )

    assert adapter.deliver(duplicate) == first_receipt
    assert keyboard.sent == ["shift+insert"]
    assert "synthetic-must-not-deliver" not in clipboard.writes

    cancelled = adapter.cancel("cancelled-finalizer", 10)
    cancelled_request = _request(
        adapter,
        request_id="cancelled-finalizer",
        generation=10,
        style=DeliveryStyle.KEYSTROKE,
        payload="synthetic-cancelled-payload",
    )
    assert adapter.deliver(cancelled_request) == cancelled
    assert cancelled.status is DeliveryStatus.CANCELLED
    assert keyboard.typed == []
    timers.items[0].fire()
    assert clipboard.value == "synthetic-owner-clipboard"


def test_snapshot_failure_selects_direct_typing_before_input() -> None:
    clipboard = _Clipboard()
    clipboard.fail_read = True
    adapter, _, keyboard, _ = _adapter(clipboard=clipboard)
    request = _request(
        adapter,
        request_id="clipboard-read-failure",
        generation=1,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-read-edge",
    )
    receipt = adapter.deliver(request)
    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.units_completed == 1
    assert receipt.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)
    assert keyboard.typed == [("synthetic-read-edge", 0)]
    assert keyboard.sent == []
    assert clipboard.writes == []


def test_snapshot_fallback_typing_failure_is_terminal_without_paste() -> None:
    clipboard = _Clipboard()
    clipboard.fail_read = True
    keyboard = _Keyboard()
    keyboard.fail_type_at = 1
    adapter, _, _, timers = _adapter(clipboard=clipboard, keyboard=keyboard)

    receipt = adapter.deliver(
        _request(
            adapter,
            request_id="clipboard-fallback-failure",
            generation=2,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-fallback-edge",
        )
    )

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.failure is DeliveryFailure.INPUT_FAILED
    assert receipt.units_completed == 0
    assert receipt.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)
    assert keyboard.type_attempts == 1
    assert keyboard.sent == []
    assert clipboard.writes == []
    assert timers.items == []


def test_non_text_snapshot_selects_typing_without_mutating_clipboard() -> None:
    clipboard = _Clipboard()
    clipboard.value = object()  # type: ignore[assignment]
    original = clipboard.value
    adapter, _, keyboard, timers = _adapter(clipboard=clipboard)

    receipt = adapter.deliver(
        _request(
            adapter,
            request_id="clipboard-non-text-fallback",
            generation=3,
            style=DeliveryStyle.CLIPBOARD,
            payload="synthetic-non-text-edge",
        )
    )

    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert receipt.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)
    assert clipboard.value is original
    assert clipboard.writes == []
    assert keyboard.typed == [("synthetic-non-text-edge", 0)]
    assert keyboard.sent == []
    assert timers.items == []


def test_snapshot_fallback_is_selected_once_for_the_whole_batch() -> None:
    clipboard = _Clipboard()
    clipboard.fail_read = True
    adapter, _, keyboard, timers = _adapter(
        clipboard=clipboard,
        keystroke_delay_ms=4,
    )
    started = adapter.begin_batch(
        _request(
            adapter,
            request_id="clipboard-fallback-batch",
            generation=4,
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
    assert clipboard.reads == 1
    assert clipboard.writes == []
    assert keyboard.typed == [
        ("synthetic-first", 4),
        (" synthetic-second", 4),
    ]
    assert keyboard.sent == []
    assert timers.items == []
    assert final.warnings == (DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,)


def test_focused_clipboard_copy_failure_remains_best_effort() -> None:

    failing_clipboard = _Clipboard()
    failing_clipboard.fail_write = True
    failed, _, failed_keyboard, _ = _adapter(clipboard=failing_clipboard)
    failed_request = _request(
        failed,
        request_id="clipboard-copy-failure",
        generation=6,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-copy-edge",
    )
    failed_receipt = failed.deliver(failed_request)
    assert failed_receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    assert failed_receipt.failure is None
    assert failed_receipt.units_completed == 0
    assert DeliveryWarning.CLIPBOARD_COPY_FAILED in failed_receipt.warnings
    assert failed_keyboard.sent == []


def test_partial_batch_failure_is_terminal_and_close_restores_state() -> None:
    keyboard = _Keyboard()
    keyboard.fail_send_at = 2
    adapter, clipboard, _, timers = _adapter(keyboard=keyboard)
    request = _request(
        adapter,
        request_id="partial-batch",
        generation=5,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-not-retained",
    )
    started = adapter.begin_batch(request)
    assert started.token is not None
    first = adapter.deliver_batch_unit(
        started.token,
        "synthetic-first-unit",
        last=False,
    )
    assert first.units_completed == 1
    failed = adapter.deliver_batch_unit(
        started.token,
        "synthetic-second-unit",
        last=False,
    )
    assert failed.status is DeliveryStatus.FAILED
    assert failed.failure is DeliveryFailure.INPUT_FAILED
    assert failed.units_attempted == 2
    assert failed.units_completed == 1
    assert failed.partial is True
    assert adapter.active_batch_count == 0

    adapter.close()
    assert adapter.active_batch_count == 0
    assert adapter.pending_restore_count == 0
    assert clipboard.value == "synthetic-owner-clipboard"
    assert timers.items[-1].cancelled is True
    assert (
        adapter.resolve_target("closed-generation", 1, DeliveryRoute.FOCUSED).failure
        is DeliveryFailure.CLEANUP_FAILED
    )


def test_text_injector_stops_a_focused_partial_batch_without_promotion() -> None:
    from whisperstream.text_injector import (
        DeliveryStatus as PublicDeliveryStatus,
        TextInjector,
    )

    keyboard = _Keyboard()
    keyboard.fail_send_at = 2
    adapter, clipboard, _, _ = _adapter(keyboard=keyboard)
    injector = TextInjector(
        keyboard_manager=keyboard,
        logger=logging.getLogger("test.windows-delivery"),
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
        request_id="focused-partial",
        generation=8,
    )
    failed = injector.paste_chunk(
        "synthetic-second-unit",
        first=False,
        last=False,
        request_id="focused-partial",
        generation=8,
    )

    assert first.status is PublicDeliveryStatus.FOCUSED
    assert failed.status is PublicDeliveryStatus.FAILED
    assert failed.receipt is not None and failed.receipt.partial is True
    assert injector.last_transcript == "synthetic-prior-complete"
    assert keyboard.sent == ["shift+insert"]
    injector.close()
    assert clipboard.value == "synthetic-owner-clipboard"


def test_batch_cancel_and_close_restore_without_held_or_partial_redelivery() -> None:
    keyboard = _Keyboard()
    keyboard.held = ("ctrl",)
    keyboard.physical = {"ctrl"}
    adapter, clipboard, _, _ = _adapter(keyboard=keyboard)
    request = _request(
        adapter,
        request_id="cancel-active-batch",
        generation=6,
        style=DeliveryStyle.CLIPBOARD,
        payload="synthetic-unused",
    )
    started = adapter.begin_batch(request)
    assert started.token is not None
    adapter.deliver_batch_unit(started.token, "synthetic-partial", last=False)

    receipt = adapter.cancel("cancel-active-batch", 6)
    assert receipt.status is DeliveryStatus.CANCELLED
    adapter.close()
    assert clipboard.value == "synthetic-owner-clipboard"
    assert keyboard._controller.released[-1] == "ctrl"
    assert adapter.close() is None


def test_receipts_and_logs_never_retain_payload(caplog) -> None:
    payload = "SYNTHETIC_PRIVATE_PAYLOAD_8f2a"

    adapter, _, _, timers = _adapter()
    request = _request(
        adapter,
        request_id="privacy-receipt",
        generation=11,
        style=DeliveryStyle.CLIPBOARD,
        payload=payload,
    )
    with caplog.at_level(logging.INFO, logger="test.windows-delivery"):
        receipt = adapter.deliver(request)

    retained = "\n".join(
        (repr(request), repr(receipt), str(receipt.as_dict()), caplog.text)
    )
    assert payload not in retained
    assert receipt.status is DeliveryStatus.FOCUSED_ATTEMPTED
    timers.items[0].fire()
