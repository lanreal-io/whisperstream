"""Content-free real-Keychain probe for the internal macOS engine.

This module generates synthetic bytes inside the probing process, exercises
one exact phase-owned Keychain identity, deletes it, and writes metadata only.
It is not a product-facing command and never accepts secret material.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .macos_protected_secret import MacosKeychainProtectedSecretStore
from .protected_secret_contract import (
    MACOS_KEYCHAIN_PREFIX,
    ProtectionAvailability,
    ProtectedSecretError,
    ProtectedSecretFailure,
    ProtectedSecretOperation,
    ProtectedSecretPurpose,
    macos_keychain_account,
    macos_keychain_service,
    parse_protected_secret_reference,
)


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    path = path.resolve()
    if path.exists():
        raise RuntimeError("probe output already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def run_probe(*, output: Path, test_namespace: str) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("real Keychain probe requires native Darwin arm64")

    service = macos_keychain_service(test_namespace=test_namespace)
    store = MacosKeychainProtectedSecretStore(test_namespace=test_namespace)
    reference: str | None = None
    account: str | None = None
    cleanup_verified = False
    stages: list[str] = []
    error: dict[str, str] | None = None

    try:
        if store.availability() is not ProtectionAvailability.AVAILABLE:
            raise ProtectedSecretError(
                ProtectedSecretOperation.AVAILABILITY,
                ProtectedSecretFailure.UNAVAILABLE,
            )
        stages.append("availability")

        first = secrets.token_bytes(32)
        reference = store.store(
            first,
            purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY,
        )
        parsed = parse_protected_secret_reference(reference)
        account = macos_keychain_account(parsed.opaque_id)
        if not reference.startswith(MACOS_KEYCHAIN_PREFIX) or not store.contains(reference):
            raise RuntimeError("stored identity could not be confirmed")
        stages.append("store")

        if not secrets.compare_digest(
            store.load(reference),
            first,
        ):
            raise RuntimeError("round trip mismatch")
        stages.append("load")

        second = secrets.token_bytes(32)
        store.update(
            reference,
            second,
            purpose=ProtectedSecretPurpose.TRANSCRIPT_DATA_KEY,
        )
        if not secrets.compare_digest(
            store.load(reference),
            second,
        ):
            raise RuntimeError("update mismatch")
        stages.append("update")

        store.delete(reference)
        if store.contains(reference):
            raise RuntimeError("exact item remained after delete")
        cleanup_verified = True
        stages.append("delete")

        try:
            store.load(reference)
        except ProtectedSecretError as caught:
            if caught.failure is not ProtectedSecretFailure.NOT_FOUND:
                raise
        else:
            raise RuntimeError("missing item did not fail closed")
        stages.append("missing")
    except ProtectedSecretError as caught:
        if not hasattr(caught, "operation") or not hasattr(caught, "failure"):
            error = {"operation": "availability", "failure": "unavailable"}
        else:
            error = {
                "operation": caught.operation.value,
                "failure": caught.failure.value,
            }
    except Exception:  # noqa: BLE001 - report stays content-free
        error = {"operation": "probe", "failure": "backend-failure"}
    finally:
        if reference is not None and not cleanup_verified:
            try:
                if store.contains(reference):
                    store.delete(reference)
                cleanup_verified = not store.contains(reference)
            except ProtectedSecretError:
                cleanup_verified = False

    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "PASS" if error is None and cleanup_verified else "BLOCKED",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "provider": "macos-keychain-generic-password-v1",
        "service": service,
        "account": account,
        "reference": reference,
        "stages": stages,
        "cleanup_verified": cleanup_verified,
        "secret_material_recorded": False,
        "authentication_ui_allowed": False,
        "security_configuration_changed": False,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if error is not None:
        payload["error"] = error
    _write_new_json(output, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-namespace", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = run_probe(
            output=args.output,
            test_namespace=args.test_namespace,
        )
    except Exception:  # noqa: BLE001 - never print native details or material
        print("BLOCKED macos-keychain-probe setup-failure", file=sys.stderr)
        return 1
    if payload["verdict"] != "PASS":
        print("BLOCKED macos-keychain-probe", file=sys.stderr)
        return 1
    print("PASS macos-keychain-probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
