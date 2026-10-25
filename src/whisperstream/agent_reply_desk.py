"""The single owner of Agent Speak reply state.

One draft per reply target, one foreground target, one key policy. Hook
requests, managed transports and question opportunities are delivery routes
and timing owners; none of them holds draft text.

Targets are opaque strings:

- ``conv:<conversation handle>``: a reply to one agent conversation. The hook
  card and the managed console for that conversation share this draft. A
  wrapped session whose provider session id is not yet known has no target and
  takes no reply.
- ``question:<session handle>:<generation>``: a spoken or typed answer to one
  question opportunity.

The foreground is what the user is addressing. It comes from one precedence
function: an active question outranks everything; otherwise it is the target
the HUD reports it actually painted. A target is routable only when it is the
foreground and painted. The key policy is published as an immutable value so
the keyboard listener thread can read it without a lock and without raising.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# Draft text is transcript-class. Never log it, never put it in an exception
# message, and never log target ids (they correlate sessions).
# ============================================================================
"""

from __future__ import annotations

import itertools
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Literal

DraftPhase = Literal["empty", "recording", "processing", "ready", "editing", "sending"]
DraftKind = Literal["reply", "question"]
Surface = Literal["card", "console", "question"]
Delivery = Literal["now", "hold", "none"]
OutcomeResult = Literal["sent", "refused"]
# Why a draft came back: refused at submit, or handed to a wrapper that never
# collected it (withdrawn after the pickup window, or a held reply dropped).
RefusalReason = Literal["refused", "not_delivered"]
CommandResult = Literal["applied", "refused", "stale", "conflict"]

MAX_DRAFT_CHARS = 50_000
DRAFT_IDLE_TTL_SECONDS = 30 * 60.0
_MAX_RECEIPTS = 16
_TARGET_PREFIXES = ("conv:", "question:")
_CAPTURING = frozenset({"recording", "processing"})
# Phases the user cannot type into: a capture owns it, or a send is out.
_LOCKED = frozenset({"recording", "processing", "sending"})


def reply_target(conversation_handle: str) -> str:
    return f"conv:{conversation_handle}"


def question_target(session_handle: str, generation: int) -> str:
    return f"question:{session_handle}:{generation}"


def target_kind(target_id: str) -> DraftKind:
    return "question" if target_id.startswith("question:") else "reply"


def is_target_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 600
        and value.startswith(_TARGET_PREFIXES)
    )


def append_dictation(prefix: str, text: str) -> str:
    """Join a dictated transcript onto the text that was already there."""

    if prefix.strip():
        return f"{prefix.rstrip()} {text.strip()}" if text.strip() else prefix
    return text


@dataclass(slots=True)
class ReplyDraft:
    draft_id: str
    target_id: str
    kind: DraftKind
    touched_at: float
    text: str = ""
    text_revision: int = 0
    phase: DraftPhase = "empty"
    compose_generation: int | None = None
    capture_mode: str | None = None
    # The phase to return to when a capture produces nothing.
    phase_before_capture: DraftPhase = "empty"
    # (text_revision, reason) of the last refusal. A refusal at the current
    # revision releases global Enter until the text changes.
    outcome: tuple[int, RefusalReason] | None = None
    # The revision a submit is validating; a second submit is refused.
    submitting: int | None = None
    # While a send is out (handed to a wrapper, or held for the next Stop) the
    # draft is kept read-only under this id until it settles.
    send_id: str | None = None
    held: bool = False
    # A capture put text in this draft. Delivery reports it so the reply hint
    # can retire once the user has dictated a few replies.
    dictated: bool = False
    # The capture generation an Enter was claimed for while it transcribed.
    # That capture's final submits the draft; its cancel or idle clears this.
    submit_on_ready: int | None = None

    @property
    def refused_now(self) -> bool:
        return self.outcome is not None and self.outcome[0] == self.text_revision


