"""Audit Git bytes before source publication. Never prints matched content or filenames.

The caller must supply a reviewed checker/policy, independently of the candidate.
CLI modes always run the pinned Gitleaks binary with an isolated default config.
Range mode audits introduced/modified entries in every commit against each
parent, commit metadata, and the complete final tree. Previously hosted base
objects are not retrospectively cleared for initial public-history publication.
The Python audit functions expose deterministic checks for tests and integration;
use the CLI, not those functions alone, for a publication gate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata


class AuditError(RuntimeError):
    """Safe, content-free failure."""


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    data: bytes


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finding(rule: str, path: str = "", severity: str = "violation") -> dict:
    # A name can itself contain personal/secret data. Receipts identify it by hash.
    return {"rule": rule, "severity": severity, "path_id": digest(path.encode("utf-8", "surrogateescape"))[:16] if path else None}


def safe_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not any(t in k.upper() for t in ("TOKEN", "SECRET", "PASSWORD", "GITLEAKS", "GIT_CONFIG", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE"))}
    # Replacement objects are local views; push transmits the original objects.
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_NO_REPLACE_OBJECTS="1")
    return env


def require_original_history(repo: Path) -> None:
    if os.environ.get("GIT_GRAFT_FILE") or os.environ.get("GIT_SHALLOW_FILE"):
        raise AuditError("Git history overrides must be removed before publication audit")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, env=safe_env())
    if result.returncode:
        raise AuditError("Git repository metadata unavailable")
    common = Path(os.fsdecode(result.stdout).strip())
    if (common / "info" / "grafts").exists():
        raise AuditError("Git grafts must be removed before publication audit")


def git(repo: Path, *args: str) -> bytes:
    require_original_history(repo)
    env = safe_env()
    if args and args[0] in ("ls-files", "write-tree") and os.environ.get("GIT_INDEX_FILE"):
        # Git commit --only/-a passes the actual commit index to its hook.
        # Honor that explicit index, never silently inspect the ordinary index.
        index = Path(os.environ["GIT_INDEX_FILE"])
        env["GIT_INDEX_FILE"] = str(index if index.is_absolute() else (repo / index).resolve())
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    if result.returncode:
        raise AuditError("Git input unavailable or incomplete; fetch complete source history and retry")
    return result.stdout


def resolve(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise AuditError("An explicit Git revision is required")
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def load_policy(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1:
            raise ValueError()
        for key in ("allowed_roots", "allowed_root_files", "forbidden_components", "forbidden_names", "forbidden_patterns", "text_extensions", "allowed_skill_paths", "content_rules", "binary_assets", "secret_exceptions", "gitleaks_version"):
            if key not in value:
                raise ValueError()
        for row in value["content_rules"]:
            re.compile(row["pattern"])
        generated = value.get("generated_assets")
        if generated is not None:
            # The ordinary pins file can never be a trusted control, and a
            # generated path is never also a trusted pin (one source of truth).
            controls = {"scripts/source_policy.py", "scripts/source-policy.json", "scripts/source_controls.py"}
            if not isinstance(generated["pins_file"], str) or generated["pins_file"] in controls or not generated["paths"]:
                raise ValueError()
            trusted = {row["path"] for row in value["binary_assets"]}
            for row in generated["paths"]:
                if not isinstance(row["path"], str) or not isinstance(row["signature"], str) or not row["signature"] or row["path"] in trusted:
                    raise ValueError()
        return value
    except (ValueError, OSError, TypeError, KeyError):
        raise AuditError("Invalid reviewed source policy") from None


def entries(repo: Path, ref: str | None) -> list[Entry]:
    raw = git(repo, "ls-files", "--stage", "-z") if ref is None else git(repo, "ls-tree", "-r", "-z", resolve(repo, ref))
    rows = []
    objects = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        fields = meta.decode().split()
        mode = fields[0]
        if ref is None:
            oid, stage = fields[1:]
            if stage != "0":
                raise AuditError("Resolve index conflicts before publication audit")
        else:
            oid = fields[2]
        name = path.decode("utf-8", "surrogateescape")
        rows.append((name, mode, oid))
        if mode != "160000":
            objects.append(oid)
    # One cat-file process avoids a subprocess per file and never uses the worktree.
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"], input=("\n".join(objects) + "\n").encode(), capture_output=True, env=safe_env())
    if proc.returncode:
        raise AuditError("Git blob input unavailable")
    blobs = {}
    offset = 0
    for oid in objects:
        end = proc.stdout.find(b"\n", offset)
        header = proc.stdout[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise AuditError("Expected complete Git blob input")
        size = int(header[2]); offset = end + 1
        blobs[oid] = proc.stdout[offset:offset + size]
        offset += size + 1
    return [Entry(path, mode, blobs.get(oid, b"")) for path, mode, oid in rows]


def normalize(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def generated_pins(items: list[Entry], policy: dict) -> tuple[dict, set, list[dict]]:
    """Generated-output hashes come from an ordinary reviewed file in the audited tree.

    The trusted policy fixes which paths are generated outputs and the leading
    bytes each must carry; the pins file only supplies their current hashes, so
    regenerating a document needs ordinary review, not a trust update. A pin for
    any other path is a violation and is never honored.
    """
    config = policy.get("generated_assets")
    if not config:
        return {}, set(), []
    declared = {row["path"]: row["signature"].encode("utf-8") for row in config["paths"]}
    source = next((row for row in items if row.path == config["pins_file"]), None)
    if source is None:
        return declared, set(), []
    try:
        if source.mode not in ("100644", "100755"):
            raise ValueError()
        value = json.loads(source.data.decode("utf-8"))
        rows = [(row["path"], row["sha256"]) for row in value["assets"]]
        if value.get("version") != 1 or any(not isinstance(path, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for path, sha in rows):
            raise ValueError()
        # One current hash per output: a retained stale entry would readmit a superseded file.
        if len({path for path, _ in rows}) != len(rows):
            raise ValueError()
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError):
        return declared, set(), [finding("invalid-generated-asset-pins", source.path)]
    undeclared = [path for path, _ in rows if path not in declared]
    findings = [finding("undeclared-generated-asset-pin", source.path)] if undeclared else []
    return declared, {(path, sha) for path, sha in rows if path in declared}, findings


def check_entries(items: list[Entry], policy: dict) -> list[dict]:
    result = []
    seen = {}
    paths = {row.path for row in items}
    links = {row.path for row in items if row.mode == "120000"}
    pinned = {(row["path"], row["sha256"]) for row in policy["binary_assets"]}
    signatures, generated, pin_findings = generated_pins(items, policy)
    result.extend(pin_findings)

    def reviewed(path: str, data: bytes) -> bool:
        key = (path, digest(data))
        return key in pinned or (key in generated and data.startswith(signatures[path]))
    for item in items:
        path, data = item.path, item.data
        result.extend(finding(row["rule"], path) for row in check_metadata(path.encode("utf-8", "surrogateescape"), policy))
        norm = normalize(path)
        parts = norm.split("/")
        reserved = {"con", "prn", "aux", "nul"} | {f"{kind}{i}" for kind in ("com", "lpt") for i in range(1, 10)}
        invalid = (path != unicodedata.normalize("NFKC", path) or "\\" in path or ":" in path or path.startswith("/") or any(p in ("", ".", "..") for p in parts) or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in path) or any(p.endswith((" ", ".")) or p.split(".")[0] in reserved for p in parts))
        if invalid:
            result.append(finding("unsafe-path", path)); continue
        if norm in seen:
            result.append(finding("case-or-unicode-collision", path))
            result.append(finding("case-or-unicode-collision", seen[norm]))
        seen[norm] = path
        if norm in {normalize(p) for p in policy.get("forbidden_paths", [])}:
            result.append(finding("excluded-path", path))
        if any(p in policy["forbidden_components"] for p in parts) or parts[-1] in policy["forbidden_names"] or any(re.search(pattern, norm) for pattern in policy["forbidden_patterns"]):
            result.append(finding("excluded-category", path))
        if (len(parts) == 1 and path not in policy["allowed_root_files"]) or (len(parts) > 1 and parts[0] not in {normalize(p) for p in policy["allowed_roots"]}):
            result.append(finding("unclassified-root", path, "review"))
        if parts[-1] == "skill.md" and path not in policy["allowed_skill_paths"]:
            result.append(finding("unclassified-agent-skill", path, "review"))
        if item.mode == "160000":
            result.append(finding("external-submodule", path, "review")); continue
        if any(parent.as_posix() in links for parent in PurePosixPath(path).parents):
            result.append(finding("symlink-parent", path))
        if item.mode == "120000":
            try:
                target = data.decode("utf-8")
                destination = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if "\\" in target or ":" in target or target.startswith("/") or destination.startswith("../") or destination not in paths or destination in links:
                    result.append(finding("unsafe-symlink", path))
            except UnicodeDecodeError:
                result.append(finding("unsafe-symlink", path))
            continue
        if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
            result.append(finding("external-lfs-object", path, "review"))
        try:
            text = data.decode("utf-8")
            if "\0" in text:
                raise UnicodeDecodeError("utf8", data, 0, 1, "opaque")
        except UnicodeDecodeError:
            if not reviewed(path, data):
                result.append(finding("unreviewed-binary", path, "review"))
            continue
        # No broad test/docs exemption: synthetic fixtures construct dangerous strings.
        for rule in policy["content_rules"]:
            if re.search(rule["pattern"], text):
                result.append(finding(rule["id"], path))
        suffix = PurePosixPath(path).suffix.lower()
        if suffix and suffix not in policy["text_extensions"] and not reviewed(path, data):
            result.append(finding("unclassified-file-type", path, "review"))
    return result


def audit_tree(repo: Path, ref: str, policy: dict) -> list[dict]:
    return check_entries(entries(repo, ref), policy)


def audit_staged(repo: Path, policy: dict) -> list[dict]:
    return check_entries(entries(repo, None), policy)


def range_commits(repo: Path, base: str, head: str, source_base: str | None = None) -> list[str]:
    if git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise AuditError("Complete source history is required; shallow history cannot be audited")
    base, head = resolve(repo, base), resolve(repo, head)
    source = resolve(repo, source_base or base)
    known = set(git(repo, "rev-list", source).decode().split())
    commits = git(repo, "rev-list", "--reverse", "--topo-order", head, "^" + base).decode().split()
    # Each introduced commit must descend from the approved source boundary.
    # This rejects sibling private histories even when they share an old root.
    allowed = known | set(git(repo, "rev-list", base).decode().split())
    for commit in commits:
        parents = git(repo, "show", "-s", "--format=%P", commit).decode().split()
        if commit not in known:
            descendant = any(subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", boundary, commit], capture_output=True, env=safe_env()).returncode == 0 for boundary in (base, source))
            if not descendant or not parents or any(p not in allowed for p in parents):
                raise AuditError("Unapproved ancestry; import reviewed patches onto the source target")
        allowed.add(commit)
    return commits


def metadata_entries(repo: Path, commit: str) -> list[Entry]:
    return [Entry("commit-metadata.txt", "100644", expand_metadata(git(repo, "show", "-s", "--format=%B%n%an <%ae>%n%cn <%ce>", commit)))]


def expand_metadata(data: bytes) -> bytes:
    """Retain wire text and inspect decoded JSON keys/values as published text."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return data
    except RecursionError:
        raise AuditError("Metadata nesting exceeds the supported audit depth") from None
    decoded = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            decoded.append(item)
        elif isinstance(item, dict):
            for key, child in reversed(list(item.items())):
                pending.extend((child, key))
        elif isinstance(item, list):
            pending.extend(reversed(item))
    return data + b"\n" + "\n".join(decoded).encode("utf-8", "replace")


