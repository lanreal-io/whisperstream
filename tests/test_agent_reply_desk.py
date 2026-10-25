"""Unit tests for the single Agent Speak reply owner."""

from __future__ import annotations

from whisperstream.agent_reply_desk import (
    Presented,
    ReplyDesk,
    question_target,
    reply_target,
    resolve_foreground,
)

CONV = reply_target("c1")
OTHER = reply_target("c2")
QUESTION = question_target("c1", 3)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def desk(**kwargs) -> ReplyDesk:
    return ReplyDesk(clock=kwargs.pop("clock", Clock()), **kwargs)


def paint(d: ReplyDesk, target=CONV, surface="console", *, focused=False, seq=None):
    seq = d.presented().seq + 1 if seq is None else seq
    return d.present(seq=seq, target_id=target, surface=surface, painted=target is not None, focused=focused)


def dictate(d: ReplyDesk, target: str, text: str, generation: int = 7) -> None:
    assert d.begin_capture(target, generation, "push_to_talk") is not None
    d.compose(generation, "processing")
    d.compose(generation, "final", text=text)
    d.compose(generation, "idle")


class TestForeground:
    def test_question_outranks_painted_console(self):
        presented = Presented(seq=1, target_id=CONV, surface="console", painted=True, focused=True)
        fg = resolve_foreground(presented, QUESTION)
        assert fg.target_id == QUESTION
        assert fg.painted is False  # the console is covered until the HUD paints the question

    def test_painted_target_is_foreground(self):
        fg = resolve_foreground(Presented(seq=1, target_id=CONV, surface="card", painted=True), None)
        assert (fg.target_id, fg.surface, fg.painted) == (CONV, "card", True)

    def test_nothing_presented_is_no_foreground(self):
        assert resolve_foreground(Presented(), None) is None

    def test_stale_presented_seq_is_rejected(self):
        d = desk()
        assert paint(d, seq=5) == "applied"
        assert d.present(seq=4, target_id=OTHER, surface="card", painted=True, focused=False) == "stale"
        assert d.foreground().target_id == CONV

    def test_new_socket_restarts_sequence(self):
        d = desk()
        d.present(seq=9, target_id=CONV, surface="card", painted=True, focused=False, socket="a")
        assert d.present(seq=0, target_id=OTHER, surface="card", painted=True, focused=False, socket="b") == "applied"

    def test_revoke_clears_painted(self):
        d = desk()
        paint(d)
        d.revoke_presented()
        assert d.key_policy.ptt is None


