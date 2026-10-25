from types import SimpleNamespace

import pytest

from whisperstream.speech.content import readable_response, select_speech_content


def test_default_requires_explicit_summary():
    assert select_speech_content({"text": "Done."}, SimpleNamespace()) is None


def test_full_removes_code_links_and_duplicate_summary():
    text = "# Done\nSee [docs](https://example.com).\n```py\nsecret()\n```\nhttps://raw.test\n🔊 Done."
    assert readable_response(text) == "Done See docs."
    assert readable_response("Safe\n~~~\nunfinished code") == "Safe"


@pytest.mark.parametrize("speed,expected", [(0.5, "Brief."), (2.0, "word " * 50)])
def test_short_uses_selected_speed(speed, expected):
    result = {"text": "word " * 50, "spoken_summary": "Brief."}
    config = SimpleNamespace(agent_speech_content="short", agent_speech_speed=speed)
    assert select_speech_content(result, config) == expected.strip()


def test_oversized_response_cannot_be_mistaken_for_short():
    result = {"text": "x" * 32_001, "spoken_summary": "Brief."}
    assert select_speech_content(result, SimpleNamespace(agent_speech_content="short")) == "Brief."
    assert select_speech_content(result, SimpleNamespace(agent_speech_content="full")) == "Brief."


def test_short_without_summary_fails_silent_when_long():
    config = SimpleNamespace(agent_speech_content="short")
    assert select_speech_content({"text": "x" * 2000}, config) is None
    assert select_speech_content({"text": "All done."}, config) == "All done."


def test_inline_code_is_not_narrated():
    assert readable_response("Run `private_command()` then read the result.") == "Run then read the result."


@pytest.mark.parametrize("policy,expected", [("summary", "Brief."), ("short", "The complete reply."), ("full", "The complete reply.")])
def test_selected_content_reaches_the_real_response_queue(policy, expected, accepted_eula):
    from tests.test_claude_hook_endpoint import _make_client, _make_controller, PROMPT_SUBMIT
    from whisperstream.config_manager import WhisperstreamConfig
    config = WhisperstreamConfig(claude_hud_enabled=True, agent_speech_enabled=True,
        agent_speech_activation="always_on", agent_speech_content=policy)
    controller = _make_controller()
    controller.speak_spoken_summary.return_value = True
    client, server = _make_client(config=config, speech_controller=controller)
    try:
        client.post("/api/claude-hook", json={**PROMPT_SUBMIT, "session_id": "synthetic-session"})
        server._queue_agent_response(agent="claude", completion_id="policy-test", completion_sequence=1,
            result={"session_id": "synthetic-session", "text": "The complete reply.", "spoken_summary": "Brief."})
        controller.speak_spoken_summary.assert_called_once()
        assert controller.speak_spoken_summary.call_args.args == (expected,)
    finally:
        server._get_agent_response_coordinator().clear()
        client.close()


@pytest.mark.parametrize("value", [True, 4, 121, 12.5, "20"])
def test_threshold_validation_is_strict(value):
    from whisperstream.config_manager import WhisperstreamConfig
    with pytest.raises(ValueError, match="agent_speech_short_seconds"):
        WhisperstreamConfig(agent_speech_short_seconds=value).validate()
