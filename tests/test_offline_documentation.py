"""Protect the recovery instructions against runtime model-catalog drift."""
import json
from pathlib import Path

from whisperstream.model_downloader import DOWNLOAD_REGISTRY, ModelId
from whisperstream.model_sources import MODEL_SOURCES


ROOT = Path(__file__).resolve().parents[1]


def test_recovery_matches_all_seven_runtime_payloads():
    page = (ROOT / "docs/offline/articles/model-recovery.md").read_text(encoding="utf-8")
    # Seven exact payloads cover six public choices: Google QAT is Gemma's
    # internal download fallback, and still needs its own recovery checksum.
    models = [
        ModelId.PARAKEET_V2_INT8, ModelId.PARAKEET_V3_INT8,
        ModelId.QWEN3_ASR_06B_INT8, ModelId.COHERE_TRANSCRIBE_INT8,
        ModelId.GEMMA_4_E2B_Q4, ModelId.GEMMA_4_E2B_GOOGLE_QAT, ModelId.STREAM_1_MINI_Q4,
    ]
    for model in models:
        spec = DOWNLOAD_REGISTRY[model]
        source = MODEL_SOURCES[model.value]
        assert source["repo"] in page
        assert source["revision"] in page
        if spec.kind == "stt":
            assert f"models\\{spec.dir_name}" in page
        else:
            assert "%LOCALAPPDATA%\\Whisperstream\\local-llm\\" in page
            assert spec.files[0] in page
        for filename, metadata in source["files"].items():
            assert filename in page
            assert metadata["sha256"] in page
            assert str(metadata["size"]) in page.replace(",", "")


def test_complete_article_bundle_and_markdown_links():
    bundle = ROOT / "frontend/src/assets/documentation"
    receipt = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    articles = {article["source"] for article in receipt["articles"]}
    assert "model-recovery.md" in articles
    for article in receipt["articles"]:
        assert f"markdown/{article['source']}" in receipt["files"]
    # The exporter owns published/placeholder selection; every selected source,
    # including the recovery chapter, is fingerprinted from the shared guide.
    for name in articles:
        assert f"docs/offline/articles/{name}" in receipt["inputs"]
    assert all(name.startswith(("docs/offline/", "frontend/public/fonts/")) for name in receipt["inputs"])
    import re
    # Labelled hashes keep secret scanners from reading "<path with a keyword>": "<hex>" as a credential.
    for value in [*receipt["inputs"].values(), *receipt["files"].values()]:
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", value)
    for file in (bundle / "markdown").glob("*.md"):
        for destination in re.findall(r"\]\(([^\s)]+)\)", file.read_text(encoding="utf-8")):
            assert not destination.startswith("/"), (file.name, destination)
            if "://" not in destination and not destination.startswith("mailto:"):
                assert (file.parent / destination.split("#")[0]).is_file(), destination


def test_documented_github_backups_match_runtime_catalog():
    page = (ROOT / "docs/offline/articles/model-recovery.md").read_text(encoding="utf-8")
    models = {
        ModelId.PARAKEET_V2_INT8.value, ModelId.PARAKEET_V3_INT8.value,
        ModelId.QWEN3_ASR_06B_INT8.value, ModelId.COHERE_TRANSCRIBE_INT8.value,
    }
    backups = {model: source["github"] for model, source in MODEL_SOURCES.items() if "github" in source}
    assert set(backups) == models
    import re
    documented_urls = set(re.findall(r"https://github\.com/k2-fsa/sherpa-onnx/releases/download/[^)\s]+", page))
    assert documented_urls == {source["url"] for source in backups.values()}
