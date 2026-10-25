#!/usr/bin/env python3
"""Provision and resolve a persistent, machine-local code-signing identity."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import secrets
import subprocess
import tempfile

NAME = "Whisperstream Local Development"


def certificate_path() -> Path:
    return Path.home() / "Library/Application Support/Whisperstream-build/local-signing/certificate.pem"


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Local signing command failed: {args[0]} {args[1]}: {result.stderr.strip()}")
    return result.stdout


def fingerprint(path: Path) -> str:
    import ssl
    return hashlib.sha1(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest().upper()


def identity() -> str:
    path = certificate_path()
    if not path.is_file():
        raise RuntimeError("Run python3 scripts/macos_local_signing.py setup once before building on this Mac")
    digest = fingerprint(path)
    identities = run("security", "find-identity", "-v", "-p", "codesigning")
    if not any(digest in line and f'"{NAME}"' in line for line in identities.splitlines()):
        raise RuntimeError("Local signing certificate is unavailable or untrusted; rerun python3 scripts/macos_local_signing.py setup")
    return digest


def setup() -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("Local signing setup requires macOS")
    path = certificate_path()
    if path.is_file():
        try:
            return identity()
        except RuntimeError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        # Never replace an existing key: doing so changes the permission identity.
        existing = run("security", "find-certificate", "-a", "-c", NAME, "-p")
        if existing.strip():
            if existing.count("BEGIN CERTIFICATE") != 1:
                raise RuntimeError("Multiple local signing certificates found; resolve the duplicate in Keychain Access")
            path.write_text(existing)
        else:
            with tempfile.TemporaryDirectory(prefix="whisperstream-signing-") as directory:
                root = Path(directory)
                config = root / "openssl.cnf"
                config.write_text(f"""[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
CN = {NAME}
[extensions]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
""")
                key, cert, archive = (root / name for name in ("key.pem", "cert.pem", "identity.p12"))
                run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-x509", "-sha256", "-days", "3650", "-config", str(config), "-keyout", str(key), "-out", str(cert))
                os.chmod(key, 0o600)
                password = secrets.token_hex(24)
                password_file = root / "password"
                password_file.write_text(password)
                os.chmod(password_file, 0o600)
                run("openssl", "pkcs12", "-export", "-inkey", str(key), "-in", str(cert), "-out", str(archive), "-passout", f"file:{password_file}", "-keypbe", "PBE-SHA1-3DES", "-certpbe", "PBE-SHA1-3DES", "-macalg", "sha1")
                keychain = Path.home() / "Library/Keychains/login.keychain-db"
                run("security", "import", str(archive), "-k", str(keychain), "-P", password, "-T", "/usr/bin/codesign")
                path.write_bytes(cert.read_bytes())
    # Trust only code signing, in the current user's trust domain, never TLS.
    run("security", "add-trusted-cert", "-r", "trustRoot", "-p", "codeSign", str(path))
    return identity()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "status"))
    args = parser.parse_args()
    try:
        print(f"{NAME}: {setup() if args.action == 'setup' else identity()}")
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
