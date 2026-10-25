"""Tests for the bounded, memory-only agent completion store."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pytest

from whisperstream.agent_activity import (
    AgentActivityStore,
    EnqueueResult,
    MAX_SESSION_ID_CHARS,
    OVERFLOW_COPY_TEMPLATE,
    SourceKey,
)


def _response(
    text: str,
    *,
    agent: str = "claude",
    session_id: str | None = "session-1",
    cwd: str | None = r"C:\Users\USERNAME\Documents\Dev\whisperstream",
    prompt_id: str | None = None,
    empty: bool = False,
    source_name: object = None,
    source_name_is_custom: object = None,
) -> dict:
    event = {
        "kind": "response",
        "agent": agent,
        "session_id": session_id,
        "cwd": cwd,
        "text": text,
        "empty": empty,
    }
    if prompt_id is not None:
        event["prompt_id"] = prompt_id
    if source_name is not None:
        event["source_name"] = source_name
    if source_name_is_custom is not None:
        event["source_name_is_custom"] = source_name_is_custom
    return event


def _ids(*values: str):
    iterator = iter(values)
    return lambda: next(iterator)


def test_enqueue_preserves_exact_text_and_global_sequence() -> None:
    store = AgentActivityStore(id_factory=_ids("first", "second"))
    first_text = '# Result\n\n```json\n{"ok": true}\n```\n'
    second_text = "Unicode stays exact: café ☕"

    first_result = store.enqueue(_response(first_text, prompt_id="prompt-1"))
    second_result = store.enqueue(
        _response(
            second_text,
            session_id="session-2",
            prompt_id="prompt-2",
        )
    )
    assert isinstance(first_result, EnqueueResult)
    assert first_result.duplicate is False
    assert second_result.duplicate is False
    (first,) = first_result.changed
    (second,) = second_result.changed

    assert first.completion_id == "first"
    assert first.sequence == 1
    assert first.source_key == SourceKey("claude", "session-1")
    assert first.text == first_text
    assert second.completion_id == "second"
    assert second.sequence == 2
    assert second.text == second_text
    assert store.snapshot() == (first, second)


def test_retry_with_same_upstream_identity_and_response_is_deduplicated() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))
    first_event = _response(
        "first delivery",
        session_id="shared-session",
        prompt_id="upstream-prompt",
    )

    first_result = store.enqueue(first_event)
    (first,) = first_result.changed

    assert first_result.duplicate is False
    retry_result = store.enqueue(dict(first_event))
    assert retry_result == EnqueueResult(changed=(), duplicate=True)
    assert store.snapshot() == (first,)

    assert store.acknowledge(first.completion_id) is True
    assert store.enqueue(dict(first_event)) == EnqueueResult(
        changed=(), duplicate=True
    )
    assert store.snapshot() == ()


def test_codex_turn_identity_dedupes_without_colliding_with_claude() -> None:
    store = AgentActivityStore(id_factory=_ids("codex", "claude"))
    codex = _response(
        "same delivery",
        agent="codex",
        session_id="shared-session",
        prompt_id=None,
    )
    codex["turn_id"] = "shared-turn"
    claude = _response(
        "same delivery",
        agent="claude",
        session_id="shared-session",
        prompt_id="shared-turn",
    )

    first = store.enqueue(codex)
    retry = store.enqueue(dict(codex))
    second = store.enqueue(claude)

    assert first.duplicate is False
    assert retry == EnqueueResult(changed=(), duplicate=True)
    assert second.duplicate is False
    assert [item.agent for item in store.snapshot()] == ["codex", "claude"]
    assert [item.source_label for item in store.snapshot()] == [
        "Codex · whisperstream",
        "Claude · whisperstream",
    ]


def test_distinct_responses_in_same_upstream_prompt_are_not_deduplicated() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))
    first_event = _response(
        "first delivery",
        session_id="shared-session",
        prompt_id="continued-prompt",
    )
    second_event = {**first_event, "text": "second delivery"}

    first_result = store.enqueue(first_event)
    second_result = store.enqueue(second_event)

    assert first_result.duplicate is False
    assert second_result.duplicate is False
    assert [item.text for item in store.snapshot()] == [
        "first delivery",
        "second delivery",
    ]


def test_events_without_upstream_identity_are_not_content_deduplicated() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))
    event = _response("same visible response", prompt_id=None)

    assert len(store.enqueue(event).changed) == 1
    assert len(store.enqueue(event).changed) == 1
    assert [item.completion_id for item in store.snapshot()] == ["one", "two"]


def test_oversized_session_metadata_is_digested_before_store_retention() -> None:
    store = AgentActivityStore(id_factory=_ids("boundary", "huge", "huge-again"))
    boundary_session = "s" * MAX_SESSION_ID_CHARS
    private_session = "PRIVATE_SESSION_SENTINEL:" + ("x" * 100_000)
    private_prompt = "PRIVATE_PROMPT_SENTINEL:" + ("y" * 100_000)
    private_path = "C:\\PRIVATE_PATH_SENTINEL\\" + ("z" * 100_000)
    expected_digest = "sha256:" + hashlib.sha256(
        private_session.encode("utf-8")
    ).hexdigest()

    (boundary,) = store.enqueue(
        _response("boundary", session_id=boundary_session, prompt_id="boundary-prompt")
    ).changed
    (huge,) = store.enqueue(
        _response(
            "huge",
            session_id=private_session,
            prompt_id=private_prompt,
            cwd=private_path,
        )
    ).changed
    (huge_again,) = store.enqueue(
        _response(
            "huge again",
            session_id=private_session,
            prompt_id=f"different:{private_prompt}",
            cwd=private_path,
        )
    ).changed

    assert boundary.source_key == SourceKey("claude", boundary_session)
    assert huge.source_key == SourceKey("claude", expected_digest)
    assert huge_again.source_key == huge.source_key
    assert len(expected_digest) == 71
    assert list(store._source_labels) == [boundary.source_key, huge.source_key]

    retained_metadata = repr(
        (
            store.snapshot(),
            store._source_labels,
            store._pending_dedupe,
            store._completion_dedupe,
            store._recent_dedupe,
        )
    )
    for sentinel in (
        "PRIVATE_SESSION_SENTINEL",
        "PRIVATE_PROMPT_SENTINEL",
        "PRIVATE_PATH_SENTINEL",
    ):
        assert sentinel not in retained_metadata


def test_same_project_collision_gets_ephemeral_privacy_safe_ordinal() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two", "three"))
    first_result = store.enqueue(
        _response("one", session_id="source-one", prompt_id="prompt-one")
    )
    second_result = store.enqueue(
        _response("two", session_id="source-two", prompt_id="prompt-two")
    )
    second_again_result = store.enqueue(
        _response("three", session_id="source-two", prompt_id="prompt-three")
    )
    (first,) = first_result.changed
    (second,) = second_result.changed
    (second_again,) = second_again_result.changed

    assert first.source_label == "Claude · whisperstream"
    assert second.source_label == "Claude · whisperstream · 2"
    assert second_again.source_label == second.source_label
    assert "Users" not in second.source_label
    assert "source-two" not in second.source_label


def test_historical_source_does_not_force_suffix_for_new_live_source() -> None:
    store = AgentActivityStore(id_factory=_ids("first", "second"))
    (first,) = store.enqueue(
        _response("one", session_id="historical", prompt_id="prompt-one")
    ).changed
    assert first.source_label == "Claude · whisperstream"
    assert store.acknowledge(first.completion_id) is True

    (second,) = store.enqueue(
        _response("two", session_id="new-live", prompt_id="prompt-two")
    ).changed

    assert second.source_label == "Claude · whisperstream"


def test_recurrent_live_source_preserves_its_collision_suffix() -> None:
    store = AgentActivityStore(id_factory=_ids("first", "second", "third"))
    store.enqueue(_response("one", session_id="first", prompt_id="prompt-one"))
    (second,) = store.enqueue(
        _response("two", session_id="recurrent", prompt_id="prompt-two")
    ).changed
    assert second.source_label == "Claude · whisperstream · 2"
    assert store.acknowledge(second.completion_id) is True

    (recurrent,) = store.enqueue(
        _response("three", session_id="recurrent", prompt_id="prompt-three")
    ).changed

    assert recurrent.source_label == second.source_label


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        (r"C:\Users\USERNAME\Documents\Dev\whisperstream", "Claude · whisperstream"),
        ("/home/private-name/dev/whisperstream/", "Claude · whisperstream"),
        (None, "Claude session"),
        ("/", "Claude session"),
    ],
)
def test_source_label_uses_only_project_basename(
    cwd: str | None, expected: str
) -> None:
    store = AgentActivityStore(id_factory=_ids("one"))

    result = store.enqueue(
        _response(
            "done",
            agent="claude",
            session_id="claude-session",
            cwd=cwd,
            prompt_id="prompt-one",
        )
    )
    (completion,) = result.changed

    assert completion.source_label == expected
    # The resolved session name is the project basename when one exists, and
    # the terminal literal otherwise, so the frontend never invents a string.
    expected_name = expected.split(" · ")[1] if " · " in expected else "Claude"
    payload = completion.to_dict()
    assert payload == {
        "completion_id": "one",
        "sequence": 1,
        "kind": "response",
        "text": "done",
        "agent": "claude",
        "source_label": expected,
        "source_name": expected_name,
        "source_name_is_custom": False,
        "empty": False,
    }


def test_supplied_custom_name_wins_the_resolution_chain() -> None:
    store = AgentActivityStore(id_factory=_ids("one"))

    (completion,) = store.enqueue(
        _response(
            "done",
            prompt_id="prompt-one",
            source_name="speak-harness",
            source_name_is_custom=True,
        )
    ).changed

    assert completion.source_name == "speak-harness"
    assert completion.source_name_is_custom is True
    # The display label is untouched by the name.
    assert completion.source_label == "Claude · whisperstream"
    payload = completion.to_dict()
    assert payload["source_name"] == "speak-harness"
    assert payload["source_name_is_custom"] is True


def test_project_basename_is_the_second_rung_and_is_not_custom() -> None:
    store = AgentActivityStore(id_factory=_ids("one"))

    (completion,) = store.enqueue(_response("done", prompt_id="prompt-one")).changed

    assert completion.source_name == "whisperstream"
    assert completion.source_name_is_custom is False


def test_terminal_rung_is_the_literal_agent_name() -> None:
    store = AgentActivityStore(id_factory=_ids("one"))

    (completion,) = store.enqueue(
        _response("done", cwd=None, prompt_id="prompt-one")
    ).changed

    assert completion.source_name == "Claude"
    assert completion.source_name_is_custom is False


def test_custom_name_is_sanitized_before_it_reaches_the_wire() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))

    (sliced,) = store.enqueue(
        _response(
            "done",
            prompt_id="prompt-one",
            source_name="app/pty-proxy",
            source_name_is_custom=True,
        )
    ).changed
    (controlled,) = store.enqueue(
        _response(
            "done",
            session_id="session-2",
            prompt_id="prompt-two",
            source_name="pay\x00kit\\review",
            source_name_is_custom=True,
        )
    ).changed

    for completion in (sliced, controlled):
        assert completion.source_name is not None
        assert "/" not in completion.source_name
        assert "\\" not in completion.source_name
        assert all(character >= " " for character in completion.source_name)
    assert sliced.source_name == "app pty-proxy"
    assert controlled.source_name == "pay kit review"


def test_overlong_custom_name_is_capped() -> None:
    store = AgentActivityStore(id_factory=_ids("one"))
    raw = "a" * 36

    (completion,) = store.enqueue(
        _response(
            "done",
            prompt_id="prompt-one",
            source_name=raw,
            source_name_is_custom=True,
        )
    ).changed

    assert completion.source_name == "a" * 32
    assert len(completion.source_name) == 32


def test_unsanitizable_custom_name_falls_through_to_the_project_basename() -> None:
    store = AgentActivityStore(id_factory=_ids("one"))

    (completion,) = store.enqueue(
        _response(
            "done",
            prompt_id="prompt-one",
            source_name="   ",
            source_name_is_custom=True,
        )
    ).changed

    assert completion.source_name == "whisperstream"
    assert completion.source_name_is_custom is False


def test_overflow_marker_carries_neither_name_field() -> None:
    store = AgentActivityStore(
        max_items=2,
        max_response_chars=1_000,
        id_factory=_ids("one", "two", "overflow", "three"),
    )
    for number in range(1, 4):
        store.enqueue(
            _response(
                str(number),
                prompt_id=f"prompt-{number}",
                source_name="speak-harness",
                source_name_is_custom=True,
            )
        )

    marker = store.snapshot()[0]

    assert marker.kind == "overflow"
    payload = marker.to_dict()
    assert "source_name" not in payload
    assert "source_name_is_custom" not in payload


def test_colliding_sessions_keep_distinct_labels_but_unsuffixed_names() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))

    (first,) = store.enqueue(
        _response("one", session_id="source-one", prompt_id="prompt-one")
    ).changed
    (second,) = store.enqueue(
        _response("two", session_id="source-two", prompt_id="prompt-two")
    ).changed

    assert first.source_label != second.source_label
    assert first.source_name == second.source_name == "whisperstream"


def test_snapshot_and_acknowledgement_are_idempotent() -> None:
    store = AgentActivityStore(id_factory=_ids("one", "two"))
    (first,) = store.enqueue(_response("one", prompt_id="prompt-one")).changed
    (second,) = store.enqueue(_response("two", prompt_id="prompt-two")).changed

    assert store.snapshot() == store.snapshot() == (first, second)
    assert store.acknowledge("missing") is False
    assert store.acknowledge(first.completion_id) is True
    assert store.acknowledge(first.completion_id) is False
    assert store.snapshot() == (second,)


def test_item_limit_groups_oldest_replies_into_one_acknowledgeable_marker() -> None:
    store = AgentActivityStore(
        max_items=3,
        max_response_chars=1_000,
        id_factory=_ids("one", "two", "three", "four", "overflow", "five"),
    )
    for number in range(1, 4):
        result = store.enqueue(_response(str(number), prompt_id=f"prompt-{number}"))
        assert len(result.changed) == 1

    overflow_result = store.enqueue(_response("4", prompt_id="prompt-4"))

    snapshot = store.snapshot()

    assert len(snapshot) == 3
    marker, third, fourth = snapshot
    assert marker.kind == "overflow"
    assert marker.sequence == 1
    assert marker.completion_id == "overflow"
    assert marker.overflow_count == 2
    assert marker.text == OVERFLOW_COPY_TEMPLATE.format(count=2)
    assert marker.to_dict() == {
        "completion_id": "overflow",
        "sequence": 1,
        "kind": "overflow",
        "text": OVERFLOW_COPY_TEMPLATE.format(count=2),
        "overflow_count": 2,
    }
    assert [third.text, fourth.text] == ["3", "4"]
    assert overflow_result == EnqueueResult(
        changed=(marker, fourth), duplicate=False
    )
    assert third not in overflow_result.changed
    assert store.stats() == {
        "pending_items": 3,
        "pending_response_chars": sum(len(item.text) for item in snapshot),
        "overflow_count": 2,
        "recent_dedup_identities": 2,
        "source_labels": 1,
    }

    update_result = store.enqueue(_response("5", prompt_id="prompt-5"))
    updated_marker, retained_fourth, fifth = store.snapshot()
    assert updated_marker.completion_id == marker.completion_id
    assert updated_marker.overflow_count == 3
    assert updated_marker.text == OVERFLOW_COPY_TEMPLATE.format(count=3)
    assert [retained_fourth.text, fifth.text] == ["4", "5"]
    assert update_result == EnqueueResult(
        changed=(updated_marker, fifth), duplicate=False
    )
    assert retained_fourth not in update_result.changed

    assert store.acknowledge(updated_marker.completion_id) is True
    assert store.acknowledge(updated_marker.completion_id) is False
    assert all(item.kind == "response" for item in store.snapshot())


def test_character_limit_counts_marker_copy_and_keeps_newest_useful_reply() -> None:
    store = AgentActivityStore(
        max_items=10,
        max_response_chars=60,
        id_factory=_ids("one", "two", "three", "four", "overflow"),
    )
    for number in range(1, 4):
        result = store.enqueue(
            _response(str(number) * 20, prompt_id=f"prompt-{number}")
        )
        assert len(result.changed) == 1
    overflow_result = store.enqueue(_response("4" * 20, prompt_id="prompt-4"))

    snapshot = store.snapshot()

    assert [(item.kind, item.overflow_count) for item in snapshot] == [
        ("overflow", 3),
        ("response", 0),
    ]
    assert snapshot[-1].text == "4" * 20
    assert overflow_result.changed == (snapshot[0], snapshot[-1])
    assert sum(len(item.text) for item in snapshot) <= 60
    assert store.stats()["pending_response_chars"] <= 60


def test_single_oversized_reply_is_grouped_without_retaining_its_content() -> None:
    store = AgentActivityStore(
        max_items=2,
        max_response_chars=30,
        id_factory=_ids("huge", "overflow"),
    )
    sensitive_text = "sensitive" * 20
    result = store.enqueue(_response(sensitive_text, prompt_id="huge-prompt"))

    snapshot = store.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].kind == "overflow"
    assert snapshot[0].text == "1 earlier replies grouped"
    assert result == EnqueueResult(changed=snapshot, duplicate=False)
    assert all(item.text != sensitive_text for item in result.changed)
    assert "sensitive" not in snapshot[0].text
    assert store.stats()["pending_response_chars"] <= 30


def test_parallel_sources_keep_per_source_fifo_and_unique_monotonic_sequence() -> None:
    store = AgentActivityStore(max_items=200, max_response_chars=50_000)

    def enqueue_source(source_number: int) -> None:
        for turn_number in range(25):
            event = _response(
                f"source {source_number} turn {turn_number}",
                session_id=f"session-{source_number}",
                prompt_id=f"prompt-{source_number}-{turn_number}",
            )
            result = store.enqueue(event)
            assert result.duplicate is False
            assert len(result.changed) == 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(enqueue_source, range(4)))

    snapshot = store.snapshot()
    assert len(snapshot) == 100
    assert [item.sequence for item in snapshot] == list(range(1, 101))
    assert len({item.completion_id for item in snapshot}) == 100

    turns_by_source: dict[str, list[int]] = defaultdict(list)
    for item in snapshot:
        _, source_number, _, turn_number = item.text.split()
        turns_by_source[source_number].append(int(turn_number))
    assert all(turns == list(range(25)) for turns in turns_by_source.values())


def test_internal_accounting_stays_bounded_under_many_acknowledged_events() -> None:
    store = AgentActivityStore(
        max_items=5,
        max_response_chars=200,
        dedupe_history=12,
        source_history=12,
    )
    for number in range(100):
        result = store.enqueue(
            _response(
                "done",
                session_id=f"session-{number}",
                prompt_id=f"prompt-{number}",
                cwd=rf"C:\private\project-{number}",
            )
        )
        assert result.duplicate is False
        (completion,) = result.changed
        store.acknowledge(completion.completion_id)

    stats = store.stats()
    assert stats["pending_items"] == 0
    assert stats["pending_response_chars"] == 0
    assert stats["recent_dedup_identities"] <= 12
    assert stats["source_labels"] <= 12


@pytest.mark.parametrize(
    "event",
    [
        _response("text", agent="unknown", prompt_id="prompt"),
        {**_response("text", prompt_id="prompt"), "kind": "progress"},
        {**_response("text", prompt_id="prompt"), "text": object()},
    ],
)
def test_invalid_events_are_rejected_without_mutating_the_store(event: dict) -> None:
    store = AgentActivityStore()

    with pytest.raises((TypeError, ValueError)):
        store.enqueue(event)

    assert store.snapshot() == ()
    assert store.stats()["pending_response_chars"] == 0
