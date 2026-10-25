"""Stream-1 Mini serving contract: the evaluated E full13 configuration."""
import asyncio
import hashlib
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_policy import ModelBehaviorFamily, resolve_model_policy
from whisperstream.stream_mini_prompts import (
    STREAM_MINI_CHAT_PROMPT,
    STREAM_MINI_CHAT_SHA256,
    STREAM_MINI_EMAIL_PROMPT,
    STREAM_MINI_EMAIL_SHA256,
)

CANDIDATE = {
    "id": "stream-1-mini",
    "source": "production",
    "prompt_key": "stream-mini",
    "serving_contract": "wrap-v2",
    "trained_modes": ["chat", "email"],
}


def _supervisor():
    sup = MagicMock()
    sup.is_running = True
    sup.is_ready = True
    sup.base_url = "http://127.0.0.1:12345/v1"
    return sup


def _response(text):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 5},
    }
    return resp


def test_prompts_are_byte_exact_with_the_evaluated_configuration():
    assert hashlib.sha256(STREAM_MINI_CHAT_PROMPT.encode()).hexdigest() == (
        "6a0f060dec99873ee6deb48ea7b4dc50d3268753a815c0a1adf0d84dfba5ec08"
    ) == STREAM_MINI_CHAT_SHA256
    assert hashlib.sha256(STREAM_MINI_EMAIL_PROMPT.encode()).hexdigest() == (
        "b83896dd1836b67ebcc87a87c1b65ebf671bcbf5a4b47a5526bf85e53f6a8a4b"
    ) == STREAM_MINI_EMAIL_SHA256
    assert (len(STREAM_MINI_CHAT_PROMPT), len(STREAM_MINI_EMAIL_PROMPT)) == (2039, 2940)


@pytest.mark.parametrize(
    ("mode", "prompt"),
    (("chat", STREAM_MINI_CHAT_PROMPT), ("email", STREAM_MINI_EMAIL_PROMPT)),
    ids=("chat", "email"),
)
def test_registry_resolves_mode_prompt_wrapped_user_turn_and_greedy_decode(mode, prompt):
    from whisperstream.enhancement_prompts import (
        format_registered_user_content,
        local_generation_options,
        resolve_registered_system_prompt,
    )

    assert resolve_registered_system_prompt("stream-mini", mode, "wrap-v2") == prompt
    assert format_registered_user_content(
        "raw text", prompt_key="stream-mini", profile_mode=mode, serving_contract="wrap-v2",
    ) == "<TRANSCRIPT>raw text</TRANSCRIPT>"
    assert local_generation_options("stream-mini") == {
        "temperature": 0.0,
        "top_k": 1,
        "top_p": 1.0,
        "repeat_penalty": 1.0,
        "max_tokens": 512,
    }


def test_capabilities_are_fixed_prompt_chat_and_email():
    policy = resolve_model_policy("local", CANDIDATE, model_id="stream-1-mini")
    caps = policy.capabilities
    assert caps.family is ModelBehaviorFamily.TRAINED_MODE
    assert caps.editable_prompt is False
    assert caps.supported_modes == ("chat", "email")
    assert policy.generation_options()["cache_prompt"] is True
    assert policy.generation_options()["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(
    ("profile_id", "prompt", "guard"),
    (("chat", STREAM_MINI_CHAT_PROMPT, "stream_chat"), ("email", STREAM_MINI_EMAIL_PROMPT, "local_email")),
    ids=("chat", "email"),
)
def test_whole_request_matches_evaluated_contract(profile_id, prompt, guard):
    from whisperstream.llm_enhancer import LLMEnhancer

    raw = "so please send the final report tomorrow"
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model="stream-1-mini",
        default_profile_id=profile_id,
        profiles=[{"id": profile_id, "name": profile_id.title(), "prompt": "ignored for a fixed model"}],
    )
    enhancer = LLMEnhancer(config, llama_server=_supervisor())
    policy = resolve_model_policy("local", CANDIDATE, model_id="stream-1-mini")
    assert policy.prepare(profile_id, config.profiles[0]).contract.prompt_family == guard
    posted = []

    async def capture_post(url, *args, **kwargs):
        posted.append(kwargs["json"])
        return _response("Please send the final report tomorrow.")

    with (
        patch("whisperstream.llm_enhancer.get_local_model", return_value=CANDIDATE),
        patch.object(enhancer._get_client(), "post", side_effect=capture_post),
    ):
        asyncio.run(enhancer.enhance(raw, profile_id=profile_id))

    assert posted == [
        {
            "model": "stream-1-mini",
            "stream": False,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"<TRANSCRIPT>{raw}</TRANSCRIPT>"},
            ],
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 1.0,
            "repeat_penalty": 1.0,
            "max_tokens": 512,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ]


def test_code_mode_is_not_sent_to_the_model():
    from whisperstream.llm_enhancer import LLMEnhancer

    raw = "please refactor this method after lunch"
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="local",
        enhancement_local_model="stream-1-mini",
        default_profile_id="code",
        profiles=[{"id": "code", "name": "Code", "prompt": "code prompt"}],
    )
    enhancer = LLMEnhancer(config, llama_server=_supervisor())
    with (
        patch("whisperstream.llm_enhancer.get_local_model", return_value=CANDIDATE),
        patch.object(enhancer._get_client(), "post") as post,
    ):
        assert asyncio.run(enhancer.enhance(raw, profile_id="code")) == raw
    post.assert_not_called()


def test_catalog_default_and_server_flags():
    from whisperstream.enhancement_models import LOCAL_BUNDLED_MODELS, get_local_model
    from whisperstream.llama_server import _thinking_off_model

    assert LOCAL_BUNDLED_MODELS[0]["id"] == "stream-1-mini"
    row = get_local_model("stream-1-mini")
    assert row["label"] == "Stream-1 Mini"
    assert (row["prompt_key"], row["serving_contract"], row["trained_modes"]) == (
        "stream-mini-vocabulary", "wrap-v2", ["chat", "email"],
    )
    assert WhisperstreamConfig().enhancement_local_model == "stream-1-mini"
    assert _thinking_off_model("stream-1-mini")
    assert not _thinking_off_model("gemma4-e2b-instruct")


def test_unknown_saved_local_model_falls_back_to_stream_1_mini(tmp_path):
    from whisperstream.config_manager import ConfigManager

    path = tmp_path / "config.toml"
    path.write_text('enhancement_local_model = "retired-model"\n', encoding="utf-8")
    manager = ConfigManager(config_path=path, models_root=tmp_path / "models")
    assert manager.load().enhancement_local_model == "stream-1-mini"
