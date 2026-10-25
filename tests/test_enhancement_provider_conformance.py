"""Cloud and Ollama conformance for the daily-use enhancement contract."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.configured_enhancement_provider import (
    ConfiguredEnhancementProviderAdapter,
    ProviderTransportFailure,
)
from whisperstream.enhancement_provider_contract import (
    EnhancementFailure,
    EnhancementProviderAdapter,
    EnhancementProviderKind,
    EnhancementRequest,
)
from whisperstream.llm_enhancer import LLMEnhancer


@pytest.fixture(autouse=True)
def isolated_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))


def grant(config):
    from whisperstream.byok_consent import record_consent
    from whisperstream.enhancement_security import consent_target
    target = consent_target(config)
    record_consent(target.provider, "Synthetic transport test consent", target=target)


RAW = "Please clean this reviewed synthetic dictation fixture"
SECRET = "synthetic-disposable-key"


def _request(
    request_id: str = "request-1",
    *,
    kind: EnhancementProviderKind = EnhancementProviderKind.CLOUD,
    timeout: float = 1.0,
) -> EnhancementRequest:
    return EnhancementRequest(
        request_id=request_id,
        provider_id="ollama" if kind is EnhancementProviderKind.OLLAMA else "openai",
        provider_kind=kind,
        timeout_seconds=timeout,
        transcript=RAW,
    )


def _adapter(
    transport,
    *,
    kind: EnhancementProviderKind = EnhancementProviderKind.CLOUD,
    credential: bool = True,
    endpoint: bool = True,
    lockdown: bool = False,
) -> ConfiguredEnhancementProviderAdapter:
    return ConfiguredEnhancementProviderAdapter(
        "ollama" if kind is EnhancementProviderKind.OLLAMA else "openai",
        kind,
        transport,
        credential_available=credential,
        endpoint_available=endpoint,
        lockdown=lockdown,
    )


def test_configured_adapter_satisfies_provider_protocol() -> None:
    async def transport(_request):
        return "Cleaned fixture."

    adapter = _adapter(transport)

    assert isinstance(adapter, EnhancementProviderAdapter)
    assert adapter.policy.retries == 0
    assert adapter.policy.raw_fallback is True


def test_one_attempt_success_and_all_typed_raw_fallbacks() -> None:
    calls = 0

    async def success(_request):
        nonlocal calls
        calls += 1
        return "Cleaned fixture."

    successful = asyncio.run(_adapter(success).enhance(_request()))
    assert successful.enhanced_text == "Cleaned fixture."
    assert successful.use_raw is False
    assert calls == 1

    async def empty(_request):
        return "   "

    async def http_failure(_request):
        raise ProviderTransportFailure(EnhancementFailure.HTTP)

    async def invalid(_request):
        raise KeyError("shape")

    assert asyncio.run(_adapter(empty).enhance(_request("empty"))).failure is EnhancementFailure.EMPTY_RESPONSE
    assert asyncio.run(_adapter(http_failure).enhance(_request("http"))).failure is EnhancementFailure.HTTP
    assert asyncio.run(_adapter(invalid).enhance(_request("invalid"))).failure is EnhancementFailure.INVALID_RESPONSE

    missing_secret = asyncio.run(
        _adapter(success, credential=False).enhance(_request("secret"))
    )
    missing_endpoint = asyncio.run(
        _adapter(success, endpoint=False).enhance(_request("endpoint"))
    )
    blocked = asyncio.run(
        _adapter(success, lockdown=True).enhance(_request("lockdown"))
    )
    assert missing_secret.failure is EnhancementFailure.CREDENTIAL_UNAVAILABLE
    assert missing_endpoint.failure is EnhancementFailure.ENDPOINT_UNAVAILABLE
    assert blocked.failure is EnhancementFailure.BLOCKED_BY_POLICY
    assert all(item.use_raw for item in (missing_secret, missing_endpoint, blocked))


def test_timeout_and_exact_request_cancellation_cleanup() -> None:
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait(_request):
            entered.set()
            await release.wait()
            return "too late"

        timeout_adapter = _adapter(wait)
        timed_out = await timeout_adapter.enhance(_request("timeout", timeout=0.01))

        entered.clear()
        cancel_adapter = _adapter(wait)
        task = asyncio.create_task(cancel_adapter.enhance(_request("cancel", timeout=2)))
        await entered.wait()
        wrong = await cancel_adapter.cancel("other")
        cancelled = await cancel_adapter.cancel("cancel")
        outcome = await task
        return timed_out, wrong, cancelled, outcome, cancel_adapter

    timed_out, wrong, cancelled, outcome, adapter = asyncio.run(scenario())
    assert timed_out.failure is EnhancementFailure.TIMEOUT
    assert wrong.failure is EnhancementFailure.CANCELLED
    assert cancelled.failure is EnhancementFailure.CANCELLED
    assert outcome.failure is EnhancementFailure.CANCELLED
    assert adapter._active == {}


def test_request_outcome_and_logs_hide_transcript_and_secret(caplog) -> None:
    async def fail(_request):
        raise RuntimeError(f"{RAW}:{SECRET}")

    caplog.set_level(logging.DEBUG)
    request = _request("private")
    outcome = asyncio.run(_adapter(fail).enhance(request))
    combined = repr(request) + repr(outcome) + caplog.text

    assert outcome.failure is EnhancementFailure.INVALID_RESPONSE
    assert RAW not in combined
    assert SECRET not in combined


def test_cloud_transport_uses_protected_key_once_and_explicit_nonstreaming() -> None:
    seen = []

    async def scenario():
        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Cleaned fixture."}}]},
            )

        config = WhisperstreamConfig(
            enhancement_enabled=True,
            enhancement_provider="openai",
            enhancement_openai_api_key=SECRET,
            enhancement_api_key=SECRET,
        )
        grant(config)
        enhancer = LLMEnhancer(config)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        enhancer._client = client
        try:
            return await enhancer._call_provider(RAW, "Clean this transcript.")
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    payload = json.loads(seen[0].content)
    assert result == "Cleaned fixture."
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert payload["stream"] is False


def test_enhancer_returns_raw_once_on_cloud_transport_failure(caplog) -> None:
    calls = 0

    async def scenario():
        nonlocal calls

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("synthetic timeout")

        config = WhisperstreamConfig(
            enhancement_enabled=True,
            enhancement_provider="openai",
            enhancement_openai_api_key=SECRET,
            enhancement_api_key=SECRET,
            enhancement_safeguards_enabled=False,
        )
        grant(config)
        enhancer = LLMEnhancer(config)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        enhancer._client = client
        try:
            return await enhancer.enhance(RAW)
        finally:
            await client.aclose()

    caplog.set_level(logging.WARNING)
    result = asyncio.run(scenario())
    assert result == RAW
    assert calls == 1
    assert RAW not in caplog.text
    assert SECRET not in caplog.text


class _OllamaHandler(BaseHTTPRequestHandler):
    request_payload = None

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", "0"))
        type(self).request_payload = json.loads(self.rfile.read(length))
        body = json.dumps(
            {"choices": [{"message": {"content": "Cleaned Ollama fixture."}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def test_ollama_real_loopback_transport_and_cleanup() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        config = WhisperstreamConfig(
            enhancement_enabled=True,
            enhancement_provider="ollama",
            enhancement_ollama_endpoint=f"http://127.0.0.1:{server.server_port}",
            enhancement_ollama_model="review-fixture",
            enhancement_timeout_seconds=30.0,
        )
        grant(config)
        enhancer = LLMEnhancer(config)
        try:
            result = await enhancer._call_provider(RAW, "Clean this transcript.")
            assert enhancer._client is not None
            await enhancer._client.aclose()
            return result
        finally:
            if enhancer._client is not None and not enhancer._client.is_closed:
                await enhancer._client.aclose()

    try:
        result = asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result == "Cleaned Ollama fixture."
    assert _OllamaHandler.request_payload["stream"] is False
    assert not thread.is_alive()
