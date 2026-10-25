"""Preserve private build evidence and assemble explicitly reviewed source files.

No downloads, builds, signing, publication or automatic compliance certification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile


class InputError(ValueError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def regular_file(path: Path) -> Path:
    path = path.absolute()
    # Reject links/junctions at any level rather than following a changed target.
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise InputError(f"Linked input is not permitted: {path.name}")
    if not path.is_file():
        raise InputError(f"Required file missing: {path.name}")
    return path


def tree_files(root: Path):
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise InputError("Linked build directory is not permitted")
    def traversal_error(error: OSError):
        raise InputError("Unable to read the complete build directory") from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
        for name in dirs:
            child = Path(directory) / name
            if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                raise InputError("Linked build directory is not permitted")
        for name in sorted(files):
            yield regular_file(Path(directory) / name)


def json_file(path: Path) -> dict:
    data = json.loads(regular_file(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise InputError("Expected a JSON object")
    return data


def copy_checked(source: Path, destination: Path) -> dict:
    before = digest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if digest(source) != before or digest(destination) != before:
        raise InputError("Input changed during capture; wait for the build to finish")
    return {"sha256": before, "bytes": destination.stat().st_size}


def capture(repo: Path, artifact: Path, output: Path, build_dirs: list[Path], report: Path) -> Path:
    repo, output = repo.resolve(), output.absolute()
    artifact, report = regular_file(artifact), regular_file(report)
    if output.exists():
        raise InputError("Output already exists; select a new directory")
    if not build_dirs:
        build_dirs = sorted(p for p in (repo / "dist").glob("*")
                            if p.is_dir() and p.name.endswith((".build", ".dist", ".onefile-build")))
    if not any(p.name.endswith(".build") for p in build_dirs):
        raise InputError("Nuitka .build inputs missing; an installer alone is insufficient")
    if not any(p.name.endswith(".dist") for p in build_dirs):
        raise InputError("Nuitka .dist inputs missing; retain the complete compile outputs")
    names = [p.name.casefold() for p in build_dirs]
    if len(set(names)) != len(names):
        raise InputError("Build directory names must be unique")
    for directory in build_dirs:
        if not directory.is_dir():
            raise InputError("Build directory missing")
        if output.resolve().is_relative_to(directory.resolve()):
            raise InputError("Output cannot be inside a captured build directory")
    git = shutil.which("git")
    if not git:
        raise InputError("git is required to identify tracked source inputs")

    def git_read(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    head = git_read("rev-parse", "HEAD").decode().strip()
    status_before = git_read("status", "--porcelain=v1", "--untracked-files=no")
    tracked = git_read("ls-files", "-z").decode("utf-8").split("\0")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="source-capture-", dir=output.parent))
    records, skipped_links = {}, []
    try:
        for item in sorted(set(filter(None, tracked))):
            source = repo / item
            if source.is_symlink():
                skipped_links.append({"path": item, "target": os.readlink(source)})
                continue
            source = regular_file(source)
            if not source.resolve().is_relative_to(repo):
                raise InputError("Tracked input is outside the repository")
            name = "working-tree/" + item
            records[name] = copy_checked(source, temporary / name)
        for directory in build_dirs:
            for source in tree_files(directory):
                name = "nuitka/" + directory.name + "/" + source.relative_to(directory.absolute()).as_posix()
                records[name] = copy_checked(source, temporary / name)
        for source, name in [(artifact, "artifact/" + artifact.name), (report, "evidence/compilation-report.xml")]:
            records[name] = copy_checked(source, temporary / name)
        if git_read("rev-parse", "HEAD").decode().strip() != head or git_read(
            "status", "--porcelain=v1", "--untracked-files=no"
        ) != status_before:
            raise InputError("Repository changed during capture; retry on a stable checkout")
        artifact_hash = records["artifact/" + artifact.name]["sha256"]
        if digest(artifact) != artifact_hash:
            raise InputError("Artifact changed during capture; retry on stable outputs")
        receipt = {
            "schema_version": 1, "visibility": "private-do-not-publish", "commit": head,
            "working_tree_dirty": bool(status_before), "skipped_source_links": skipped_links,
            "artifact_sha256": artifact_hash, "files": records,
            "limitations": [
                "Snapshot taken after build; does not prove these inputs produced the artifact.",
                "Tracked working files and explicit compile outputs only; no untracked dependencies or SDKs.",
                "Build reports/generated code may contain personal paths or confidential values.",
                "Preservation is not proof of LGPL relinkability or complete license coverage.",
            ],
        }
        (temporary / "capture.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return output


def archive_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise InputError("Archive paths must be relative POSIX paths")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", ".", "") for part in name.split("/")):
        raise InputError("Unsafe archive path")
    if any(part.rstrip(" .") != part for part in path.parts):
        raise InputError("Ambiguous archive path")
    for part in path.parts:
        if any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise InputError("Archive path is invalid on Windows")
        if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", part.split(".")[0], re.I):
            raise InputError("Archive path uses a Windows device name")
    return name


def forbidden_name(name: str) -> bool:
    for part in PurePosixPath(name.replace("\\", "/")).parts:
        lower = part.lower()
        if lower in {".git", ".env", "config.toml", "capture.json", "compilation-report.xml"}:
            return True
        if lower.startswith((".env.", "ip_assignment", "board_resolution_ip_assignment")):
            return True
        if lower.endswith((".key", ".pfx", ".p12", ".pem")):
            return True
    return False


def checked_entry(entry: dict, base: Path) -> Path:
    source = regular_file(base / entry["source"])
    expected = entry.get("sha256", "")
    if len(expected) != 64 or digest(source) != expected:
        raise InputError(f"Missing or mismatched SHA256: {source.name}")
    return source


def public_materials(plan: dict) -> dict:
    """Whitelist public metadata; arbitrary private plan fields never leave it."""
    components = []
    for item in plan.get("components", []):
        clean = {}
        for field in ("id", "version", "license"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise InputError("Component id, version and license must be nonempty strings")
            clean[field] = item[field]
        for field in ("modified", "relink_required"):
            if type(item.get(field)) is not bool:
                raise InputError("Component modified/relink_required must be explicit booleans")
            clean[field] = item[field]
        components.append(clean)
    files = []
    for entry in plan.get("files", []):
        clean = {}
        for key in ("path", "sha256", "role", "component"):
            if key in entry:
                if not isinstance(entry[key], str) or not entry[key]:
                    raise InputError("Public file metadata must be nonempty strings")
                clean[key] = entry[key]
        files.append(clean)
    if not isinstance(plan.get("release_id"), str) or not plan["release_id"]:
        raise InputError("release_id must be a nonempty string")
    return {"release_id": plan["release_id"], "artifact_sha256": plan["artifact"]["sha256"],
            "components": sorted(components, key=lambda c: c["id"]),
            "files": sorted(files, key=lambda f: f["path"])}


def materials_hash(materials: dict) -> str:
    return hashlib.sha256(json.dumps(materials, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def validate_plan(plan_path: Path, *, require_validation: bool = True) -> tuple[dict, list[tuple[Path, dict]], dict]:
    plan = json_file(plan_path)
    base = plan_path.resolve().parent
    if plan.get("schema_version") != 1 or not plan.get("release_id"):
        raise InputError("Unsupported plan or missing release_id")
    artifact = checked_entry(plan["artifact"], base)
    material_description = public_materials(plan)
    source_hash = materials_hash(material_description)
    if plan.get("public_files_reviewed") is not True:
        raise InputError("Review the exact public file list before assembly")
    components = plan.get("components", [])
    if not components or len({c["id"] for c in components}) != len(components):
        raise InputError("A unique component inventory is required")
    ids = {c["id"] for c in components}
    files, names, coverage = [], set(), {key: set() for key in ids}
    global_roles = set()
    for entry in plan.get("files", []):
        name = archive_name(entry["path"])
        key = name.casefold()
        occupied = names | {"source-materials.json"}
        if any(key == other or key.startswith(other + "/") or other.startswith(key + "/")
               for other in occupied):
            raise InputError("Duplicate or reserved archive name")
        names.add(key)
        source = checked_entry(entry, base)
        if forbidden_name(name) or forbidden_name(str(source)):
            raise InputError("Private evidence or credential-like file selected for public archive")
        role = entry["role"]
        component = entry.get("component")
        if component is not None:
            if component not in ids:
                raise InputError("File references an unknown component")
            coverage[component].add(role)
        global_roles.add(role)
        files.append((source, entry))
    if not {"notices", "build-instructions", "application-code"} <= global_roles:
        raise InputError("Notices, build instructions and application relinking code are required")
    for component in components:
        if not component.get("version") or not component.get("license"):
            raise InputError("Exact component version and license are required")
        if "source" not in coverage[component["id"]]:
            raise InputError(f"Source missing for component: {component['id']}")
        if component.get("modified") and "modifications" not in coverage[component["id"]]:
            raise InputError(f"Modification materials missing: {component['id']}")
    proof = None
    if require_validation:
        proof = checked_entry(plan["relink_validation"], base)
        validation = json_file(proof)
        if (validation.get("status") != "passed" or validation.get("artifact_sha256") != digest(artifact)
                or not validation.get("modified_components") or not validation.get("commands")
                or not validation.get("result") or validation.get("requires_publisher_secrets") is not False):
            raise InputError("Matching successful relink validation without publisher secrets is required")
        for component in components:
            if component.get("relink_required") and component["id"] not in validation["modified_components"]:
                raise InputError(f"Relink validation missing for component: {component['id']}")
        if validation.get("materials_sha256") != source_hash:
            raise InputError("Relink validation does not match the exact materials manifest")
    public_manifest = {
        "schema_version": 1, **material_description, "materials_sha256": source_hash,
        "validation_receipt_sha256": digest(proof) if proof else None,
        "scope": "Assembly checks only; not a certificate of legal compliance or independently executed relinking.",
    }
    return plan, files, public_manifest


def assemble(plan_path: Path, output: Path) -> Path:
    _, files, manifest = validate_plan(plan_path)
    if output.exists():
        raise InputError("Output already exists; select a new archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental overwrite, including on concurrent runs.
    try:
        with output.open("xb") as stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                for source, entry in sorted(files, key=lambda pair: pair[1]["path"]):
                    if digest(source) != entry["sha256"]:
                        raise InputError("Input changed after validation")
                    archive.write(source, entry["path"])
                archive.writestr("source-materials.json", json.dumps(manifest, indent=2) + "\n")
        with zipfile.ZipFile(output) as archive:
            for _, entry in files:
                h = hashlib.sha256()
                with archive.open(entry["path"]) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(block)
                if h.hexdigest() != entry["sha256"]:
                    raise InputError("Archived content differs from reviewed SHA256")
    except FileExistsError:
        raise InputError("Output already exists; select a new archive") from None
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture", help="Preserve PRIVATE evidence after the owner finishes a build")
    cap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    cap.add_argument("--artifact", type=Path, required=True)
    cap.add_argument("--output", type=Path, required=True)
    cap.add_argument("--build-dir", type=Path, action="append", default=[])
    cap.add_argument("--report", type=Path)
    check = commands.add_parser("check", help="Validate explicit materials and existing relink evidence")
    check.add_argument("--plan", type=Path, required=True)
    fingerprint = commands.add_parser("materials-digest", help="Check materials and compute the identity to bind in a real relink receipt")
    fingerprint.add_argument("--plan", type=Path, required=True)
    package = commands.add_parser("assemble", help="Create a ZIP containing only explicitly reviewed files")
    package.add_argument("--plan", type=Path, required=True)
    package.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = capture(args.repo, args.artifact, args.output, args.build_dir,
                             args.report or args.repo / "dist" / "compilation-report.xml")
            print(f"Private evidence captured: {result}. Do not publish this directory.")
        elif args.command == "materials-digest":
            _, _, manifest = validate_plan(args.plan, require_validation=False)
            print(manifest["materials_sha256"])
        elif args.command == "check":
            _, files, _ = validate_plan(args.plan)
            print(f"Assembly inputs verified: {len(files)} files. Legal/relink review is separate.")
        else:
            result = assemble(args.plan, args.output)
            print(f"Reviewed materials assembled: {result} SHA256={digest(result)}")
        return 0
    except (InputError, OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Source-material preparation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
