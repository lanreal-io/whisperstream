"""Final-answer isolation and bounded, content-free workspace recovery."""
from __future__ import annotations

import re

from .configured_enhancement_provider import ProviderTransportFailure
from .enhancement_provider_contract import EnhancementFailure


class InvalidEnhancementResponse(ValueError):
    def __init__(self):
        super().__init__("Provider returned an incomplete or invalid answer")


class AnthropicWorkspaceFailure(ProviderTransportFailure):
    def __init__(self, error_code):
        self.error_code = error_code
        super().__init__(EnhancementFailure.HTTP, error_code)


class ConnectionCheckResult(tuple):
    """Keep the historical two-value API while carrying a safe optional code."""
    def __new__(cls, connected, message, error_code=None):
        result = super().__new__(cls, (connected, message))
        result.error_code = error_code
        return result


def _final_text(parts):
    text = "".join(parts).strip()
    # Some compatibility servers inline reasoning instead of separating it.
    # Reject the whole answer rather than guessing which substring to inject.
    if not text or re.search(r"</?(?:think|thinking|reasoning)(?:\s|>)", text, re.I):
        raise InvalidEnhancementResponse()
    return text


def parse_anthropic(data) -> str:
    if not isinstance(data, dict) or data.get("stop_reason") not in ("end_turn", "stop_sequence"):
        raise InvalidEnhancementResponse()
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise InvalidEnhancementResponse()
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise InvalidEnhancementResponse()
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind not in {"thinking", "redacted_thinking"}:
            raise InvalidEnhancementResponse()
    return _final_text(parts)


def parse_openai(data) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise InvalidEnhancementResponse()
    choice = choices[0]
    # Legacy compatibility endpoints omit finish_reason. Explicit non-final
    # reasons always fail closed, independently of optional output safeguards.
    if choice.get("finish_reason") not in (None, "stop"):
        raise InvalidEnhancementResponse()
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
        raise InvalidEnhancementResponse()
    content = message.get("content")
    if isinstance(content, str):
        return _final_text([content])
    if not isinstance(content, list):
        raise InvalidEnhancementResponse()
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise InvalidEnhancementResponse()
        parts.append(block["text"])
    return _final_text(parts)


def workspace_error_code(status, data, *, has_workspace):
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 4096:
        return None
    message = message.lower()
    if status == 400 and error.get("type") == "invalid_request_error" and "anthropic-workspace-id" in message:
        if any(word in message for word in ("required", "requires", "missing", "must provide", "must include", "must send")):
            return "workspace_required"
        if any(word in message for word in ("invalid", "malformed", "format", "must be a valid workspace id")):
            return "workspace_invalid"
    if status == 404 and error.get("type") == "not_found_error" and has_workspace:
        if re.search(r"\bworkspace(?:\s+[`\"'][^`\"'\r\n]{1,128}[`\"'])?\s+(?:not found|does not exist|is not accessible|is unavailable)\b", message):
            return "workspace_unavailable"
    return None
