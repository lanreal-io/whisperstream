"""Content-free optional live Ollama probe for Windows Unit 03 evidence."""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import urllib.error
import urllib.request

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.llm_enhancer import LLMEnhancer


ENDPOINT = "http://127.0.0.1:11434"
RAW = "Please clean this reviewed synthetic dictation fixture"


def _available_model() -> str | None:
    request = urllib.request.Request(f"{ENDPOINT}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    for row in models:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]:
            return row["name"]
    return None


async def _probe(model: str) -> bool:
    config = WhisperstreamConfig(
        enhancement_enabled=True,
        enhancement_provider="ollama",
        enhancement_ollama_endpoint=ENDPOINT,
        enhancement_ollama_model=model,
        enhancement_timeout_seconds=15.0,
    )
    enhancer = LLMEnhancer(config)
    try:
        result = await enhancer._call_provider(RAW, "Clean this transcript.")
        return bool(result.strip())
    finally:
        if enhancer._client is not None and not enhancer._client.is_closed:
            await enhancer._client.aclose()


def main() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        print("FAIL platform_not_native_windows_x64")
        return 2
    model = _available_model()
    if model is None:
        print("NOT_AVAILABLE ollama_endpoint_or_model_unavailable")
        return 3
    try:
        passed = asyncio.run(_probe(model))
    except Exception as exc:
        print(f"FAIL ollama_live_adapter error_type={type(exc).__name__}")
        return 1
    print("PASS ollama_live_adapter" if passed else "FAIL ollama_live_empty")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
