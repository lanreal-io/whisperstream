"""Owned Codex plugin payload and CLI lifecycle.

Codex owns its global config and plugin cache. Whisperstream only materializes
its owner-marked local marketplace under app data, then uses Codex's JSON CLI
commands to add, inspect, refresh, and remove that exact registration.

This module handles paths adjacent to transcript-class hook payloads. Never log
CLI stdout/stderr, hook definitions, CODEX_HOME, or materialized paths.
"""

from __future__ import annotations

from .agent_plugin_runtime import (
    plugin_identity, payload_files, payload_fingerprint, payload_needs_sync,
    mark_payload_synced, remove_sync_receipt, RUNTIME_TARGET,
    legacy_dev_marketplace_owned,
)
from .macos_user_path import ensure_user_path


import importlib.resources
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import sys
import re
import subprocess
import tempfile
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)
_runtime_executable: "ContextVar[str | None]" = ContextVar(
    "codex_plugin_executable", default=None
)
_CODEX_VERSION = re.compile(r"(?:codex-cli\s+)?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")


@contextmanager
def codex_plugin_runtime():
    """Keep every command in one lifecycle operation on one chosen engine.

    The plugin cache and hook trust live in the shared Codex home, so any
    installed engine can manage them. When both the standalone CLI and the
    app's embedded engine exist, the newer one runs the commands so an old
    CLI cannot block an app that works.
    """
    token = _runtime_executable.set(_select_codex_executable())
    try:
        yield
    finally:
        _runtime_executable.reset(token)


