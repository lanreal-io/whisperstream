"""Async LLM enhancement client.

Post-processes transcription output through a cloud (or local) LLM.
Supports Gemini, OpenAI, Anthropic, and Ollama.

Graceful degradation: any failure (network error, timeout, non-200 response,
unexpected exception) returns the original text unchanged.  This module never
blocks the transcription pipeline.
"""
from __future__ import annotations

import asyncio
import itertools
import copy
import logging
import os
import time
from typing import Any, Callable, Literal, Mapping, Optional, cast

import httpx

from .enhancement_outcomes import EnhancementOutcome, EnhancementResult

from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.configured_enhancement_provider import (
    ConfiguredEnhancementProviderAdapter,
    EnhancementProviderFailure,
    ProviderTransportFailure,
)
from whisperstream.digit_repair import repair_source_digits
from whisperstream.enhancement_provider_contract import (
    EnhancementFailure,
    EnhancementProviderKind,
    EnhancementProviderAdapter,
    EnhancementRequest,
)
from whisperstream.enhancement_models import get_local_model
from whisperstream.output_guard import (
    guard_digit_integrity,
    run_stage1_guards,
)
from whisperstream.vocabulary import (
    VocabularyIndex, supports_vocabulary, vocabulary_section, VOCABULARY_INSTRUCTIONS,
    LOCAL_VOCABULARY_BYTES, MAX_VOCABULARY_BYTES,
)
from whisperstream.text_guards import validate_and_fallback, validate_and_fallback_outcome


EnhancementProvider = Literal[
    "gemini", "openai", "anthropic", "ollama", "local", "custom"
]
TimeoutObserver = Callable[[EnhancementProvider], None]
ErrorObserver = Callable[[EnhancementProvider], None]
ENHANCEMENT_PROVIDERS: frozenset[str] = frozenset(
    {"gemini", "openai", "anthropic", "ollama", "local", "custom"}
)


def _bounded_provider(provider: str) -> EnhancementProvider:
    """Return the closed provider identity used by timeout outcome observers."""
    if provider in ENHANCEMENT_PROVIDERS:
        return cast(EnhancementProvider, provider)
    # Config validation prevents this in production. Retaining the generic
    # custom-provider bucket keeps direct/test construction bounded too.
    return "custom"


# Existing callers import these prompt/format contracts from this module.
# Keep those demonstrated entry points while new policy code uses their owner.
from .enhancement_prompts import (
    CHAT_PROMPT,
    GEMMA_LOCAL_CHAT_PROMPT,
    CHAT_TEMPLATE_PROMPT,
    CODE_PROMPT,
    LOCAL_CHAT_PROMPT,
    LOCAL_CODE_PROMPT,
    LOCAL_STUDENT_EMAIL_PROMPT,
    _STUDENT_V2_BOUNDARY,
    LOCAL_CHAT_PROMPT_V2,
    LOCAL_CODE_PROMPT_V2,
    LOCAL_STUDENT_EMAIL_PROMPT_V2,
    EMAIL_PROMPT,
    LOCAL_EMAIL_PROMPT,
    _wrap_transcript,
    ServingContract,
    _SERVING_CONTRACTS,
    _RAW_USER_CONTENT_CONTRACTS,
    resolve_serving_contract,
    format_user_content,
    _student_prompt_for_mode,
    resolve_registered_system_prompt,
    format_registered_user_content,
    local_generation_options,
)

# ---------------------------------------------------------------------------
# LLMEnhancer
# ---------------------------------------------------------------------------

