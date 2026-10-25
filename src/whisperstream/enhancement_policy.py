"""Model behavior policies composed by the shared enhancement service.

Selection is metadata-based and carries no authorization. Execution must perform
fresh admission even when a prepared prompt or descriptor has been retained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .enhancement_prompts import (
    CHAT_PROMPT, CODE_PROMPT, GEMMA_LOCAL_CHAT_PROMPT, LOCAL_EMAIL_PROMPT,
    LOCAL_CODE_PROMPT, LOCAL_CHAT_PROMPT, STREAM_MINI_PROMPT_KEYS,
    ServingContract,
    resolve_registered_system_prompt, resolve_serving_contract,
    format_registered_user_content, local_generation_options,
)
from .output_guard import GuardContract


class ModelBehaviorFamily(str, Enum):
    CUSTOMIZABLE = "customizable"
    TRAINED_MODE = "trained-mode"


class OutputPolicy(str, Enum):
    LOCAL_ORDERED = "local-ordered"
    REMOTE_TOGGLE = "remote-toggle"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    family: ModelBehaviorFamily
    editable_prompt: bool
    supported_modes: tuple[str, ...] | None

    def public_dict(self) -> dict[str, object]:
        """Safe UI description, excluding paths, endpoints, prompts and secrets."""
        return {
            "family": self.family.value,
            "editable_prompt": self.editable_prompt,
            "supported_modes": list(self.supported_modes) if self.supported_modes is not None else None,
        }


def model_capabilities(provider: str, metadata: Mapping[str, object] | None = None) -> ModelCapabilities:
    """Describe behavior without inferring model family from an extensible ID."""
    key = str((metadata or {}).get("prompt_key", "")) if provider == "local" else ""
    modes = (metadata or {}).get("trained_modes") if provider == "local" else None
    supported = tuple(m for m in modes if isinstance(m, str)) if isinstance(modes, list) else None
    if key == "stream" or key in STREAM_MINI_PROMPT_KEYS:
        return ModelCapabilities(ModelBehaviorFamily.TRAINED_MODE, False, supported)
    return ModelCapabilities(ModelBehaviorFamily.CUSTOMIZABLE, True, supported)


@dataclass(frozen=True, slots=True)
class PromptGuardResolution:
    """One prepared prompt, format, sampling and output contract for a call."""
    policy: EnhancementModelPolicy = field(repr=False)
    effective_provider: str
    profile_id: str
    profile_mode: str
    local_model: str
    prompt_key: str
    serving_contract: ServingContract | None
    mode_supported: bool
    system_prompt: str = field(repr=False)
    contract: GuardContract


@dataclass(frozen=True, slots=True)
class EnhancementModelPolicy:
    provider: str
    model_id: str
    prompt_key: str
    serving_contract: ServingContract | None
    capabilities: ModelCapabilities
    output_policy: OutputPolicy

    def user_content(self, text: str, *, profile_mode: str) -> str:
        # TRANSCRIPT DATA - DO NOT LOG. The caller owns the request lifetime.
        return format_registered_user_content(
            text, prompt_key=self.prompt_key, profile_mode=profile_mode,
            serving_contract=self.serving_contract,
        )

    def generation_options(self) -> dict[str, object]:
        if self.provider != "local":
            return {}
        options: dict[str, object] = dict(local_generation_options(self.prompt_key))
        if self.prompt_key in STREAM_MINI_PROMPT_KEYS:
            # Reuse the fixed system-prompt prefix across requests; each request
            # still carries only the current dictation, never prior turns.
            options["cache_prompt"] = True
        if self.prompt_key in STREAM_MINI_PROMPT_KEYS:
            options["chat_template_kwargs"] = {"enable_thinking": False}
        return options

    def prepare(self, profile_id: str, profile: Mapping[str, object] | None) -> PromptGuardResolution:
        """Resolve the captured profile's exact existing prompt and guard rules."""
        pid = profile_id
        provider = self.provider
        model_id = self.model_id
        prompt_key = self.prompt_key
        serving_contract = self.serving_contract
        profile_prompt = profile.get("prompt") if profile is not None else ""
        system_prompt = profile_prompt or CHAT_PROMPT
        configured_mode = (profile or {}).get("mode")
        if pid in {"chat", "code", "email"}:
            profile_mode = pid
        elif configured_mode in {"chat", "code", "email"}:
            profile_mode = str(configured_mode)
        else:
            profile_mode = "chat"
        contract = GuardContract("unknown", "unknown", check_structure=False)

        trained_modes = self.capabilities.supported_modes
        mode_supported = (
            trained_modes is None or profile_mode in trained_modes
        )

        if provider == "local":
            registered_prompt = resolve_registered_system_prompt(
                prompt_key,
                profile_mode,
                serving_contract,
            )
            if registered_prompt is not None:
                system_prompt = registered_prompt
                if profile_mode == "email":
                    contract = GuardContract("email", "local_email")
                elif profile_mode == "code":
                    contract = GuardContract("code_stream", "stream_code")
                else:
                    contract = GuardContract("chat_stream", "stream_chat")
            elif pid == "email":
                system_prompt = LOCAL_EMAIL_PROMPT
                contract = (
                    GuardContract("email", "local_email")
                    if prompt_key in {"gemma", "stream"}
                    else GuardContract("unknown", "local_email", check_structure=False)
                )
            elif prompt_key == "stream":
                if profile_mode == "code":
                    system_prompt = LOCAL_CODE_PROMPT
                    contract = GuardContract("code_stream", "stream_code")
                else:
                    system_prompt = LOCAL_CHAT_PROMPT
                    contract = GuardContract("chat_stream", "stream_chat")
            elif prompt_key == "gemma" and pid == "chat":
                system_prompt = GEMMA_LOCAL_CHAT_PROMPT
                contract = GuardContract("chat_gemma", "gemma_chat")
            elif prompt_key == "gemma" and pid == "code" and system_prompt == CODE_PROMPT:
                contract = GuardContract("code_gemma", "gemma_code")
            elif profile is not None and str(profile_prompt).strip():
                contract = GuardContract("custom", "custom", check_structure=False)

        return PromptGuardResolution(
            policy=self,
            effective_provider=provider,
            profile_id=pid,
            profile_mode=profile_mode,
            local_model=model_id,
            prompt_key=prompt_key,
            serving_contract=serving_contract,
            mode_supported=mode_supported,
            system_prompt=system_prompt,
            contract=contract,
        )



def resolve_model_policy(
    provider: str,
    metadata: Mapping[str, object] | None = None,
    *,
    model_id: str = "",
) -> EnhancementModelPolicy:
    """Select one behavior family independently from provider protocol choice."""
    provider = provider.lower()
    local = provider == "local"
    return EnhancementModelPolicy(
        provider=provider,
        model_id=model_id if local else "",
        prompt_key=str((metadata or {}).get("prompt_key", "")) if local else "",
        serving_contract=resolve_serving_contract(metadata) if local else None,
        capabilities=model_capabilities(provider, metadata),
        output_policy=OutputPolicy.LOCAL_ORDERED if provider in {"", "local"} else OutputPolicy.REMOTE_TOGGLE,
    )
