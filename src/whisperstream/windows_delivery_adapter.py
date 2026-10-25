"""Windows focus, clipboard, and keyboard delivery adapter.

The portable schema lives in :mod:`whisperstream.delivery_contract`. Clipboard
objects, pynput controllers, timers, and native failure
details remain private to this module. The adapter accepts only already-
processed text and returns content-free receipts.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .delivery_contract import (
    DeliveryClaimLedger,
    DeliveryClaimStatus,
    DeliveryFailure,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus,
    DeliveryStyle,
    DeliveryTarget,
    DeliveryTargetOutcome,
    DeliveryWarning,
)


@dataclass(frozen=True, slots=True)
class _TargetBinding:
    target: DeliveryTarget


@dataclass(slots=True)
class _InputResult:
    delivered: bool
    modifiers_restored: bool = True
    warnings: list[DeliveryWarning] = field(default_factory=list)


@dataclass(slots=True, eq=False)
class _PendingClipboardRestore:
    saved: str = field(repr=False)
    timer: object | None = field(default=None, repr=False)


@dataclass(slots=True)
class _DeliveryPreparation:
    effective_style: DeliveryStyle
    saved_clipboard: str | None = field(default=None, repr=False)
    snapshot_fallback: bool = False
    warnings: list[DeliveryWarning] = field(default_factory=list)


@dataclass(slots=True)
class _BatchState:
    token: str
    request_id: str
    generation: int
    binding: _TargetBinding
    style: DeliveryStyle
    effective_style: DeliveryStyle
    snapshot_fallback: bool
    restore_clipboard: bool
    saved_clipboard: str | None = field(default=None, repr=False)
    warnings: list[DeliveryWarning] = field(default_factory=list)
    units_attempted: int = 0
    units_completed: int = 0
    modifiers_restored: bool = True


@dataclass(frozen=True, slots=True)
class WindowsDeliveryBatchStart:
    """Adapter-private incremental-delivery start result."""

    token: str | None = field(default=None, repr=False)
    receipt: DeliveryReceipt | None = None


def _warnings_tuple(items: list[DeliveryWarning]) -> tuple[DeliveryWarning, ...]:
    return tuple(dict.fromkeys(items))


class WindowsDeliveryAdapter:
    """Schema-v1 Windows delivery adapter with exactly-once input claims.

    The existing keyboard manager is injected so tests can use disposable
    clipboard/focus implementations without installing hooks or touching the
    owner's clipboard.
    """

    supports_incremental_delivery = True
    supports_modifier_wait = True

    def __init__(
        self,
        *,
        keyboard_manager: object,
        logger: logging.Logger,
        clipboard: object,
        timer_factory: Callable[[float, Callable[[], None]], object],
        sleep: Callable[[float], None],
        clipboard_behavior: bool,
        clipboard_clear_delay: float,
        keystroke_delay_ms: int = 0,
    ) -> None:
        self._keyboard = keyboard_manager
        self._logger = logger
        self._clipboard = clipboard
        self._timer_factory = timer_factory
        self._sleep = sleep
        self._clipboard_behavior = bool(clipboard_behavior)
        self._clipboard_clear_delay = max(0.1, float(clipboard_clear_delay))
        self._keystroke_delay_ms = max(0, int(keystroke_delay_ms))

        self._claim_ledger = DeliveryClaimLedger()
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._targets: dict[str, _TargetBinding] = {}
        self._receipts: dict[tuple[str, int], DeliveryReceipt] = {}
        self._batches: dict[str, _BatchState] = {}
        self._pending_restores: list[_PendingClipboardRestore] = []
        self._synthetic_modifiers: set[object] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def pending_restore_count(self) -> int:
        with self._state_lock:
            return len(self._pending_restores)

    @property
    def active_batch_count(self) -> int:
        with self._state_lock:
            return len(self._batches)

    def resolve_target(
        self,
        request_id: str,
        generation: int,
        route: DeliveryRoute,
    ) -> DeliveryTargetOutcome:
        """Resolve the currently focused window as one opaque target."""
        if not isinstance(route, DeliveryRoute):
            raise ValueError("invalid delivery route")
        if self.closed:
            return DeliveryTargetOutcome(failure=DeliveryFailure.CLEANUP_FAILED)

        opaque_id = f"windows-delivery-{uuid.uuid4().hex}"
        target = DeliveryTarget(
            route=DeliveryRoute.FOCUSED,
            opaque_id=opaque_id,
            generation=generation,
        )
        binding = _TargetBinding(target=target)
        with self._state_lock:
            if self._closed:
                return DeliveryTargetOutcome(failure=DeliveryFailure.CLEANUP_FAILED)
            self._targets[opaque_id] = binding
        return DeliveryTargetOutcome(target=target)

    def _take_binding(self, target: DeliveryTarget) -> _TargetBinding | None:
        with self._state_lock:
            binding = self._targets.pop(target.opaque_id, None)
        if binding is None or binding.target != target:
            return None
        return binding

    def _existing_or_reused_receipt(
        self, request: DeliveryRequest
    ) -> DeliveryReceipt | None:
        claim = self._claim_ledger.claim(request.request_id, request.generation)
        if claim is DeliveryClaimStatus.CLAIMED:
            return None
        with self._state_lock:
            receipt = self._receipts.get((request.request_id, request.generation))
        if receipt is not None:
            return receipt
        return self._failed_receipt(
            request,
            DeliveryFailure.STALE_GENERATION,
            route=request.target.route,
        )

    def deliver(
        self,
        request: DeliveryRequest,
        *,
        wait_for_modifiers: bool = True,
    ) -> DeliveryReceipt:
        """Attempt one already-processed payload exactly once."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log request.processed_text or a native target. Receipts and logs
        # below contain only counts, style, route, status, warnings, and failures.
        # ============================================================================
        with self._operation_lock:
            reused = self._existing_or_reused_receipt(request)
            if reused is not None:
                self._take_binding(request.target)
                return reused
            binding = self._take_binding(request.target)
            if binding is None:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.STALE_GENERATION,
                    route=request.target.route,
                )
            elif self.closed:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.CLEANUP_FAILED,
                    route=binding.target.route,
                )
            else:
                preparation = self._prepare_delivery(request)
                unit = self._deliver_unit(
                    binding,
                    style=preparation.effective_style,
                    processed_text=request.processed_text,
                    submit=request.submit,
                    wait_for_modifiers=wait_for_modifiers,
                    warnings=preparation.warnings,
                    snapshot_fallback=preparation.snapshot_fallback,
                )
                self._schedule_clipboard_restore(preparation.saved_clipboard)
                receipt = DeliveryReceipt(
                    request_id=request.request_id,
                    generation=request.generation,
                    route=binding.target.route,
                    status=unit.status,
                    failure=unit.failure,
                    warnings=_warnings_tuple(unit.warnings),
                    units_attempted=1,
                    units_completed=1 if unit.completed else 0,
                    clipboard_restored=(
                        None
                        if preparation.saved_clipboard is not None
                        else (True if request.style is DeliveryStyle.CLIPBOARD else None)
                    ),
                    modifiers_restored=unit.modifiers_restored,
                )
            with self._state_lock:
                self._receipts[(request.request_id, request.generation)] = receipt
            self._log_receipt(receipt, request.style)
            return receipt

    def begin_batch(self, request: DeliveryRequest) -> WindowsDeliveryBatchStart:
        """Claim one incremental delivery and save its clipboard exactly once."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The request payload is used only by the caller's first unit and is not
        # retained in batch state.
        # ============================================================================
        with self._operation_lock:
            reused = self._existing_or_reused_receipt(request)
            if reused is not None:
                self._take_binding(request.target)
                return WindowsDeliveryBatchStart(receipt=reused)
            binding = self._take_binding(request.target)
            if binding is None:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.STALE_GENERATION,
                    route=request.target.route,
                )
                with self._state_lock:
                    self._receipts[(request.request_id, request.generation)] = receipt
                return WindowsDeliveryBatchStart(receipt=receipt)
            if self.closed:
                receipt = self._failed_receipt(
                    request,
                    DeliveryFailure.CLEANUP_FAILED,
                    route=binding.target.route,
                )
                with self._state_lock:
                    self._receipts[(request.request_id, request.generation)] = receipt
                return WindowsDeliveryBatchStart(receipt=receipt)

            preparation = self._prepare_delivery(request)
            token = uuid.uuid4().hex
            batch = _BatchState(
                token=token,
                request_id=request.request_id,
                generation=request.generation,
                binding=binding,
                style=request.style,
                effective_style=preparation.effective_style,
                snapshot_fallback=preparation.snapshot_fallback,
                restore_clipboard=request.restore_clipboard,
                saved_clipboard=preparation.saved_clipboard,
                warnings=preparation.warnings,
            )
            with self._state_lock:
                self._batches[token] = batch
            return WindowsDeliveryBatchStart(token=token)

    def deliver_batch_unit(
        self,
        token: str,
        processed_text: str,
        *,
        last: bool,
    ) -> DeliveryReceipt:
        """Attempt one incremental unit without re-resolving or re-claiming."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never retain or log processed_text.
        # ============================================================================
        if not isinstance(processed_text, str):
            raise ValueError("processed_text must be a string")
        with self._operation_lock:
            with self._state_lock:
                batch = self._batches.get(token)
            if batch is None:
                raise ValueError("unknown delivery batch")
            if self.closed:
                return self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )

            batch.units_attempted += 1
            unit = self._deliver_unit(
                batch.binding,
                style=batch.effective_style,
                processed_text=processed_text,
                submit=False,
                wait_for_modifiers=False,
                warnings=batch.warnings,
                snapshot_fallback=batch.snapshot_fallback,
            )
            batch.warnings = unit.warnings
            batch.modifiers_restored = (
                batch.modifiers_restored and unit.modifiers_restored
            )
            if unit.completed:
                batch.units_completed += 1
            receipt = DeliveryReceipt(
                request_id=batch.request_id,
                generation=batch.generation,
                route=batch.binding.target.route,
                status=unit.status,
                failure=unit.failure,
                warnings=_warnings_tuple(batch.warnings),
                units_attempted=batch.units_attempted,
                units_completed=batch.units_completed,
                clipboard_restored=None,
                modifiers_restored=batch.modifiers_restored,
            )
            terminal_failure = unit.failure
            terminal_status = unit.status
            if not unit.completed and terminal_failure is None:
                # A standalone focused attempt remains best effort, but an
                # incremental unit with no accepted input must terminate the
                # sequence so later units cannot create or promote a partial
                # delivery.
                terminal_failure = DeliveryFailure.INPUT_FAILED
                terminal_status = DeliveryStatus.FAILED
            if terminal_failure is not None or last:
                receipt = self._finish_batch(
                    batch,
                    status=terminal_status,
                    failure=terminal_failure,
                    add_attempt=False,
                )
            self._log_receipt(receipt, batch.style)
            return receipt

    def abort_batch(
        self,
        token: str,
        *,
        cancelled: bool = False,
    ) -> DeliveryReceipt | None:
        """End an incremental request once and restore its prior clipboard."""
        with self._operation_lock:
            with self._state_lock:
                batch = self._batches.get(token)
            if batch is None:
                return None
            return self._finish_batch(
                batch,
                status=(
                    DeliveryStatus.CANCELLED if cancelled else DeliveryStatus.FAILED
                ),
                failure=(None if cancelled else DeliveryFailure.NATIVE_ERROR),
                add_attempt=not cancelled,
            )

    def _finish_batch(
        self,
        batch: _BatchState,
        *,
        status: DeliveryStatus,
        failure: DeliveryFailure | None,
        add_attempt: bool,
    ) -> DeliveryReceipt:
        with self._state_lock:
            self._batches.pop(batch.token, None)
        if add_attempt:
            batch.units_attempted = max(
                batch.units_attempted,
                batch.units_completed + 1,
            )
        self._schedule_clipboard_restore(batch.saved_clipboard)
        receipt = DeliveryReceipt(
            request_id=batch.request_id,
            generation=batch.generation,
            route=batch.binding.target.route,
            status=status,
            failure=failure,
            warnings=_warnings_tuple(batch.warnings),
            units_attempted=batch.units_attempted,
            units_completed=batch.units_completed,
            clipboard_restored=(
                None if batch.saved_clipboard is not None else True
            ),
            modifiers_restored=batch.modifiers_restored,
        )
        with self._state_lock:
            self._receipts[(batch.request_id, batch.generation)] = receipt
        return receipt

    def cancel(self, request_id: str, generation: int) -> DeliveryReceipt:
        """Cancel a claimed batch or reserve an undelivered generation."""
        with self._operation_lock:
            key = (request_id, generation)
            with self._state_lock:
                existing = self._receipts.get(key)
                batch = next(
                    (
                        item
                        for item in self._batches.values()
                        if (item.request_id, item.generation) == key
                    ),
                    None,
                )
            if existing is not None:
                return existing
            if batch is not None:
                return self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )
            claim = self._claim_ledger.claim(request_id, generation)
            if claim is DeliveryClaimStatus.REUSED:
                with self._state_lock:
                    existing = self._receipts.get(key)
                if existing is not None:
                    return existing
            receipt = DeliveryReceipt(
                request_id=request_id,
                generation=generation,
                route=DeliveryRoute.FOCUSED,
                status=DeliveryStatus.CANCELLED,
                units_attempted=0,
                units_completed=0,
                clipboard_restored=True,
            )
            with self._state_lock:
                self._receipts[key] = receipt
            return receipt

    def close(self) -> None:
        """Restore adapter-owned state and prevent any later input attempt."""
        with self._operation_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._closed = True
                batches = tuple(self._batches.values())
            for batch in batches:
                self._finish_batch(
                    batch,
                    status=DeliveryStatus.CANCELLED,
                    failure=None,
                    add_attempt=False,
                )
            self._restore_all_pending_clipboards()
            self._release_synthetic_modifiers()
            with self._state_lock:
                self._targets.clear()

    @dataclass(slots=True)
    class _UnitOutcome:
        status: DeliveryStatus
        failure: DeliveryFailure | None
        completed: bool
        warnings: list[DeliveryWarning]
        modifiers_restored: bool

    def _deliver_unit(
        self,
        binding: _TargetBinding,
        *,
        style: DeliveryStyle,
        processed_text: str,
        submit: bool,
        wait_for_modifiers: bool,
        warnings: list[DeliveryWarning],
        snapshot_fallback: bool,
    ) -> _UnitOutcome:
        warnings = list(warnings)
        if style is DeliveryStyle.CLIPBOARD:
            try:
                self._clipboard.copy(processed_text)
            except Exception as exc:  # noqa: BLE001 - clipboard failures are typed
                warnings.append(DeliveryWarning.CLIPBOARD_COPY_FAILED)
                self._logger.error(
                    "clipboard copy failed reason=%s", _short_exc(exc)
                )
                return self._UnitOutcome(
                    DeliveryStatus.FOCUSED_ATTEMPTED,
                    None,
                    False,
                    warnings,
                    True,
                )
            self._sleep(0.05)

        held = self._snapshot_modifiers(wait_for_modifiers)
        input_result = self._send_input(
            held,
            style=style,
            processed_text=processed_text,
            submit=submit,
        )
        warnings.extend(input_result.warnings)
        if snapshot_fallback and not input_result.delivered:
            return self._UnitOutcome(
                DeliveryStatus.FAILED,
                DeliveryFailure.INPUT_FAILED,
                False,
                warnings,
                input_result.modifiers_restored,
            )
        return self._UnitOutcome(
            DeliveryStatus.FOCUSED_ATTEMPTED,
            None,
            input_result.delivered,
            warnings,
            input_result.modifiers_restored,
        )

    def _snapshot_modifiers(self, wait_for_modifiers: bool) -> tuple[object, ...]:
        try:
            held_before = tuple(self._keyboard.snapshot_held_modifiers())
        except Exception:  # noqa: BLE001 - delivery remains bounded and typed
            return ()
        if not wait_for_modifiers or not held_before:
            return held_before
        self._sleep(0.25)
        try:
            held = tuple(self._keyboard.snapshot_held_modifiers())
        except Exception:  # noqa: BLE001
            return held_before
        for key in held_before:
            if key not in held:
                self._logger.info("modifier guard released")
        for _key in held:
            self._logger.warning("modifier guard timeout")
        return held

    def _send_input(
        self,
        held: tuple[object, ...],
        *,
        style: DeliveryStyle,
        processed_text: str,
        submit: bool,
    ) -> _InputResult:
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log processed_text. It crosses only the direct typing call.
        # ============================================================================
        result = _InputResult(delivered=False)
        try:
            suppress = self._keyboard.suppress_self_events()
            with suppress:
                try:
                    for key in held:
                        try:
                            self._keyboard._controller.release(key)
                        except Exception:  # noqa: BLE001 - record and keep cleanup going
                            result.modifiers_restored = False
                            result.warnings.append(
                                DeliveryWarning.MODIFIER_RESTORE_FAILED
                            )

                    try:
                        if style is DeliveryStyle.KEYSTROKE:
                            self._keyboard.type_text(
                                processed_text,
                                delay_ms=self._keystroke_delay_ms,
                            )
                        else:
                            self._keyboard.send("shift+insert")
                        result.delivered = True
                    except Exception as exc:  # noqa: BLE001 - no retry after input
                        self._logger.error(
                            "delivery input failed style=%s reason=%s",
                            style.value,
                            _short_exc(exc),
                        )
                finally:
                    for key in held:
                        try:
                            physically_down = bool(
                                self._keyboard.modifier_physically_down(key)
                            )
                        except Exception:  # noqa: BLE001
                            physically_down = False
                            result.modifiers_restored = False
                            result.warnings.append(
                                DeliveryWarning.MODIFIER_RESTORE_FAILED
                            )
                        if not physically_down:
                            continue
                        try:
                            self._keyboard._controller.press(key)
                            with self._state_lock:
                                self._synthetic_modifiers.add(key)
                        except Exception:  # noqa: BLE001
                            result.modifiers_restored = False
                            result.warnings.append(
                                DeliveryWarning.MODIFIER_RESTORE_FAILED
                            )
                if submit and result.delivered:
                    try:
                        self._keyboard.send("enter")
                    except Exception as exc:  # noqa: BLE001
                        self._logger.error(
                            "submit enter send failed reason=%s", _short_exc(exc)
                        )
                        result.delivered = False
        except Exception as exc:  # noqa: BLE001 - suppressor failure is input failure
            self._logger.error("delivery suppression failed reason=%s", _short_exc(exc))
            result.delivered = False
        result.warnings = list(dict.fromkeys(result.warnings))
        return result

    def _prepare_delivery(self, request: DeliveryRequest) -> _DeliveryPreparation:
        if (
            request.style is not DeliveryStyle.CLIPBOARD
            or not request.restore_clipboard
            or not self._clipboard_behavior
        ):
            return _DeliveryPreparation(effective_style=request.style)
        try:
            saved = self._clipboard.paste()
        except Exception:  # noqa: BLE001 - substitute once before input
            self._logger.warning("clipboard snapshot unavailable")
            return _DeliveryPreparation(
                effective_style=DeliveryStyle.KEYSTROKE,
                snapshot_fallback=True,
                warnings=[DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK],
            )
        if not isinstance(saved, str):
            self._logger.warning("clipboard snapshot unavailable")
            return _DeliveryPreparation(
                effective_style=DeliveryStyle.KEYSTROKE,
                snapshot_fallback=True,
                warnings=[DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK],
            )
        return _DeliveryPreparation(
            effective_style=DeliveryStyle.CLIPBOARD,
            saved_clipboard=saved,
        )

    def update_clipboard_settings(self, *, behavior: bool, clear_delay: float) -> None:
        """Update future deliveries without cancelling owned restoration work."""
        with self._state_lock:
            self._clipboard_behavior = bool(behavior)
            self._clipboard_clear_delay = max(0.1, float(clear_delay))

    def _schedule_clipboard_restore(self, saved: str | None) -> None:
        # A saved snapshot belongs to an admitted transaction, even if the
        # preference changes before that transaction settles.
        if saved is None:
            return
        with self._state_lock:
            delay = self._clipboard_clear_delay
        pending = _PendingClipboardRestore(saved=saved)

        def restore() -> None:
            self._restore_pending(pending)

        try:
            pending.timer = self._timer_factory(
                delay,
                restore,
            )
            with self._state_lock:
                self._pending_restores.append(pending)
            pending.timer.daemon = True
            pending.timer.start()
        except Exception as exc:  # noqa: BLE001 - restore synchronously on timer failure
            self._logger.warning(
                "clipboard restore schedule failed reason=%s", _short_exc(exc)
            )
            self._restore_pending(pending)

    def _restore_pending(self, pending: _PendingClipboardRestore) -> None:
        with self._state_lock:
            if pending not in self._pending_restores:
                # A timer-construction failure restores a record that was never
                # registered; allow that exact one attempt.
                if pending.timer is not None:
                    return
            else:
                self._pending_restores.remove(pending)
        try:
            self._clipboard.copy(pending.saved)
        except Exception as exc:  # noqa: BLE001 - content-free warning only
            self._logger.warning(
                "clipboard restore failed reason=%s", _short_exc(exc)
            )

    def _restore_all_pending_clipboards(self) -> None:
        with self._state_lock:
            pending = tuple(self._pending_restores)
            self._pending_restores.clear()
        if not pending:
            return
        for item in pending:
            try:
                if item.timer is not None:
                    item.timer.cancel()
            except Exception:  # noqa: BLE001 - continue exact restoration
                pass
        # The first pending delivery observed the true pre-Whisperstream value.
        # Later pending transactions may have saved an intermediate payload.
        try:
            self._clipboard.copy(pending[0].saved)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "clipboard close restore failed reason=%s", _short_exc(exc)
            )

    def _release_synthetic_modifiers(self) -> None:
        with self._state_lock:
            modifiers = tuple(self._synthetic_modifiers)
            self._synthetic_modifiers.clear()
        if not modifiers:
            return
        try:
            with self._keyboard.suppress_self_events():
                for key in modifiers:
                    try:
                        self._keyboard._controller.release(key)
                    except Exception:  # noqa: BLE001 - exact best-effort release
                        self._logger.warning("modifier cleanup failed")
        except Exception:  # noqa: BLE001
            self._logger.warning("modifier cleanup suppression failed")

    def _failed_receipt(
        self,
        request: DeliveryRequest,
        failure: DeliveryFailure,
        *,
        route: DeliveryRoute,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            request_id=request.request_id,
            generation=request.generation,
            route=route,
            status=DeliveryStatus.FAILED,
            failure=failure,
            units_attempted=0,
            units_completed=0,
            clipboard_restored=True,
        )

    def _log_receipt(
        self, receipt: DeliveryReceipt, style: DeliveryStyle
    ) -> None:
        self._logger.info(
            "delivery result=%s route=%s style=%s failure=%s warnings=%d attempted=%d completed=%d",
            receipt.status.value,
            receipt.route.value,
            style.value,
            receipt.failure.value if receipt.failure is not None else "none",
            len(receipt.warnings),
            receipt.units_attempted,
            receipt.units_completed,
        )


def _short_exc(exc: BaseException) -> str:
    msg = str(exc)
    return msg[:80].replace("\n", " ") if msg else type(exc).__name__


__all__ = [
    "WindowsDeliveryAdapter",
    "WindowsDeliveryBatchStart",
]