@dataclass(frozen=True, slots=True)
class Presented:
    """What the HUD reports it actually painted, from native truth."""

    seq: int = -1
    target_id: str | None = None
    surface: Surface | None = None
    painted: bool = False
    focused: bool = False


@dataclass(frozen=True, slots=True)
class Foreground:
    target_id: str
    surface: Surface
    painted: bool
    focused: bool


@dataclass(frozen=True, slots=True)
class KeyPolicy:
    """Immutable, lock-free view for the keyboard listener thread.

    ``enter`` names the exact draft and revision a global Enter submits; a
    claimed Enter is bound to it, never to "whatever is current" when the
    callback runs. ``escape`` and ``ptt`` name the foreground target only
    while it is painted. ``enter_when_ready`` names a dictated draft still
    being transcribed: Enter pressed right after releasing push-to-talk is
    claimed and submits that draft as soon as its text lands.
    """

    foreground: str | None = None
    surface: Surface | None = None
    enter: tuple[str, int] | None = None
    enter_when_ready: str | None = None
    escape: str | None = None
    ptt: str | None = None
    hud_focused: bool = False

    @property
    def enter_owner(self) -> Literal["global", "hud", "none"]:
        if self.enter is not None or self.enter_when_ready is not None:
            return "global"
        if self.hud_focused and self.foreground is not None:
            return "hud"
        return "none"


def resolve_foreground(
    presented: Presented, question: str | None
) -> Foreground | None:
    """The one precedence function: question > painted card/console."""

    if (
        presented.target_id is not None
        and presented.target_id.startswith("question:")
        and presented.target_id != question
    ):
        # A painted question that is no longer the current one routes nothing.
        presented = Presented(seq=presented.seq)
    if question is not None:
        painted = presented.painted and presented.target_id == question
        return Foreground(
            target_id=question,
            surface="question",
            painted=painted,
            focused=painted and presented.focused,
        )
    if presented.target_id is None or presented.surface is None:
        return None
    return Foreground(
        target_id=presented.target_id,
        surface=presented.surface,
        painted=presented.painted,
        focused=presented.painted and presented.focused,
    )


def derive_key_policy(
    foreground: Foreground | None, draft: ReplyDraft | None
) -> KeyPolicy:
    if foreground is None:
        return KeyPolicy()
    if not foreground.painted:
        return KeyPolicy(
            foreground=foreground.target_id,
            surface=foreground.surface,
            hud_focused=False,
        )
    # A ready spoken draft has no text box to own Enter, so the global Enter
    # takes it whether or not the HUD has focus. Depending on the HUD's focus
    # report left Enter with nobody when that report was stale or named
    # another reply (owner, 2026-10-25). Typing ("editing") keeps Enter in
    # the field.
    enter = None
    if (
        draft is not None
        and draft.phase == "ready"
        and draft.text.strip()
        and not draft.refused_now
        and draft.submitting is None
    ):
        enter = (draft.draft_id, draft.text_revision)
    enter_when_ready = (
        draft.draft_id
        if draft is not None and draft.phase == "processing"
        else None
    )
    return KeyPolicy(
        foreground=foreground.target_id,
        surface=foreground.surface,
        enter=enter,
        enter_when_ready=enter_when_ready,
        escape=foreground.target_id,
        ptt=foreground.target_id,
        hud_focused=foreground.focused,
    )


@dataclass(frozen=True, slots=True)
class DraftView:
    """Copy of one draft for snapshots and server decisions."""

    draft_id: str
    target_id: str
    kind: DraftKind
    text: str
    text_revision: int
    phase: DraftPhase
    compose_generation: int | None
    capture_mode: str | None
    outcome: tuple[int, RefusalReason] | None
    held: bool = False
    # Set only on the view ``compose(final)`` returns: an Enter claimed while
    # this capture transcribed asks for it to be submitted now.
    submit_requested: bool = False