def _codex_engine_version(executable: str) -> "tuple[int, int, int] | None":
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 4096:
        return None
    # Prerelease builds such as 0.162.0-alpha still order by their release core.
    match = _CODEX_VERSION.match(result.stdout.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _select_codex_executable() -> "str | None":
    try:
        from .agent_runtime_compatibility import desktop_codex_executable

        candidates: list[str] = []
        ensure_user_path()
        for executable in (shutil.which("codex"), desktop_codex_executable()):
            if executable and executable not in candidates:
                candidates.append(executable)
    except (OSError, TypeError):
        return None
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    ranked = sorted(
        candidates,
        key=lambda executable: _codex_engine_version(executable) or (-1, -1, -1),
        reverse=True,
    )
    return ranked[0]

_TEMPLATE_PACKAGE = "whisperstream.codex_plugin"
_MARKETPLACE_SUBPATH = Path("codex-plugin-marketplace")
_MARKETPLACE_MANIFEST = Path(".agents") / "plugins" / "marketplace.json"
_OWNER_MARKER = Path(".whisperstream-owned")
_OWNER_MARKER_BYTES = b"whisperstream-codex-plugin-marketplace-v1\n"
_CLI_TIMEOUT_SECONDS = 20.0
_TEMPLATE_FILES = (
    Path(".codex-plugin") / "plugin.json",
    Path("skills") / "speak" / "SKILL.md",
    Path("hooks") / "hooks.json",
    Path("scripts") / "forward-hook.cmd",
    Path("scripts") / "forward-hook.ps1",
    Path("scripts") / "forward-hook.sh",
)


def _template_files() -> tuple[Path, ...]:
    return tuple(path for path in _TEMPLATE_FILES
                 if sys.platform != "darwin" or path.suffix not in {".cmd", ".ps1"})


class CodexPluginConflictError(RuntimeError):
    """Raised instead of overwriting a same-named unowned marketplace."""


class CodexPluginPayloadError(RuntimeError):
    """Raised when packaged payload data is missing or malformed."""


def _template_bytes(relative_path: Path) -> bytes:
    if relative_path == Path("scripts") / "forward-hook.sh":
        # The same bounded authenticated Darwin transport serves both providers.
        return importlib.resources.files("whisperstream.claude_plugin").joinpath(
            "scripts", "forward-hook.sh"
        ).read_bytes()
    try:
        return (
            importlib.resources.files(_TEMPLATE_PACKAGE)
            .joinpath(*relative_path.parts)
            .read_bytes()
        )
    except Exception:
        return (Path(__file__).parent / "codex_plugin" / relative_path).read_bytes()


def _plugin_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(
            _template_bytes(Path(".codex-plugin") / "plugin.json").decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexPluginPayloadError("The packaged Codex manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != "whisperstream":
        raise CodexPluginPayloadError("The packaged Codex manifest identity is invalid")
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise CodexPluginPayloadError("The packaged Codex manifest has no version")
    return manifest


def _plugin_version() -> str:
    return str(_plugin_manifest()["version"])


def _marketplace_bytes() -> bytes:
    marketplace = {
        "name": plugin_identity().name,
        "interface": {"displayName": plugin_identity().display_name},
        "plugins": [
            {
                "name": plugin_identity().name,
                "source": {
                    "source": "local",
                    "path": f"./plugins/{plugin_identity().name}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    return (json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _owned_codex_plugin_paths(app_dir: Path) -> tuple[Path, Path]:
    subpath = str(_MARKETPLACE_SUBPATH) + ("-dev" if plugin_identity().dev else "")
    marketplace_dir = Path(app_dir) / subpath
    return marketplace_dir, marketplace_dir / "plugins" / plugin_identity().name


def _has_owner_marker(marketplace_dir: Path) -> bool:
    try:
        return (
            (marketplace_dir / _OWNER_MARKER).read_bytes()
            == _OWNER_MARKER_BYTES
        )
    except OSError:
        return False


def materialize_codex_plugin(app_dir: Path) -> dict[str, Any]:
    """Install or refresh only Whisperstream-owned local marketplace files."""

    _plugin_manifest()  # Preserve fail-closed validation before generating variants.
    marketplace_dir, plugin_dir = _owned_codex_plugin_paths(Path(app_dir))
    marker_path = marketplace_dir / _OWNER_MARKER
    marketplace_path = marketplace_dir / _MARKETPLACE_MANIFEST
    generated = payload_files("codex", app_dir, {
        relative: _template_bytes(relative) for relative in _template_files()
    })
    managed_targets = {plugin_dir / relative: data for relative, data in generated.items()}
    managed_targets[marketplace_path] = _marketplace_bytes()
    owned_before = _has_owner_marker(marketplace_dir)

    if not owned_before and (
        marker_path.exists() or any(path.exists() for path in managed_targets)
    ):
        raise CodexPluginConflictError(
            "A same-named Codex marketplace already exists and is not owned "
            "by Whisperstream"
        )

    changed = not owned_before
    if not owned_before:
        _atomic_write_bytes(marker_path, _OWNER_MARKER_BYTES)
    for target, expected in managed_targets.items():
        try:
            current = target.read_bytes()
        except OSError:
            current = None
        if current != expected:
            _atomic_write_bytes(target, expected)
            changed = True

    status = "installed" if not owned_before else ("updated" if changed else "current")
    logger.info("[CodexPlugin] payload status=%s", status)
    return {
        "status": status,
        "plugin_dir": str(plugin_dir),
        "marketplace_dir": str(marketplace_dir),
        "version": json.loads(generated[Path('.codex-plugin/plugin.json')])['version'],
        "payload_fingerprint": payload_fingerprint(generated),
        "refresh_required": payload_needs_sync(plugin_dir, payload_fingerprint(generated)),
    }


def _refresh_not_installed(materialized: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        **materialized,
        "source_status": materialized["status"],
        "status": "not_installed",
        "installed_version": None,
        "codex_cli_available": True,
        "codex_cli_supported": True,
        "trust_required": False,
        "trust_message": None,
        "update_available": False,
        "error": None,
    }


def _trust_message() -> str:
    """Surface-agnostic trust instruction; the UI shows per-install routes."""
    name = plugin_identity().display_name
    return (
        f"Trust {name} in Codex. In the app, open Settings > Hooks > {name}, "
        "then Trust all. In the CLI, run /hooks."
    )


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured and configured.strip():
        return Path(configured)
    return Path.home() / ".codex"


def _codex_cli_path() -> str | None:
    return _runtime_executable.get() or _select_codex_executable()


def _run_codex_plugin_cli(executable: str, arguments: list[str]):
    """Run one bounded JSON-capable Codex plugin command without a shell."""

    return subprocess.run(
        [executable, "plugin", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CLI_TIMEOUT_SECONDS,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )


def _plugin_list_arguments(marketplace: str) -> list[str]:
    """List one marketplace's plugins.

    An unscoped ``plugin list`` also queries remote marketplaces; Codex 0.159
    can take longer than the CLI timeout there, while a scoped list is local.
    """

    return ["list", "--json", "--marketplace", marketplace]


def _json_object(result: object) -> dict[str, Any] | None:
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        decoded = json.loads(getattr(result, "stdout", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _marketplace_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    records = payload.get("marketplaces")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        return None
    return records


def _installed_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    records = payload.get("installed")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        return None
    return records


def _configured_marketplace(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (record for record in records if record.get("name") == plugin_identity().name),
        None,
    )


def _marketplace_path(record: dict[str, Any]) -> str | None:
    source = record.get("source")
    if isinstance(source, dict):
        for key in ("path", "root"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("path", "root"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _same_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        actual = os.path.normcase(str(Path(value).resolve(strict=False)))
        wanted = os.path.normcase(str(expected.resolve(strict=False)))
    except (OSError, RuntimeError):
        return False
    return actual == wanted


def _installed_plugin(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        selector = (
            record.get("pluginId")
            or record.get("id")
            or record.get("selector")
        )
        if selector == plugin_identity().selector:
            return record
        if (
            record.get("name") == plugin_identity().name
            and (
                record.get("marketplaceName") == plugin_identity().name
                or record.get("marketplace") == plugin_identity().name
            )
        ):
            return record
    return None


def _installed_version(record: dict[str, Any] | None) -> str | None:
    if record is None:
        return None
    value = record.get("version")
    if isinstance(value, str) and value:
        return value
    source = record.get("source")
    if isinstance(source, dict):
        value = source.get("version")
        if isinstance(value, str) and value:
            return value
    return None


def _version_key(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _installed_record_reports_trust(installed: dict[str, Any] | None) -> bool:
    """Accept future plugin-list trust fields conservatively."""

    if not isinstance(installed, dict):
        return False
    for key in ("hooks_trusted", "hooksTrusted", "trusted"):
        if installed.get(key) is True:
            return True
    for key in ("hook_trust", "hookTrust", "trust"):
        if installed.get(key) in {"trusted", "approved"}:
            return True
    hooks = installed.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get("trusted") is True:
            return True
        if hooks.get("trust") in {"trusted", "approved"}:
            return True
    return False


def _hooks_list_reports_trust(payload: object) -> bool:
    """Return true only when every enabled Whisperstream hook is trusted."""

    if not isinstance(payload, dict):
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        return False
    matching: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        matching.extend(
            hook
            for hook in hooks
            if isinstance(hook, dict)
            and hook.get("pluginId") == plugin_identity().selector
        )
    return bool(matching) and all(
        hook.get("enabled") is True
        and hook.get("trustStatus") in {"trusted", "managed"}
        for hook in matching
    )


def _codex_hooks_trusted(
    codex_home_path: Path,
    plugin_dir: Path,
    installed: dict[str, Any] | None,
) -> bool:
    """Ask Codex's read-only app-server hook inventory for exact trust state.

    Plugin installation does not imply hook trust. The stable plugin CLI does
    not currently include trust metadata, while the documented ``hooks/list``
    app-server method returns each plugin hook's ``trustStatus``. Any protocol,
    process, or shape failure stays conservatively untrusted.
    """

    if _installed_record_reports_trust(installed):
        return True
    executable = _codex_cli_path()
    if executable is None:
        return False
    request_lines = (
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "whisperstream",
                    "title": "Whisperstream",
                    "version": _plugin_version(),
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized", "params": {}},
        {
            "method": "hooks/list",
            "id": 1,
            "params": {"cwds": [str(plugin_dir)]},
        },
    )
    stdin_text = "".join(
        json.dumps(line, separators=(",", ":")) + "\n"
        for line in request_lines
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home_path)
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            if process is not None and process.stdout is not None:
                for line in process.stdout:
                    lines.put(line)
        finally:
            lines.put(None)

    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        if process.stdin is None:
            return False
        process.stdin.write(stdin_text)
        process.stdin.flush()

        # Codex 0.149 resolves hooks/list asynchronously. Closing stdin through
        # subprocess.run(..., input=...) can make app-server exit after the
        # initialize response and before the trust inventory is emitted. Keep
        # stdin open while a reader drains JSONL output, with the same bounded
        # fail-closed behavior as the previous one-shot probe.
        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + 5.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return False
            if line is None:
                return False
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == 1:
                return _hooks_list_reports_trust(message.get("result"))
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        if reader is not None:
            reader.join(timeout=0.5)


def _base_failure(
    error: str,
    *,
    available: bool,
    supported: bool,
    materialized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.warning("[CodexPlugin] lifecycle failed reason=%s", error)
    result: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "codex_cli_available": available,
        "codex_cli_supported": supported,
        "trust_required": False,
        "error": error,
    }
    if materialized is not None:
        result = {
            **result,
            **materialized,
            "source_status": materialized["status"],
            "status": "failed",
            "error": error,
        }
    return result


def _inspect_cli(
    executable: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | str:
    try:
        marketplaces_result = _run_codex_plugin_cli(
            executable, ["marketplace", "list", "--json"]
        )
    except (OSError, subprocess.SubprocessError):
        return "codex_cli_unsupported"
    marketplaces_payload = _json_object(marketplaces_result)
    if marketplaces_payload is None:
        return "codex_cli_unsupported"
    marketplaces = _marketplace_records(marketplaces_payload)
    if marketplaces is None:
        return "marketplace_list_invalid_json"

    try:
        plugins_result = _run_codex_plugin_cli(
            executable, _plugin_list_arguments(plugin_identity().name)
        )
    except (OSError, subprocess.SubprocessError):
        return "plugin_list_failed"
    plugins_payload = _json_object(plugins_result)
    if plugins_payload is None:
        return "plugin_list_failed"
    installed = _installed_records(plugins_payload)
    if installed is None:
        return "plugin_list_invalid_json"
    return marketplaces, installed


def codex_plugin_status(app_dir: Path) -> dict[str, Any]:
    """Inspect the owned Codex integration without changing any state."""

    _plugin_manifest()
    executable = _codex_cli_path()
    if executable is None:
        return _base_failure(
            "codex_cli_unavailable", available=False, supported=False
        )
    inspected = _inspect_cli(executable)
    if isinstance(inspected, str):
        return _base_failure(
            inspected,
            available=True,
            supported=inspected != "codex_cli_unsupported",
        )
    marketplaces, installed_records = inspected
    marketplace_dir, plugin_dir = _owned_codex_plugin_paths(Path(app_dir))
    marketplace = _configured_marketplace(marketplaces)
    if marketplace is not None and not _same_path(
        _marketplace_path(marketplace), marketplace_dir
    ):
        return _base_failure(
            "marketplace_conflict", available=True, supported=True
        )

    installed = _installed_plugin(installed_records)
    generated = payload_files("codex", app_dir, {
        relative: _template_bytes(relative) for relative in _template_files()
    })
    version = json.loads(generated[Path('.codex-plugin/plugin.json')])['version']
    installed_version = _installed_version(installed)
    if marketplace is None or installed is None:
        return {
            "ok": True,
            "status": "not_installed",
            "version": version,
            "installed_version": installed_version,
            "codex_cli_available": True,
            "codex_cli_supported": True,
            "trust_required": False,
            "trust_message": None,
            "error": None,
        }

    trusted = _codex_hooks_trusted(_codex_home(), plugin_dir, installed)
    current = installed_version == version
    return {
        "ok": True,
        "status": "current" if current and trusted else "installed",
        "version": version,
        "installed_version": installed_version,
        "codex_cli_available": True,
        "codex_cli_supported": True,
        "trust_required": not trusted,
        "trust_message": None if trusted else _trust_message(),
        "update_available": not current,
        "error": None,
    }


def enable_codex_plugin(
    app_dir: Path, *, force: bool = False, refresh_only: bool = False
) -> dict[str, Any]:
    """Materialize and idempotently install/update the owned Codex plugin.

    ``force`` re-adds even an identical version (Connect is the recovery path).
    Hook trust is keyed by plugin id and hook definition, so a re-add keeps it.
    ``refresh_only`` (startup for an inactive connection) updates an existing
    install but never adds the marketplace or installs the plugin.
    """

    materialized = materialize_codex_plugin(Path(app_dir))
    executable = _codex_cli_path()
    if executable is None:
        return _base_failure(
            "codex_cli_unavailable",
            available=False,
            supported=False,
            materialized=materialized,
        )

    marketplace_dir = Path(materialized["marketplace_dir"])
    try:
        marketplaces_result = _run_codex_plugin_cli(
            executable, ["marketplace", "list", "--json"]
        )
    except (OSError, subprocess.SubprocessError):
        return _base_failure(
            "codex_cli_unsupported",
            available=True,
            supported=False,
            materialized=materialized,
        )
    marketplaces_payload = _json_object(marketplaces_result)
    if marketplaces_payload is None:
        return _base_failure(
            "codex_cli_unsupported",
            available=True,
            supported=False,
            materialized=materialized,
        )
    marketplaces = _marketplace_records(marketplaces_payload)
    if marketplaces is None:
        return _base_failure(
            "marketplace_list_invalid_json",
            available=True,
            supported=True,
            materialized=materialized,
        )
    legacy = next((item for item in marketplaces if item.get("name") == "whisperstream"
                   and legacy_dev_marketplace_owned(_marketplace_path(item), app_dir, "codex")), None)
    if legacy is not None:
        try:
            listed = _json_object(_run_codex_plugin_cli(executable, _plugin_list_arguments("whisperstream")))
            records = _installed_records(listed) if listed is not None else None
            if records is None:
                return _base_failure("plugin_list_failed", available=True, supported=True, materialized=materialized)
            if any(item.get("pluginId", item.get("id")) == "whisperstream@whisperstream"
                   or (item.get("name") == "whisperstream" and item.get("marketplace", item.get("marketplaceName")) == "whisperstream")
                   for item in records):
                if _json_object(_run_codex_plugin_cli(executable, ["remove", "whisperstream@whisperstream", "--json"])) is None:
                    return _base_failure("legacy_plugin_remove_failed", available=True, supported=True, materialized=materialized)
            if _json_object(_run_codex_plugin_cli(executable, ["marketplace", "remove", "whisperstream", "--json"])) is None:
                return _base_failure("legacy_marketplace_remove_failed", available=True, supported=True, materialized=materialized)
            marketplaces = [item for item in marketplaces if item is not legacy]
        except (OSError, subprocess.SubprocessError):
            return _base_failure("legacy_migration_failed", available=True, supported=True, materialized=materialized)
    marketplace = _configured_marketplace(marketplaces)
    if marketplace is not None and not _same_path(
        _marketplace_path(marketplace), marketplace_dir
    ):
        return _base_failure(
            "marketplace_conflict",
            available=True,
            supported=True,
            materialized=materialized,
        )
    if marketplace is None and refresh_only:
        return _refresh_not_installed(materialized)
    if marketplace is None:
        try:
            added_marketplace = _run_codex_plugin_cli(
                executable,
                ["marketplace", "add", str(marketplace_dir), "--json"],
            )
        except (OSError, subprocess.SubprocessError):
            return _base_failure(
                "marketplace_add_failed",
                available=True,
                supported=True,
                materialized=materialized,
            )
        if _json_object(added_marketplace) is None:
            return _base_failure(
                "marketplace_add_failed",
                available=True,
                supported=True,
                materialized=materialized,
            )

    try:
        listed = _run_codex_plugin_cli(
            executable, _plugin_list_arguments(plugin_identity().name)
        )
    except (OSError, subprocess.SubprocessError):
        return _base_failure(
            "plugin_list_failed",
            available=True,
            supported=True,
            materialized=materialized,
        )
    listed_payload = _json_object(listed)
    installed_records = (
        _installed_records(listed_payload) if listed_payload is not None else None
    )
    if installed_records is None:
        return _base_failure(
            "plugin_list_invalid_json",
            available=True,
            supported=True,
            materialized=materialized,
        )

    installed = _installed_plugin(installed_records)
    if installed is None and refresh_only:
        return _refresh_not_installed(materialized)
    installed_version = _installed_version(installed)
    bundled_version = str(materialized["version"])
    bundled_key = _version_key(bundled_version)
    installed_key = _version_key(installed_version)
    needs_change = installed is None or force or (
        (installed_version != bundled_version or materialized.get("refresh_required"))
        and not (
            installed_key is not None
            and bundled_key is not None
            and installed_key > bundled_key
        )
    )
    action = "installed" if installed is None else ("updated" if needs_change else "current")
    if needs_change:
        try:
            changed = _run_codex_plugin_cli(
                executable, ["add", plugin_identity().selector, "--json"]
            )
        except (OSError, subprocess.SubprocessError):
            return _base_failure(
                "plugin_add_failed",
                available=True,
                supported=True,
                materialized=materialized,
            )
        if _json_object(changed) is None:
            return _base_failure(
                "plugin_add_failed",
                available=True,
                supported=True,
                materialized=materialized,
            )

    mark_payload_synced(materialized)
    trusted = _codex_hooks_trusted(
        _codex_home(), Path(materialized["plugin_dir"]), installed
    )
    logger.info(
        "[CodexPlugin] reconcile status=%s trust_required=%s",
        action,
        not trusted,
    )
    return {
        "ok": True,
        **materialized,
        "source_status": materialized["status"],
        "status": action if not trusted else "current",
        "installed_version": bundled_version if needs_change else installed_version,
        "codex_cli_available": True,
        "codex_cli_supported": True,
        "trust_required": not trusted,
        "trust_message": None if trusted else _trust_message(),
        "update_available": False,
        "error": None,
    }


def _remove_empty_directories(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.rmdir()
        except OSError:
            pass


def _remove_owned_payload(app_dir: Path) -> str:
    marketplace_dir, plugin_dir = _owned_codex_plugin_paths(Path(app_dir))
    if not marketplace_dir.exists():
        return "not_installed"
    if not _has_owner_marker(marketplace_dir):
        return "not_owned"
    remove_sync_receipt(plugin_dir)
    (plugin_dir / RUNTIME_TARGET).unlink(missing_ok=True)
    for relative in _template_files():
        if relative == Path("skills/speak/SKILL.md"):
            relative = Path("skills") / plugin_identity().codex_skill / "SKILL.md"
        (plugin_dir / relative).unlink(missing_ok=True)
    (marketplace_dir / _MARKETPLACE_MANIFEST).unlink(missing_ok=True)
    (marketplace_dir / _OWNER_MARKER).unlink(missing_ok=True)
    _remove_empty_directories(
        (
            plugin_dir / ".codex-plugin",
            plugin_dir / "skills" / plugin_identity().codex_skill,
            plugin_dir / "skills",
            plugin_dir / "hooks",
            plugin_dir / "scripts",
            plugin_dir,
            marketplace_dir / "plugins",
            marketplace_dir / ".agents" / "plugins",
            marketplace_dir / ".agents",
            marketplace_dir,
        )
    )
    return "removed"


def disable_codex_plugin(app_dir: Path) -> dict[str, Any]:
    """Remove the exact owned plugin and marketplace through Codex's CLI."""

    executable = _codex_cli_path()
    if executable is None:
        return _base_failure(
            "codex_cli_unavailable", available=False, supported=False
        )
    inspected = _inspect_cli(executable)
    if isinstance(inspected, str):
        return _base_failure(
            inspected,
            available=True,
            supported=inspected != "codex_cli_unsupported",
        )
    marketplaces, installed_records = inspected
    marketplace_dir, _plugin_dir = _owned_codex_plugin_paths(Path(app_dir))
    marketplace = _configured_marketplace(marketplaces)
    if marketplace is not None and not _same_path(
        _marketplace_path(marketplace), marketplace_dir
    ):
        return _base_failure(
            "marketplace_conflict", available=True, supported=True
        )

    installed = _installed_plugin(installed_records)
    if installed is not None:
        try:
            removed = _run_codex_plugin_cli(
                executable, ["remove", plugin_identity().selector, "--json"]
            )
        except (OSError, subprocess.SubprocessError):
            return _base_failure(
                "plugin_remove_failed", available=True, supported=True
            )
        if _json_object(removed) is None:
            return _base_failure(
                "plugin_remove_failed", available=True, supported=True
            )
    if marketplace is not None:
        try:
            removed_marketplace = _run_codex_plugin_cli(
                executable,
                ["marketplace", "remove", plugin_identity().name, "--json"],
            )
        except (OSError, subprocess.SubprocessError):
            return _base_failure(
                "marketplace_remove_failed", available=True, supported=True
            )
        if _json_object(removed_marketplace) is None:
            return _base_failure(
                "marketplace_remove_failed", available=True, supported=True
            )

    payload_status = _remove_owned_payload(Path(app_dir))
    if payload_status == "not_owned":
        return _base_failure(
            "payload_not_owned", available=True, supported=True
        )
    status = "removed" if installed is not None or marketplace is not None or payload_status == "removed" else "not_installed"
    logger.info("[CodexPlugin] remove status=%s", status)
    return {
        "ok": True,
        "status": status,
        "codex_cli_available": True,
        "codex_cli_supported": True,
        "trust_required": False,
        "trust_message": None,
        "error": None,
    }


__all__ = [
    "CodexPluginConflictError",
    "CodexPluginPayloadError",
    "codex_plugin_status",
    "disable_codex_plugin",
    "enable_codex_plugin",
    "materialize_codex_plugin",
]
