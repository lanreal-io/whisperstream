"""Shared helpers for the build and release scripts.

Native-exe runner, version helpers, and Tee log helper. No bash.exe or MSYS
coreutil spawns anywhere in this module.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

# ---------------------------------------------------------------------------
# Repo root (absolute, no cd)
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
"""Absolute path to the repository root, resolved from this file's location."""


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

log = logging.getLogger("build")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Native-exe runner (no bash.exe / coreutil)
# ---------------------------------------------------------------------------

AZURE_SIGNING_SECRETS = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")
TAURI_SIGNING_SECRETS = ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD")
AWS_UPLOAD_SECRETS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
_BUILD_SECRETS = frozenset((*AZURE_SIGNING_SECRETS, *TAURI_SIGNING_SECRETS,
    *AWS_UPLOAD_SECRETS, "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "WHISPERSTREAM_CF_PURGE_TOKEN"))


def child_environment(env=None, *, permitted_secrets: Sequence[str] = ()) -> dict[str, str]:
    """Grant credentials only to an explicitly scoped signer or publisher."""
    permitted = frozenset(permitted_secrets)
    if not permitted <= _BUILD_SECRETS:
        raise ValueError("unknown child credential scope")
    return {key: value for key, value in (os.environ if env is None else env).items()
            if key.upper() not in _BUILD_SECRETS or key.upper() in permitted}

def run(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    permitted_secrets: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """Run a native executable via subprocess.

    Logs ``[build] exec run argv=...`` before each call. Raises
    ``subprocess.CalledProcessError`` on nonzero exit when ``check=True``.
    When ``capture=True``, stdout and stderr are captured and returned in the
    result object rather than streamed to the terminal.

    Args:
        cmd: The command and its arguments as a list of strings. Must be
            native Windows exes (git.exe, cargo.exe, etc.), NOT bash or
            coreutils.
        cwd: Working directory for the subprocess. Defaults to REPO_ROOT.
        env: Optional environment dict. The current environment is used when
            absent; signing/publication credentials are removed in either case.
        permitted_secrets: Explicit signer/publisher credential grant.
        check: Raise on nonzero exit code (default True).
        capture: Capture stdout + stderr instead of streaming (default False).

    Returns:
        ``subprocess.CompletedProcess`` with returncode (and stdout/stderr
        when capture=True).
    """
    resolved_cwd = str(cwd) if cwd is not None else str(REPO_ROOT)
    # Resolve argv[0] through shutil.which so .cmd/.bat shims (npm, npx) are
    # found. CreateProcess (shell=False) only auto-appends .exe, never .cmd, so
    # a bare "npm" raises WinError 2. shutil.which honors PATHEXT and returns
    # the full path. Falls back to the original name when not found (so a
    # genuinely-missing tool still raises the same error, not a masked one).
    argv = list(cmd)
    resolved = shutil.which(argv[0])
    if resolved is not None:
        argv[0] = resolved
    log.debug("[build] exec run argv=%s cwd=%s", argv, resolved_cwd)

    result = subprocess.run(
        argv,
        cwd=resolved_cwd,
        env=child_environment(env, permitted_secrets=permitted_secrets),
        check=False,
        capture_output=capture,
        text=capture,
    )

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, argv)

    return result


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def read_tauri_version(root: Path | str = REPO_ROOT) -> str:
    """Read the ``version`` field from ``src-tauri/tauri.conf.json``.

    Returns:
        The version string, e.g. ``"1.0.1"``.

    Raises:
        FileNotFoundError: If tauri.conf.json does not exist.
        KeyError: If the ``version`` field is absent.
    """
    conf_path = Path(root) / "src-tauri" / "tauri.conf.json"
    with conf_path.open(encoding="utf-8") as fh:
        conf = json.load(fh)
    return conf["version"]


class LegalReleaseGateError(RuntimeError):
    """Raised when release legal documents are not publication-ready."""


# The official installer's updater comes from a provider the official build
# names through these variables (see src-tauri/build.rs and frontend/edition.ts).
# Signed releases carry it, so the signed path requires all three.
UPDATER_POLICY_ENV = "WHISPERSTREAM_UPDATER_POLICY"
# A Tauri config overlay with the updater's endpoint, public key and updater
# artifacts. The shared Tauri configs carry none of them.
TAURI_OVERLAY_ENV = "WHISPERSTREAM_TAURI_OVERLAY"
UPDATER_HOOK_ENVS = (
    UPDATER_POLICY_ENV,
    "WHISPERSTREAM_UPDATER_RS",
    "WHISPERSTREAM_FRONTEND_EDITION",
    TAURI_OVERLAY_ENV,
)


def require_updater_provider(env: Mapping[str, str] | None = None) -> None:
    """Fail a signed build that would ship without its updater provider."""
    environ = os.environ if env is None else env
    missing = [name for name in UPDATER_HOOK_ENVS if not environ.get(name)]
    if missing:
        raise LegalReleaseGateError(
            "signed builds ship the official updater, but this build was not given its "
            f"provider ({', '.join(missing)} unset); run it through the official build command"
        )


def tauri_overlay_args(
    root: Path | str = REPO_ROOT, env: Mapping[str, str] | None = None
) -> list[str]:
    """``--config`` arguments for the build's Tauri overlay, or none without one.

    Tauri merges ``--config`` values after the platform config, so the overlay
    goes before any later value that must win (such as an unsigned build's
    ``createUpdaterArtifacts: false``). A variable that names a missing file,
    or a file outside the checkout being built, is a broken official build and
    fails instead of building without the updater configuration.
    """
    environ = os.environ if env is None else env
    value = environ.get(TAURI_OVERLAY_ENV)
    if not value:
        return []
    checkout = Path(root).resolve()
    overlay = Path(value)
    if not overlay.is_absolute():
        overlay = checkout / overlay
    overlay = overlay.resolve()
    if not overlay.is_file():
        raise FileNotFoundError(f"{TAURI_OVERLAY_ENV} names a missing file: {overlay}")
    if not overlay.is_relative_to(checkout):
        raise ValueError(f"{TAURI_OVERLAY_ENV} must name a file inside the checkout: {overlay}")
    return ["--config", str(overlay)]


def updater_policy_relative(root: Path | str = REPO_ROOT) -> str:
    """The provider's updater policy as a checkout-relative path.

    The policy is a release document bound into the legal attestation, so it
    must live inside the checkout. Relative values resolve against ``root``.
    """
    value = os.environ.get(UPDATER_POLICY_ENV)
    if not value:
        raise LegalReleaseGateError(
            f"signed releases need the updater policy ({UPDATER_POLICY_ENV} is unset)"
        )
    checkout = Path(root).resolve()
    policy = Path(value)
    if not policy.is_absolute():
        policy = checkout / policy
    try:
        return policy.resolve().relative_to(checkout).as_posix()
    except ValueError:
        raise LegalReleaseGateError(
            f"{UPDATER_POLICY_ENV} must name a file inside the checkout"
        ) from None


def validate_updater_release_policy(root: Path | str = REPO_ROOT, *, now=None) -> None:
    """Admit an explicit customer policy or a still-open finite policy.

    Disabled source builds do not enter this signed customer-release path.
    Legal/provider notice gates and exact-byte attestation remain independent.
    """
    from datetime import datetime, timezone

    try:
        policy_path = Path(root) / updater_policy_relative(root)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        raw = policy["cutoff_utc"]
        if policy["mode"] == "customer":
            if raw is not None:
                raise ValueError("customer policy must have no cutoff")
        elif policy["mode"] == "finite":
            if not isinstance(raw, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw) is None:
                raise ValueError("unset or noncanonical cutoff")
            cutoff = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if cutoff <= (now or datetime.now(timezone.utc)):
                raise ValueError("expired cutoff")
        else:
            raise ValueError("disabled or unsupported updater policy")
        if policy["download_page"] != "https://whisperstream.io/download":
            raise ValueError("unreviewed download page")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LegalReleaseGateError(
            "updater policy is missing, disabled, invalid or expired; use a reviewed "
            "customer or finite policy before signing/release"
        ) from exc


_EULA_POLICY_RELATIVE = "src-tauri/resources/eula-policy.json"


def _validate_eula_policy_resources(checkout: Path) -> dict[str, object]:
    """Validate the active policy, selected terms, and installer EULA bytes."""
    policy_path = checkout / _EULA_POLICY_RELATIVE
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalReleaseGateError(
            f"EULA policy: cannot read valid JSON from {policy_path}: {exc}"
        ) from exc
    if not isinstance(policy, dict):
        raise LegalReleaseGateError("EULA policy: top-level value must be an object")

    required = {
        "schema_version": int,
        "notice_id": str,
        "requirement_kind": str,
        "terms_version": str,
        "terms_sha256": str,
        "terms_path": str,
    }
    invalid = [
        key
        for key, expected_type in required.items()
        if not isinstance(policy.get(key), expected_type)
        or isinstance(policy.get(key), bool)
        or (expected_type is str and not policy.get(key))
    ]
    if invalid:
        raise LegalReleaseGateError(
            "EULA policy: missing or invalid fields: " + ", ".join(invalid)
        )
    if policy["schema_version"] != 1:
        raise LegalReleaseGateError("EULA policy: unsupported schema_version")
    if policy["requirement_kind"] not in {
        "reacknowledgement",
        "substantive_update",
    }:
        raise LegalReleaseGateError("EULA policy: unsupported requirement_kind")

    terms_version = policy["terms_version"]
    terms_sha256 = policy["terms_sha256"]
    terms_path_text = policy["terms_path"]
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", terms_version) is None:
        raise LegalReleaseGateError("EULA policy: terms_version is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", terms_sha256) is None:
        raise LegalReleaseGateError("EULA policy: terms_sha256 is invalid")
    if terms_path_text != f"eula/{terms_version}.txt":
        raise LegalReleaseGateError(
            "EULA policy: terms_path must identify eula/<terms_version>.txt"
        )

    tauri_config_path = checkout / "src-tauri" / "tauri.conf.json"
    try:
        tauri_config = json.loads(tauri_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalReleaseGateError(
            f"EULA policy: cannot read valid JSON from {tauri_config_path}: {exc}"
        ) from exc
    try:
        bundled_resources = tauri_config["bundle"]["resources"]
    except (KeyError, TypeError) as exc:
        raise LegalReleaseGateError(
            "EULA policy: tauri.conf.json bundle.resources is missing"
        ) from exc
    selected_resource = (Path("resources") / Path(terms_path_text)).as_posix()
    if not isinstance(bundled_resources, list) or selected_resource not in bundled_resources:
        raise LegalReleaseGateError(
            "EULA policy: policy-selected terms are missing from "
            f"tauri.conf.json bundle.resources: {selected_resource}"
        )

    terms_relative = Path("src-tauri") / "resources" / Path(terms_path_text)
    terms_path = checkout / terms_relative
    installer_path = checkout / "src-tauri" / "resources" / "EULA.txt"
    try:
        terms_bytes = terms_path.read_bytes()
        installer_bytes = installer_path.read_bytes()
    except OSError as exc:
        raise LegalReleaseGateError(
            "EULA policy: selected terms or installer EULA is unreadable"
        ) from exc

    actual_sha256 = hashlib.sha256(terms_bytes).hexdigest()
    version_match = re.search(
        rb"^Version ([0-9]+(?:\.[0-9]+)+)\s*$", terms_bytes, re.MULTILINE
    )
    if version_match is None:
        raise LegalReleaseGateError(
            "EULA policy: selected terms lack an exact Version header"
        )
    actual_version = version_match.group(1).decode("ascii")
    if actual_version != terms_version:
        raise LegalReleaseGateError(
            "EULA policy: selected terms Version does not match terms_version"
        )
    if actual_sha256 != terms_sha256:
        raise LegalReleaseGateError(
            "EULA policy: selected terms SHA-256 does not match terms_sha256"
        )
    if installer_bytes != terms_bytes:
        raise LegalReleaseGateError(
            "EULA policy: installer EULA bytes do not match policy-selected terms"
        )
    return policy


def validate_packaged_legal_resources(root: Path | None = None) -> None:
    """Check installable legal resources, including for unsigned test builds.

    Publication dates and signing requirements deliberately belong to the
    separate release gate. Missing or mismatched installed terms never do.
    """
    checkout = root if root is not None else REPO_ROOT
    _validate_eula_policy_resources(checkout)
    config = json.loads((checkout / "src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    resources = config["bundle"]["resources"]
    required = {"resources/EULA.txt", "resources/PRIVACY.txt", "resources/eula-policy.json"}
    required.update(
        resource for resource in resources
        if isinstance(resource, str) and resource.startswith("resources/eula/")
        and resource.endswith(".txt")
    )
    for relative in sorted(required):
        if relative not in resources:
            raise LegalReleaseGateError(
                f"Legal resources: missing from tauri.conf.json bundle.resources: {relative}"
            )
        path = checkout / "src-tauri" / relative
        if not path.is_file() or not path.read_bytes():
            raise LegalReleaseGateError(f"Legal resources: missing or empty file: {relative}")


def validate_legal_release_documents(
    root: Path | str = REPO_ROOT,
    *,
    source_only: bool = False,
    required_effective_date: str | None = None,
) -> None:
    """Validate publication dates in canonical and bundled legal documents.

    Unreleased work may deliberately retain ``YYYY-MM-DD`` placeholders.
    Callers that create or publish a signed artifact must use this validator
    so a placeholder, malformed calendar date or stale derived file fails
    closed. Document versions and effective dates are independent; each
    bundled document must still match its own canonical source.

    Args:
        root: Checkout containing ``legal/`` and ``src-tauri/resources/``.
        source_only: Check only the canonical Markdown sources. The signed
            build uses this fail-fast mode before deriving bundled plaintext,
            then checks all four files after derivation.
        required_effective_date: When provided, require each source document
            to use this exact date. Release passes its run date so a build
            prepared for an earlier publication date cannot ship unchanged.
    """
    checkout = Path(root)
    documents = [
        ("EULA source", checkout / "legal" / "EULA.md", True),
        ("Privacy source", checkout / "legal" / "PRIVACY.md", True),
    ]
    if not source_only:
        documents.extend(
            [
                (
                    "EULA bundle",
                    checkout / "src-tauri" / "resources" / "EULA.txt",
                    False,
                ),
                (
                    "Privacy bundle",
                    checkout / "src-tauri" / "resources" / "PRIVACY.txt",
                    False,
                ),
            ]
        )

    parsed: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    for label, path, markdown in documents:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{label}: cannot read {path}: {exc}")
            continue

        if markdown:
            version_match = re.search(
                r"^\*\*Version ([0-9]+\.[0-9]+)\*\*$", text, re.MULTILINE
            )
            date_match = re.search(
                r"^\*\*Effective ([^*\r\n]+)\*\*$", text, re.MULTILINE
            )
        else:
            version_match = re.search(
                r"^Version ([0-9]+\.[0-9]+)\s*$", text, re.MULTILINE
            )
            date_match = re.search(
                r"^Effective ([^\r\n]+)\s*$", text, re.MULTILINE
            )

        if version_match is None or date_match is None:
            errors.append(f"{label}: missing exact Version or Effective header")
            continue

        version = version_match.group(1)
        effective = date_match.group(1).strip()
        if effective == "YYYY-MM-DD":
            errors.append(f"{label}: Effective date is still YYYY-MM-DD")
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective) is None:
            errors.append(
                f"{label}: Effective date {effective!r} is not in YYYY-MM-DD format"
            )
        else:
            try:
                datetime.date.fromisoformat(effective)
            except ValueError:
                errors.append(
                    f"{label}: Effective date {effective!r} is not a valid YYYY-MM-DD date"
                )
        parsed[label] = (version, effective)

    if required_effective_date is not None:
        for label in ("EULA source", "Privacy source"):
            if label in parsed and parsed[label][1] != required_effective_date:
                errors.append(
                    f"{label} effective date {parsed[label][1]!r} does not "
                    f"match required publication date {required_effective_date!r}"
                )

    if not source_only:
        for source_label, bundle_label in (
            ("EULA source", "EULA bundle"),
            ("Privacy source", "Privacy bundle"),
        ):
            if source_label in parsed and bundle_label in parsed:
                if parsed[source_label] != parsed[bundle_label]:
                    errors.append(
                        f"{bundle_label} Version/Effective header is stale relative to "
                        f"{source_label}"
                    )
        for label, source_relative, bundle_relative, converter in (
            (
                "EULA bundle",
                "legal/EULA.md",
                "src-tauri/resources/EULA.txt",
                "derive-eula-txt.py",
            ),
            (
                "Privacy bundle",
                "legal/PRIVACY.md",
                "src-tauri/resources/PRIVACY.txt",
                "derive-privacy-txt.py",
            ),
        ):
            try:
                expected = _render_derived_legal_document(
                    (checkout / source_relative).read_text(encoding="utf-8"),
                    converter,
                )
                actual = (checkout / bundle_relative).read_bytes()
            except (OSError, RuntimeError, UnicodeError) as exc:
                errors.append(f"{label}: cannot verify full derivation: {exc}")
            else:
                if actual != expected:
                    errors.append(
                        f"{label}: full content is stale relative to its "
                        "canonical Markdown source"
                    )
        try:
            _validate_eula_policy_resources(checkout)
        except LegalReleaseGateError as exc:
            errors.append(str(exc))

    if errors:
        joined = "\n  - ".join(errors)
        raise LegalReleaseGateError(
            "legal documents are not ready for a signed release:\n"
            f"  - {joined}\n"
            "Set each document's actual publication date in legal/EULA.md "
            "and legal/PRIVACY.md, then regenerate bundled plaintext and "
            "verify the active EULA policy."
        )


def _render_derived_legal_document(source: str, converter_filename: str) -> bytes:
    """Render a legal Markdown source with its checked-in derivation script."""
    converter_path = Path(__file__).resolve().parent / converter_filename
    module_name = "_whisperstream_" + converter_filename.replace("-", "_").replace(
        ".", "_"
    )
    spec = importlib.util.spec_from_file_location(module_name, converter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load legal converter {converter_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    convert_md = getattr(module, "convert_md", None)
    if not callable(convert_md):
        raise RuntimeError(f"legal converter lacks convert_md: {converter_path}")
    rendered = convert_md(source)
    if not isinstance(rendered, str):
        raise RuntimeError(f"legal converter returned non-text: {converter_path}")
    return rendered.encode("ascii")


_BASE_LEGAL_RELEASE_DOCUMENTS = (
    "legal/EULA.md",
    "legal/PRIVACY.md",
    "src-tauri/resources/EULA.txt",
    "src-tauri/resources/PRIVACY.txt",
    _EULA_POLICY_RELATIVE,
    "THIRD_PARTY_LICENSES.txt",
    "src-tauri/resources/THIRD_PARTY_LICENSES.txt",
)


def _legal_release_documents(checkout: Path) -> tuple[str, ...]:
    policy = _validate_eula_policy_resources(checkout)
    selected_terms = f"src-tauri/resources/{policy['terms_path']}"
    return (*_BASE_LEGAL_RELEASE_DOCUMENTS, selected_terms, updater_policy_relative(checkout))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path | str) -> str:
    """Return the lowercase SHA-256 digest for one file."""
    return _file_sha256(Path(path))


def source_revision(root: Path = REPO_ROOT) -> str:
    """Return the checkout's full committed HEAD revision."""
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                            capture_output=True, text=True)
    revision = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise LegalReleaseGateError("A committed source revision is required")
    return revision


# Build-generated notices; the legal attestation binds their bytes.
GENERATED_NOTICE_PATHS = frozenset({
    "THIRD_PARTY_LICENSES.txt", "src-tauri/resources/THIRD_PARTY_LICENSES.txt",
    "docs/dependency-licenses.md",
})


def require_clean_source_revision(root: Path = REPO_ROOT, *, generated_notices: bool = False) -> str:
    """Refuse a dirty checkout; optionally allow only the generated notices."""
    revision = source_revision(root)
    changed = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", "HEAD"],
                             cwd=root, check=True, capture_output=True, text=True).stdout
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                               cwd=root, check=True, capture_output=True, text=True).stdout
    paths = set(filter(None, (changed + untracked).split("\0")))
    allowed = GENERATED_NOTICE_PATHS if generated_notices else frozenset()
    if paths - allowed:
        raise LegalReleaseGateError("Official installers require a clean committed source checkout")
    return revision


# Finished installers are also copied here so they are easy to find; the
# original locations stay authoritative for release and update tooling.
BUILDS_DIR: Path = REPO_ROOT / "builds"


def copy_to_builds(artifact: Path | str, platform: str, filename: str) -> Path:
    """Copy one finished installer to builds/<platform>/<filename>."""
    destination_dir = BUILDS_DIR / platform
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    partial = destination_dir / f".{filename}.partial"
    shutil.copy2(artifact, partial)
    partial.replace(destination)
    print(f"Installer copy: {destination}")
    return destination


def write_legal_release_attestation(
    version: str,
    installer: Path | str,
    updater_signature: Path | str,
    *,
    root: Path | str = REPO_ROOT,
) -> Path:
    """Bind final legal-document bytes to every uploaded release artifact.

    The signed installer is also the update payload; ``updater_signature`` is
    the updater signature Tauri wrote beside it (``<installer>.sig``).
    """
    checkout = Path(root)
    validate_updater_release_policy(checkout)
    installer_path = Path(installer)
    updater_signature_path = Path(updater_signature)
    latest_json_path = checkout / "dist" / "latest.json"
    validate_legal_release_documents(checkout)

    effective_match = re.search(
        r"^\*\*Effective ([0-9]{4}-[0-9]{2}-[0-9]{2})\*\*$",
        (checkout / "legal" / "EULA.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if effective_match is None:  # pragma: no cover - validator gives the detail
        raise LegalReleaseGateError("cannot read the validated EULA effective date")

    try:
        attestation = {
            "schema_version": 5,
            "app_version": version,
            "effective_date": effective_match.group(1),
            "artifacts": {
                "installer": {
                    "filename": installer_path.name,
                    "sha256": _file_sha256(installer_path),
                },
                "updater_signature": {
                    "filename": updater_signature_path.name,
                    "sha256": _file_sha256(updater_signature_path),
                },
                "latest_json": {
                    "filename": latest_json_path.name,
                    "sha256": _file_sha256(latest_json_path),
                },
            },
            "documents": {
                relative: _file_sha256(checkout / relative)
                for relative in _legal_release_documents(checkout)
            },
        }
    except OSError as exc:
        raise LegalReleaseGateError(
            "a legal-release input is missing or unreadable; the signed build "
            "cannot write an attestation"
        ) from exc
    output = checkout / "dist" / "legal-release.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(attestation, indent=2) + "\n", encoding="utf-8")
    return output


def verify_legal_release_attestation(
    version: str,
    installer: Path | str,
    updater_signature: Path | str,
    *,
    root: Path | str = REPO_ROOT,
) -> None:
    """Verify that a post-gate build bound legal bytes to uploaded artifacts."""
    checkout = Path(root)
    validate_updater_release_policy(checkout)
    installer_path = Path(installer)
    updater_signature_path = Path(updater_signature)
    latest_json_path = checkout / "dist" / "latest.json"
    validate_legal_release_documents(checkout)
    attestation_path = checkout / "dist" / "legal-release.json"
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegalReleaseGateError(
            "missing or invalid dist/legal-release.json; rebuild the signed "
            "installer with the current build command"
        ) from exc

    try:
        effective_match = re.search(
            r"^\*\*Effective ([0-9]{4}-[0-9]{2}-[0-9]{2})\*\*$",
            (checkout / "legal" / "EULA.md").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if effective_match is None:
            raise OSError("cannot read the validated EULA effective date")
        expected_documents = {
            relative: _file_sha256(checkout / relative)
            for relative in _legal_release_documents(checkout)
        }
        expected_artifacts = {
            "installer": {
                "filename": installer_path.name,
                "sha256": _file_sha256(installer_path),
            },
            "updater_signature": {
                "filename": updater_signature_path.name,
                "sha256": _file_sha256(updater_signature_path),
            },
            "latest_json": {
                "filename": latest_json_path.name,
                "sha256": _file_sha256(latest_json_path),
            },
        }
    except OSError as exc:
        raise LegalReleaseGateError(
            "a legal-release input is missing or unreadable; rebuild the signed "
            "installer with the current build command"
        ) from exc
    if (
        attestation.get("schema_version") != 5
        or attestation.get("app_version") != version
        or attestation.get("effective_date") != effective_match.group(1)
        or attestation.get("artifacts") != expected_artifacts
        or attestation.get("documents") != expected_documents
    ):
        raise LegalReleaseGateError(
            "dist/legal-release.json does not match the uploaded artifacts "
            "and current legal documents; rebuild the signed installer"
        )


def numeric_version(v: str) -> str:
    """Convert a semver string to a 4-part dotted numeric version.

    Strips any pre-release (``-rc1``) or build-metadata (``+abc``) suffix and
    pads to exactly 4 dotted integers. Replaces the ``sed``/``awk`` pipeline
    in build.sh lines 78-81.

    Examples:
        ``"0.7.0"`` -> ``"0.7.0.0"``
        ``"1.2.3-rc1+abc"`` -> ``"1.2.3.0"``
        ``"1.2.3.4"`` -> ``"1.2.3.4"``

    Args:
        v: A semver-ish version string.

    Returns:
        A 4-part dotted string of integers, suitable for Windows VERSIONINFO.
    """
    # Strip pre-release (-anything) and build metadata (+anything)
    bare = re.split(r"[-+]", v, maxsplit=1)[0]
    parts = bare.split(".")
    # Pad to 4 parts with zeros
    while len(parts) < 4:
        parts.append("0")
    # Take only the first 4 parts (guard against malformed strings)
    return ".".join(parts[:4])


# ---------------------------------------------------------------------------
# Tee log helper (replaces bash ``| tee``, ``date`` -> datetime)
# ---------------------------------------------------------------------------

class Tee:
    """Mirror all writes to a primary stream AND to a log file.

    Usage::

        with Tee(log_path) as tee:
            sys.stdout = tee
            sys.stderr = tee
            ... build steps ...
        # On exit, sys.stdout/stderr are restored and the log file is closed.

    This replaces the bash ``| tee`` pipeline and the ``date -u`` calls with
    Python's ``datetime`` module. All output lines are written verbatim; the
    log file is opened in write mode (overwrite per run), matching ``release.sh``'s
    ``tee -a`` behaviour of a fresh log each invocation.

    Args:
        log_path: Path to the log file (typically ``dist/build-signing.log``).
        primary: The stream to mirror writes to (default ``sys.stdout``).
    """

    def __init__(
        self,
        log_path: Path | str,
        primary: "TextIO | None" = None,
    ) -> None:
        self._log_path = Path(log_path)
        self._primary = primary  # resolved on __enter__ so sys.stdout is live
        self._log_file = None
        self._original_stdout = None
        self._original_stderr = None

    # -- Context manager protocol ------------------------------------------

    def __enter__(self) -> "Tee":
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_path.open("w", encoding="utf-8")
        # Write a header with a UTC timestamp (replaces ``date -u +...``)
        ts = _iso_utc_now()
        self._log_file.write(f"[build] log start ts={ts}\n")
        self._log_file.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        ts = _iso_utc_now()
        if self._log_file and not self._log_file.closed:
            self._log_file.write(f"[build] log end ts={ts}\n")
            self._log_file.close()
        return False  # do not suppress exceptions

    # -- File-like interface (for sys.stdout/stderr assignment) -------------

    def write(self, data: str) -> int:
        primary = self._primary if self._primary is not None else sys.stdout
        n = primary.write(data)
        if self._log_file and not self._log_file.closed:
            self._log_file.write(data)
            self._log_file.flush()
        return n

    def flush(self) -> None:
        primary = self._primary if self._primary is not None else sys.stdout
        primary.flush()
        if self._log_file and not self._log_file.closed:
            self._log_file.flush()

    @property
    def encoding(self) -> str:
        primary = self._primary if self._primary is not None else sys.stdout
        return getattr(primary, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        primary = self._primary if self._primary is not None else sys.stdout
        return getattr(primary, "errors", "replace")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iso_utc_now() -> str:
    """Return the current time as a UTC ISO 8601 string (no microseconds).

    Replaces ``date -u +%Y-%m-%dT%H:%M:%SZ`` from build.sh.

    Returns:
        A string like ``"2026-06-25T14:30:00Z"``.
    """
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# Re-export for convenience in later tasks (Task 1.2 uses this).
iso_utc_now = _iso_utc_now


# ---------------------------------------------------------------------------
# latest.json generation + CHANGELOG note extraction (Task 1.2)
# ---------------------------------------------------------------------------

CHANGELOG_PLATFORMS = ("windows", "mac")

# A bullet that applies to one platform only: ``- [Mac] Fixed ...``.
_PLATFORM_BULLET = re.compile(r"^(\s*[-*]\s+)\[(mac|windows)\]\s*", re.IGNORECASE)


def filter_changelog_platform(notes: str, platform: str) -> str | None:
    """Keep the notes that apply to *platform*.

    Shared bullets stay as they are. A ``[Mac]`` or ``[Windows]`` bullet is
    kept, without its tag, on the matching platform and dropped (with any
    indented continuation lines) on the other. A ``###`` heading left with no
    content is dropped too, so the update popup never shows an empty group.
    """
    platform = platform.lower()
    if platform not in CHANGELOG_PLATFORMS:
        raise ValueError(f"unknown changelog platform: {platform!r}")

    kept: list[str] = []
    dropping = False
    for line in notes.splitlines():
        match = _PLATFORM_BULLET.match(line)
        if match:
            dropping = match.group(2).lower() != platform
            if not dropping:
                kept.append(match.group(1) + line[match.end():])
            continue
        if dropping and line.strip() and line[:1].isspace():
            continue  # continuation of a dropped bullet
        dropping = False
        kept.append(line)

    # Drop headings whose group is now empty, then collapse blank runs.
    blocks: list[list[str]] = [[]]
    for line in kept:
        if line.startswith("#"):
            blocks.append([])
        blocks[-1].append(line)
    lines = [
        line
        for block in blocks
        if not (block and block[0].startswith("#") and not any(l.strip() for l in block[1:]))
        for line in block
    ]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def extract_changelog_notes(
    version: str, changelog_text: str, platform: str | None = None
) -> str | None:
    """Extract the release notes body for *version* from a CHANGELOG.md string.

    Matches the ``## [X.Y.Z]`` section heading (plus optional date suffix) and
    returns the stripped text body up to the next ``## [`` heading or EOF.
    Ports the inline Python fragment in build.sh lines 376-388.

    Args:
        version: The version string to look up, e.g. ``"1.0.1"``.
        changelog_text: The full text of ``CHANGELOG.md``.
        platform: ``"windows"`` or ``"mac"`` to keep only the notes for that
            platform's update feed (see ``filter_changelog_platform``).

    Returns:
        The stripped bullet body for *version*, or ``None`` if the section is
        absent or empty.
    """
    if not changelog_text:
        return None

    pattern = (
        rf"^## \[{re.escape(version)}\][^\n]*\n"  # heading line
        r"(.*?)"                                    # captured body (non-greedy)
        r"(?=^## \[|\Z)"                           # stop at next section or EOF
    )
    match = re.search(pattern, changelog_text, flags=re.MULTILINE | re.DOTALL)
    if not (match and match.group(1).strip()):
        return None
    notes = match.group(1).strip()
    return filter_changelog_platform(notes, platform) if platform else notes


def update_download_base(
    root: Path | str = REPO_ROOT, env: Mapping[str, str] | None = None
) -> str:
    """The update feed's directory URL, from the build's Tauri overlay.

    The overlay (``WHISPERSTREAM_TAURI_OVERLAY``) names the one update endpoint
    the app checks; ``latest.json`` points at the release's immutable
    ``<version>/`` folder beside it. The shared tree names no update host, so a build without the
    overlay has no feed to publish to and fails here.
    """
    args = tauri_overlay_args(root, env)
    if not args:
        raise LegalReleaseGateError(
            f"the update manifest needs the build's Tauri overlay ({TAURI_OVERLAY_ENV} unset)"
        )
    overlay = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    try:
        endpoints = overlay["plugins"]["updater"]["endpoints"]
    except (KeyError, TypeError) as exc:
        raise LegalReleaseGateError("the Tauri overlay names no update endpoint") from exc
    if not (isinstance(endpoints, list) and len(endpoints) == 1 and isinstance(endpoints[0], str)):
        raise LegalReleaseGateError("the Tauri overlay must name exactly one update endpoint")
    endpoint = endpoints[0]
    base, _, name = endpoint.rpartition("/")
    if not endpoint.startswith("https://") or name != "latest.json" or base.count("/") < 2:
        raise LegalReleaseGateError(f"unexpected update endpoint: {endpoint}")
    return f"{base}/"


def versioned_artifact_url(download_base: str, version: str, filename: str) -> str:
    """The URL of ``filename`` in the release's immutable ``<version>/`` folder."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?", version):
        raise LegalReleaseGateError(f"unexpected release version: {version!r}")
    if not download_base.endswith("/") or "/" in filename or not filename:
        raise LegalReleaseGateError("unexpected update artifact location")
    return f"{download_base}{version}/{filename}"


def build_update_manifest(
    version: str,
    pub_date: str,
    notes: str | None,
    platforms: Mapping[str, Mapping[str, str]],
) -> dict:
    """A Tauri v2 updater feed: ``version``, ``notes``, ``pub_date``, ``platforms``.

    ``platforms`` maps a Tauri target key (``windows-x86_64``,
    ``darwin-aarch64``) to its ``signature`` and ``url``. Each platform has its
    own feed, so a feed normally names one target.
    """
    resolved_notes = notes if notes is not None else f"Whisperstream v{version}"
    return {
        "version": version,
        "notes": resolved_notes,
        "pub_date": pub_date,
        "platforms": {
            target: {"signature": entry["signature"], "url": entry["url"]}
            for target, entry in platforms.items()
        },
    }


def build_latest_manifest(
    version: str,
    pub_date: str,
    signature: str,
    notes: str | None,
    *,
    download_base: str,
) -> dict:
    """Build the Windows ``dist/latest.json`` feed for the Tauri updater.

    The signed NSIS installer is the update payload (Tauri v2 updater
    artifacts), published in the release's ``<version>/`` folder under the
    feed's directory.

    Args:
        version: The release version string, e.g. ``"1.0.1"``.
        pub_date: The publication date in ISO 8601 UTC format, e.g.
            ``"2026-06-24T12:00:00Z"`` (from ``iso_utc_now()``).
        signature: The base64-encoded Tauri updater signature (contents of the
            installer's ``.exe.sig`` file).
        notes: The release notes body (from ``extract_changelog_notes``), or
            ``None`` to use the generic ``"Whisperstream v{version}"`` fallback.
        download_base: The update feed's directory URL, ending in ``/``
            (from ``update_download_base``).
    """
    url = versioned_artifact_url(download_base, version, f"Whisperstream_{version}_x64-setup.exe")
    return build_update_manifest(
        version, pub_date, notes, {"windows-x86_64": {"signature": signature, "url": url}}
    )


# ---------------------------------------------------------------------------
# Version + dev-tools stamping with try/finally restoration (Task 1.3)
# ---------------------------------------------------------------------------

# Paths for the two sentinel files (relative to REPO_ROOT)
_VERSION_PY = REPO_ROOT / "src" / "whisperstream" / "_version.py"
_BUILD_FLAGS_PY = REPO_ROOT / "src" / "whisperstream" / "_build_flags.py"

# The signing overlay JSON written to src-tauri/ during a signed build.
_SIGNING_OVERLAY = REPO_ROOT / "src-tauri" / "tauri.signing.generated.conf.json"

# Azure Trusted Signing defaults (same as build.sh lines 41-43).
_AZURE_ENDPOINT_DEFAULT = "https://eus.codesigning.azure.net"
_AZURE_ACCOUNT_DEFAULT = "lanreal-signing"
_AZURE_PROFILE_DEFAULT = "whisperstream-cert"


def stamp_version(version: str, numeric: str) -> None:
    """Overwrite ``_version.py`` with the build-time version.

    Replaces the ``cat >`` heredoc in build.sh line 84-88. The file content
    is minimal (just a docstring and the ``__version__`` assignment) to match
    what the build script wrote. Call ``restore_version_sentinel()`` in a
    ``finally`` block to undo this change.

    Args:
        version: The human-readable version, e.g. ``"1.0.1"``.
        numeric: The 4-part numeric version, e.g. ``"1.0.1.0"`` (not currently
            written to ``_version.py`` itself but kept as a parameter for
            parity with the Nuitka step that needs it).
    """
    log.debug(
        "[build] stamp version path=%s version=%s numeric=%s",
        _VERSION_PY,
        version,
        numeric,
    )
    _VERSION_PY.write_text(
        f'"""Build-time version constant. Restored to sentinel after build."""\n'
        f"\n"
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )


# The exact multi-line sentinel body from build.sh lines 111-125.
# Copied VERBATIM -- do not edit this string without syncing build.sh.
_VERSION_SENTINEL = '''\
"""Build-time version constant.

`scripts/build.sh` overwrites this file before invoking Nuitka, embedding the
version from `src-tauri/tauri.conf.json` into the frozen sidecar. The sentinel
value below is what's checked into git; the build script restores it after the
build so dev work doesn't see a dirty working tree.

In dev mode (running from source, not Nuitka), `_read_software_version()` in
`server.py` falls back to reading `tauri.conf.json` directly when this module
still holds the sentinel.
"""

__version__ = "0.0.0+dev"
'''


def restore_version_sentinel() -> None:
    """Restore ``_version.py`` to the committed sentinel value.

    Writes the exact docstring + ``__version__ = "0.0.0+dev"`` body from
    build.sh lines 111-125. Always call this in a ``finally`` block after
    ``stamp_version()``.
    """
    log.debug("[build] restore version sentinel path=%s", _VERSION_PY)
    _VERSION_PY.write_text(_VERSION_SENTINEL, encoding="utf-8")


def disable_dev_tools() -> None:
    """Flip ``DEV_TOOLS_AVAILABLE = True`` to ``False`` in ``_build_flags.py``.

    Uses a line-anchored regex replace (mirrors the ``sed -i 's/^DEV_TOOLS.../``
    in build.sh line 96). Only matches the bare assignment line (not occurrences
    inside the module docstring). Asserts the flip succeeded and raises with
    build.sh's exact error message if the anchor line is absent. Call
    ``restore_dev_tools()`` in a ``finally`` block to undo this change.

    Raises:
        RuntimeError: If the line ``DEV_TOOLS_AVAILABLE = True`` was not found
            after the replacement, matching build.sh's assertion logic.
    """
    log.debug("[build] disable dev tools path=%s", _BUILD_FLAGS_PY)
    text = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    # Line-anchored: only match the bare assignment at the start of a line.
    # Mirrors sed 's/^DEV_TOOLS_AVAILABLE = True/DEV_TOOLS_AVAILABLE = False/'
    new_text = re.sub(
        r"^DEV_TOOLS_AVAILABLE = True$",
        "DEV_TOOLS_AVAILABLE = False",
        text,
        flags=re.MULTILINE,
        count=1,
    )
    _BUILD_FLAGS_PY.write_text(new_text, encoding="utf-8")

    # Assert the flip actually took -- mirrors build.sh lines 98-107.
    # If _build_flags.py is ever reformatted so the line no longer matches,
    # the flag would stay True while --nofollow-import-to strips the dev-tools module,
    # producing a prod binary that ImportErrors on startup. Catch it here.
    written = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    if not re.search(r"^DEV_TOOLS_AVAILABLE = False$", written, flags=re.MULTILINE):
        raise RuntimeError(
            "ERROR: failed to set DEV_TOOLS_AVAILABLE=False (sed did not match).\n"
            "  src/whisperstream/_build_flags.py format may have changed; aborting\n"
            "  to avoid shipping (or crashing on) the dev-tools surface."
        )


def restore_dev_tools() -> None:
    """Restore ``DEV_TOOLS_AVAILABLE = True`` in ``_build_flags.py``.

    Mirrors the ``sed -i 's/^DEV_TOOLS_AVAILABLE = False/DEV_TOOLS_AVAILABLE = True/``
    in build.sh cleanup() line 127. Always call this in a ``finally`` block
    after ``disable_dev_tools()``.
    """
    log.debug("[build] restore dev tools path=%s", _BUILD_FLAGS_PY)
    text = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    # Line-anchored: only match the bare assignment at the start of a line.
    new_text = re.sub(
        r"^DEV_TOOLS_AVAILABLE = False$",
        "DEV_TOOLS_AVAILABLE = True",
        text,
        flags=re.MULTILINE,
        count=1,
    )
    _BUILD_FLAGS_PY.write_text(new_text, encoding="utf-8")


def disable_experimental() -> None:
    """Flip ``EXPERIMENTAL_AVAILABLE = True`` to ``False`` in ``_build_flags.py``.

    Same line-anchored regex + flip-assertion pattern as ``disable_dev_tools()``,
    for the agent-speech experimental gate. A default (production) build calls
    this so the experimental modules are compile-time dead; ``--experimental``
    dogfood builds skip it. Call ``restore_experimental()`` in a ``finally`` block
    to undo this change.

    Raises:
        RuntimeError: If the line ``EXPERIMENTAL_AVAILABLE = True`` was not found
            after the replacement (``_build_flags.py`` format drifted), matching
            ``disable_dev_tools()``'s assertion logic.
    """
    log.debug("[build] disable experimental path=%s", _BUILD_FLAGS_PY)
    text = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    # Line-anchored: only match the bare assignment at the start of a line.
    new_text = re.sub(
        r"^EXPERIMENTAL_AVAILABLE = True$",
        "EXPERIMENTAL_AVAILABLE = False",
        text,
        flags=re.MULTILINE,
        count=1,
    )
    _BUILD_FLAGS_PY.write_text(new_text, encoding="utf-8")

    # Assert the flip took -- same guard as disable_dev_tools(). If the flag were
    # left True while --nofollow-import-to strips the experimental modules, a prod
    # binary could ImportError on a lazy experimental import. Catch it here.
    written = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    if not re.search(r"^EXPERIMENTAL_AVAILABLE = False$", written, flags=re.MULTILINE):
        raise RuntimeError(
            "ERROR: failed to set EXPERIMENTAL_AVAILABLE=False (regex did not match).\n"
            "  src/whisperstream/_build_flags.py format may have changed; aborting\n"
            "  to avoid shipping (or crashing on) the experimental surface."
        )


def restore_experimental() -> None:
    """Restore ``EXPERIMENTAL_AVAILABLE = True`` in ``_build_flags.py``.

    Mirror of ``restore_dev_tools()``. Always call this in a ``finally`` block
    after a build so a crash mid-compile never leaves a dirty working tree. Safe
    to call even when the flag was never flipped (``--experimental`` builds): the
    regex only matches ``= False``, so it is a no-op when the flag is already
    ``= True``.
    """
    log.debug("[build] restore experimental path=%s", _BUILD_FLAGS_PY)
    text = _BUILD_FLAGS_PY.read_text(encoding="utf-8")
    # Line-anchored: only match the bare assignment at the start of a line.
    new_text = re.sub(
        r"^EXPERIMENTAL_AVAILABLE = False$",
        "EXPERIMENTAL_AVAILABLE = True",
        text,
        flags=re.MULTILINE,
        count=1,
    )
    _BUILD_FLAGS_PY.write_text(new_text, encoding="utf-8")


import contextlib


# ---------------------------------------------------------------------------
# Azure Trusted Signing config + sidecar signing helper (Task 1.4)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SigningConfig:
    """Azure Trusted Signing configuration.

    Populated by ``signing_config_from_env()`` when the three required secrets
    are present in the environment. Mirrors the variables build.sh reads at
    lines 39-43.

    Attributes:
        client_id: Value of ``AZURE_CLIENT_ID``.
        client_secret: Value of ``AZURE_CLIENT_SECRET``.
        tenant_id: Value of ``AZURE_TENANT_ID``.
        endpoint: Trusted Signing endpoint URL (default:
            ``https://eus.codesigning.azure.net``).
        account: Signing account name (default: ``lanreal-signing``).
        profile: Certificate profile name (default: ``whisperstream-cert``).
    """

    client_id: str
    client_secret: str
    tenant_id: str
    endpoint: str = _AZURE_ENDPOINT_DEFAULT
    account: str = _AZURE_ACCOUNT_DEFAULT
    profile: str = _AZURE_PROFILE_DEFAULT


def signing_config_from_env() -> SigningConfig | None:
    """Read Azure Trusted Signing configuration from the environment.

    Returns a populated ``SigningConfig`` when all three required secrets
    (``AZURE_CLIENT_ID``, ``AZURE_CLIENT_SECRET``, ``AZURE_TENANT_ID``) are
    present and non-empty. Returns ``None`` when any secret is absent, matching
    build.sh's ``SIGNING_ENABLED=false`` branch (lines 37-67).

    Optional env vars with build.sh defaults:
    - ``AZURE_ENDPOINT`` (default: ``https://eus.codesigning.azure.net``)
    - ``AZURE_SIGNING_ACCOUNT`` (default: ``lanreal-signing``)
    - ``AZURE_CERT_PROFILE`` (default: ``whisperstream-cert``)

    Returns:
        A ``SigningConfig`` dataclass when creds are present, or ``None``.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID", "")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")

    if not (client_id and client_secret and tenant_id):
        return None

    endpoint = os.environ.get("AZURE_ENDPOINT", _AZURE_ENDPOINT_DEFAULT)
    account = os.environ.get("AZURE_SIGNING_ACCOUNT", _AZURE_ACCOUNT_DEFAULT)
    profile = os.environ.get("AZURE_CERT_PROFILE", _AZURE_PROFILE_DEFAULT)

    return SigningConfig(
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
        endpoint=endpoint,
        account=account,
        profile=profile,
    )


def require_trusted_signing_cli() -> None:
    """Assert that ``trusted-signing-cli`` is on PATH.

    Mirrors the ``command -v trusted-signing-cli`` check in build.sh lines
    45-49. Raises ``SystemExit`` with the ``cargo install`` hint if the tool
    is not found, so the error surfaces before a long Nuitka compile wastes
    time.

    Raises:
        SystemExit: When ``trusted-signing-cli`` is absent from PATH, printing
            the install hint.
    """
    if shutil.which("trusted-signing-cli") is None:
        sys.exit(
            "ERROR: AZURE_CLIENT_ID is set but trusted-signing-cli is not on PATH.\n"
            "  Install with: cargo install trusted-signing-cli"
        )


def sign_file(cfg: SigningConfig, path: Path | str, description: str) -> None:
    """Sign a single binary using Azure Trusted Signing.

    Invokes ``trusted-signing-cli`` via the existing ``run()`` helper. Mirrors
    the manual sidecar signing block in build.sh lines 213-222. The environment
    passed to the subprocess includes the three Azure secret vars so the CLI
    can authenticate without them being baked into the command line.

    Args:
        cfg: A ``SigningConfig`` populated by ``signing_config_from_env()``.
        path: Absolute path to the binary to sign (e.g.
            ``dist/whisperstream-engine.exe``).
        description: Human-readable description embedded in the signature
            (e.g. ``"Whisperstream Engine"``).

    Raises:
        subprocess.CalledProcessError: If ``trusted-signing-cli`` exits
            non-zero (signing failed).
    """
    target = Path(path)
    log.debug(
        "[build] sign file path=%s description=%s endpoint=%s account=%s profile=%s",
        target,
        description,
        cfg.endpoint,
        cfg.account,
        cfg.profile,
    )
    # Merge Azure secret vars into the inherited environment so the CLI can
    # authenticate. The three secrets must not appear in argv (visible in
    # process listings); trusted-signing-cli picks them up from env vars.
    env = os.environ.copy()
    env["AZURE_CLIENT_ID"] = cfg.client_id
    env["AZURE_CLIENT_SECRET"] = cfg.client_secret
    env["AZURE_TENANT_ID"] = cfg.tenant_id

    run(
        [
            "trusted-signing-cli",
            "-e", cfg.endpoint,
            "-a", cfg.account,
            "-c", cfg.profile,
            "-d", description,
            str(target),
        ],
        env=env,
        permitted_secrets=AZURE_SIGNING_SECRETS,
    )


@contextlib.contextmanager
def signing_overlay(
    endpoint: str = _AZURE_ENDPOINT_DEFAULT,
    account: str = _AZURE_ACCOUNT_DEFAULT,
    profile: str = _AZURE_PROFILE_DEFAULT,
):
    """Context manager that writes the Tauri signing overlay JSON and removes it on exit.

    Writes ``src-tauri/tauri.signing.generated.conf.json`` with the exact JSON
    schema from build.sh lines 55-63. Removes the file on exit (success or
    failure). This replaces the ``cat > "$SIGN_CONFIG" <<EOF ... EOF`` + ``rm -f``
    in cleanup().

    The ``signCommand`` string interpolates the three Azure values identically to
    build.sh's shell interpolation in line 59.

    Args:
        endpoint: Azure Trusted Signing endpoint URL.
        account: Azure signing account name.
        profile: Azure certificate profile name.

    Yields:
        The ``Path`` to the written overlay file.
    """
    sign_command = (
        f"trusted-signing-cli "
        f"-e {endpoint} "
        f"-a {account} "
        f"-c {profile} "
        f"-d Whisperstream %1"
    )
    overlay = {
        "bundle": {
            "windows": {
                "signCommand": sign_command,
            }
        }
    }
    log.debug(
        "[build] signing overlay write path=%s endpoint=%s account=%s profile=%s",
        _SIGNING_OVERLAY,
        endpoint,
        account,
        profile,
    )
    _SIGNING_OVERLAY.write_text(
        json.dumps(overlay, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        yield _SIGNING_OVERLAY
    finally:
        if _SIGNING_OVERLAY.exists():
            log.debug("[build] signing overlay remove path=%s", _SIGNING_OVERLAY)
            _SIGNING_OVERLAY.unlink()


# ---------------------------------------------------------------------------
# Artifact discovery + build-time display (Task 2.1)
# ---------------------------------------------------------------------------

def find_nsis_installer(
    version: str,
    base_dir: Path | str | None = None,
) -> tuple[Path, ...]:
    """Locate the signed NSIS installer and its updater signature for *version*.

    Looks in ``<base_dir>/src-tauri/target/release/bundle/nsis/`` for:

    - ``Whisperstream_{version}_x64-setup.exe``      (the installer and update payload)
    - ``Whisperstream_{version}_x64-setup.exe.sig``  (its Tauri updater signature)

    Args:
        version: The release version string, e.g. ``"1.0.1"``.
        base_dir: Root directory to resolve the NSIS bundle path from.
            Defaults to ``REPO_ROOT`` when ``None``, which is correct for
            production use. Pass a ``tmp_path`` fixture value in tests.

    Returns:
        A 2-tuple ``(exe, sig)`` of absolute ``Path`` objects.

    Raises:
        FileNotFoundError: If either expected file is absent.
            The message names the missing file so the caller knows which
            artifact to look for.
    """
    root = Path(base_dir) if base_dir is not None else REPO_ROOT
    nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"

    stem = f"Whisperstream_{version}_x64-setup"
    exe = nsis_dir / f"{stem}.exe"
    sig = nsis_dir / f"{stem}.exe.sig"

    for path in (exe, sig):
        if not path.exists():
            raise FileNotFoundError(
                f"NSIS artifact not found: {path}\n"
                f"  Expected file: {path.name}\n"
                f"  Ensure 'cargo tauri build' completed successfully and "
                f"TAURI_SIGNING_PRIVATE_KEY is set (for the updater .sig)."
            )

    log.debug("[release] artifacts found exe=%s sig=%s", exe, sig)
    return (exe, sig)


def format_build_time(path: Path | str) -> str:
    """Return the mtime of *path* formatted as both local and UTC ISO strings.

    Replaces the ``date``/``stat`` shell commands used to display build
    timestamps. Both forms are included so the release operator can confirm
    the artifact was built when expected, regardless of their local timezone.

    No em dashes are used in the returned string (project style rule).

    Args:
        path: Path to the artifact file whose mtime will be read.

    Returns:
        A human-readable string containing both the local and UTC
        representations of the file's mtime, e.g.::

            "2026-06-25 10:30:00 local / 2026-06-25T14:30:00Z UTC"

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    target = Path(path)
    mtime_posix = target.stat().st_mtime
    mtime_utc = datetime.datetime.fromtimestamp(mtime_posix, tz=datetime.timezone.utc)
    mtime_local = datetime.datetime.fromtimestamp(mtime_posix).astimezone()

    # Format local as "YYYY-MM-DD HH:MM:SS" (no offset clutter for display)
    local_str = mtime_local.strftime("%Y-%m-%d %H:%M:%S")
    # Format UTC as ISO 8601 with a trailing Z (matches iso_utc_now style)
    utc_str = mtime_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use a forward slash separator (no em dash per style rule)
    return f"{local_str} local / {utc_str} UTC"


# ---------------------------------------------------------------------------
# Authenticode signature verification (Task 2.2)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SignatureResult:
    """Result of an Authenticode signature verification.

    Attributes:
        trusted: ``True`` when the file has a valid, trusted Authenticode
            signature; ``False`` otherwise (unsigned, tampered, or untrusted
            chain).
        tool: A short string naming the verification tool used, e.g.
            ``"signtool"`` or ``"Get-AuthenticodeSignature"``.
        detail: A human-readable summary of the verification outcome, drawn
            from the tool's output. No em dashes (project style rule).
    """

    trusted: bool
    tool: str
    detail: str


def _find_signtool() -> Path | None:
    """Locate the highest-version signtool.exe under the Windows Kits directory.

    Globs ``C:\\Program Files (x86)\\Windows Kits\\10\\bin\\*\\x64\\signtool.exe``
    and picks the highest version directory (semver-like folder name, e.g.
    ``10.0.26100.0``). Returns ``None`` if no signtool is found.

    Returns:
        The ``Path`` to the highest-version ``signtool.exe``, or ``None``.
    """
    kits_bin = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if not kits_bin.exists():
        return None

    candidates: list[tuple[tuple[int, ...], Path]] = []
    for child in kits_bin.iterdir():
        if not child.is_dir():
            continue
        tool = child / "x64" / "signtool.exe"
        if not tool.exists():
            continue
        # Parse the directory name as a version tuple for sorting.
        # Format is like "10.0.26100.0"; non-matching dirs are ignored.
        try:
            version_tuple = tuple(int(p) for p in child.name.split("."))
        except ValueError:
            continue
        candidates.append((version_tuple, tool))

    if not candidates:
        return None

    # Sort descending by version tuple; return the first (highest).
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def parse_signtool_result(returncode: int, stdout: str) -> bool:
    """Classify a signtool verify /pa /v invocation as trusted or not.

    This is a pure function: it accepts captured output strings and does no
    process I/O, making it directly unit-testable without a signed file.

    ``signtool verify /pa`` exits 0 and prints ``Successfully verified:`` on
    success. Any nonzero exit code means the verification failed (unsigned,
    untrusted chain, unrecognized format, etc.).

    Args:
        returncode: The process exit code from ``signtool.exe``.
        stdout: Captured combined stdout+stderr text from the invocation.

    Returns:
        ``True`` when the signature is trusted; ``False`` otherwise.
    """
    if returncode != 0:
        return False
    return "Successfully verified:" in stdout


def parse_cmdlet_status(stdout: str) -> bool:
    """Classify the output of ``Get-AuthenticodeSignature ... | Select Status``.

    This is a pure function: it accepts a captured status string and does no
    process I/O, making it directly unit-testable.

    PowerShell's ``Get-AuthenticodeSignature`` returns one of these
    ``AuthenticodeSignatureStatus`` enum values:
    - ``Valid`` -- the signature is present and the chain is trusted.
    - ``NotSigned`` -- the file has no signature.
    - ``HashMismatch`` -- the file was tampered after signing.
    - ``NotTrusted`` -- signature present but chain is not trusted.
    - ``UnknownError`` -- file format not recognized or other error.

    Args:
        stdout: The captured ``Status`` string from the PowerShell command,
            possibly with surrounding whitespace.

    Returns:
        ``True`` only when the stripped status is exactly ``"Valid"``;
        ``False`` for every other value.
    """
    return stdout.strip() == "Valid"


def verify_signature(path: Path | str) -> SignatureResult:
    """Verify the Authenticode signature of a Windows binary.

    Uses ``signtool verify /pa /v <path>`` when ``signtool.exe`` is found
    under ``C:\\Program Files (x86)\\Windows Kits\\10\\bin\\*\\x64\\``,
    picking the highest version. Falls back to a single
    ``powershell.exe -NoProfile -Command "Get-AuthenticodeSignature ... | Select Status"``
    call when signtool is absent.

    The ``release`` command HARD-FAILS when ``trusted`` is ``False``; the
    result is unambiguous (no partial-trust states).

    No em dashes appear in ``detail`` (project style rule).

    Args:
        path: Path to the binary to verify (e.g. the NSIS installer).

    Returns:
        A ``SignatureResult`` with ``trusted``, ``tool``, and ``detail``
        fields. ``trusted`` is ``True`` only when the signature is valid and
        the chain traces to a trusted root.
    """
    target = Path(path)
    signtool = _find_signtool()

    if signtool is not None:
        # Use signtool verify /pa /v -- the /pa flag enables the default
        # authentication verification policy (same as release.sh).
        # We must call via subprocess.run directly here (not the project run()
        # helper) so we can capture output without raising on nonzero rc.
        log.debug(
            "[release] verify signature tool=signtool path=%s signtool=%s",
            target,
            signtool,
        )
        result = subprocess.run(
            [str(signtool), "verify", "/pa", "/v", str(target)],
            env=child_environment(),
            capture_output=True,
            text=True,
        )
        # signtool writes its output to both stdout and stderr depending on
        # the platform and invocation context; merge both for reliable parsing.
        combined = result.stdout + result.stderr
        trusted = parse_signtool_result(result.returncode, combined)
        # Prefer the actual error over signtool's opening verification counts.
        lines = [
            line.rstrip()
            for line in combined.splitlines()
            if line.strip()
        ]
        errors = [line for line in lines if "signtool error:" in line.lower()]
        first_lines = (errors if not trusted and errors else lines)[:3]
        detail = " | ".join(first_lines).replace("—", "-")
        if trusted and not _expected_publisher(target):
            trusted = False
            detail = "publisher verification failed"
        return SignatureResult(trusted=trusted, tool="signtool", detail=detail)

    # Fallback: PowerShell Get-AuthenticodeSignature cmdlet.
    log.debug(
        "[release] verify signature tool=Get-AuthenticodeSignature path=%s "
        "(signtool not found, using cmdlet fallback)",
        target,
    )
    trusted = _expected_publisher(target)
    detail = "publisher verified" if trusted else "publisher verification failed"
    return SignatureResult(
        trusted=trusted,
        tool="Get-AuthenticodeSignature",
        detail=detail,
    )


def _expected_publisher(target: Path) -> bool:
    """A trusted chain alone does not identify the product's publisher.

    Azure certificates rotate, so pin the verified leaf's simple subject name
    instead of a particular short-lived certificate thumbprint.
    """
    literal = str(target).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$signature=Get-AuthenticodeSignature -LiteralPath '{literal}'; "
        "@{status=[string]$signature.Status; "
        "publisher=$signature.SignerCertificate.GetNameInfo("
        "[System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)} "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture=True, check=False)
        data = json.loads(result.stdout)
        return (result.returncode == 0 and data.get("status") == "Valid"
                and data.get("publisher") == "Lanreal Technologies Inc.")
    except (OSError, ValueError, AttributeError, TypeError):
        return False