@dataclass(frozen=True, slots=True)
class Receipt:
    """A delivered draft, kept briefly so the HUD can confirm exactly it."""

    target_id: str
    draft_id: str
    text: str


@dataclass(frozen=True, slots=True)
class DeskSnapshot:
    """Everything the HUD renders, copied under one lock."""

    epoch: str
    revision: int
    foreground: Foreground | None
    enter_owner: Literal["global", "hud", "none"]
    drafts: tuple[DraftView, ...]
    receipts: tuple[Receipt, ...]


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    result: CommandResult
    draft: DraftView | None = None


def _view(draft: ReplyDraft) -> DraftView:
    return DraftView(
        draft_id=draft.draft_id,
        target_id=draft.target_id,
        kind=draft.kind,
        text=draft.text,
        text_revision=draft.text_revision,
        phase=draft.phase,
        compose_generation=draft.compose_generation,
        capture_mode=draft.capture_mode,
        outcome=draft.outcome,
        held=draft.held,
    )


@dataclass(slots=True)
class _State:
    drafts: dict[str, ReplyDraft] = field(default_factory=dict)
    presented: Presented = field(default_factory=Presented)
    presented_socket: object = None
    question: str | None = None


class ReplyDesk:
    """Thread-safe, memory-only owner of drafts, foreground and key policy."""

    def __init__(
        self,
        *,
        changed: Callable[[], None] | None = None,
        delivered: Callable[[bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_ttl_seconds: float = DRAFT_IDLE_TTL_SECONDS,
    ) -> None:
        self._changed = changed
        self._delivered = delivered
        self._clock = clock
        self._idle_ttl = idle_ttl_seconds
        self._lock = threading.RLock()
        self._state = _State()
        self._epoch = secrets.token_hex(6)
        self._revision = 0
        self._ids = itertools.count(1)
        self._key_policy = KeyPolicy()
        self._composing = False
        # target -> the last delivered draft, so the HUD can show a correlated
        # receipt (with the exact text sent) after the draft itself is gone.
        self._receipts: dict[str, Receipt] = {}

    # -- read side -------------------------------------------------------

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def key_policy(self) -> KeyPolicy:
        # Attribute reads are atomic; the value is immutable.
        return self._key_policy

    @property
    def composing(self) -> bool:
        """The user is writing in the focused console: new responses wait."""
        return self._composing

    def foreground(self) -> Foreground | None:
        with self._lock:
            return resolve_foreground(self._state.presented, self._state.question)

    def presented(self) -> Presented:
        with self._lock:
            return self._state.presented

    def draft(self, target_id: str) -> DraftView | None:
        with self._lock:
            draft = self._state.drafts.get(target_id)
            return None if draft is None else _view(draft)

    def draft_by_id(self, draft_id: str) -> DraftView | None:
        with self._lock:
            draft = self._find_id_locked(draft_id)
            return None if draft is None else _view(draft)

    def drafts(self) -> tuple[DraftView, ...]:
        with self._lock:
            self._prune_locked()
            return tuple(_view(draft) for draft in self._state.drafts.values())

    def snapshot(self) -> DeskSnapshot:
        """One coherent copy. Callers compute routes outside the desk lock."""

        with self._lock:
            self._prune_locked()
            return DeskSnapshot(
                epoch=self._epoch,
                revision=self._revision,
                foreground=resolve_foreground(
                    self._state.presented, self._state.question
                ),
                enter_owner=self._key_policy.enter_owner,
                drafts=tuple(_view(d) for d in self._state.drafts.values()),
                receipts=tuple(self._receipts.values()),
            )

    def publish_if_current(self, revision: int, publish: Callable[[], None]) -> bool:
        """Run a non-blocking publish only if nothing changed since *revision*.

        A snapshot computed before a later change (or a teardown) is dropped;
        that change's own notification publishes the newer state.
        """

        with self._lock:
            if revision != self._revision:
                return False
            publish()
            return True

    def capturing(self) -> tuple[int, str | None, str] | None:
        """(generation, mode, target) of the one running capture, if any."""

        with self._lock:
            for draft in self._state.drafts.values():
                if draft.phase in _CAPTURING and draft.compose_generation is not None:
                    return draft.compose_generation, draft.capture_mode, draft.target_id
            return None

    # -- foreground inputs -----------------------------------------------

    def present(
        self,
        *,
        seq: object,
        target_id: object,
        surface: object,
        painted: object,
        focused: object,
        socket: object = None,
    ) -> CommandResult:
        """Record the HUD's presented acknowledgement; stale seqs are refused."""

        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            return "refused"
        if target_id is not None and not is_target_id(target_id):
            return "refused"
        if surface not in {None, "card", "console", "question"}:
            return "refused"
        if (target_id is None) != (surface is None):
            return "refused"
        with self._lock:
            current = self._state.presented
            same_socket = socket is self._state.presented_socket
            if same_socket and seq <= current.seq:
                return "stale"
            self._state.presented = Presented(
                seq=seq,
                target_id=target_id,  # type: ignore[arg-type]
                surface=surface,  # type: ignore[arg-type]
                painted=bool(painted) and target_id is not None,
                focused=bool(focused) and bool(painted) and target_id is not None,
            )
            self._state.presented_socket = socket
            changed = self._state.presented != current
            self._recompute_locked()
        if changed:
            self._notify()
        return "applied"

    def revoke_presented(self, socket: object = None) -> None:
        """Hide/disconnect: nothing is painted for that socket any more."""

        with self._lock:
            if socket is not None and socket is not self._state.presented_socket:
                return
            if self._state.presented == Presented(seq=self._state.presented.seq):
                return
            self._state.presented = Presented(seq=self._state.presented.seq)
            self._state.presented_socket = None
            self._recompute_locked()
        self._notify()

    def set_question(self, target_id: str | None) -> int | None:
        """The current answerable question, which outranks every reply.

        A question draft never outlives its question. Returns the generation
        of a capture still running into the previous question, which the
        caller must cancel; its draft goes when that capture ends.
        """

        orphaned: int | None = None
        with self._lock:
            if target_id == self._state.question:
                return None
            previous = self._state.question
            self._state.question = target_id
            if previous is not None and previous in self._state.drafts:
                draft = self._state.drafts[previous]
                if draft.phase in _CAPTURING:
                    orphaned = draft.compose_generation
                else:
                    del self._state.drafts[previous]
            self._recompute_locked()
        self._notify()
        return orphaned

    def rekey(self, old_target: str, new_target: str) -> bool:
        """Carry a draft across a session-id rebind; never merges silently."""

        if old_target == new_target or not is_target_id(new_target):
            return False
        with self._lock:
            draft = self._state.drafts.get(old_target)
            moved = False
            if draft is not None and new_target not in self._state.drafts:
                del self._state.drafts[old_target]
                draft.target_id = new_target
                self._state.drafts[new_target] = draft
                moved = True
            presented = self._state.presented
            if presented.target_id == old_target:
                self._state.presented = replace(presented, target_id=new_target)
                moved = True
            if moved:
                self._recompute_locked()
        if moved:
            self._notify()
        return moved

    # -- draft commands --------------------------------------------------

    def update(
        self,
        *,
        target_id: object = None,
        draft_id: object = None,
        base_revision: object,
        text: object,
    ) -> CommandOutcome:
        """Typed edit. The first update for a target creates its draft."""

        if not isinstance(text, str) or len(text) > MAX_DRAFT_CHARS:
            return CommandOutcome("refused")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            return CommandOutcome("refused")
        with self._lock:
            draft = self._resolve_locked(target_id, draft_id)
            if draft is None:
                if not is_target_id(target_id) or draft_id is not None:
                    return CommandOutcome("stale")
                if base_revision != 0:
                    return CommandOutcome("stale")
                draft = self._create_locked(target_id)  # type: ignore[arg-type]
            if draft.phase in _LOCKED:
                return CommandOutcome("refused", _view(draft))
            if base_revision != draft.text_revision:
                return CommandOutcome("conflict", _view(draft))
            if text == draft.text and draft.phase == "editing":
                return CommandOutcome("applied", _view(draft))
            draft.text = text
            draft.text_revision += 1
            draft.phase = "editing"
            draft.touched_at = self._clock()
            self._recompute_locked()
            view = _view(draft)
        self._notify()
        return CommandOutcome("applied", view)

    def begin_capture(
        self, target_id: str, generation: int, mode: str
    ) -> DraftView | None:
        """Bind one recording to one draft. Capture never clears text."""

        if not is_target_id(target_id):
            return None
        with self._lock:
            for other in self._state.drafts.values():
                if other.phase in _CAPTURING:
                    return None
            draft = self._state.drafts.get(target_id)
            if draft is not None and draft.phase == "sending":
                return None
            if draft is None:
                draft = self._create_locked(target_id)
            draft.phase_before_capture = draft.phase
            draft.phase = "recording"
            draft.compose_generation = generation
            draft.capture_mode = mode
            draft.touched_at = self._clock()
            self._recompute_locked()
            view = _view(draft)
        self._notify()
        return view

    def compose(
        self, generation: int, state: str, *, text: object = None
    ) -> DraftView | None:
        """Apply one exact compose transition; returns the draft it changed."""

        submit_requested = False
        with self._lock:
            draft = self._generation_locked(generation)
            if draft is None:
                return None
            if state == "processing":
                if draft.phase != "recording":
                    return None
                draft.phase = "processing"
            elif state == "final":
                if draft.phase not in _CAPTURING:
                    return None
                if not isinstance(text, str):
                    return None
                draft.text = append_dictation(draft.text, text)[:MAX_DRAFT_CHARS]
                draft.text_revision += 1
                if text.strip():
                    draft.dictated = True
                draft.phase = "ready" if draft.text.strip() else draft.phase_before_capture
                submit_requested = (
                    draft.submit_on_ready == generation and draft.phase == "ready"
                )
                draft.submit_on_ready = None
                draft.compose_generation = None
                draft.capture_mode = None
            elif state in {"idle", "cancelled"}:
                if draft.phase not in _CAPTURING:
                    return None
                # The capture produced nothing. The text is untouched, and an
                # Enter claimed for it dies with it.
                draft.phase = draft.phase_before_capture
                draft.submit_on_ready = None
                draft.compose_generation = None
                draft.capture_mode = None
            else:
                return None
            draft.touched_at = self._clock()
            self._drop_if_empty_locked(draft)
            if draft.kind == "question" and draft.target_id != self._state.question:
                # Its question moved on while the capture ran.
                self._state.drafts.pop(draft.target_id, None)
            self._recompute_locked()
            view = _view(draft)
            if submit_requested:
                view = replace(view, submit_requested=True)
        self._notify()
        return view

    def claim_enter_when_ready(self, draft_id: str) -> tuple[str, int] | None:
        """Bind an Enter claimed while transcribing to that exact capture.

        Returns ``(draft_id, revision)`` to submit now when the text already
        landed; otherwise records the capture so its final submits the draft.
        One lock covers both, so a final landing meanwhile cannot drop it.
        """

        with self._lock:
            draft = self._find_id_locked(draft_id)
            if draft is None:
                return None
            if draft.phase == "ready":
                return (draft.draft_id, draft.text_revision)
            if draft.phase == "processing" and draft.compose_generation is not None:
                draft.submit_on_ready = draft.compose_generation
            return None

    def check_submit(
        self, draft_id: object, revision: object, text: object = None
    ) -> CommandOutcome:
        """Validate a submit, apply the HUD's exact text and mark it submitting."""

        if isinstance(revision, bool) or not isinstance(revision, int):
            return CommandOutcome("refused")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_DRAFT_CHARS):
            return CommandOutcome("refused")
        with self._lock:
            draft = self._find_id_locked(draft_id)
            if draft is None:
                return CommandOutcome("stale")
            if draft.submitting is not None or draft.phase == "sending":
                # A second submit for text already on its way. No outcome is
                # recorded: the first submit owns this draft's result.
                return CommandOutcome("refused")
            if draft.phase in _CAPTURING:
                return CommandOutcome("refused", _view(draft))
            if revision != draft.text_revision:
                return CommandOutcome("stale", _view(draft))
            if isinstance(text, str) and text != draft.text:
                draft.text = text
                draft.text_revision += 1
                draft.phase = "editing"
            if not draft.text.strip():
                self._recompute_locked()
                return CommandOutcome("refused", _view(draft))
            draft.submitting = draft.text_revision
            self._recompute_locked()
            view = _view(draft)
        self._notify()
        return CommandOutcome("applied", view)

    def record_outcome(
        self,
        draft_id: str,
        revision: int,
        result: OutcomeResult,
        reason: RefusalReason = "refused",
    ) -> None:
        """Sent drops the draft with a receipt; refused keeps it, releases Enter."""

        with self._lock:
            draft = self._find_id_locked(draft_id)
            if draft is None or draft.text_revision != revision:
                return
            draft.submitting = None
            if result == "sent":
                self._deliver_locked(draft)
            else:
                self._refuse_locked(draft, reason)
            self._recompute_locked()
        if result == "sent":
            self._report_delivery(draft.dictated)
        self._notify()

    def mark_sending(self, draft_id: str, revision: int, *, held: bool) -> str | None:
        """The text was handed to a wrapper (or held for its next Stop).

        The draft stays, read-only, until :meth:`settle` reports pickup or a
        drop. Returns the send id the transport entry carries.
        """

        with self._lock:
            draft = self._find_id_locked(draft_id)
            if draft is None or draft.text_revision != revision:
                return None
            draft.submitting = None
            draft.phase = "sending"
            draft.held = held
            draft.send_id = f"s{next(self._ids)}"
            draft.touched_at = self._clock()
            self._recompute_locked()
            send_id = draft.send_id
        self._notify()
        return send_id

    def settle(self, send_id: object, delivered: bool) -> bool:
        """A wrapper collected the text (receipt) or it was dropped (Copy)."""

        if not isinstance(send_id, str):
            return False
        with self._lock:
            draft = next(
                (d for d in self._state.drafts.values() if d.send_id == send_id),
                None,
            )
            if draft is None:
                return False
            if delivered:
                self._deliver_locked(draft)
            else:
                self._refuse_locked(draft, "not_delivered")
            self._recompute_locked()
        if delivered:
            self._report_delivery(draft.dictated)
        self._notify()
        return True

    def discard(self, target_id: str) -> bool:
        with self._lock:
            draft = self._state.drafts.get(target_id)
            if draft is None or draft.phase in _LOCKED:
                return False
            del self._state.drafts[target_id]
            self._recompute_locked()
        self._notify()
        return True

    def capture_generation(self, target_id: str) -> int | None:
        with self._lock:
            draft = self._state.drafts.get(target_id)
            if draft is None or draft.phase not in _CAPTURING:
                return None
            return draft.compose_generation

    def receipts(self) -> tuple[Receipt, ...]:
        """Recent delivered drafts, so the HUD can show a correlated receipt."""
        with self._lock:
            return tuple(self._receipts.values())

    def clear(self) -> None:
        """Teardown, Lockdown, provider disable: nothing survives."""

        with self._lock:
            self._state = _State()
            self._receipts.clear()
            self._epoch = secrets.token_hex(6)
            self._recompute_locked()
        self._notify()

    def drop_targets(self, predicate: Callable[[str], bool]) -> None:
        with self._lock:
            doomed = [t for t in self._state.drafts if predicate(t)]
            for target in doomed:
                del self._state.drafts[target]
            for target in [t for t in self._receipts if predicate(t)]:
                del self._receipts[target]
                doomed.append(target)
            presented = self._state.presented
            if presented.target_id is not None and predicate(presented.target_id):
                self._state.presented = Presented(seq=presented.seq)
                doomed.append(presented.target_id)
            if not doomed:
                return
            self._recompute_locked()
        self._notify()

    # -- internals -------------------------------------------------------

    def _report_delivery(self, dictated: bool) -> None:
        # Outside the lock and before the snapshot publishes, so state the
        # callback derives from this delivery rides on the same snapshot.
        callback = self._delivered
        if callback is None:
            return
        try:
            callback(dictated)
        except Exception:
            pass

    def _deliver_locked(self, draft: ReplyDraft) -> None:
        del self._state.drafts[draft.target_id]
        self._receipts.pop(draft.target_id, None)
        self._receipts[draft.target_id] = Receipt(
            target_id=draft.target_id, draft_id=draft.draft_id, text=draft.text
        )
        while len(self._receipts) > _MAX_RECEIPTS:
            del self._receipts[next(iter(self._receipts))]

    def _refuse_locked(self, draft: ReplyDraft, reason: RefusalReason) -> None:
        draft.outcome = (draft.text_revision, reason)
        draft.send_id = None
        draft.held = False
        draft.touched_at = self._clock()
        if draft.phase in {"ready", "sending"}:
            # The text is now something the user edits or copies.
            draft.phase = "editing"

    def _create_locked(self, target_id: str) -> ReplyDraft:
        self._prune_locked()
        draft = ReplyDraft(
            draft_id=f"d{next(self._ids)}",
            target_id=target_id,
            kind=target_kind(target_id),
            touched_at=self._clock(),
        )
        self._state.drafts[target_id] = draft
        return draft

    def _drop_if_empty_locked(self, draft: ReplyDraft) -> None:
        if (
            draft.phase in {"empty", "ready"}
            and not draft.text
            and draft.compose_generation is None
        ):
            self._state.drafts.pop(draft.target_id, None)

    def _prune_locked(self) -> None:
        cutoff = self._clock() - self._idle_ttl
        for target, draft in list(self._state.drafts.items()):
            if (
                draft.touched_at < cutoff
                and draft.phase not in _LOCKED
                and target != self._state.presented.target_id
            ):
                del self._state.drafts[target]

    def _find_id_locked(self, draft_id: object) -> ReplyDraft | None:
        if not isinstance(draft_id, str):
            return None
        for draft in self._state.drafts.values():
            if draft.draft_id == draft_id:
                return draft
        return None

    def _resolve_locked(
        self, target_id: object, draft_id: object
    ) -> ReplyDraft | None:
        if draft_id is not None:
            draft = self._find_id_locked(draft_id)
            if draft is not None and target_id is not None and draft.target_id != target_id:
                return None
            return draft
        if isinstance(target_id, str):
            return self._state.drafts.get(target_id)
        return None

    def _generation_locked(self, generation: int) -> ReplyDraft | None:
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        for draft in self._state.drafts.values():
            if draft.compose_generation == generation:
                return draft
        return None

    def _recompute_locked(self) -> None:
        self._revision += 1
        foreground = resolve_foreground(self._state.presented, self._state.question)
        draft = (
            self._state.drafts.get(foreground.target_id)
            if foreground is not None
            else None
        )
        self._key_policy = derive_key_policy(foreground, draft)
        self._composing = (
            foreground is not None
            and foreground.surface == "console"
            and foreground.focused
            and draft is not None
            and draft.phase != "sending"
            and bool(draft.text.strip())
        )

    def _notify(self) -> None:
        callback = self._changed
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass
