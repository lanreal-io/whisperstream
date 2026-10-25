"""Shared channel identity and generated local plugin targeting.

Never log targets, hook credentials, or provider CLI output. Templates remain
portable; only owner-marked app-data payloads contain machine-local paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from . import _build_flags
from .atomic_io import atomic_write_text

VARIANT_ENV = 'WHISPERSTREAM_AGENT_PLUGIN_VARIANT'
RUNTIME_TARGET = Path('runtime-target.json')
HOOK_TOKEN_HEADER = 'x-whisperstream-hook-token'
_SYNC_RECEIPT = '.whisperstream-payload-synced'


@dataclass(frozen=True)
class PluginIdentity:
    dev: bool = False

    @property
    def name(self) -> str:
        return 'whisperstream-dev' if self.dev else 'whisperstream'

    @property
    def display_name(self) -> str:
        return 'Whisperstream Dev' if self.dev else 'Whisperstream'

    @property
    def selector(self) -> str:
        return f'{self.name}@{self.name}'

    @property
    def codex_skill(self) -> str:
        return 'speak-dev' if self.dev else 'speak'


def plugin_identity() -> PluginIdentity:
    # Compile-time false in packaged builds, including inherited dev shells.
    return PluginIdentity(
        _build_flags.DEV_TOOLS_AVAILABLE and os.environ.get(VARIANT_ENV) == 'dev'
    )


def payload_files(
    provider: str, app_dir: Path, templates: dict[Path, bytes]
) -> dict[Path, bytes]:
    identity = plugin_identity()
    result = dict(templates)
    manifest_path = Path(f'.{provider}-plugin') / 'plugin.json'
    manifest = json.loads(result[manifest_path])
    manifest['name'] = identity.name
    if identity.dev:
        if provider == 'claude':
            manifest['displayName'] = identity.display_name
        manifest['description'] = 'Connects coding-agent sessions to the local Whisperstream Dev HUD.'
        if provider == 'codex':
            skill = result.pop(Path('skills/speak/SKILL.md'))
            result[Path('skills/speak-dev/SKILL.md')] = skill.replace(
                b'\r\n', b'\n'
            ).replace(b'name: speak\n', b'name: speak-dev\n', 1)
    target = {
        'data_dir': str(app_dir.resolve()),
        'variant': 'dev' if identity.dev else 'prod',
    }
    result[RUNTIME_TARGET] = (json.dumps(target, indent=2) + '\n').encode('utf-8')
    if identity.dev:
        # Claude keeps same-version cache contents even after a successful update.
        # Content-addressed dev versions refresh through its supported CLI without
        # deleting the last working cache or continually bumping tracked versions.
        manifest['version'] += '-dev.' + payload_fingerprint(result)[:16]
    result[manifest_path] = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'
    ).encode('utf-8')
    return result


def payload_fingerprint(files: dict[Path, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(path.as_posix().encode('utf-8') + b'\0' + data + b'\0')
    return digest.hexdigest()


def payload_needs_sync(plugin_dir: Path, fingerprint: str) -> bool:
    try:
        return (plugin_dir / _SYNC_RECEIPT).read_text('ascii') != fingerprint
    except (OSError, UnicodeError):
        return True


def mark_payload_synced(materialized: dict) -> None:
    atomic_write_text(
        Path(materialized['plugin_dir']) / _SYNC_RECEIPT,
        materialized['payload_fingerprint'],
    )


def remove_sync_receipt(plugin_dir: Path) -> None:
    (plugin_dir / _SYNC_RECEIPT).unlink(missing_ok=True)


def legacy_dev_marketplace_owned(path: object, app_dir: Path, provider: str) -> bool:
    """Recognize only the old dev layout, never arbitrary same-name sources."""
    if not isinstance(path, str):
        return False
    root = Path(path)
    subpath = 'claude-plugin' if provider == 'claude' else 'codex-plugin-marketplace'
    try:
        if root.name != subpath or root.is_symlink():
            return False
        if plugin_identity().dev:
            if root.resolve() != (app_dir / subpath).resolve():
                return False
        elif root.parent.name.casefold() != 'whisperstream-dev':
            return False
        plugin = root / 'plugins/whisperstream'
        marker = plugin / '.whisperstream-owned' if provider == 'claude' else root / '.whisperstream-owned'
        expected = b'whisperstream-claude-plugin-v1\n' if provider == 'claude' else b'whisperstream-codex-plugin-marketplace-v1\n'
        manifest = json.loads((plugin / f'.{provider}-plugin/plugin.json').read_text('utf-8'))
        marketplace_path = root / ('.claude-plugin/marketplace.json' if provider == 'claude' else '.agents/plugins/marketplace.json')
        marketplace = json.loads(marketplace_path.read_text('utf-8'))
        return marker.read_bytes() == expected and manifest.get('name') == 'whisperstream' and marketplace.get('name') == 'whisperstream'
    except (OSError, ValueError, TypeError, AttributeError):
        return False
