"""Canonical displayed consent and evidence for the installed edition.

This module identifies resources; it does not activate policies or assign dates.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
from urllib.parse import urlsplit

from .enhancement_security import ConsentTarget, EnhancementAdmissionError

LABELS = {"gemini": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic", "ollama": "Ollama", "custom": "Custom endpoint"}
LINKS = {
    "gemini": [("Google privacy policy", "https://policies.google.com/privacy"), ("Gemini API terms", "https://ai.google.dev/gemini-api/terms")],
    "openai": [("OpenAI privacy policy", "https://openai.com/policies/privacy-policy/"), ("OpenAI API services agreement", "https://openai.com/policies/services-agreement/")],
    "anthropic": [("Anthropic privacy policy", "https://www.anthropic.com/legal/privacy")],
    "ollama": [("Ollama cloud documentation", "https://docs.ollama.com/cloud")],
}


def legal_resource_identity() -> dict[str, str]:
    from .eula import load_eula_policy_status, _find_bundled_resource_path
    policy = load_eula_policy_status()
    if not policy.valid:
        raise EnhancementAdmissionError("consent_documents_unavailable")
    try:
        privacy = _find_bundled_resource_path("PRIVACY.txt").read_bytes()
        version = re.search(r"^Version (\d+(?:\.\d+)+)\s*$", privacy.decode("utf-8"), re.MULTILINE)
        if version is None:
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise EnhancementAdmissionError("consent_documents_unavailable") from None
    return {
        "eula_version": policy.terms_version,
        "eula_content_hash": policy.terms_sha256,
        "privacy_policy_version": version.group(1),
        "privacy_policy_content_hash": hashlib.sha256(privacy).hexdigest(),
    }


def disclosure_lines(target: ConsentTarget) -> list[str]:
    from .config_manager import is_loopback_endpoint
    label = LABELS[target.provider]
    local = is_loopback_endpoint(target.endpoint)
    scheme = urlsplit(target.endpoint).scheme.upper()
    if target.provider in {"gemini", "openai", "anthropic"}:
        destination = f"Requests go to {label} over HTTPS."
    elif target.provider == "ollama":
        if local:
            destination = f"Requests go to Ollama on this device over {scheme}. "
            if scheme == "HTTP":
                destination += "This connection is not encrypted. "
            destination += "Ollama can use cloud models, so this address does not guarantee local processing."
        else:
            destination = "Requests go to this Ollama endpoint over HTTPS. Its operator may process your text there or forward it to a cloud model."
    elif local:
        destination = f"Requests go to a server on this device over {scheme}. "
        if scheme == "HTTP":
            destination += "This connection is not encrypted. "
        destination += "The server may forward your text to other services."
    else:
        destination = "Requests go to this endpoint over HTTPS. Its operator controls how your data is handled and may forward it to other services."
    lines = [
        "Privacy notice", label, f"Endpoint: {target.endpoint}", f"Model: {target.model}", destination,
        "What is sent",
        "Enhancement sends the text being processed and the selected profile's instructions. This includes dictation, imported transcripts and saved text you choose to enhance.",
        "Prompt tests send your sample text and the instructions being tested, even for short samples.",
        "Enhancement and prompt tests may also send relevant Boost words selected from your Dictionary. Connection checks do not send Boost words.",
        "Connection checks send fixed test text and fixed instructions. They do not send your transcripts or saved profile instructions.",
        "Requests include the model choice, required credentials and connection information. Remote services can see your public IP address.",
        "AI Enhancement sends text, not audio. Speech transcription happens on your device.",
    ]
    if target.provider == "gemini":
        lines += [
            "Before using Gemini",
            "Google restricts Gemini API use to professional or business development by adults 18 and over in supported regions.",
            "API clients serving users in the EEA, Switzerland or the UK must use Paid Services. For Gemini API, that requires a project with active billing.",
            "Under Google's unpaid-service data terms, prompts and responses may improve Google's products and be reviewed by people. Do not send personal, sensitive or confidential information under those terms. Different data terms apply in the EEA, Switzerland and the UK.",
            "Paid-service terms exclude product-improvement use of prompts and responses, but allow limited safety and legal retention. Check the terms for your account before continuing.",
        ]
    lines += [
        "Saved keys",
        ("On Mac, saved API keys are stored in macOS Keychain. macOS may ask you to allow access when saving or using them."
         if platform.system() == "Darwin" else
         "On Windows, saved API keys are encrypted for your Windows user account. Apps running as that user may be able to decrypt them."),
        "Your choice",
        "Allowing this connection covers the endpoint and model shown above. Changing either requires another privacy review. Turning Enhancement off also stops prompt tests and connection checks.",
        "Whisperstream privacy policy: bundled PRIVACY.pdf",
    ]
    lines += [f"{label}: {url}" for label, url in LINKS.get(target.provider, [])]
    return lines


def consent_context(target: ConsentTarget) -> dict:
    from .byok_consent import has_target_consent
    identity = legal_resource_identity()
    lines = disclosure_lines(target)
    body = "\n".join(lines)
    token = hashlib.sha256(json.dumps([target.identity, identity, body], sort_keys=True).encode()).hexdigest()
    return {
        "provider": target.provider, "endpoint": target.endpoint, "model": target.model,
        "recipient_identity": target.identity, "context_token": token,
        "has_consent": has_target_consent(target), "dialog_body_text": body,
        "lines": lines, "links": [{"label": label, "url": url} for label, url in LINKS.get(target.provider, [])],
        "legal_identity": identity,
    }
