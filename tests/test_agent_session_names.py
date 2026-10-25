"""Tests for the shared session-name sanitizer and speech/label normalizer."""

import json
import stat
from types import SimpleNamespace

import pytest

import whisperstream.agent_session_names as agent_session_names
from whisperstream.agent_session_names import (
    MAX_SESSION_NAME_CHARS,
    TranscriptTitleCache,
    normalize_session_name_for_speech,
    read_custom_title,
    sanitize_session_name,
)


def _title_line(title: str, *, session_id: str = "s1") -> str:
    return json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})


def _other_line() -> str:
    return json.dumps({"type": "message", "role": "user", "content": "hello"})


class TestSanitizeSessionName:
    def test_none_is_none(self):
        assert sanitize_session_name(None) is None

    def test_empty_string_is_none(self):
        assert sanitize_session_name("") is None

    def test_non_str_is_none(self):
        assert sanitize_session_name(123) is None
        assert sanitize_session_name(["speak-harness"]) is None

    def test_slash_never_survives(self):
        result = sanitize_session_name("app/pty-proxy")
        assert result is not None
        assert "/" not in result
        assert "\\" not in result

    def test_backslash_path_is_sanitized(self):
        result = sanitize_session_name("app\\pty-proxy")
        assert result is not None
        assert "/" not in result
        assert "\\" not in result

    def test_control_characters_replaced(self):
        result = sanitize_session_name("speak\x00harness\x1f")
        assert result is not None
        assert all(character >= " " for character in result)

    def test_long_input_capped_at_32(self):
        long_name = "a" * 60
        result = sanitize_session_name(long_name)
        assert result is not None
        assert len(result) == MAX_SESSION_NAME_CHARS

    def test_whitespace_only_is_none(self):
        assert sanitize_session_name("   ") is None

    def test_property_every_output_is_safe(self):
        samples = [
            "app/pty-proxy",
            "app\\pty-proxy",
            "speak\x00harness\x1f",
            "a" * 60,
            "speak-harness",
            "  padded  name  ",
        ]
        for sample in samples:
            result = sanitize_session_name(sample)
            if result is None:
                continue
            assert "/" not in result
            assert "\\" not in result
            assert all(character >= " " for character in result)
            assert 1 <= len(result) <= MAX_SESSION_NAME_CHARS


class TestNormalizeSessionNameForSpeech:
    def test_hyphen_to_space(self):
        assert normalize_session_name_for_speech("speak-harness") == "speak harness"

    def test_underscore_to_space(self):
        assert normalize_session_name_for_speech("pty_proxy") == "pty proxy"

    def test_camel_case_split(self):
        assert normalize_session_name_for_speech("payKitReview") == "pay Kit Review"

    def test_collapses_repeated_separators(self):
        assert normalize_session_name_for_speech("a--b") == "a b"

    def test_already_clean_unchanged(self):
        assert normalize_session_name_for_speech("speak harness") == "speak harness"

    def test_does_not_change_case(self):
        assert normalize_session_name_for_speech("SPEAK-HARNESS") == "SPEAK HARNESS"

    def test_slash_to_space(self):
        assert normalize_session_name_for_speech("app/pty") == "app pty"


