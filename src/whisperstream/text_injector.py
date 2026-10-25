from __future__ import annotations

import importlib
import logging
import platform
import re
import time
from threading import Lock, Timer as _Timer
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, Tuple

from .delivery_contract import (
    DeliveryFailure as ContractDeliveryFailure,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus as ContractDeliveryStatus,
    DeliveryStyle,
)

if TYPE_CHECKING:
    from .keyboard_shortcuts import KeyboardShortcutManager


class _LazyPyperclip:
    """Keep clipboard frameworks off file-only startup and bundle probes."""

    _module: Any | None = None

    def __getattr__(self, name: str) -> Any:
        module = self._module
        if module is None:
            module = importlib.import_module("pyperclip")
            self._module = module
        return getattr(module, name)


# Existing tests and delivery call sites patch/use this module-shaped name.
# The real clipboard dependency loads only when a clipboard operation occurs.
pyperclip = _LazyPyperclip()


class DeliveryStatus(str, Enum):
    """Public destination outcome for one single or stored delivery."""

    FOCUSED = "focused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Strict, content-free result returned by text-delivery entry points."""

    status: DeliveryStatus
    reason: ContractDeliveryFailure | None = None
    receipt: DeliveryReceipt | None = dataclass_field(
        default=None, repr=False, compare=False
    )

    @property
    def ok(self) -> bool:
        return self.status is DeliveryStatus.FOCUSED

    def __bool__(self) -> bool:
        """Keep stored-delivery call sites falsey on an attempted failure."""
        return self.ok

    @classmethod
    def focused(cls) -> DeliveryOutcome:
        return cls(DeliveryStatus.FOCUSED)

    @classmethod
    def failed(cls, reason: ContractDeliveryFailure) -> DeliveryOutcome:
        return cls(DeliveryStatus.FAILED, reason)

    @classmethod
    def cancelled(cls, receipt: DeliveryReceipt | None = None) -> DeliveryOutcome:
        return cls(DeliveryStatus.CANCELLED, receipt=receipt)

    @classmethod
    def blocked(cls) -> DeliveryOutcome:
        """Return a content-free policy denial."""
        return cls(DeliveryStatus.BLOCKED)


@dataclass(slots=True)
class StyleGuide:
    sentence_case: bool = False
    uppercase: bool = False
    lowercase: bool = False
    prepend: str = ""
    append: str = ""

    @classmethod
    def from_prompt(cls, prompt: str) -> "StyleGuide":
        guide = cls()
        for raw_line in prompt.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if not line:
                continue
            if lower in {"sentence case", "sentence-case", "capitalize sentences"}:
                guide.sentence_case = True
            elif lower in {"uppercase", "upper"}:
                guide.uppercase = True
                guide.sentence_case = False
                guide.lowercase = False
            elif lower in {"lowercase", "lower"}:
                guide.lowercase = True
                guide.uppercase = False
                guide.sentence_case = False
            elif lower.startswith("prepend:"):
                guide.prepend = line.partition(":")[2].strip()
            elif lower.startswith("append:"):
                guide.append = line.partition(":")[2].strip()
        return guide

    def apply(self, text: str, *, do_prepend: bool = True, do_append: bool = True) -> str:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        # do_prepend / do_append let the chunked-cleanup path apply prepend only to
        # the first chunk and append only to the last, so the affixes don't repeat
        # on every chunk. Defaults keep single-call callers unchanged.
        result = text
        if self.uppercase:
            result = result.upper()
        elif self.lowercase:
            result = result.lower()
        elif self.sentence_case:
            result = _sentence_case(result)
        if self.prepend and do_prepend:
            result = f"{self.prepend} {result}".strip()
        if self.append and do_append:
            result = f"{result} {self.append}".strip()
        return result

    def active_directive_names(self) -> List[str]:
        """Return the names of active directives (never their payload values)."""
        names: List[str] = []
        if self.sentence_case:
            names.append("sentence_case")
        if self.uppercase:
            names.append("uppercase")
        if self.lowercase:
            names.append("lowercase")
        if self.prepend:
            names.append("prepend")
        if self.append:
            names.append("append")
        return names


