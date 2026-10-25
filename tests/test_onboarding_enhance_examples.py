"""Historical onboarding examples, bound to their original model and runtime.

The canned preview outputs were verified with the recorded E artifact. They are
illustrations, not a claim of exact output from every later production model.
The optional reproduction check uses that historical serving metadata and refuses
a different artifact. New production integration is covered by the serving tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "frontend" / "src" / "components" / "onboarding" / "enhanceExamples.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _examples(data: dict) -> list[dict]:
    return [*data["intro"], *data["tryIt"]]


def _pinned(data: dict) -> list[dict]:
    # An unpinned intro card shows canned text that the model only produces on
    # some runs; it is listed with its reason and skipped by the real-model run.
    return [e for e in _examples(data) if e.get("pinned", True)]


def test_fixture_retains_original_model_provenance():
    data = _load()
    assert data["model"] == {
        "id": "stream-1-mini",
        "revision": "8a26b93930de183051afd4a54cc4e8fc5dc91aa8",
        "file": "stream-1-mini-q4_k_m.gguf",
        "sha256": "ec447cdd8dbbcdedca8fe0722db662835a9c6d8b7b9fec6ffd359d057e709910",
    }
    assert data["serving_contract"] == {
        "prompt_key": "stream-mini", "serving_contract": "wrap-v2",
        "trained_modes": ["chat", "email"],
    }


def test_fixture_matches_llama_cpp_pin():
    text = (REPO / "scripts" / "fetch_llama_server.py").read_text(encoding="utf-8")
    assert f'LLAMA_CPP_RELEASE_TAG = "{_load()["llama_cpp"]}"' in text


def test_fixture_shape_and_copy_rules():
    data = _load()
    assert len(data["tryIt"]) == 2
    assert [card["id"] for card in data["intro"]] == ["correction", "list", "email"]
    assert data["intro"][0]["dropped"] in data["intro"][0]["input"]
    assert [card.get("email", False) for card in data["intro"]] == [False, False, True]
    for example in _examples(data):
        assert example["profile"] in {"chat", "email"}
        if not example.get("pinned", True):
            assert example["unpinnedReason"].strip()
        assert example["input"].strip() and example["expected"].strip()
        assert example["input"] != example["expected"]
        for value in (example["input"], example["expected"]):
            assert "\u2014" not in value  # no em dashes in user-visible copy


def _real_gguf_path() -> Path:
    # conftest pins WHISPERSTREAM_DATA_DIR to a temp dir, so the GGUF is named
    # explicitly rather than found in a data directory.
    return Path(os.environ.get("WHISPERSTREAM_TEST_STREAM_1_MINI_GGUF", ""))


def _binary_path() -> Path:
    override = os.environ.get("WHISPERSTREAM_LLAMA_SERVER_BINARY")
    if override:
        return Path(override)
    return REPO / "src-tauri" / "binaries" / "llama-server-x86_64-pc-windows-msvc.exe"


@pytest.mark.skipif(
    os.environ.get("WHISPERSTREAM_REAL_GGUF_TESTS") != "1",
    reason="opt-in: set WHISPERSTREAM_REAL_GGUF_TESTS=1",
)
def test_real_model_reproduces_pinned_outputs(monkeypatch):
    gguf = _real_gguf_path()
    binary = _binary_path()
    if not gguf.is_file() or not binary.is_file():
        pytest.skip("Stream 1 mini GGUF or llama-server binary not present")

    from whisperstream.config_manager import ConfigManager
    from whisperstream.llama_server import LlamaServerSupervisor
    from whisperstream.llm_enhancer import LLMEnhancer

    data = _load()
    import hashlib
    assert hashlib.file_digest(gguf.open("rb"), "sha256").hexdigest() == data["model"]["sha256"]
    metadata = {"id": data["model"]["id"], **data["serving_contract"]}
    from whisperstream import enhancement_models
    original_lookup = enhancement_models.get_local_model
    monkeypatch.setattr(enhancement_models, "get_local_model",
        lambda model_id: metadata if model_id == metadata["id"] else original_lookup(model_id))
    cfg = ConfigManager().load()
    cfg.enhancement_enabled = True
    cfg.enhancement_provider = "local"
    cfg.enhancement_local_model = data["model"]["id"]
    cfg.enhancement_local_endpoint = ""
    # This checks output, not latency: a cold CPU Email prefill can exceed the
    # 5s product timeout and would otherwise read as a wrong output.
    cfg.enhancement_timeout_seconds = 60.0
    ngl = int(os.environ.get("WHISPERSTREAM_LLAMA_NGL", "99"))

    async def run() -> list[tuple[str, str, str]]:
        sup = LlamaServerSupervisor(
            gguf,
            binary_path=binary,
            n_gpu_layers=ngl,
            device_index_provider=lambda: -1 if ngl == 0 else 0,
            model_id_provider=lambda: data["model"]["id"],
        )
        assert await sup.ensure_ready()
        try:
            enhancer = LLMEnhancer(cfg, llama_server=sup, local_model_metadata=metadata)
            rows = []
            # A warm server's earlier calls can change greedy output, so every
            # example runs both first and after the others.
            examples = _pinned(data)
            for example in [*examples, *reversed(examples)]:
                result = await enhancer.enhance_with_outcome(
                    example["input"], profile_id=example["profile"]
                )
                rows.append((example["expected"], result.text, result.outcome))
            return rows
        finally:
            await sup.stop()

    rows = asyncio.run(run())
    assert [(actual, outcome) for _, actual, outcome in rows] == [
        (expected, "success") for expected, _, _ in rows
    ]