def _resolve_timeout_seconds(configured_timeout: float = 5.0) -> float:
    """Resolve the LLM HTTP call timeout.

    The validated config value is the normal request budget. A valid positive
    developer override takes precedence over that setting.

    Defaults to 5.0s, the production latency budget for cloud providers
    on dictation post-processing. The ``WHISPERSTREAM_LLM_TIMEOUT_SECONDS``
    env var lets dev / benchmark harnesses extend it without touching
    config — local models on slower CPUs can exceed 5s and we want their
    quality measurable, not silently timed out and falling through to
    raw text. Non-numeric or non-positive overrides fall back to the setting.
    """
    raw = os.environ.get("WHISPERSTREAM_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(configured_timeout)
    try:
        value = float(raw)
    except ValueError:
        return float(configured_timeout)
    return value if value > 0 else float(configured_timeout)

# Skip LLM enhancement for transcripts with at most this many words.
# Matches VoiceInk's production default. Short utterances ("Sure.",
# "Okay thanks.") are already clean from Parakeet; routing them through
# an LLM costs a round-trip and risks degrading the output.
_SHORT_INPUT_WORD_THRESHOLD = 3


from .enhancement_policy import PromptGuardResolution, OutputPolicy, resolve_model_policy


class LLMEnhancer:
    """Post-process transcription text through an LLM.

    Create one instance per config.  Call ``enhance(text)`` in the
    transcription pipeline.  If the LLM call fails for any reason, the
    original *text* is returned silently.
    """

    def __init__(
        self,
        config: WhisperstreamConfig,
        logger: Optional[logging.Logger] = None,
        *,
        llama_server=None,
        llama_server_provider: Optional[Callable[[], object]] = None,
        local_runtime_provider: Optional[Callable[[], object]] = None,
        simulated_tps_provider: Optional[Callable[[], Optional[float]]] = None,
        local_model_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._timeout_seconds = _resolve_timeout_seconds(
            getattr(config, "enhancement_timeout_seconds", 5.0)
        )
        # Lazy-initialised shared async client + the event loop it was
        # created on. The dictation pipeline calls ``asyncio.run()`` per
        # transcription, which creates a fresh loop each time; a client
        # cached from a prior (now-dead) loop hangs on socket I/O until
        # its read timeout fires. ``_get_client()`` recreates when the
        # running loop differs from ``_client_loop``.
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[Any] = None
        # Supervisor handle for the local provider, resolved at call time.
        # Production passes ``llama_server_provider=lambda: app.llama_server``
        # so the enhancer always reads the *current* supervisor — the spawn
        # completes asynchronously after the download flow's reload_config(),
        # so capturing the handle by value here would freeze it at None.
        # Tests can still pass ``llama_server=<mock>`` directly.
        self._llama_server = llama_server
        self._llama_server_provider = llama_server_provider
        self._local_runtime_provider = local_runtime_provider
        # Bakeoff evaluators bind one explicit catalog row so a later registry
        # or global-model change cannot redirect this enhancer's prompt family
        # or serving contract. Normal production enhancers leave this unset and
        # continue resolving the current catalog row at call time.
        self._local_model_metadata = (
            copy.deepcopy(dict(local_model_metadata))
            if local_model_metadata is not None
            else None
        )
        # Dev-only latency harness (Task 1.7 / WHISPERSTREAM_SIMULATED_TPS):
        # callable returning the current simulated tok/s override, or None
        # ("Off"). Production never wires this (defaults to None, so
        # _pad_for_simulated_tps is a guaranteed no-op); the dev/test wiring
        # lives in main.py + server.py, never here.
        self._simulated_tps_provider = simulated_tps_provider
        # Populated by _call_openai_compat from the local llama-server
        # response's usage.completion_tokens (when present); read by
        # _pad_for_simulated_tps as the preferred output_tokens signal. Reset
        # to None at the top of _call_provider so a stale value from a
        # previous call/provider never leaks into a later fallback estimate.
        self._last_completion_tokens: Optional[int] = None
        # Content-free generations let the provider adapter own exact request
        # cancellation without retaining transcript text in lifecycle state.
        self._provider_request_ids = itertools.count(1)
        self._active_provider_adapters: dict[
            str, EnhancementProviderAdapter
        ] = {}

    def _notify_outcome(
        self,
        observer: Callable[[EnhancementProvider], None] | None,
        provider: str,
        outcome: str,
    ) -> None:
        """Report one bounded fallback outcome without weakening raw fallback.

        Observers are per-call and optional. They receive only the closed
        provider enum, never request, model, endpoint, exception, or transcript
        data. An observer failure is diagnostic-only and cannot escape the
        enhancer's never-raise contract.
        """
        if observer is None:
            return
        try:
            observer(_bounded_provider(provider))
        except Exception as exc:
            self._logger.warning(
                "LLM %s observer failed error_type=%s",
                outcome,
                type(exc).__name__,
            )

    @classmethod
    def for_local_evaluation(
        cls,
        config_snapshot: WhisperstreamConfig,
        *,
        model_metadata: Mapping[str, object],
        llama_server: object,
        logger: Optional[logging.Logger] = None,
        simulated_tps_provider: Optional[Callable[[], Optional[float]]] = None,
    ) -> "LLMEnhancer":
        """Create a temporary local enhancer bound to one model supervisor.

        The caller supplies a run-scoped configuration snapshot. This method
        copies it again before forcing only the ephemeral provider/model fields,
        so neither the live config nor the shared run snapshot can be mutated by
        pair construction. The local endpoint override is cleared because the
        explicit resident supervisor is authoritative for Bakeoff inference.
        """

        model_id = model_metadata.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model metadata must contain a non-empty id")
        pair_config = copy.deepcopy(config_snapshot)
        pair_config.enhancement_enabled = True
        pair_config.enhancement_provider = "local"
        pair_config.enhancement_local_model = model_id
        pair_config.enhancement_local_endpoint = ""
        return cls(
            pair_config,
            logger,
            llama_server=llama_server,
            simulated_tps_provider=simulated_tps_provider,
            local_model_metadata=model_metadata,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared AsyncClient, creating it lazily.

        Recreates the client when the running event loop differs from the
        one the cached client was bound to. The dictation pipeline calls
        ``asyncio.run()`` per transcription — each call spins up a fresh
        loop and tears it down on exit, leaving any previously-cached
        client's connection pool wired to a dead loop. Reusing it then
        hangs until the HTTP read timeout fires (the symptom: empty
        ``ReadTimeout`` after ~5s).
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        loop_changed = (
            current_loop is not None
            and self._client_loop is not None
            and current_loop is not self._client_loop
        )

        if self._client is None or self._client.is_closed or loop_changed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds, trust_env=False, follow_redirects=False,
            )
            self._client_loop = current_loop
        return self._client

    def _resolve_llama_server(self):
        """Return the live llama-server supervisor, or None if unavailable.

        Production wires this via ``llama_server_provider`` so the supervisor
        compatibility alias is read at call time (the selected resident entry
        can be promoted without reconstructing this enhancer). Explicit
        resident bindings are also accepted and unwrapped to their immutable
        supervisor. Tests fall back to the directly-passed instance.
        """
        resolved = None
        if self._llama_server_provider is not None:
            try:
                resolved = self._llama_server_provider()
            except Exception:
                return None
        else:
            resolved = self._llama_server
        # Import lazily to keep llama_server's warmup-time CHAT_PROMPT import
        # acyclic. An explicit type check is required because test doubles and
        # proxy supervisors may expose arbitrary attributes.
        from .resident_llama_pool import ResidentLlama  # noqa: PLC0415

        if isinstance(resolved, ResidentLlama):
            return resolved.supervisor
        return resolved

    def _resolve_local_runtime(self):
        """Return the live generation-bound Windows runtime, when installed."""

        if self._local_runtime_provider is None:
            return None
        try:
            return self._local_runtime_provider()
        except Exception:
            return None

    def _resolve_prompt_contract(
        self, profile_id: str | None = None
    ) -> PromptGuardResolution:
        """Prepare policy for the caller's profile; never inspect desktop focus."""
        pid = profile_id or self._config.default_profile_id
        provider = (self._config.effective_provider() or "").lower()
        profile = next((p for p in self._config.profiles if p.get("id") == pid), None)
        model_id = self._config.enhancement_local_model if provider == "local" else ""
        metadata = None
        if provider == "local":
            explicit = self._local_model_metadata
            metadata = (
                explicit if explicit is not None and explicit.get("id") == model_id
                else get_local_model(model_id)
            )
        return resolve_model_policy(provider, metadata, model_id=model_id).prepare(pid, profile)
    def _get_system_prompt(self, profile_id: str | None = None) -> str:
        """Return the prompt from the shared prompt/contract resolver."""
        return self._resolve_prompt_contract(profile_id).system_prompt

    def _vocabulary_candidates(self, text: str) -> list[str]:
        # TRANSCRIPT DATA - DO NOT LOG. Cache only the current dictionary index
        # on this enhancer, never transcripts or candidate requests globally.
        if not supports_vocabulary(self._config, self._local_model_metadata):
            return []
        terms = tuple(self._config.word_boosts)
        index = getattr(self, "_vocabulary_index", None)
        if index is None or index.source != terms:
            index = self._vocabulary_index = VocabularyIndex(terms)
        budget = (LOCAL_VOCABULARY_BYTES if self._config.effective_provider() == "local"
                  else MAX_VOCABULARY_BYTES)
        return index.select(text, byte_budget=budget)

    def _requires_api_key(self) -> bool:
        """Return True if the current provider requires a non-empty API key."""
        # Both ollama and local run on-device / on-prem; no cloud key needed.
        # Route through effective_provider() so Lockdown Mode (which forces
        # "local") reports no cloud key requirement, even when the stored
        # provider is a cloud one.
        return self._config.effective_provider() not in ("ollama", "local")

    def _skip_reason(self, text: str) -> Optional[str]:
        """Return a short reason if enhancement should be skipped, else None.

        Centralizes enhance()'s short-circuits (in their original order) so the
        chunked-cleanup path's ``will_enhance()`` can reuse the exact same gating
        instead of duplicating it and drifting. A non-None return means enhance()
        returns the raw text unchanged.
        """
        cfg = self._config
        # Disabled
        if not cfg.enhancement_enabled:
            return "disabled"
        # Key required but missing
        if self._requires_api_key() and not cfg.active_api_key():
            return "no_api_key"
        # Very short transcript (no LLM work to do)
        if (len(text.split()) <= _SHORT_INPUT_WORD_THRESHOLD
                and not self._vocabulary_candidates(text)):
            return "short_input"
        # Local provider not yet warm. The lazy lifecycle spawns + warms the
        # supervisor in the background on recording start; if it hasn't finished
        # by the time this transcript is ready, we inject raw rather than wait.
        # effective_provider() so Lockdown Mode engages the local-warmth gate.
        provider = (cfg.effective_provider() or "").lower()
        if provider == "local" and not cfg.enhancement_local_endpoint.strip():
            supervisor = self._resolve_llama_server()
            if supervisor is None or not supervisor.is_ready:
                return "local_not_warm"
        return None

    def will_enhance(self, text: str) -> bool:
        """True iff ``enhance(text)`` would actually call the LLM (no short-circuit).

        Read-only. The chunked-cleanup path uses this to decide between chunking
        (enhancement will run, so split + clean + paste per chunk) and a single
        raw paste (it won't, so paste the transcript once unchanged).
        """
        return self._skip_reason(text) is None

    def _post_process_local_output(
        self,
        *,
        raw: str,
        cleaned: str,
        profile_id: str | None,
        resolution: PromptGuardResolution | None = None,
    ) -> str:
        """Apply the single ordered post-pass used by both local call paths."""
        if not self._config.enhancement_safeguards_enabled:
            return cleaned

        # Legacy contract/mode settings no longer reject semantic or formatting edits.
        stage1 = run_stage1_guards(
            raw=raw,
            cleaned=cleaned,
            check_structure=False,
        )
        if stage1.action == "fallback":
            return stage1.final_text

        repaired = repair_source_digits(
            enhanced=stage1.final_text,
            raw=raw,
        )
        digit_integrity = guard_digit_integrity(raw=raw, cleaned=repaired)
        if digit_integrity.action == "fallback":
            return digit_integrity.final_text

        return digit_integrity.final_text

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enhance(
        self,
        text: str,
        *,
        profile_id: str | None = None,
        on_timeout: TimeoutObserver | None = None,
        on_error: ErrorObserver | None = None,
    ) -> str:
        """Return enhanced text while preserving the original string API."""
        result = await self.enhance_with_outcome(
            text,
            profile_id=profile_id,
            on_timeout=on_timeout,
            on_error=on_error,
        )
        return result.text

    async def enhance_with_outcome(
        self,
        text: str,
        *,
        profile_id: str | None = None,
        on_timeout: TimeoutObserver | None = None,
        on_error: ErrorObserver | None = None,
    ) -> EnhancementResult:
        """Return enhanced text and exactly one bounded terminal outcome.

        Skips the LLM call (returns *text* unchanged) for transcripts of 3
        or fewer words without a relevant vocabulary candidate — Parakeet already capitalizes and punctuates short
        replies cleanly ("Sure.", "Okay thanks."), so the LLM has no useful
        work and may degrade clean output. Saves a round-trip per short
        reply. Threshold matches VoiceInk's production default.

        *profile_id* selects which enhancement profile's prompt drives the
        cleanup; ``None`` uses the global ``default_profile_id`` (PR-A always
        passes nothing). PR-B will thread a per-app id here.

        Outcome observers receive only the bounded effective provider.
        ``on_timeout`` is reserved for ``httpx.TimeoutException``; ``on_error``
        reports every other operational raw-text fallback.

        Never raises.
        """
        cfg = self._config

        skip = self._skip_reason(text)
        if skip is not None:
            # Log EVERY skip reason (short_input / disabled / no_api_key /
            # local_not_warm), not just local_not_warm. A silent skip looks
            # identical to "enhancement vanished" in the log and costs a debugging
            # round to tell apart (e.g. a <=3-word transcript that never reaches
            # the LLM). INFO, not WARNING: skips are expected. Metadata only
            # (reason + word count); never the transcript text.
            self._logger.info(
                "[LLMEnhance] skipped reason=%s words_in=%d",
                skip, len(text.split()),
            )
            if skip in {"no_api_key", "local_not_warm"}:
                self._notify_outcome(
                    on_error, self._config.effective_provider() or "", "error"
                )
            outcome: EnhancementOutcome = (
                "error"
                if skip in {"no_api_key", "local_not_warm"}
                else "not_attempted"
            )
            return EnhancementResult(text, outcome)

        # effective_provider() so the dispatch + run_guards decision honor
        # Lockdown Mode (forces "local"); the stored provider is untouched.
        provider = (cfg.effective_provider() or "").lower()
        prompt_resolution = self._resolve_prompt_contract(profile_id)
        if not prompt_resolution.mode_supported:
            self._logger.info(
                "[LLMEnhance] skipped reason=unsupported_mode model=%s mode=%s",
                prompt_resolution.local_model,
                prompt_resolution.profile_mode,
            )
            return EnhancementResult(text, "not_attempted")
        system_prompt = prompt_resolution.system_prompt
        start = time.perf_counter()
        try:
            result = await self._call_provider(
                text,
                system_prompt,
                serving_contract=prompt_resolution.serving_contract,
                prompt_key=prompt_resolution.prompt_key,
                profile_mode=prompt_resolution.profile_mode,
            )
            elapsed_seconds = time.perf_counter() - start
            duration_ms = int(elapsed_seconds * 1000)
            self._logger.info(
                "[LLMEnhance] duration_ms=%d provider=%s words_in=%d chars_in=%d",
                duration_ms, provider or "unknown",
                len(text.split()), len(text),
            )
            # Dev-only latency harness (Task 1.7): pad the call up to the
            # simulated tok/s target. No-op in production -- see
            # _pad_for_simulated_tps.
            await self._pad_for_simulated_tps(result, elapsed_seconds)
            # Empty / whitespace-only LLM response: fall back to raw
            # transcript rather than typing an empty string into the
            # focused window.
            if not result.strip():
                self._notify_outcome(on_error, provider, "error")
                return EnhancementResult(text, "error")
            # check_connection() bypasses this guard by design — it does not go through enhance()
            provider_is_local = prompt_resolution.policy.output_policy is OutputPolicy.LOCAL_ORDERED
            if provider_is_local:
                processed = self._post_process_local_output(
                    raw=text,
                    cleaned=result,
                    profile_id=profile_id,
                    resolution=prompt_resolution,
                )
                if processed == text and result != text:
                    self._notify_outcome(on_error, provider, "error")
                outcome = "error" if processed == text and result != text else "success"
                return EnhancementResult(processed, outcome)
            # Preserve cloud, Ollama, and custom-provider behavior exactly:
            # their existing user-facing safeguard toggle still controls only
            # validate_and_fallback(), and they do not enter the local pass.
            processed, fell_back = validate_and_fallback_outcome(
                result,
                text,
                run_guards=cfg.enhancement_safeguards_enabled,
            )
            if fell_back:
                self._notify_outcome(on_error, provider, "error")
            return EnhancementResult(processed, "error" if fell_back else "success")
        except httpx.TimeoutException as exc:
            self._notify_outcome(on_timeout, provider, "timeout")
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.warning(
                "LLM enhancement failed, returning raw text (after %d ms) error_type=%s",
                duration_ms, type(exc).__name__,
            )
            return EnhancementResult(text, "timeout")
        except Exception as exc:
            self._notify_outcome(on_error, provider, "error")
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.warning(
                "LLM enhancement failed, returning raw text (after %d ms): %s: %r",
                duration_ms, type(exc).__name__, str(exc),
            )
            return EnhancementResult(text, "error")

    async def _pad_for_simulated_tps(self, result: str, elapsed_seconds: float) -> None:
        """Dev-only latency harness (Task 1.7 / WHISPERSTREAM_SIMULATED_TPS).

        Sleeps just long enough that the TOTAL ``enhance()`` latency matches
        ``output_tokens / simulated_tps``, padding calls that already
        completed faster than the simulated device would have. Padding
        ONLY -- the sleep floor is 0, so this can never speed a call up. A
        guaranteed no-op in production, where ``_simulated_tps_provider`` is
        unset or returns None/<=0 (dev latency simulator "Off").

        *output_tokens* prefers the local llama-server response's
        ``usage.completion_tokens`` (captured by ``_call_openai_compat`` into
        ``self._last_completion_tokens``); when unavailable, falls back to a
        words-to-tokens estimate of ``len(result.split()) * 1.3``.
        """
        if self._simulated_tps_provider is None:
            return
        try:
            simulated_tps = self._simulated_tps_provider()
        except Exception:
            return
        if not simulated_tps or simulated_tps <= 0:
            return

        output_tokens = self._last_completion_tokens
        if not output_tokens or output_tokens <= 0:
            output_tokens = len(result.split()) * 1.3

        target_seconds = output_tokens / simulated_tps
        pad_seconds = max(0.0, target_seconds - elapsed_seconds)
        if pad_seconds > 0:
            await asyncio.sleep(pad_seconds)

    async def enhance_chunk(
        self,
        chunk: str,
        *,
        profile_id: str | None = None,
        on_timeout: TimeoutObserver | None = None,
        on_error: ErrorObserver | None = None,
    ) -> str:
        """Return one cleaned chunk while preserving the original string API."""
        result = await self.enhance_chunk_with_outcome(
            chunk,
            profile_id=profile_id,
            on_timeout=on_timeout,
            on_error=on_error,
        )
        return result.text

    async def enhance_chunk_with_outcome(
        self,
        chunk: str,
        *,
        profile_id: str | None = None,
        on_timeout: TimeoutObserver | None = None,
        on_error: ErrorObserver | None = None,
    ) -> EnhancementResult:
        """Clean one chunk and return its bounded terminal outcome.

        For the chunked-cleanup path. Assumes the caller already confirmed
        ``will_enhance()`` for the full transcript, so the short-circuits are NOT
        re-checked here — every chunk is sent to the LLM. Mirrors enhance()'s
        empty-result guard and ``validate_and_fallback`` rule on a per-chunk basis.

        *profile_id* selects which enhancement profile's prompt drives the
        cleanup (Power Mode routing); ``None`` uses the global default profile.
        Resolved once here and reused for every chunk by the caller.
        Outcome observers follow ``enhance()``'s bounded fallback contract.
        Never raises.
        """
        cfg = self._config
        # effective_provider() so the dispatch + run_guards decision honor
        # Lockdown Mode (forces "local"); the stored provider is untouched.
        provider = (cfg.effective_provider() or "").lower()
        prompt_resolution = self._resolve_prompt_contract(profile_id)
        if not prompt_resolution.mode_supported:
            self._logger.info(
                "[LLMEnhance] chunk skipped reason=unsupported_mode model=%s mode=%s",
                prompt_resolution.local_model,
                prompt_resolution.profile_mode,
            )
            return EnhancementResult(chunk, "not_attempted")
        system_prompt = prompt_resolution.system_prompt
        start = time.perf_counter()
        try:
            result = await self._call_provider(
                chunk,
                system_prompt,
                serving_contract=prompt_resolution.serving_contract,
                prompt_key=prompt_resolution.prompt_key,
                profile_mode=prompt_resolution.profile_mode,
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.info(
                "[LLMEnhance] chunk duration_ms=%d provider=%s words_in=%d chars_in=%d",
                duration_ms, provider or "unknown",
                len(chunk.split()), len(chunk),
            )
            # Empty / whitespace-only response: keep the raw chunk rather than
            # pasting an empty block into the focused window.
            if not result.strip():
                self._notify_outcome(on_error, provider, "error")
                return EnhancementResult(chunk, "error")
            provider_is_local = prompt_resolution.policy.output_policy is OutputPolicy.LOCAL_ORDERED
            if provider_is_local:
                processed = self._post_process_local_output(
                    raw=chunk,
                    cleaned=result,
                    profile_id=profile_id,
                    resolution=prompt_resolution,
                )
                if processed == chunk and result != chunk:
                    self._notify_outcome(on_error, provider, "error")
                outcome: EnhancementOutcome = (
                    "error" if processed == chunk and result != chunk else "success"
                )
                return EnhancementResult(processed, outcome)
            processed, fell_back = validate_and_fallback_outcome(
                result,
                chunk,
                run_guards=cfg.enhancement_safeguards_enabled,
            )
            if fell_back:
                self._notify_outcome(on_error, provider, "error")
            return EnhancementResult(processed, "error" if fell_back else "success")
        except httpx.TimeoutException as exc:
            self._notify_outcome(on_timeout, provider, "timeout")
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.warning(
                "LLM chunk enhancement failed, returning raw chunk (after %d ms) error_type=%s",
                duration_ms, type(exc).__name__,
            )
            return EnhancementResult(chunk, "timeout")
        except Exception as exc:
            self._notify_outcome(on_error, provider, "error")
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.warning(
                "LLM chunk enhancement failed, returning raw chunk (after %d ms): %s: %r",
                duration_ms, type(exc).__name__, str(exc),
            )
            return EnhancementResult(chunk, "error")

    async def preview(
        self,
        text: str,
        system_prompt: str,
        *,
        profile_id: str | None = None,
    ) -> str:
        """Run the configured provider on *text* with its preview contract.

        Powers the per-profile "Try it" prompt tester (POST
        /api/enhancement/preview). Unlike ``enhance``/``enhance_chunk`` this
        bypasses the skip-reason gating (short-input, etc.) and the
        ``validate_and_fallback`` output guards: the tester wants to show the
        provider's raw cleaned output, including on short samples. Editable
        models use the exact in-editor prompt; fixed local candidates resolve
        the selected profile through the registry. On any provider failure it
        returns *text* unchanged so the caller always has something to show.

        Never logs the sample text or the output (transcript-logging ban): only
        provider + duration metadata. Never raises.
        """
        # effective_provider() so the preview dispatch honors Lockdown Mode
        # (forces "local"); the stored provider is untouched.
        provider = (self._config.effective_provider() or "").lower()
        start = time.perf_counter()
        try:
            call_kwargs: dict[str, str | None] = {}
            if provider == "local":
                resolution = self._resolve_prompt_contract(profile_id)
                fixed_prompt = resolve_registered_system_prompt(
                    resolution.prompt_key,
                    resolution.profile_mode,
                    resolution.serving_contract,
                )
                if fixed_prompt is not None:
                    if not resolution.mode_supported:
                        return text
                    system_prompt = fixed_prompt
                    call_kwargs = {
                        "serving_contract": resolution.serving_contract,
                        "prompt_key": resolution.prompt_key,
                        "profile_mode": resolution.profile_mode,
                    }
            result = await self._call_provider(text, system_prompt, **call_kwargs)
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.info(
                "[LLMEnhance] preview duration_ms=%d provider=%s words_in=%d",
                duration_ms, provider or "unknown", len(text.split()),
            )
            return result if result.strip() else text
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._logger.warning(
                "LLM preview failed, returning raw text (after %d ms) error_type=%s",
                duration_ms, type(exc).__name__,
            )
            return text

    async def _call_provider(
        self,
        text: str,
        system_prompt: str | None = None,
        *,
        serving_contract: str | None = None,
        prompt_key: str = "",
        profile_mode: str = "chat",
        use_vocabulary: bool = True,
    ) -> str:
        """Dispatch to the correct provider and return the cleaned text.

        *system_prompt* is the resolved enhancement-profile prompt; when
        ``None``, the default profile's prompt is
        resolved here. *serving_contract* is passed only by production
        enhancement paths after resolving the selected local model. ``None``
        preserves wrapped legacy/cloud behavior for preview and connectivity
        calls.
        """
        from .enhancement_security import require_enhancement_admission
        require_enhancement_admission(self._config)
        if system_prompt is None:
            resolution = self._resolve_prompt_contract()
            system_prompt = resolution.system_prompt
            if (self._config.effective_provider() or "").lower() == "local":
                serving_contract = serving_contract or resolution.serving_contract
                prompt_key = prompt_key or resolution.prompt_key
                profile_mode = resolution.profile_mode
        # Reset before dispatch: only _call_openai_compat's local branch sets
        # this (from the llama-server response's usage), so a stale count
        # from a previous call/provider never leaks into the Task 1.7 padding
        # fallback estimate.
        self._last_completion_tokens = None
        # effective_provider() is the dispatch chokepoint: under Lockdown Mode
        # it forces "local" so the cloud branches below are unreachable, while
        # the stored enhancement_provider is left untouched for restore-on-off.
        provider = self._config.effective_provider()
        vocabulary = self._vocabulary_candidates(text) if use_vocabulary else []
        if vocabulary:
            system_prompt += VOCABULARY_INSTRUCTIONS
        vocabulary_kwargs = {"vocabulary": vocabulary} if vocabulary else {}
        if provider == "local":
            runtime = self._resolve_local_runtime()
            if runtime is None:
                # Direct unit tests and explicit endpoint overrides retain the
                # long-standing supervisor seam. Production Windows installs
                # the generation-bound runtime through WhisperstreamApp.
                return await self._call_openai_compat(
                    text,
                    system_prompt,
                    serving_contract=serving_contract,
                    prompt_key=prompt_key,
                    profile_mode=profile_mode,
                    **vocabulary_kwargs,
                )

            from .local_llm_runtime_contract import LocalInferenceRequest, RuntimeFailure

            request = LocalInferenceRequest(
                f"local-{next(self._provider_request_ids)}",
                runtime.current_generation,
                self._timeout_seconds,
                text,
            )

            async def transport(_request: LocalInferenceRequest) -> str:
                return await self._call_openai_compat(
                    text,
                    system_prompt,
                    serving_contract=serving_contract,
                    prompt_key=prompt_key,
                    profile_mode=profile_mode,
                    **vocabulary_kwargs,
                )

            outcome = await runtime.infer(request, transport)
            if outcome.use_raw or outcome.enhanced_text is None:
                if outcome.failure is RuntimeFailure.INFERENCE_TIMEOUT:
                    raise httpx.ReadTimeout("local enhancement deadline exceeded")
                raise RuntimeError(
                    "local runtime returned raw fallback "
                    f"reason={outcome.failure.value if outcome.failure else 'unknown'}"
                )
            return outcome.enhanced_text

        kind = (
            EnhancementProviderKind.OLLAMA
            if provider == "ollama"
            else EnhancementProviderKind.CLOUD
        )
        request_id = f"enhancement-{next(self._provider_request_ids)}"
        request = EnhancementRequest(
            request_id=request_id,
            provider_id=provider or "openai",
            provider_kind=kind,
            timeout_seconds=self._timeout_seconds,
            transcript=text,
        )

        transport_errors: list[Exception] = []

        async def transport(_request: EnhancementRequest) -> str:
            try:
                if provider == "anthropic":
                    return await self._call_anthropic(
                        text,
                        system_prompt,
                        serving_contract=serving_contract,
                        **vocabulary_kwargs,
                    )
                return await self._call_openai_compat(
                    text,
                    system_prompt,
                    serving_contract=serving_contract,
                    prompt_key=prompt_key,
                    profile_mode=profile_mode,
                    **vocabulary_kwargs,
                )
            except Exception as exc:
                transport_errors.append(exc)
                raise

        endpoint_available = True
        if provider == "ollama":
            endpoint_available = bool(self._config.enhancement_ollama_endpoint.strip())
        elif provider == "custom":
            endpoint_available = bool(self._config.enhancement_custom_base_url.strip())
        adapter = ConfiguredEnhancementProviderAdapter(
            request.provider_id,
            kind,
            transport,
            credential_available=(
                kind is EnhancementProviderKind.OLLAMA
                or bool(self._config.active_api_key())
            ),
            endpoint_available=endpoint_available,
            lockdown=bool(getattr(self._config, "lockdown_mode", False)),
        )
        self._active_provider_adapters[request_id] = adapter
        try:
            outcome = await adapter.enhance(request)
        finally:
            self._active_provider_adapters.pop(request_id, None)
        if outcome.failure is not EnhancementFailure.NONE:
            if outcome.failure is EnhancementFailure.TIMEOUT:
                raise httpx.ReadTimeout("enhancement provider deadline exceeded")
            if transport_errors:
                raise transport_errors[0]
            raise EnhancementProviderFailure(outcome.failure)
        assert outcome.enhanced_text is not None
        return outcome.enhanced_text

    async def cancel_provider_request(self, request_id: str) -> bool:
        """Cancel one exact in-flight cloud/Ollama request generation."""

        adapter = self._active_provider_adapters.get(request_id)
        if adapter is None:
            return False
        await adapter.cancel(request_id)
        return True

    async def _call_openai_compat(
        self,
        text: str,
        system_prompt: str,
        *,
        serving_contract: str | None = None,
        prompt_key: str = "",
        profile_mode: str = "chat",
        vocabulary: list[str] | None = None,
    ) -> str:
        """Call an OpenAI-compatible /v1/chat/completions endpoint."""
        cfg = self._config
        from .enhancement_security import (
            EnhancementAdmissionError, require_enhancement_admission, normalize_endpoint,
        )
        target = require_enhancement_admission(cfg)
        # effective_provider() picks the endpoint URL: under Lockdown Mode it
        # forces "local", so the cloud-URL branches are never selected while
        # the stored enhancement_provider stays intact for restore-on-off.
        provider = cfg.effective_provider()

        if provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            model = cfg.enhancement_gemini_model
            headers = {"Authorization": f"Bearer {cfg.active_api_key()}"}
        elif provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            model = cfg.enhancement_openai_model
            headers = {"Authorization": f"Bearer {cfg.active_api_key()}"}
        elif provider == "local":
            # llama-server speaks the same OpenAI-compat protocol as Ollama.
            # URL resolution priority:
            #   1. enhancement_local_endpoint override (power-user, manual llama-server)
            #   2. supervisor.base_url (auto-spawned bundled server)
            #   3. neither available / supervisor not ready → raise so enhance()
            #      degrades to raw text. We check ``is_ready`` (subprocess up
            #      AND warmup complete) rather than ``is_running`` so the
            #      user's first dictation after a cold start injects raw
            #      immediately instead of waiting 5-12s for the model to warm.
            supervisor = self._resolve_llama_server()
            if supervisor is None:
                raise RuntimeError("local llama-server unavailable")
            if not supervisor.is_ready:
                raise RuntimeError("local llama-server not yet warm")
            # Every Local override and supervisor address must stay loopback.
            override = cfg.enhancement_local_endpoint.strip()
            endpoint = normalize_endpoint(override or supervisor.base_url, local_only=True)
            # Accept either a bare base URL or one already including /v1.
            url = (f"{endpoint}/chat/completions" if endpoint.endswith("/v1")
                   else f"{endpoint}/v1/chat/completions")
            model = cfg.enhancement_local_model
            headers = {}
        elif provider == "ollama":
            endpoint = target.endpoint
            url = f"{endpoint}/v1/chat/completions"
            model = cfg.enhancement_ollama_model
            headers = {}
        elif provider == "custom":
            endpoint = target.endpoint
            # Accept base URLs with or without a trailing /v1 (mirror local/ollama normalization).
            url = (f"{endpoint}/chat/completions" if endpoint.endswith("/v1")
                   else f"{endpoint}/v1/chat/completions")
            model = cfg.enhancement_custom_model
            headers = {"Authorization": f"Bearer {cfg.active_api_key()}"}
        else:
            raise EnhancementAdmissionError("provider_dispatch_mismatch")

        request_policy = resolve_model_policy(
            provider, {"prompt_key": prompt_key, "serving_contract": serving_contract},
            model_id=model,
        )
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": request_policy.user_content(
                        text, profile_mode=profile_mode,
                    ) + vocabulary_section(vocabulary or []),
                },
            ],
        }

        # Local provider (gemma 4 E2B via bundled llama-server) requires
        # vendor-recommended sampling to reproduce the 36/36 stable result
        # from the iter 4 variance characterization. Without these explicit
        # values, llama-server falls back to its own defaults and quality
        # drifts. Cloud providers keep their own per-provider defaults; the
        # iter 4 cloud benchmarks were taken at those defaults, so changing
        # them now would invalidate the unified-prompt decision.
        payload.update(request_policy.generation_options())
        from .enhancement_thinking import thinking_options
        payload.update(thinking_options(cfg))

        client = self._get_client()
        response = await client.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            # Do not log response.text: some providers echo the submitted
            # transcript inside error payloads, which would route user
            # dictation content into whisperstream.log (F-R4-2).
            self._logger.warning(
                "LLM provider %s returned HTTP %d",
                provider,
                response.status_code,
            )
            raise ProviderTransportFailure(
                EnhancementFailure.HTTP,
                f"HTTP {response.status_code}",
            )

        data = response.json()
        # Task 1.7 dev latency harness: capture the local llama-server's
        # completion_tokens (when the response includes a usage block) so
        # _pad_for_simulated_tps can pad against the REAL output length
        # instead of the word-count estimate. Cloud OpenAI-compat providers
        # also return usage, but the harness is only exercised against local
        # -- harmless either way since simulated_tps is None in production.
        usage = data.get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        completion_tokens = usage.get("completion_tokens")
        if isinstance(completion_tokens, (int, float)) and completion_tokens > 0:
            self._last_completion_tokens = completion_tokens
        from .enhancement_response import parse_openai
        return parse_openai(data)

    async def _call_anthropic(
        self,
        text: str,
        system_prompt: str,
        *,
        serving_contract: str | None = None,
        vocabulary: list[str] | None = None,
    ) -> str:
        """Call the Anthropic /v1/messages endpoint."""
        cfg = self._config
        from .enhancement_security import EnhancementAdmissionError, require_enhancement_admission
        target = require_enhancement_admission(cfg)
        if target.provider != "anthropic":
            raise EnhancementAdmissionError("provider_dispatch_mismatch")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": cfg.active_api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if cfg.enhancement_anthropic_workspace_id:
            headers["anthropic-workspace-id"] = cfg.enhancement_anthropic_workspace_id
        payload = {
            "model": cfg.enhancement_anthropic_model,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": format_user_content(text, serving_contract) + vocabulary_section(vocabulary or []),
                }
            ],
        }

        from .enhancement_thinking import thinking_options
        payload.update(thinking_options(cfg))
        if payload.get("thinking", {}).get("type") == "adaptive":
            # Thinking shares the output budget. Keep the existing deadline.
            payload["max_tokens"] = 4096

        client = self._get_client()
        response = await client.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            # See note above: do not log response.text.
            from .enhancement_response import workspace_error_code, AnthropicWorkspaceFailure
            try:
                error_code = workspace_error_code(response.status_code, response.json(),
                                                  has_workspace=bool(cfg.enhancement_anthropic_workspace_id))
            except (ValueError, TypeError):
                error_code = None
            if error_code:
                raise AnthropicWorkspaceFailure(error_code)
            self._logger.warning(
                "Anthropic API returned HTTP %d",
                response.status_code,
            )
            raise ProviderTransportFailure(
                EnhancementFailure.HTTP,
                f"HTTP {response.status_code}",
            )

        data = response.json()
        from .enhancement_response import parse_anthropic
        return parse_anthropic(data)

    async def check_connection(self) -> tuple[bool, str]:
        """Validate the API key/connection by making a minimal test request.

        Returns ``(True, "Connected")`` on success or ``(False, error_message)``
        on failure.  Never raises.
        """
        cfg = self._config

        if not cfg.enhancement_enabled:
            return (False, "Enhancement is disabled")

        if self._requires_api_key() and not cfg.active_api_key():
            return (False, "API key is not set")

        try:
            # Use a minimal single-token prompt so usage is negligible.
            result = await self._call_provider("Hi", "Reply with OK.", use_vocabulary=False)
            if isinstance(result, str) and result.strip():
                return (True, "Connected")
            return (False, "Empty response")
        except Exception as exc:
            from .enhancement_response import AnthropicWorkspaceFailure, ConnectionCheckResult
            if isinstance(exc, AnthropicWorkspaceFailure):
                return ConnectionCheckResult(False, "Anthropic workspace settings need attention.", exc.error_code)
            # Provider exceptions can contain request URLs or echoed input.
            message = str(exc) if isinstance(exc, ProviderTransportFailure) else "Connection failed. Check your settings and try again."
            return (False, message)