class TextInjector:
    def __init__(
        self,
        *,
        keyboard_manager: KeyboardShortcutManager,
        logger: logging.Logger,
        word_filters: List[str],
        word_replacements: Dict[str, str],
        post_processing: str,
        clipboard_behavior: bool,
        clipboard_clear_delay: float,
        # Input style: "clipboard" (default, current behavior) or "keystroke"
        # (type char-by-char via pynput, never touches the clipboard).
        # The per-call override on inject/paste_last/paste_chunk takes precedence
        # over this construction-time default when supplied (Task 1.5 threads the
        # resolved effective style in from main.py).
        input_style: str = "clipboard",
        keystroke_delay_ms: int = 0,
        delivery_adapter: object | None = None,
        content_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._keyboard = keyboard_manager
        self._logger = logger
        # Filter stage: words that should be removed from the transcript entirely,
        # along with any surrounding comma+whitespace so disfluencies vanish cleanly.
        self._word_filters = [w.lower() for w in word_filters if w.strip()]
        # Replacement stage: source -> target substitution for literal word tokens.
        # Non-empty target is required; empty values are rejected at this layer.
        self._word_replacements = {
            k.lower(): v for k, v in word_replacements.items() if k.strip() and v
        }
        self._filter_pattern = self._build_filter_pattern(self._word_filters)
        self._replacement_pattern = self._build_replacement_pattern(self._word_replacements)
        self._style = StyleGuide.from_prompt(post_processing)
        self._clipboard_behavior = clipboard_behavior
        self._clipboard_clear_delay = max(0.1, clipboard_clear_delay)
        self._input_style = input_style
        self._keystroke_delay_ms = keystroke_delay_ms
        # Production supplies the exact acceptance authority. Direct utility
        # construction retains the historical allow-by-default behavior.
        self._content_allowed_callback = content_allowed
        self._last_transcript: Optional[str] = None
        self._last_original: Optional[str] = None
        self._delivery_identity_lock = Lock()
        self._delivery_generation = 0
        if delivery_adapter is None:
            system = platform.system()
            if system == "Windows":
                adapter_type = importlib.import_module(
                    ".windows_delivery_adapter", __package__
                ).WindowsDeliveryAdapter
                delivery_adapter = adapter_type(
                    keyboard_manager=keyboard_manager,
                    logger=logger,
                    clipboard=pyperclip,
                    timer_factory=_Timer,
                    sleep=time.sleep,
                    clipboard_behavior=clipboard_behavior,
                    clipboard_clear_delay=clipboard_clear_delay,
                    keystroke_delay_ms=keystroke_delay_ms,
                )
            elif system == "Darwin":
                adapter_type = importlib.import_module(
                    ".macos_delivery_adapter", __package__
                ).MacosDeliveryAdapter
                delivery_adapter = adapter_type(
                    keyboard_manager=keyboard_manager,
                    logger=logger,
                    timer_factory=_Timer,
                    sleep=time.sleep,
                    clipboard_behavior=clipboard_behavior,
                    clipboard_clear_delay=clipboard_clear_delay,
                    keystroke_delay_ms=keystroke_delay_ms,
                )
            else:
                raise RuntimeError("native delivery adapter is unavailable")
        self._delivery = delivery_adapter
        # Batch-paste state contains portable identity and processed-unit
        # accumulation only. Clipboard, modifier, and native target state stays
        # inside the selected platform adapter.
        self._batch_accum: List[str] = []
        self._batch_saved: Optional[str] = None
        self._batch_active = False
        self._batch_outcome: DeliveryOutcome | None = None
        self._batch_style: str | None = None
        self._batch_token: str | None = None
        self._batch_request_id: str | None = None
        self._batch_generation: int | None = None

    def _content_allowed(self) -> bool:
        """Return the current processing decision; a broken authority fails closed."""
        callback = self._content_allowed_callback
        if callback is None:
            return True
        try:
            return bool(callback())
        except Exception:  # noqa: BLE001 - policy failures must deny content work
            return False

    @property
    def last_transcript(self) -> Optional[str]:
        """The processed text from the most recent inject() call, or None."""
        return self._last_transcript

    @property
    def last_original(self) -> Optional[str]:
        """The pre-enhancement processed text stored by remember_original(), or None."""
        return self._last_original

    def update_clipboard_settings(self, *, behavior: bool, clear_delay: float) -> None:
        """Apply live clipboard preferences while retaining delivery identity."""
        self._delivery.update_clipboard_settings(
            behavior=behavior, clear_delay=clear_delay
        )
        self._clipboard_behavior = bool(behavior)
        self._clipboard_clear_delay = max(0.1, float(clear_delay))

    def process(self, text: str, *, do_prepend: bool = True, do_append: bool = True) -> str:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._content_allowed():
            return ""

        # do_prepend / do_append flow to the style guide so the chunked path can
        # apply prepend on the first chunk and append on the last only. Defaults
        # keep single-call callers (and inject()) unchanged.
        # Sanitize input: remove non-printable characters (e.g. control codes) to prevent injection
        result = self._sanitize(text, strip_text=True)

        if not result:
            return result

        # Filters must run before replacements so a filtered word (e.g. "um")
        # can never be picked up as a replacement source.
        result, filters_triggered = self._apply_word_filters_counted(result)
        result, replacements_made = self._apply_word_replacements_counted(result)

        # Log dictionary activity (counts only — no matched text)
        self._logger.debug("dictionary filters_triggered=%d", filters_triggered)
        self._logger.debug("dictionary replacements=%d", replacements_made)

        result = _normalize_punctuation(result)
        result = self._style.apply(result, do_prepend=do_prepend, do_append=do_append)

        # Log post-processing directives (names only — never payload values)
        active_directives = self._style.active_directive_names()
        self._logger.debug(
            "post_processing directives_applied=%d directives=%s",
            len(active_directives),
            ",".join(active_directives) if active_directives else "none",
        )

        # Final sanitization: ensure no unsafe characters were introduced by replacements or styling
        # Note: Do not strip output, as replacements or styling might intentionally add whitespace.
        return self._sanitize(result, strip_text=False)

    @staticmethod
    def _sanitize(text: str, strip_text: bool = True) -> str:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        safe_chars = [ch for ch in text if ch.isprintable() or ch in " \t\n"]
        result = "".join(safe_chars)
        return result.strip() if strip_text else result

    def _next_delivery_identity(
        self,
        *,
        source: str,
        request_id: str | None,
        generation: int | None,
    ) -> tuple[str, int]:
        if (request_id is None) != (generation is None):
            raise ValueError("request_id and generation must be supplied together")
        if request_id is not None and generation is not None:
            return request_id, generation
        with self._delivery_identity_lock:
            self._delivery_generation += 1
            current = self._delivery_generation
        return f"{source}-{current}", current

    @staticmethod
    def _delivery_style(style: str) -> DeliveryStyle:
        return (
            DeliveryStyle.KEYSTROKE
            if style == DeliveryStyle.KEYSTROKE.value
            else DeliveryStyle.CLIPBOARD
        )

    def _target_failure_receipt(
        self,
        *,
        request_id: str,
        generation: int,
        failure: ContractDeliveryFailure,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            request_id=request_id,
            generation=generation,
            route=DeliveryRoute.FOCUSED,
            status=ContractDeliveryStatus.FAILED,
            failure=failure,
            units_attempted=0,
            units_completed=0,
            clipboard_restored=True,
        )

    @staticmethod
    def _outcome_from_receipt(receipt: DeliveryReceipt) -> DeliveryOutcome:
        if receipt.status is ContractDeliveryStatus.FOCUSED_ATTEMPTED:
            return DeliveryOutcome(DeliveryStatus.FOCUSED, receipt=receipt)
        if receipt.status is ContractDeliveryStatus.CANCELLED:
            return DeliveryOutcome.cancelled(receipt)
        return DeliveryOutcome(
            DeliveryStatus.FAILED,
            receipt.failure or ContractDeliveryFailure.NATIVE_ERROR,
            receipt,
        )

    def _log_delivery_outcome(
        self,
        outcome: DeliveryOutcome,
        *,
        source: str = "inject",
        submit: bool = False,
    ) -> None:
        """Log only typed route metadata, never transcript or target identity."""
        reason = outcome.reason.value if outcome.reason is not None else "none"
        self._logger.info(
            "inject route=%s result=%s reason=%s source=%s submit=%s",
            outcome.status.value,
            "ok" if outcome.ok else "failed",
            reason,
            source,
            submit,
        )

    def inject_processed(
        self,
        processed: str,
        *,
        input_style: str | None = None,
        submit: bool = False,
        request_id: str | None = None,
        generation: int | None = None,
        source: str = "inject",
        remember_transcript: bool = True,
        wait_for_modifiers: bool = True,
    ) -> DeliveryOutcome:
        """Deliver one already-processed payload through schema v1 exactly once."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # ============================================================================
        if not self._content_allowed():
            return DeliveryOutcome.blocked()
        if not isinstance(processed, str):
            raise ValueError("processed must be a string")
        style = self._delivery_style(input_style or self._input_style)
        request_id, generation = self._next_delivery_identity(
            source=source,
            request_id=request_id,
            generation=generation,
        )
        target_outcome = self._delivery.resolve_target(
            request_id,
            generation,
            DeliveryRoute.FOCUSED,
        )
        if target_outcome.failure is not None:
            receipt = self._target_failure_receipt(
                request_id=request_id,
                generation=generation,
                failure=target_outcome.failure,
            )
            outcome = self._outcome_from_receipt(receipt)
            if remember_transcript and self._content_allowed():
                self._last_transcript = processed
            self._log_delivery_outcome(outcome, source=source, submit=submit)
            return outcome
        assert target_outcome.target is not None
        if not self._content_allowed():
            return DeliveryOutcome.blocked()
        submit_now = submit
        request = DeliveryRequest(
            request_id=request_id,
            generation=generation,
            target=target_outcome.target,
            style=style,
            processed_text=processed,
            restore_clipboard=self._clipboard_behavior,
            submit=submit_now,
        )
        if bool(getattr(self._delivery, "supports_modifier_wait", False)):
            receipt = self._delivery.deliver(
                request,
                wait_for_modifiers=wait_for_modifiers,
            )
        else:
            receipt = self._delivery.deliver(request)
        outcome = self._outcome_from_receipt(receipt)
        if remember_transcript and self._content_allowed():
            self._last_transcript = processed
        self._log_delivery_outcome(
            outcome,
            source=source,
            submit=submit_now,
        )
        return outcome

    def inject(
        self,
        text: str,
        *,
        input_style: str | None = None,
        submit: bool = False,
        request_id: str | None = None,
        generation: int | None = None,
        already_processed: bool = False,
    ) -> DeliveryOutcome:
        """Process once, then hand one generation to the delivery adapter."""
        if not self._content_allowed():
            return DeliveryOutcome.blocked()
        return self.inject_processed(
            text if already_processed else self.process(text),
            input_style=input_style,
            submit=submit,
            request_id=request_id,
            generation=generation,
        )

    def remember_original(self, raw_text: str) -> None:
        """Store the pre-enhancement processed text for paste_last_original().

        Runs raw_text through process() so the stored value matches exactly what
        the user would get with enhancement turned off. Mirrors how inject() stores
        the enhanced processed text in _last_transcript.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._content_allowed():
            return
        processed = self.process(raw_text)
        if self._content_allowed():
            self._last_original = processed

    def _paste_stored(
        self, text: Optional[str], *, source: str, input_style: str | None = None
    ) -> DeliveryOutcome | None:
        """Deliver stored, already-processed text through the same adapter."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log or serialize stored text.
        # ============================================================================
        if not self._content_allowed():
            return DeliveryOutcome.blocked()
        if text is None:
            return None
        return self.inject_processed(
            text,
            input_style=input_style,
            source=source,
            remember_transcript=False,
            wait_for_modifiers=False,
        )

    def paste_last(
        self, *, input_style: str | None = None
    ) -> DeliveryOutcome | None:
        """Paste the most recent transcript again.

        In clipboard mode: snapshots the existing text clipboard value, copies
        the last processed transcript, sends the paste keystroke, and schedules
        restore. If that text snapshot is unavailable, the adapter leaves the
        clipboard untouched and selects direct typing before input.
        In keystroke mode: types the stored transcript via type_text, never touches
        the clipboard.

        Returns a typed outcome when a transcript was available, or ``None`` if
        no transcript has been stored yet.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        return self._paste_stored(
            self._last_transcript, source="paste_last", input_style=input_style
        )

    def paste_last_original(
        self, *, input_style: str | None = None
    ) -> DeliveryOutcome | None:
        """Paste the pre-enhancement version of the most recent transcript.

        Honors the resolved input style exactly like paste_last (clipboard paste or
        keystroke typing). Returns a typed outcome when an original was
        available, or ``None`` if remember_original() has not been called yet.
        """
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        return self._paste_stored(
            self._last_original, source="paste_last_original", input_style=input_style
        )

    def _paste_chunk_v1(
        self,
        processed: str,
        *,
        first: bool,
        last: bool,
        input_style: str | None,
        request_id: str | None,
        generation: int | None,
    ) -> DeliveryOutcome:
        """Deliver one processed batch unit through an explicit adapter capability."""
        # ============================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # Never log or retain processed beyond the existing completed-batch
        # paste-last accumulator.
        # ============================================================================
        if not self._content_allowed():
            self.abort_chunked_paste()
            return DeliveryOutcome.blocked()
        if first:
            self.abort_chunked_paste()
            request_id, generation = self._next_delivery_identity(
                source="paste_chunk",
                request_id=request_id,
                generation=generation,
            )
            style = self._delivery_style(input_style or self._input_style)
            target_outcome = self._delivery.resolve_target(
                request_id,
                generation,
                DeliveryRoute.FOCUSED,
            )
            if target_outcome.failure is not None:
                receipt = self._target_failure_receipt(
                    request_id=request_id,
                    generation=generation,
                    failure=target_outcome.failure,
                )
                outcome = self._outcome_from_receipt(receipt)
                self._log_delivery_outcome(outcome, source="paste_chunk")
                self._reset_batch_state()
                return outcome
            assert target_outcome.target is not None
            piece = processed
            request = DeliveryRequest(
                request_id=request_id,
                generation=generation,
                target=target_outcome.target,
                style=style,
                processed_text=piece,
                restore_clipboard=self._clipboard_behavior,
            )
            if not bool(
                getattr(self._delivery, "supports_incremental_delivery", False)
            ):
                # Incremental cleanup must be an explicit platform capability.
                # Unsupported adapters fail without falling back to a second
                # focused delivery path.
                receipt = DeliveryReceipt(
                    request_id=request_id,
                    generation=generation,
                    route=target_outcome.target.route,
                    status=ContractDeliveryStatus.FAILED,
                    failure=ContractDeliveryFailure.UNSUPPORTED,
                    units_attempted=0,
                    units_completed=0,
                    clipboard_restored=True,
                )
                return self._outcome_from_receipt(receipt)
            started = self._delivery.begin_batch(request)
            if started.receipt is not None:
                outcome = self._outcome_from_receipt(started.receipt)
                self._reset_batch_state()
                return outcome
            assert started.token is not None
            self._batch_active = True
            self._batch_accum = []
            self._batch_saved = None
            self._batch_style = style.value
            self._batch_token = started.token
            self._batch_request_id = request_id
            self._batch_generation = generation
            self._batch_outcome = None
        else:
            if self._batch_outcome is not None and not self._batch_outcome.ok:
                outcome = self._batch_outcome
                if last:
                    self._reset_batch_state()
                return outcome
            if not self._batch_active or self._batch_token is None:
                receipt = DeliveryReceipt(
                    request_id=request_id or "paste_chunk-invalid",
                    generation=generation or 1,
                    route=DeliveryRoute.FOCUSED,
                    status=ContractDeliveryStatus.FAILED,
                    failure=ContractDeliveryFailure.STALE_GENERATION,
                    units_attempted=0,
                    units_completed=0,
                    clipboard_restored=True,
                )
                return self._outcome_from_receipt(receipt)
            if request_id is not None and request_id != self._batch_request_id:
                return DeliveryOutcome.failed(ContractDeliveryFailure.NATIVE_ERROR)
            if generation is not None and generation != self._batch_generation:
                return DeliveryOutcome.failed(ContractDeliveryFailure.NATIVE_ERROR)
            piece = f" {processed}"

        assert self._batch_token is not None
        completed_before = len(self._batch_accum)
        if not self._content_allowed():
            self.abort_chunked_paste()
            return DeliveryOutcome.blocked()
        receipt = self._delivery.deliver_batch_unit(
            self._batch_token,
            piece,
            last=last,
        )
        outcome = self._outcome_from_receipt(receipt)
        completed_this_unit = receipt.units_completed > completed_before
        if outcome.ok and not completed_this_unit:
            # Focused delivery remains best-effort at the portable receipt
            # boundary, but an incremental sequence cannot continue after an
            # input unit was not accepted. Latch a public failure so main stops
            # without promoting or retrying partial output.
            outcome = DeliveryOutcome(
                DeliveryStatus.FAILED,
                ContractDeliveryFailure.INPUT_FAILED,
                receipt,
            )
        self._batch_outcome = outcome
        if outcome.ok and completed_this_unit:
            self._batch_accum.append(piece)
        if last and outcome.ok and self._content_allowed():
            self._last_transcript = "".join(self._batch_accum)
        if last:
            self._reset_batch_state()
        self._log_delivery_outcome(outcome, source="paste_chunk")
        return outcome

    def _reset_batch_state(self) -> None:
        self._batch_active = False
        self._batch_saved = None
        self._batch_accum = []
        self._batch_outcome = None
        self._batch_style = None
        self._batch_token = None
        self._batch_request_id = None
        self._batch_generation = None

    def paste_chunk(
        self,
        processed: str,
        *,
        first: bool,
        last: bool,
        input_style: str | None = None,
        request_id: str | None = None,
        generation: int | None = None,
    ) -> DeliveryOutcome:
        """Deliver one already-processed cleanup unit through schema v1."""
        return self._paste_chunk_v1(
            processed,
            first=first,
            last=last,
            input_style=input_style,
            request_id=request_id,
            generation=generation,
        )

    def _finish_chunked_paste(self, *, remember_transcript: bool) -> None:
        """Close an active batch exactly once and release its clipboard state."""
        if not self._batch_active:
            return
        if remember_transcript and self._content_allowed():
            self._last_transcript = "".join(self._batch_accum)
        token = self._batch_token
        if token is not None and bool(
            getattr(self._delivery, "supports_incremental_delivery", False)
        ):
            self._delivery.abort_batch(token)
        self._reset_batch_state()

    def abort_chunked_paste(self) -> None:
        """Idempotently abandon a partial chunk sequence and restore clipboard.

        Unexpected enhancement or delivery failures can occur after the first
        chunk selected its effective delivery style but before a last=True call.
        Aborting closes that batch without promoting partial output to
        paste-last state.
        The delayed restore timer remains background work and does not block the
        dictation lifecycle terminal.
        """
        if not self._batch_active:
            return
        self._logger.info("paste_chunk aborted action=restore_clipboard")
        token = self._batch_token
        if token is not None and bool(
            getattr(self._delivery, "supports_incremental_delivery", False)
        ):
            self._delivery.abort_batch(token)
        self._reset_batch_state()

    def close(self) -> None:
        """Idempotently cancel delivery and restore adapter-owned state."""
        self.abort_chunked_paste()
        self._delivery.close()

    def _apply_word_filters(self, text: str) -> str:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        result, _ = self._apply_word_filters_counted(text)
        return result

    def _apply_word_filters_counted(self, text: str) -> Tuple[str, int]:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._filter_pattern:
            return text, 0

        matches = self._filter_pattern.findall(text)
        filters_triggered = len(matches)

        # Drop every filter match. Post-processing below then collapses any
        # orphan ", ," sequences (filter sat between two commas), double
        # spaces (filter sat between two words), and a dangling leading
        # comma+space (filter was the first token of a clause).
        result = self._filter_pattern.sub("", text)

        # Collapse ", ," (possibly with extra horizontal whitespace) down to a
        # single "," without consuming an intentional line break.
        result = re.sub(r",[^\S\r\n]*,", ",", result)
        # Collapse runs of horizontal whitespace introduced by the sub.
        result = re.sub(r"[^\S\r\n]+", " ", result)
        # Strip any leading comma+whitespace left over when the filter word
        # was the leading token of the sentence or clause.
        result = re.sub(r"^[^\S\r\n]*,[^\S\r\n]*", "", result)
        # Normalize horizontal whitespace immediately preceding punctuation.
        result = re.sub(r"[^\S\r\n]+([,.;!?])", r"\1", result)
        return result.strip(), filters_triggered

    def _apply_word_replacements(self, text: str) -> str:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        result, _ = self._apply_word_replacements_counted(text)
        return result

    def _apply_word_replacements_counted(self, text: str) -> Tuple[str, int]:
        # ============================================================================
        # TRANSCRIPT DATA — DO NOT LOG
        # MAKE SURE you do not log transcript data anywhere. If you add a new function
        # that receives, returns, or passes transcript text, COPY this banner to it.
        # Use `log_transcription_event()` from whisperstream.logger for metadata only
        # (char count, word count, duration, stage name). Never pass `text`,
        # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
        # ============================================================================
        if not self._replacement_pattern:
            return text, 0

        replacement_count = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal replacement_count
            replacement = self._word_replacements.get(match.group(0).lower(), match.group(0))
            if replacement != match.group(0):
                replacement_count += 1
            return replacement

        result = self._replacement_pattern.sub(_replace, text)
        return result, replacement_count

    @staticmethod
    def _build_filter_pattern(filters: List[str]) -> Optional[re.Pattern[str]]:
        if not filters:
            return None
        escaped = sorted((re.escape(word) for word in filters), key=len, reverse=True)
        # Match the filter word at word boundaries. Whitespace and comma
        # cleanup happens in _apply_word_filters post-processing so the
        # regex itself can stay readable.
        pattern = r"\b(?:" + "|".join(escaped) + r")\b"
        return re.compile(pattern, flags=re.IGNORECASE)

    @staticmethod
    def _build_replacement_pattern(replacements: Dict[str, str]) -> Optional[re.Pattern[str]]:
        if not replacements:
            return None
        escaped = sorted((re.escape(word) for word in replacements.keys()), key=len, reverse=True)
        pattern = r"\b(" + "|".join(escaped) + r")\b"
        return re.compile(pattern, flags=re.IGNORECASE)


def _short_exc(exc: BaseException) -> str:
    """Return only the exception class; messages may contain clipboard text."""
    return type(exc).__name__


def _normalize_punctuation(text: str) -> str:
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    text = re.sub(r"[^\S\r\n]+", " ", text)
    text = re.sub(r"[^\S\r\n]+([,.;!?])", r"\1", text)
    return text.strip()


def _sentence_case(text: str) -> str:
    # ============================================================================
    # TRANSCRIPT DATA — DO NOT LOG
    # MAKE SURE you do not log transcript data anywhere. If you add a new function
    # that receives, returns, or passes transcript text, COPY this banner to it.
    # Use `log_transcription_event()` from whisperstream.logger for metadata only
    # (char count, word count, duration, stage name). Never pass `text`,
    # `transcription`, `transcript`, `processed`, or `chunk` as a logging arg.
    # ============================================================================
    result = []
    capitalize_next = True
    for ch in text:
        if capitalize_next and ch.isalpha():
            result.append(ch.upper())
            capitalize_next = False
        else:
            result.append(ch.lower())
        if ch in ".!?\n":
            capitalize_next = True
    return "".join(result)