class TestKeyPolicy:
    def test_enter_claims_a_ready_painted_draft_focused_or_not(self):
        d = desk()
        dictate(d, CONV, "hello")
        assert d.key_policy.enter is None  # not painted yet
        paint(d)
        draft = d.draft(CONV)
        assert d.key_policy.enter == (draft.draft_id, draft.text_revision)
        # A spoken draft has no text box to own Enter, focused or not.
        paint(d, focused=True)
        assert d.key_policy.enter == (draft.draft_id, draft.text_revision)
        assert d.key_policy.enter_owner == "global"

    def test_focused_hud_with_nothing_to_send_owns_enter(self):
        d = desk()
        paint(d, focused=True)
        assert d.key_policy.enter is None
        assert d.key_policy.enter_owner == "hud"

    def test_processing_dictation_claims_enter_for_when_it_lands(self):
        d = desk()
        paint(d, CONV, "card")
        assert d.begin_capture(CONV, 7, "push_to_talk") is not None
        assert d.key_policy.enter_when_ready is None  # still recording
        d.compose(7, "processing")
        draft = d.draft(CONV)
        assert d.key_policy.enter_when_ready == draft.draft_id
        assert d.key_policy.enter_owner == "global"
        d.compose(7, "final", text="hello")
        assert d.key_policy.enter_when_ready is None
        assert d.key_policy.enter is not None

    def test_enter_while_transcribing_binds_to_that_capture_only(self):
        d = desk()
        paint(d, CONV, "card")
        dictate(d, CONV, "kept text", generation=6)
        draft_id = d.draft(CONV).draft_id

        # Claimed while generation 7 transcribes, then that capture is cancelled.
        d.begin_capture(CONV, 7, "push_to_talk")
        d.compose(7, "processing")
        assert d.claim_enter_when_ready(draft_id) is None
        d.compose(7, "cancelled")

        # A later capture into the same draft is not submitted by that Enter.
        d.begin_capture(CONV, 8, "push_to_talk")
        d.compose(8, "processing")
        assert d.compose(8, "final", text="more").submit_requested is False

    def test_enter_while_transcribing_submits_on_its_final(self):
        d = desk()
        paint(d, CONV, "card")
        d.begin_capture(CONV, 7, "push_to_talk")
        d.compose(7, "processing")
        draft_id = d.draft(CONV).draft_id
        assert d.claim_enter_when_ready(draft_id) is None
        view = d.compose(7, "final", text="hello")
        assert view.submit_requested is True
        # A text that landed before the claim is submitted at once instead.
        d.begin_capture(CONV, 8, "push_to_talk")
        d.compose(8, "processing")
        landed = d.compose(8, "final", text="again")
        assert landed.submit_requested is False
        assert d.claim_enter_when_ready(draft_id) == (draft_id, landed.text_revision)

    def test_editing_draft_never_claims_global_enter(self):
        d = desk()
        paint(d)
        d.update(target_id=CONV, base_revision=0, text="typed")
        assert d.key_policy.enter is None

    def test_refusal_releases_enter_until_text_changes(self):
        d = desk()
        dictate(d, CONV, "hello")
        paint(d)
        draft = d.draft(CONV)
        d.record_outcome(draft.draft_id, draft.text_revision, "refused")
        assert d.key_policy.enter is None
        assert d.draft(CONV).text == "hello"
        dictate(d, CONV, "again", generation=8)
        assert d.key_policy.enter is not None

    def test_covered_console_owns_nothing(self):
        d = desk()
        dictate(d, CONV, "hello")
        paint(d)
        d.set_question(QUESTION)
        policy = d.key_policy
        assert policy.foreground == QUESTION
        assert (policy.enter, policy.escape, policy.ptt) == (None, None, None)
        paint(d, QUESTION, "question")
        assert d.key_policy.escape == QUESTION and d.key_policy.ptt == QUESTION