def check_metadata(data: bytes, policy: dict) -> list[dict]:
    text = expand_metadata(data).decode("utf-8", "replace")
    return [finding(rule["id"]) for rule in policy["content_rules"] if re.search(rule["pattern"], text)]


def changed_paths(repo: Path, commit: str) -> set[str]:
    """Include both rename sides and every merge parent; never skip re-additions."""
    parents = git(repo, "show", "-s", "--format=%P", commit).decode().split()
    if not parents:
        return {row.path for row in entries(repo, commit)}
    paths = set()
    for parent in parents:
        raw = git(repo, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", parent, commit)
        paths.update(path.decode("utf-8", "surrogateescape") for path in raw.split(b"\0") if path)
    return paths


def findings_for_paths(findings: list[dict], paths: set[str]) -> list[dict]:
    identifiers = {finding("path", path)["path_id"] for path in paths}
    return [row for row in findings if row["path_id"] is None or row["path_id"] in identifiers]


def audit_range(repo: Path, base: str, head: str, policy: dict, source_base: str | None = None) -> list[dict]:
    result = []
    for commit in range_commits(repo, base, head, source_base):
        result.extend(findings_for_paths(audit_tree(repo, commit, policy), changed_paths(repo, commit)))
        result.extend(check_metadata(metadata_entries(repo, commit)[0].data, policy))
    result.extend(audit_tree(repo, head, policy))
    return result


def export_entries(directory: Path) -> list[Entry]:
    if not directory.is_dir():
        raise AuditError("Export directory unavailable")
    result = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in list(dirs):
            if (Path(root) / name).is_symlink():
                files.append(name); dirs.remove(name)
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(directory).as_posix()
            result.append(Entry(relative, "120000" if path.is_symlink() else "100644", os.readlink(path).encode() if path.is_symlink() else path.read_bytes()))
    return result


def audit_export(directory: Path, policy: dict) -> list[dict]:
    return check_entries(export_entries(directory), policy)


def secret_scan(items: list[Entry], policy: dict) -> list[dict]:
    binary = shutil.which("gitleaks")
    if not binary:
        raise AuditError("Install the policy-pinned Gitleaks version before publication")
    version = subprocess.run([binary, "version"], capture_output=True, env=safe_env(), text=True)
    if version.returncode or version.stdout.strip().lstrip("v") != policy["gitleaks_version"]:
        raise AuditError("Gitleaks version does not match reviewed policy")
    # Numbered blobs avoid hostile names, ignore-file injection, and symlink traversal.
    with tempfile.TemporaryDirectory(prefix="source-audit-") as temporary:
        root = Path(temporary); content = root / "content"; content.mkdir()
        indexed = {}
        scan_items = list(items) + [Entry("filename-" + digest(row.path.encode("utf-8", "surrogateescape")), "100644", row.path.encode("utf-8", "surrogateescape")) for row in items]
        for index, row in enumerate(scan_items):
            if row.mode == "160000":
                continue
            name = f"blob-{index}.txt"
            (content / name).write_bytes(row.data)
            indexed[name] = row
        config = root / "gitleaks.toml"
        config.write_text('[extend]\nuseDefault = true\n', encoding="utf-8")
        report = root / "report.json"
        proc = subprocess.run([binary, "detect", "--no-git", "--source", str(content), "--config", str(config), "--report-format", "json", "--report-path", str(report), "--redact", "--no-banner", "--exit-code", "10"], capture_output=True, cwd=root, env=safe_env())
        if proc.returncode not in (0, 10):
            raise AuditError("Secret scanner failed; publication remains blocked")
        try:
            records = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise ValueError()
        except (ValueError, OSError):
            raise AuditError("Secret scanner produced an invalid report") from None
        if proc.returncode == 10 and not records:
            raise AuditError("Secret scanner report missing findings")
        findings = []
        for record in records:
            row = indexed.get(Path(record.get("File", "")).name)
            if row is None:
                raise AuditError("Secret scanner reported an unknown input")
            excepted = any(e["path"] == row.path and e["sha256"] == digest(row.data) and e["rule"] == record.get("RuleID") and e["line"] == record.get("StartLine") for e in policy["secret_exceptions"])
            if not excepted:
                findings.append(finding("credential-detection", row.path))
        return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("source-policy.json"))
    parser.add_argument("--receipt", type=Path)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("tree").add_argument("--ref", default="HEAD")
    modes.add_parser("staged")
    outgoing = modes.add_parser("range")
    outgoing.add_argument("--base", required=True); outgoing.add_argument("--head", required=True); outgoing.add_argument("--source-base")
    modes.add_parser("export").add_argument("--directory", type=Path, required=True)
    modes.add_parser("metadata").add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = {"mode": args.mode, "status": "error", "findings": []}
    try:
        policy = load_policy(args.policy)
        receipt.update(policy_sha256=digest(args.policy.read_bytes()), checker_sha256=digest(Path(__file__).read_bytes()))
        groups = []
        if args.mode == "tree":
            receipt["head"] = resolve(args.repo, args.ref); groups.append((entries(args.repo, args.ref), None))
        elif args.mode == "staged":
            groups.append((entries(args.repo, None), None)); receipt["index_tree"] = git(args.repo, "write-tree").decode().strip()
        elif args.mode == "range":
            receipt.update(base=resolve(args.repo, args.base), head=resolve(args.repo, args.head))
            commits = range_commits(args.repo, args.base, args.head, args.source_base)
            receipt["commits"] = len(commits)
            for commit in commits:
                groups.append((entries(args.repo, commit), changed_paths(args.repo, commit)))
                meta = metadata_entries(args.repo, commit)
                receipt["findings"].extend(check_metadata(meta[0].data, policy))
                receipt["findings"].extend(secret_scan(meta, policy))
            # Every outgoing head must satisfy today's complete tree policy,
            # including unchanged files inherited from the known remote base.
            groups.append((entries(args.repo, args.head), None))
        elif args.mode == "export":
            groups.append((export_entries(args.directory), None))
        elif args.mode == "metadata":
            meta = Entry("publication-metadata.txt", "100644", expand_metadata(args.file.read_bytes()))
            receipt["findings"].extend(check_metadata(meta.data, policy))
            receipt["findings"].extend(secret_scan([meta], policy))
        checked = set()
        for group, changed in groups:
            findings = check_entries(group, policy)
            receipt["findings"].extend(findings if changed is None else findings_for_paths(findings, changed))
            selected = group if changed is None else [row for row in group if row.path in changed]
            new = [row for row in selected if (row.path, digest(row.data)) not in checked]
            checked.update((row.path, digest(row.data)) for row in new)
            if new:
                receipt["findings"].extend(secret_scan(new, policy))
        receipt["files_checked"] = len(checked)
        receipt["findings"] = list({json.dumps(row, sort_keys=True): row for row in receipt["findings"]}.values())
        receipt["status"] = "blocked" if receipt["findings"] else "pass"
        code = 1 if receipt["findings"] else 0
    except (AuditError, OSError) as exc:
        receipt["error"] = str(exc) if isinstance(exc, AuditError) else "Audit input or output unavailable"
        code = 2
    output = json.dumps(receipt, indent=2) + "\n"
    if args.receipt:
        args.receipt.write_text(output, encoding="utf-8")
    print(output, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
