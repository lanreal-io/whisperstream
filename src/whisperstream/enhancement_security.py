"""Content-free validation and identity for enhancement request admission.

No DNS lookup is used to grant a local exemption. An arbitrary loopback server
may forward content, so only the bundled Local provider is exempt from BYOK.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit


class EnhancementAdmissionError(ValueError):
    """Stable reason only: never include a URL, credential or content."""


_NAMED_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}
DISCLOSURE_VERSION = "recipient-v2-vocabulary"


def normalize_endpoint(endpoint: str, *, local_only: bool = False) -> str:
    if not isinstance(endpoint, str):
        raise EnhancementAdmissionError("invalid_endpoint")
    value = endpoint.strip()
    try:
        if not value or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname
        if "%" in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
            host = address.compressed
            if address.version == 6:
                host = f"[{host}]"
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            loopback = host == "localhost"
        port = parsed.port
        if local_only and not loopback:
            raise EnhancementAdmissionError("local_endpoint_not_loopback")
        if parsed.scheme != "https" and not loopback:
            raise EnhancementAdmissionError("endpoint_https_required")
        authority = host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))
    except EnhancementAdmissionError:
        raise
    except (ValueError, UnicodeError, AttributeError):
        raise EnhancementAdmissionError("invalid_endpoint") from None


@dataclass(frozen=True)
class ConsentTarget:
    provider: str
    endpoint: str
    model: str = ""

    @property
    def identity(self) -> str:
        payload = [DISCLOSURE_VERSION, self.provider, self.endpoint, self.model]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()


def consent_target(config, *, provider: str | None = None) -> ConsentTarget:
    selected = (provider or config.effective_provider() or "").lower()
    if selected in _NAMED_ENDPOINTS:
        return ConsentTarget(selected, _NAMED_ENDPOINTS[selected], getattr(config, f"enhancement_{selected}_model"))
    if selected == "custom":
        endpoint = normalize_endpoint(config.enhancement_custom_base_url)
        model = config.enhancement_custom_model
    elif selected == "ollama":
        endpoint = normalize_endpoint(config.enhancement_ollama_endpoint)
        model = config.enhancement_ollama_model
    elif selected == "local":
        override = config.enhancement_local_endpoint.strip()
        endpoint = normalize_endpoint(override, local_only=True) if override else "bundled-local"
        model = ""
    else:
        raise EnhancementAdmissionError("unknown_provider")
    return ConsentTarget(selected, endpoint, model)


def validate_config_endpoints(config) -> None:
    for value in (config.enhancement_custom_base_url, config.enhancement_ollama_endpoint):
        if value.strip():
            normalize_endpoint(value)
    if config.enhancement_local_endpoint.strip():
        normalize_endpoint(config.enhancement_local_endpoint, local_only=True)


def require_enhancement_admission(config) -> ConsentTarget:
    if not config.enhancement_enabled:
        raise EnhancementAdmissionError("enhancement_disabled")
    target = consent_target(config)
    if target.provider != "local":
        from .byok_consent import has_target_consent
        if not has_target_consent(target):
            raise EnhancementAdmissionError("consent_required")
    return target


def enhancement_is_admitted(config) -> bool:
    try:
        require_enhancement_admission(config)
        return True
    except EnhancementAdmissionError:
        return False
