"""Non-interactive agent session classification at the hook edge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whisperstream import agent_session_filter as f


@pytest.fixture(autouse=True)
def _clear_cache():
    f.clear_session_filter_cache()
    yield
    f.clear_session_filter_cache()


def _rollout(tmp_path: Path, source: object, name: str = "rollout.jsonl") -> str:
    path = tmp_path / name
    meta = {"type": "session_meta", "payload": {"originator": "x", "source": source}}
    path.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "event_msg"}) + "\n", encoding="utf-8")
    return str(path)


def test_codex_subagent_fields_are_excluded(tmp_path):
    path = _rollout(tmp_path, "cli")
    assert f.codex_session_excluded({"transcript_path": path, "agent_id": "a1"}) == "subagent"
    assert f.codex_session_excluded({"transcript_path": path, "agent_type": "worker"}) == "subagent"


@pytest.mark.parametrize("value", [None, "", 0])
def test_codex_without_transcript_is_ephemeral(value):
    assert f.codex_session_excluded({"transcript_path": value}) == "ephemeral"


def test_codex_payload_without_transcript_key_is_kept():
    # Current Codex always sends the key; older/synthesized payloads omit it.
    assert f.codex_session_excluded({"hook_event_name": "Stop"}) is None


def test_codex_exec_rollout_is_excluded(tmp_path):
    assert f.codex_session_excluded({"transcript_path": _rollout(tmp_path, "exec")}) == "exec"


def test_codex_desktop_subagent_rollout_is_excluded(tmp_path):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}}
    assert f.codex_session_excluded({"transcript_path": _rollout(tmp_path, source)}) == "subagent"


@pytest.mark.parametrize("source", ["cli", "vscode", "something-new"])
def test_codex_interactive_rollouts_are_kept(tmp_path, source):
    assert f.codex_session_excluded({"transcript_path": _rollout(tmp_path, source)}) is None


def test_codex_unreadable_or_unknown_rollout_is_kept(tmp_path):
    assert f.codex_session_excluded({"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert f.codex_session_excluded({"transcript_path": str(bad)}) is None
    other = tmp_path / "other.jsonl"
    other.write_text(json.dumps({"type": "event_msg"}) + "\n", encoding="utf-8")
    assert f.codex_session_excluded({"transcript_path": str(other)}) is None


def test_codex_oversized_first_line_is_kept(tmp_path):
    big = tmp_path / "big.jsonl"
    big.write_text('{"type": "session_meta", "payload": {"source": "exec", "pad": "' + "x" * (f._MAX_FIRST_LINE_BYTES + 10) + '"}}\n', encoding="utf-8")
    assert f.codex_session_excluded({"transcript_path": str(big)}) is None


def test_codex_verdict_is_cached_per_path(tmp_path):
    path = _rollout(tmp_path, "exec")
    assert f.codex_session_excluded({"transcript_path": path}) == "exec"
    Path(path).unlink()
    assert f.codex_session_excluded({"transcript_path": path}) == "exec"


def test_codex_cache_is_bounded(tmp_path):
    for index in range(f._MAX_CACHE_ENTRIES + 20):
        f.codex_session_excluded({"transcript_path": _rollout(tmp_path, "cli", f"r{index}.jsonl")})
    assert len(f._verdicts) <= f._MAX_CACHE_ENTRIES


def test_claude_agent_id_marks_subagent():
    assert f.claude_session_excluded({"agent_id": "abc"}) == "subagent"


def test_claude_agent_type_alone_is_a_main_session():
    # `claude --agent <name>` sets agent_type on the main session.
    assert f.claude_session_excluded({"agent_type": "reviewer"}) is None
    assert f.claude_session_excluded({"agent_id": ""}) is None
    assert f.claude_session_excluded({}) is None


def test_non_dict_payloads_are_kept():
    assert f.codex_session_excluded("x") is None  # type: ignore[arg-type]
    assert f.claude_session_excluded(None) is None  # type: ignore[arg-type]