class TestDraftLifetime:
    def test_capture_idle_never_clears_typed_text(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="typed")
        d.begin_capture(CONV, 4, "toggle")
        d.compose(4, "idle")
        view = d.draft(CONV)
        assert (view.text, view.phase) == ("typed", "editing")

    def test_cancel_keeps_text_and_late_idle_is_ignored(self):
        d = desk()
        dictate(d, CONV, "first")
        d.update(target_id=CONV, base_revision=d.draft(CONV).text_revision, text="first edited")
        assert d.compose(7, "idle") is None  # late event for a finished generation
        assert d.draft(CONV).text == "first edited"

    def test_redictation_appends(self):
        d = desk()
        dictate(d, CONV, "one")
        dictate(d, CONV, "two", generation=8)
        assert d.draft(CONV).text == "one two"

    def test_one_capture_at_a_time(self):
        d = desk()
        assert d.begin_capture(CONV, 1, "push_to_talk") is not None
        assert d.begin_capture(OTHER, 2, "push_to_talk") is None

    def test_rekey_carries_draft(self):
        d = desk()
        d.update(target_id=OTHER, base_revision=0, text="keep me")
        assert d.rekey(OTHER, CONV)
        assert d.draft(CONV).text == "keep me"
        assert d.draft(OTHER) is None

    def test_rekey_never_overwrites_existing(self):
        d = desk()
        d.update(target_id=OTHER, base_revision=0, text="a")
        d.update(target_id=CONV, base_revision=0, text="b")
        assert not d.rekey(OTHER, CONV)
        assert d.draft(OTHER).text == "a" and d.draft(CONV).text == "b"

    def test_question_draft_dropped_with_question(self):
        d = desk()
        d.set_question(QUESTION)
        dictate(d, QUESTION, "yes")
        d.set_question(None)
        assert d.draft(QUESTION) is None

    def test_sending_draft_is_read_only_until_settled(self):
        d = desk()
        paint(d)
        d.update(target_id=CONV, base_revision=0, text="x")
        draft = d.draft(CONV)
        assert d.check_submit(draft.draft_id, draft.text_revision).result == "applied"
        send_id = d.mark_sending(draft.draft_id, draft.text_revision, held=True)
        view = d.draft(CONV)
        assert (view.phase, view.held) == ("sending", True)
        assert d.update(target_id=CONV, base_revision=view.text_revision, text="y").result == "refused"
        assert d.discard(CONV) is False
        assert d.begin_capture(CONV, 3, "push_to_talk") is None
        assert d.settle(send_id, True)
        assert d.draft(CONV) is None and d.receipts()[-1].text == "x"

    def test_dropped_send_comes_back_editable(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="x")
        draft = d.draft(CONV)
        send_id = d.mark_sending(draft.draft_id, draft.text_revision, held=False)
        assert d.settle(send_id, False)
        view = d.draft(CONV)
        assert (view.phase, view.text, view.outcome) == ("editing", "x", (view.text_revision, "not_delivered"))
        assert d.settle(send_id, False) is False  # settles once

    def test_second_submit_is_refused_without_an_outcome(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="x")
        draft = d.draft(CONV)
        assert d.check_submit(draft.draft_id, draft.text_revision).result == "applied"
        second = d.check_submit(draft.draft_id, draft.text_revision)
        assert second.result == "refused" and second.draft is None

    def test_stale_question_target_routes_nothing(self):
        d = desk()
        d.set_question(QUESTION)
        d.set_question(question_target("c1", 4))
        paint(d, QUESTION, "question")
        assert d.key_policy.ptt is None

    def test_snapshot_publish_is_fenced_by_revision(self):
        d = desk()
        snap = d.snapshot()
        d.update(target_id=CONV, base_revision=0, text="x")
        published = []
        assert d.publish_if_current(snap.revision, lambda: published.append(1)) is False
        assert d.publish_if_current(d.snapshot().revision, lambda: published.append(2)) is True
        assert published == [2]

    def test_idle_ttl_prunes_unpainted_drafts(self):
        clock = Clock()
        d = desk(clock=clock, idle_ttl_seconds=10)
        d.update(target_id=OTHER, base_revision=0, text="old")
        clock.now += 11
        assert d.drafts() == ()

    def test_clear_empties_everything(self):
        d = desk()
        paint(d)
        d.update(target_id=CONV, base_revision=0, text="x")
        epoch = d.epoch
        d.clear()
        assert d.drafts() == () and d.foreground() is None and d.epoch != epoch


class TestRevisions:
    def test_first_update_creates_draft(self):
        d = desk()
        outcome = d.update(target_id=CONV, base_revision=0, text="h")
        assert outcome.result == "applied" and outcome.draft.text_revision == 1

    def test_stale_base_revision_conflicts(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="h")
        d.update(target_id=CONV, base_revision=1, text="he")
        outcome = d.update(target_id=CONV, base_revision=1, text="hx")
        assert outcome.result == "conflict" and d.draft(CONV).text == "he"

    def test_update_refused_while_recording(self):
        d = desk()
        d.begin_capture(CONV, 1, "push_to_talk")
        assert d.update(target_id=CONV, base_revision=0, text="x").result == "refused"

    def test_submit_applies_exact_text(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="hel")
        draft = d.draft(CONV)
        outcome = d.check_submit(draft.draft_id, draft.text_revision, "hello")
        assert outcome.result == "applied" and outcome.draft.text == "hello"

    def test_submit_stale_revision(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="a")
        draft = d.draft(CONV)
        assert d.check_submit(draft.draft_id, draft.text_revision + 1).result == "stale"

    def test_sent_drops_draft_and_records_receipt(self):
        d = desk()
        d.update(target_id=CONV, base_revision=0, text="a")
        draft = d.draft(CONV)
        d.record_outcome(draft.draft_id, draft.text_revision, "sent")
        assert d.draft(CONV) is None
        receipt = d.receipts()[-1]
        assert (receipt.target_id, receipt.draft_id, receipt.text) == (CONV, draft.draft_id, "a")


# -- state matrix -------------------------------------------------------------
#
# Every combination of question, presented surface, focus and foreground draft
# state. One invariant: push-to-talk, Enter, Escape and the foreground agree.
# Each names the foreground target or nothing; nothing routes to a covered or
# unpainted surface; Enter only ever names the foreground's own ready draft.

