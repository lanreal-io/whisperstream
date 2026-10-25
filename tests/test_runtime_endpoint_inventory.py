"""Source-level certification of the shipped desktop network inventory.

This is deliberately a static tripwire, not packet-capture proof.  A new
runtime HTTP client, direct WebView fetch, updater host, or browser-handoff
surface must be classified here and reconciled with the security docs.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "src" / "whisperstream"
FRONTEND_ROOT = ROOT / "frontend" / "src"


# Production-native modules which can issue HTTP requests.  The categories are
# security boundaries: managed Lockdown permits the fixed services, blocks the
# user-configurable enhancement clients, and keeps the local engines loopback.
FIXED_SERVICE_CLIENTS = {
    "macos_model_lifecycle.py",
    "model_downloader.py",
}
OPTIONAL_ENHANCEMENT_CLIENTS = {
    "llm_enhancer.py",
    "configured_enhancement_provider.py",
}
LOOPBACK_CLIENTS = {
    "gpu_benchmark.py",
    "llama_server.py",
    "speech/summarizer.py",
}
NO_REQUEST_CLIENTS = {
    # Imported for exception/type compatibility; contains no httpx call.
    "server/app.py",
    "macos_local_llm_runtime.py",
    "windows_local_llm_runtime.py",
}
DEVELOPER_ONLY_CLIENTS = {
    # fixture_tool is a separately invoked developer utility, not an
    # installed-app path, and source releases omit it.
    "fixture_tool.py",
}

# Static source inventory includes retained public recovery implementations.
FIXED_NATIVE_HOSTS = {"huggingface.co", "github.com"}
OPTIONAL_PROVIDER_HOSTS = {
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
}

BROWSER_HANDOFF_MODULES = {
    # About's explicit download-page handoff is blocked during Lockdown.
    "pages/About.tsx",
    # Review prompt's fixed GitHub star/issue handoffs, Lockdown-gated.
    "pages/ReviewPopup.tsx",
}
EXTERNAL_LINK_MODULES = {
    # target=_blank links handed to the WebView/browser integration.
    "components/PrivacyModal.tsx",
    "pages/Enhancement.tsx",
}
LOCAL_DOCUMENT_LINK_MODULES = {
    # Bundled document downloads, not remote browser destinations.
    "components/LockdownManageDialog.tsx",
    "components/LockdownSetupModal.tsx",
}


def _relative_python(path: Path) -> str:
    return path.relative_to(PYTHON_ROOT).as_posix()


def _imports_httpx(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "httpx" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "httpx":
            return True
    return False


def _literal_url_hosts(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        urlsplit(match.group(0).rstrip("`'\")],.")).hostname or ""
        for match in re.finditer(r"https?://[^\s'\"<>]+", text)
    }


def test_every_python_http_client_has_an_explicit_security_classification() -> None:
    actual = {
        _relative_python(path)
        for path in PYTHON_ROOT.rglob("*.py")
        if _imports_httpx(path)
    }
    expected = (
        FIXED_SERVICE_CLIENTS
        | OPTIONAL_ENHANCEMENT_CLIENTS
        | LOOPBACK_CLIENTS
        | NO_REQUEST_CLIENTS
        | {name for name in DEVELOPER_ONLY_CLIENTS if (PYTHON_ROOT / name).is_file()}
    )
    assert actual == expected


def test_fixed_native_service_hosts_match_the_documented_allowlist() -> None:
    hosts: set[str] = set()
    for relative in FIXED_SERVICE_CLIENTS:
        hosts |= _literal_url_hosts(PYTHON_ROOT / relative)

    # Legacy portal guidance is not a native request. GitHub now supplies the
    # pinned upstream speech-model backup archives through the shared client.
    hosts -= {"polar.sh"}
    assert hosts == FIXED_NATIVE_HOSTS

    # The sunset updater is a separate conditional service, never a Managed
    # Lockdown allowlist addition. Raw plugin IPC must remain unavailable.
    # Its endpoint comes only from the official build's Tauri overlay; the
    # shared configs name no update feed.
    for name in ("tauri.conf.json", "tauri.windows.conf.json"):
        tauri = json.loads((ROOT / "src-tauri" / name).read_text(encoding="utf-8"))
        assert "updater" not in tauri.get("plugins", {}), name
        assert "whisperstream.io" not in json.dumps(tauri), name
    for path in (ROOT / "src-tauri" / "capabilities").glob("*.json"):
        permissions = json.loads(path.read_text(encoding="utf-8"))["permissions"]
        assert not any(
            (permission if isinstance(permission, str) else permission["identifier"]).startswith("updater:")
            for permission in permissions
        )


def test_optional_provider_hosts_are_confined_to_enhancement_clients() -> None:
    hosts: set[str] = set()
    for relative in OPTIONAL_ENHANCEMENT_CLIENTS:
        hosts |= _literal_url_hosts(PYTHON_ROOT / relative)
    assert hosts == OPTIONAL_PROVIDER_HOSTS


def test_loopback_clients_contain_no_literal_external_host() -> None:
    hosts: set[str] = set()
    for relative in LOOPBACK_CLIENTS:
        hosts |= _literal_url_hosts(PYTHON_ROOT / relative)
    assert hosts <= {"127.0.0.1"}


def test_webview_has_no_literal_external_fetch() -> None:
    offenders: list[str] = []
    pattern = re.compile(r"fetch\(\s*[`'\"]https?://")
    for path in FRONTEND_ROOT.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(FRONTEND_ROOT).as_posix())
    assert offenders == []


def test_browser_handoffs_are_explicitly_classified_outside_native_requests() -> None:
    actual: set[str] = set()
    for path in FRONTEND_ROOT.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        if "@tauri-apps/plugin-shell" in source:
            actual.add(path.relative_to(FRONTEND_ROOT).as_posix())
    assert actual == BROWSER_HANDOFF_MODULES

    link_actual: set[str] = set()
    for path in FRONTEND_ROOT.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        # Include dynamic canonical disclosure links, not just literal URLs.
        if "target=\"_blank\"" in source:
            link_actual.add(path.relative_to(FRONTEND_ROOT).as_posix())
    assert link_actual == EXTERNAL_LINK_MODULES | LOCAL_DOCUMENT_LINK_MODULES


def test_review_handoffs_are_exact_and_data_free() -> None:
    review_popup = (FRONTEND_ROOT / "pages" / "ReviewPopup.tsx").read_text(
        encoding="utf-8"
    )
    urls = {
        match.group(0).rstrip("`'\")],.")
        for match in re.finditer(r"https?://[^\s'\"<>]+", review_popup)
    }
    assert urls == {
        "https://github.com/lanreal-io/whisperstream",
        "https://github.com/lanreal-io/whisperstream/issues/new/choose",
    }
    assert all("?" not in url and "#" not in url for url in urls)


def test_agent_plugin_forwarders_verify_only_their_loopback_runtime() -> None:
    for provider in ('claude', 'codex'):
        source = (PYTHON_ROOT / f'{provider}_plugin/scripts/forward-hook.ps1').read_text('utf-8')
        assert _literal_url_hosts(PYTHON_ROOT / f'{provider}_plugin/scripts/forward-hook.ps1') == {'127.0.0.1'}
        assert 'runtime-target.json' in source
        assert 'x-whisperstream-hook-token' in source
        assert source.index('/api/agent-hook/verify') < source.index(f'/api/{provider}-hook')