class TestReadCustomTitle:
    def test_none_path_is_none(self):
        assert read_custom_title(None) is None

    def test_empty_path_is_none(self):
        assert read_custom_title("") is None

    def test_non_str_path_is_none(self):
        assert read_custom_title(123) is None

    def test_nonexistent_path_is_none(self, tmp_path):
        assert read_custom_title(str(tmp_path / "missing.jsonl")) is None

    def test_directory_path_is_none(self, tmp_path):
        assert read_custom_title(str(tmp_path)) is None

    def test_title_near_eof_is_found(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n".join([_other_line(), _title_line("speak-harness")]) + "\n",
            encoding="utf-8",
        )
        assert read_custom_title(str(path), projects_root=tmp_path) == "speak-harness"

    def test_two_titles_last_wins(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n".join([_title_line("first-name"), _title_line("second-name")]) + "\n",
            encoding="utf-8",
        )
        assert read_custom_title(str(path), projects_root=tmp_path) == "second-name"

    def test_no_title_is_none(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(_other_line() + "\n", encoding="utf-8")
        assert read_custom_title(str(path), projects_root=tmp_path) is None

    def test_title_in_last_20kb_of_300kb_file_is_found(self, tmp_path):
        path = tmp_path / "t.jsonl"
        padding = "\n".join(_other_line() for _ in range(6000))
        content = padding + "\n" + _title_line("late-name") + "\n"
        assert len(content.encode("utf-8")) >= 300_000
        path.write_text(content, encoding="utf-8")
        assert read_custom_title(str(path), projects_root=tmp_path) == "late-name"

    def test_title_at_byte_zero_of_300kb_file_not_found(self, tmp_path):
        path = tmp_path / "t.jsonl"
        padding = "\n".join(_other_line() for _ in range(6000))
        content = _title_line("early-name") + "\n" + padding + "\n"
        assert len(content.encode("utf-8")) >= 300_000
        assert read_custom_title(str(path), projects_root=tmp_path) is None

    def test_unparseable_line_beside_valid_title(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n".join(["{not valid json", _title_line("good-name")]) + "\n",
            encoding="utf-8",
        )
        assert read_custom_title(str(path), projects_root=tmp_path) == "good-name"

    def test_invalid_utf8_bytes_do_not_raise(self, tmp_path):
        path = tmp_path / "t.jsonl"
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe not valid utf8\n")
            handle.write(_title_line("still-fine").encode("utf-8") + b"\n")
        # Must never raise; result is either the recovered title or None.
        result = read_custom_title(str(path), projects_root=tmp_path)
        assert result in (None, "still-fine")

    def test_rejects_non_jsonl_and_outside_root(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        wrong_suffix = projects / "title.txt"
        wrong_suffix.write_text(_title_line("wrong") + "\n", encoding="utf-8")
        outside = tmp_path / "outside.jsonl"
        outside.write_text(_title_line("outside") + "\n", encoding="utf-8")

        assert read_custom_title(
            str(wrong_suffix), projects_root=projects
        ) is None
        assert read_custom_title(str(outside), projects_root=projects) is None

    def test_rejects_marker_in_non_custom_title_record(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({
                "type": "message",
                "customTitle": "custom-title sentinel",
            }) + "\n",
            encoding="utf-8",
        )
        assert read_custom_title(str(path), projects_root=tmp_path) is None

    def test_rejects_symlink_component(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        real = tmp_path / "real"
        real.mkdir()
        transcript = real / "t.jsonl"
        transcript.write_text(_title_line("linked") + "\n", encoding="utf-8")
        linked = projects / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        assert read_custom_title(
            str(linked / "t.jsonl"), projects_root=projects
        ) is None

    def test_rejects_open_handle_identity_mismatch(self, tmp_path, monkeypatch):
        path = tmp_path / "t.jsonl"
        path.write_text(_title_line("swapped") + "\n", encoding="utf-8")
        actual = path.stat()
        monkeypatch.setattr(
            agent_session_names.os,
            "fstat",
            lambda _fd: SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino + 1,
                st_size=actual.st_size,
                st_file_attributes=0,
            ),
        )
        assert read_custom_title(str(path), projects_root=tmp_path) is None

    def test_append_between_stat_and_read_still_returns_title(self, tmp_path, monkeypatch):
        path = tmp_path / "t.jsonl"
        path.write_text(_title_line("live-session") + "\n", encoding="utf-8")
        original = agent_session_names._safe_transcript_stat

        def stat_then_append(*args, **kwargs):
            result = original(*args, **kwargs)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(_other_line() + "\n")
            return result

        monkeypatch.setattr(agent_session_names, "_safe_transcript_stat", stat_then_append)
        assert read_custom_title(str(path), projects_root=tmp_path) == "live-session"


class TestTranscriptTitleCache:
    def test_unchanged_file_reads_once(self, tmp_path, monkeypatch):
        path = tmp_path / "t.jsonl"
        path.write_text(_title_line("first-name") + "\n", encoding="utf-8")

        calls = []
        real_read = agent_session_names.read_custom_title

        def counting_read(transcript_path, **kwargs):
            calls.append(transcript_path)
            return real_read(transcript_path, **kwargs)

        monkeypatch.setattr(agent_session_names, "read_custom_title", counting_read)
        cache = TranscriptTitleCache(projects_root=tmp_path)

        first = cache.get("s1", str(path))
        second = cache.get("s1", str(path))

        assert first == "first-name"
        assert second == "first-name"
        assert len(calls) == 1

    def test_appended_file_yields_new_title(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(_title_line("first-name") + "\n", encoding="utf-8")
        cache = TranscriptTitleCache(projects_root=tmp_path)

        assert cache.get("s1", str(path)) == "first-name"

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_title_line("second-name") + "\n")

        assert cache.get("s1", str(path)) == "second-name"

    def test_cap_evicts_oldest(self, tmp_path):
        cache = TranscriptTitleCache(max_sessions=2, projects_root=tmp_path)
        paths = []
        for i in range(3):
            path = tmp_path / f"t{i}.jsonl"
            path.write_text(_title_line(f"name-{i}") + "\n", encoding="utf-8")
            paths.append(path)
            cache.get(f"s{i}", str(path))
        assert len(cache) == 2

    def test_failed_stat_returns_cached_value(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(_title_line("first-name") + "\n", encoding="utf-8")
        cache = TranscriptTitleCache(projects_root=tmp_path)
        assert cache.get("s1", str(path)) == "first-name"
        # A different or missing path never reuses transcript-class cache data.
        assert cache.get("s1", str(tmp_path / "missing.jsonl")) is None

    def test_failed_stat_no_cached_value_is_none(self, tmp_path):
        cache = TranscriptTitleCache(projects_root=tmp_path)
        assert cache.get("s1", str(tmp_path / "missing.jsonl")) is None