import itertools

import pytest

_PRESENTED = {
    "nothing": (None, None, False),
    "console": (CONV, "console", True),
    "card": (CONV, "card", True),
    "covered_console": (CONV, "console", False),
    "question": (QUESTION, "question", True),
}
_DRAFTS = ("none", "editing", "ready", "refused", "recording")


def _seed(d: ReplyDesk, target: str, state: str) -> None:
    if state == "none":
        return
    if state == "editing":
        d.update(target_id=target, base_revision=0, text="typed")
    elif state in {"ready", "refused"}:
        dictate(d, target, "spoken")
        if state == "refused":
            view = d.draft(target)
            d.record_outcome(view.draft_id, view.text_revision, "refused")
    elif state == "recording":
        d.begin_capture(target, 9, "push_to_talk")


@pytest.mark.parametrize(
    "question,presented,focused,draft_state",
    list(itertools.product((False, True), _PRESENTED, (False, True), _DRAFTS)),
)
def test_ptt_enter_escape_and_foreground_agree(question, presented, focused, draft_state):
    d = desk()
    if question:
        d.set_question(QUESTION)
    target, surface, painted = _PRESENTED[presented]
    # A painted question that is not the current question routes nothing.
    fg_target = QUESTION if question else (None if presented == "question" else target)
    if fg_target is not None:
        _seed(d, fg_target, draft_state)
    d.present(seq=1, target_id=target, surface=surface, painted=painted, focused=focused)

    policy = d.key_policy
    fg = d.foreground()
    assert (fg.target_id if fg else None) == policy.foreground == fg_target
    routable = fg is not None and fg.painted
    for owner in (policy.ptt, policy.escape):
        assert owner == (fg_target if routable else None)
    if policy.enter is not None:
        view = d.draft(fg_target)
        assert routable
        assert policy.enter == (view.draft_id, view.text_revision)
        assert view.phase == "ready" and view.outcome is None
    else:
        assert not (routable and draft_state == "ready")
    # A covered console or an unacknowledged question owns no key.
    if presented == "covered_console" or (question and presented != "question"):
        assert (policy.ptt, policy.enter, policy.escape) == (None, None, None)


class TestComposing:
    def test_only_a_focused_console_with_text_composes(self):
        d = desk()
        paint(d, CONV, "console", focused=True)
        assert d.composing is False  # nothing written yet
        d.update(target_id=CONV, base_revision=0, text="draft")
        assert d.composing is True

        paint(d, CONV, "console", focused=False)
        assert d.composing is False
        paint(d, CONV, "card", focused=True)
        assert d.composing is False  # a card already holds the queue as the active item

    def test_clearing_the_text_releases_the_hold(self):
        d = desk()
        paint(d, CONV, "console", focused=True)
        outcome = d.update(target_id=CONV, base_revision=0, text="draft")
        assert d.composing is True
        d.update(target_id=CONV, base_revision=outcome.draft.text_revision, text="")
        assert d.composing is False



class TestDeliveryReport:
    def test_reports_whether_a_delivered_draft_was_dictated(self):
        reports: list[bool] = []
        d = desk(delivered=reports.append)
        dictate(d, CONV, "spoken")
        draft = d.draft(CONV)
        assert d.check_submit(draft.draft_id, draft.text_revision).result == "applied"
        d.record_outcome(draft.draft_id, draft.text_revision, "sent")
        d.update(target_id=CONV, base_revision=0, text="typed")
        draft = d.draft(CONV)
        send_id = d.mark_sending(draft.draft_id, draft.text_revision, held=False)
        assert d.settle(send_id, True)
        assert reports == [True, False]

    def test_refusals_and_drops_report_nothing(self):
        reports: list[bool] = []
        d = desk(delivered=reports.append)
        dictate(d, CONV, "spoken")
        draft = d.draft(CONV)
        d.record_outcome(draft.draft_id, draft.text_revision, "refused")
        draft = d.draft(CONV)
        send_id = d.mark_sending(draft.draft_id, draft.text_revision, held=False)
        assert d.settle(send_id, False)
        assert reports == []
