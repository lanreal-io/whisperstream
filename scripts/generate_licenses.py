"""Generate the Whisperstream third-party license audit (native Python port).

This is a faithful, byte-equivalent-intent port of scripts/generate-licenses.sh
(Task 3.2). It orchestrates the native dependency tools (uv export,
uv run pip-licenses, npx license-checker, cargo about / cargo license) and
fetches HuggingFace + creativecommons.org via urllib, then does ALL parsing
in-process (json + string ops, replacing jq/awk/sed/grep/tr/sort/wc/curl).

Produces:
  - THIRD_PARTY_LICENSES.txt                       (repo root, bundled via tauri.conf.json)
  - src-tauri/resources/THIRD_PARTY_LICENSES.txt   (staged copy for Tauri resources)
  - docs/dependency-licenses.md                      (structured audit report)

Covers seven sections (matching the bash original):
  1. Python dependencies (pip-licenses against uv runtime closure)
  2. Native libraries compiled into the sidecar (ONNX Runtime, sherpa-onnx, PortAudio)
  3. Frontend/npm dependencies (license-checker, production only)
  4. Rust crates for the Tauri desktop shell (cargo-about, fallback to cargo-license)
  5. NVIDIA Parakeet TDT 0.6B v3 model license (fetched from HuggingFace)
  6. Bundled font families (Outfit and Inter OFL-1.1)
  7. Bundled binary + downloadable model attributions

License text bodies for native libraries are read from scripts/license-texts/*.txt
at runtime. This script MUST NOT inline any open-source license bodies.

Entrypoints:
  - generate_licenses() -- importable; build.py Step 5 calls it.
  - main() -> int       -- CLI entry; exit code mirrors the bash exit semantics.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import locale
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Make the sibling helper module importable when run as `python scripts/generate_licenses.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _licenses_lib as lic  # noqa: E402  (after sys.path tweak)
from fetch_llama_server import LLAMA_CPP_RELEASE_TAG  # noqa: E402


# ---------------------------------------------------------------------------
# Repo root + constants
# ---------------------------------------------------------------------------

ROOT: Path = Path(__file__).resolve().parent.parent

SEPARATOR = "============================================================"
SUB_SEP = "------------------------------------------------------------"

LICENSE_TEXTS_DIR = ROOT / "scripts" / "license-texts"
APACHE_FILE = LICENSE_TEXTS_DIR / "apache-2.0.txt"
MIT_FILE = LICENSE_TEXTS_DIR / "mit.txt"
PORTAUDIO_FILE = LICENSE_TEXTS_DIR / "portaudio.txt"
ONNXRUNTIME_FILE = LICENSE_TEXTS_DIR / "onnxruntime-mit.txt"
CCBY40_FILE = LICENSE_TEXTS_DIR / "cc-by-4.0.txt"

OUT_TXT = ROOT / "THIRD_PARTY_LICENSES.txt"
OUT_STAGED = ROOT / "src-tauri" / "resources" / "THIRD_PARTY_LICENSES.txt"
OUT_REPORT = ROOT / "docs" / "dependency-licenses.md"

PYTHON_LICENSE_TARGETS = ("3.12", "3.14")
PARAKEET_CARD_URL = "https://huggingface.co/api/models/nvidia/parakeet-tdt-0.6b-v3"
PARAKEET_ATTRIBUTION = "nvidia/parakeet-tdt-0.6b-v3 (HuggingFace)"
CCBY_URL = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"


# ---------------------------------------------------------------------------
# Logging banners (mirror the bash info/warn/err/ok/banner helpers).
# Colors are dropped (the bash original only emits color when stdout is a TTY
# AND tput is available; under a Tee'd build log neither holds, so the plain
# uncolored form is what actually shipped).
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print("")
    print(f"=== {msg} ===")


def info(msg: str) -> None:
    print(f"[info] {msg}")


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"[ok] {msg}")


class GeneratorError(SystemExit):
    """Raised to abort generation with a non-zero exit code (mirrors `exit 1`)."""


def _fail(*messages: str) -> "None":
    """Emit error lines then abort with exit code 1 (mirrors bash err + exit 1)."""
    for m in messages:
        err(m)
    raise GeneratorError(1)


# ---------------------------------------------------------------------------
# Tool prerequisites (mirrors require_tool). jq/curl are NOT required anymore
# (replaced by json + urllib), so only the native exes the port still shells
# out to are checked: uv, npx, cargo.
# ---------------------------------------------------------------------------

def _require_tool(tool: str, hint: str = "") -> None:
    if shutil.which(tool) is None:
        if hint:
            _fail(f"required tool '{tool}' not found on PATH", f"hint: {hint}")
        else:
            _fail(f"required tool '{tool}' not found on PATH")


# ---------------------------------------------------------------------------
# Row accumulator (replaces the rows.tsv tempfile + add_row).
# Each row is a 6-tuple of strings already substituted to the "-" sentinel for
# empty fields, exactly like the bash add_row writes to the TSV.
# ---------------------------------------------------------------------------

class Rows:
    """In-memory replacement for the bash $ROWS_FILE TSV."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, str, str, str, str, str]] = []

    def add(
        self,
        ecosystem: str = "",
        package: str = "",
        version: str = "",
        license_: str = "",
        source: str = "",
        flags: str = "",
    ) -> None:
        # Substitute empty fields with "-" so downstream logic mirrors the TSV
        # field-position preservation the bash add_row enforced.
        def sub(v: str) -> str:
            return v if v else "-"

        self._rows.append(
            (
                sub(ecosystem),
                sub(package),
                sub(version),
                sub(license_),
                sub(source),
                sub(flags),
            )
        )

    def count_eco(self, eco: str) -> int:
        return sum(1 for r in self._rows if r[0] == eco)

    def total(self) -> int:
        return len(self._rows)

    def flagged(self) -> int:
        # awk '$6!="-" && $6!="" {n++}'
        return sum(1 for r in self._rows if r[5] != "-" and r[5] != "")

    def all_rows(self) -> "list[tuple[str, str, str, str, str, str]]":
        return list(self._rows)


# ---------------------------------------------------------------------------
# subprocess helper: run a native tool, capture stdout (and optionally stderr).
# Mirrors the bash `cmd >outfile 2>errfile` pattern with explicit success
# checking. text mode with errors="replace" so odd bytes never crash the port.
#
# argv[0] is resolved through shutil.which() so Windows batch shims (npx.CMD,
# npm.CMD) launch the same way they did under Git Bash. CreateProcess (the
# backend of subprocess on Windows, with shell=False) only auto-appends ".exe"
# to a bare command name, so a bare "npx" never finds "npx.CMD" and raises
# FileNotFoundError. shutil.which honors PATHEXT and returns the full path with
# the real extension, matching Git Bash's shim resolution exactly.
# ---------------------------------------------------------------------------

def _run_capture(
    argv: "list[str]",
    *,
    cwd: "Path | None" = None,
) -> "subprocess.CompletedProcess[str]":
    resolved = list(argv)
    if resolved:
        which = shutil.which(resolved[0])
        if which is not None:
            resolved[0] = which
    return subprocess.run(
        resolved,
        cwd=str(cwd if cwd is not None else ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------------------------
# Output-file writer accumulating the THIRD_PARTY_LICENSES.txt body.
# The bash original appends to OUT_TXT incrementally; we accumulate in a list
# and write once at the end (then copy to OUT_STAGED). Newline handling matches
# bash `echo`/`printf` (each appended chunk ends in a newline).
# ---------------------------------------------------------------------------

class TxtBuilder:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def line(self, s: str = "") -> None:
        """Append a single line plus a trailing newline (mirrors echo)."""
        self._parts.append(s + "\n")

    def raw(self, s: str) -> None:
        """Append raw text verbatim (mirrors cat: the file content as-is)."""
        self._parts.append(s)

    def text(self) -> str:
        return "".join(self._parts)


# ---------------------------------------------------------------------------
# SECTION 1: Python dependencies
# ---------------------------------------------------------------------------

def _merge_pip_license_entries(
    entry_sets: list[list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Merge target-specific pip-licenses rows without losing marker branches."""
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for entries in entry_sets:
        for entry in entries:
            key = (
                lic.normalize_pkg_name(str(entry.get("Name") or "")),
                str(entry.get("Version") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return merged


def _section1_python(txt: TxtBuilder, rows: Rows, work: Path) -> tuple[str, str]:
    """Emit Section 1; return (uv_tree_text, compilation_note)."""
    banner("Section 1: Python dependencies (pip-licenses)")

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 1: Python Dependencies")
    txt.line(SEPARATOR)
    txt.line("")

    # Match the package-build interpreter, independently of the active venv.
    python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", python_version):
        _fail(".python-version must pin the shipping Python version")
    info(f"Capturing locked runtime closure for Python {python_version}...")
    tree = _run_capture([
        "uv", "tree", "--locked", "--no-dev", "--python-version", python_version,
    ])
    if tree.returncode != 0:
        err("uv tree --no-dev failed")
        if tree.stderr:
            print(tree.stderr, file=sys.stderr, end="")
        raise GeneratorError(1)
    uv_tree_text = tree.stdout
    # wc -l counts newlines.
    ok(f"uv tree appendix captured ({uv_tree_text.count(chr(10))} lines)")

    # Build the normalized runtime-closure set via uv export.
    export = _run_capture(
        [
            "uv", "export", "--locked",
            "--no-dev", "--no-hashes", "--no-emit-project",
            "--format", "requirements-txt",
        ]
    )
    if export.returncode != 0:
        err("uv export --no-dev failed")
        if export.stderr:
            print(export.stderr, file=sys.stderr, end="")
        raise GeneratorError(1)
    runtime_pkgs = lic.runtime_closure_from_uv_export(export.stdout)
    runtime_pkg_count = len(runtime_pkgs)
    ok(f"Runtime closure from uv export: {runtime_pkg_count} packages")

    # Audit both dependency-marker branches in isolated locked environments.
    # Python 3.12 covers the pygame branch and Python 3.14 covers pygame-ce.
    entry_sets: list[list[dict[str, object]]] = []
    for python_target in PYTHON_LICENSE_TARGETS:
        info(f"Running pip-licenses for locked Python {python_target} runtime...")
        pl = _run_capture(
            [
                "uv", "run", "--locked", "--isolated",
                "--python", python_target,
                "--no-dev", "--with", "pip-licenses", "pip-licenses",
                "--format=json",
                "--with-urls",
                "--with-license-file",
                "--no-license-path",
            ]
        )
        if pl.returncode != 0:
            err(f"pip-licenses failed for Python {python_target}")
            if pl.stderr:
                print(pl.stderr, file=sys.stderr, end="")
            raise GeneratorError(1)
        try:
            target_entries = json.loads(pl.stdout) if pl.stdout.strip() else []
        except json.JSONDecodeError:
            target_entries = []
        if not isinstance(target_entries, list):
            target_entries = []
        entry_sets.append(target_entries)
        ok(f"Python {python_target}: pip-licenses reported {len(target_entries)} packages")

    pip_entries = _merge_pip_license_entries(entry_sets)
    py_raw_count = len(pip_entries)
    ok(f"pip-licenses merged {py_raw_count} package/version rows (will filter to runtime closure)")

    # Defensive guard: pip-licenses returns {} (-> 0 packages) if .venv is empty.
    if py_raw_count == 0:
        py_declared = _count_pyproject_runtime_deps(ROOT / "pyproject.toml")
        if py_declared > 0:
            _fail(
                f"pip-licenses returned 0 packages but pyproject.toml declares {py_declared} runtime dependencies.",
                "This means the active Python environment is empty or pip-licenses failed silently.",
                "Fix: run 'uv sync --extra dev' from the repo root, then re-run this script.",
            )
        warn("pip-licenses returned 0 packages and pyproject.toml declares no runtime deps; continuing.")

    # Cross-check against Nuitka's compilation report, if present.
    compilation_report = ROOT / "dist" / "compilation-report.xml"
    shipped_pkgs: set[str] | None = None
    previous_shipped: dict[tuple[str, str], str] = {}
    if compilation_report.is_file():
        info("Cross-checking against dist/compilation-report.xml...")
        shipped_pkgs = _shipped_top_level_packages(compilation_report)
        if not shipped_pkgs:
            _fail("compilation-report.xml contains no modules; restore a valid build report or remove it.")
        ok(f"compilation-report.xml lists {len(shipped_pkgs)} top-level packages")
        compilation_note = (
            "compilation-report.xml found; 'Shipped' column reflects Nuitka's top-level package list."
        )
    else:
        previous_shipped = _committed_shipped_values()
        compilation_note = (
            "compilation-report.xml not found; 'Shipped' values retained from committed "
            "notices for matching package versions; new versions are unknown."
        )
        warn(compilation_note)

    # Emit per-package entries into OUT_TXT and rows.
    py_rows_written = 0
    for pkg in pip_entries:
        name = pkg.get("Name") or ""
        version = pkg.get("Version") or ""
        license_ = pkg.get("License") or ""
        url = pkg.get("URL") or ""
        # The bash captured ltext via `$(jq ... | tr -d '\r')` command
        # substitution, which strips ALL trailing newlines (and \r) from the
        # value before re-emitting it with a single `printf '%s\n'`. pip-licenses
        # LicenseText values carry their own trailing newline(s) in the JSON, so
        # without this strip the port would emit one extra blank line per package
        # body vs the bash original. Strip trailing \r/\n to match command-sub.
        ltext = (pkg.get("LicenseText") or "").replace("\r", "").rstrip("\n")

        # Filter: skip packages NOT in the true runtime closure.
        name_norm = lic.normalize_pkg_name(name)
        if name_norm not in runtime_pkgs:
            continue

        # Manual license overrides for packages with broken/missing PyPI metadata.
        if name_norm in lic.MANUAL_PYTHON_OVERRIDES:
            override = lic.MANUAL_PYTHON_OVERRIDES[name_norm]
            license_ = override["license"]
            ltext = override["ltext"]

        shipped = previous_shipped.get((name_norm, version), "unknown")
        if shipped_pkgs is not None:
            # Case-insensitive match against the shipped top-level package set.
            # bash: grep -qix "$name_lower" (name already lower + dash->underscore).
            name_lower = lic.normalize_pkg_name(name)
            if name_lower in shipped_pkgs:
                shipped = "yes"
            else:
                shipped = "closure-only"

        flag = lic.classify_flag(license_)
        rows.add("Python", name, version, license_, "PyPI", flag)
        py_rows_written += 1

        txt.line(SUB_SEP)
        txt.line(f"Package : {name}")
        txt.line(f"Version : {version}")
        txt.line(f"License : {license_}")
        txt.line(f"URL     : {url}")
        txt.line(f"Shipped : {shipped}")
        txt.line(SUB_SEP)
        if ltext and ltext != "UNKNOWN":
            # printf '%s\n' "$ltext": the text plus one trailing newline.
            txt.raw(ltext + "\n")
        else:
            txt.line("(no license text available from package metadata)")
        txt.line("")

    # Row-count guard.
    if py_raw_count > 0 and runtime_pkg_count > 0 and py_rows_written == 0:
        _fail(
            f"pip-licenses reported {py_raw_count} packages and uv export listed {runtime_pkg_count} runtime deps, but 0 Python rows landed in the audit table.",
            "The Python section loop dropped entries (likely a jq/pipefail interaction).",
            "Re-run the script; if this keeps happening, inspect the pip-licenses output for malformed entries.",
        )
    ok(f"Python section: {py_rows_written} rows landed in audit table")

    return uv_tree_text, compilation_note


def _committed_shipped_values() -> dict[tuple[str, str], str]:
    """Use Git evidence, never an earlier uncommitted generator run."""
    try:
        committed = _run_capture(["git", "show", "HEAD:THIRD_PARTY_LICENSES.txt"])
    except FileNotFoundError:
        warn("Git unavailable; Python 'Shipped' values will be unknown.")
        return {}
    if committed.returncode != 0:
        warn("Committed notices unavailable; Python 'Shipped' values will be unknown.")
        return {}
    section = committed.stdout.split("SECTION 2:", 1)[0]
    return {
        (lic.normalize_pkg_name(name), version): shipped
        for name, version, shipped in re.findall(
            r"^Package : ([^\n]+)\nVersion : ([^\n]+)\n"
            r"License : [^\n]*\nURL     : [^\n]*\nShipped : (yes|closure-only|unknown)$",
            section, re.MULTILINE,
        )
    }


def _count_pyproject_runtime_deps(pyproject: Path) -> int:
    """Count quoted entries in the [project] dependencies array.

    Mirrors the awk state machine in generate-licenses.sh lines 247-254:
      - enter [project]; leave on any other [section]
      - inside, find `dependencies = [`; leave the deps block on a line == `]`
      - each line containing a quoted "..." token counts once
    """
    if not pyproject.is_file():
        return 0
    in_proj = False
    in_deps = False
    count = 0
    quoted = re.compile(r'"[^"]+"')
    for raw_line in pyproject.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^\[project\]", raw_line):
            in_proj = True
            continue
        if re.match(r"^\[", raw_line):
            in_proj = False
        if in_proj and re.match(r"^dependencies[ \t]*=[ \t]*\[", raw_line):
            in_deps = True
            continue
        if in_deps and re.match(r"^\]", raw_line):
            in_deps = False
        if in_deps and quoted.search(raw_line):
            count += 1
    return count


def _shipped_top_level_packages(report: Path) -> set[str]:
    """Extract the top-level package set from a Nuitka compilation-report.xml.

    Mirrors generate-licenses.sh lines 273-276:
      grep -oE 'module name="[^"]+"' | sed 's/module name="(...)"/\\1/'
      | awk -F'.' '{print $1}' | sort -u

    Tolerant regex (works without an XML parser), then take the first
    dot-separated path segment, lowercased via the same set semantics. The bash
    did NOT lowercase here (sort -u only), so we preserve case as the bash did
    and rely on the case-insensitive membership test at the call site.
    """
    text = report.read_text(encoding="utf-8", errors="replace")
    result: set[str] = set()
    for m in re.finditer(r'module name="([^"]+)"', text):
        module = m.group(1)
        top = module.split(".", 1)[0]
        if top:
            # Normalize for the case-insensitive shipped match performed later.
            # The bash compared `grep -qix "$name_lower"` (case-insensitive) so
            # storing a normalized key here is equivalent to that comparison.
            result.add(lic.normalize_pkg_name(top))
    return result


# ---------------------------------------------------------------------------
# SECTION 2: Native libraries (manual attribution)
# ---------------------------------------------------------------------------

def _section2_native(txt: TxtBuilder, rows: Rows) -> None:
    banner("Section 2: Native libraries compiled into the sidecar")

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 2: Native Libraries (compiled into sidecar)")
    txt.line(SEPARATOR)
    txt.line("")
    txt.line("These libraries ship as compiled native code inside the frozen backend exe")
    txt.line("and are not visible to pip-licenses as standalone Python packages.")
    txt.line("")

    def emit_native(
        name: str,
        holder: str,
        spdx: str,
        url: str,
        text_file: Path,
        *,
        version: str = "bundled",
        provenance: tuple[str, ...] = (),
    ) -> None:
        txt.line(SUB_SEP)
        txt.line(f"Library : {name}")
        if version != "bundled":
            txt.line(f"Version : {version}")
        txt.line(f"Holder  : {holder}")
        txt.line(f"License : {spdx}")
        txt.line(f"URL     : {url}")
        for line in provenance:
            txt.line(line)
        txt.line(SUB_SEP)
        txt.raw(text_file.read_text(encoding="utf-8", errors="replace"))
        txt.line("")
        rows.add("Native", name, version, spdx, url, lic.classify_flag(spdx))

    emit_native(
        "ONNX Runtime",
        "Microsoft Corporation",
        "MIT",
        "https://github.com/microsoft/onnxruntime",
        ONNXRUNTIME_FILE,
        provenance=(
            "Notice source: microsoft/onnxruntime v1.24.4 LICENSE",
            "Pinned sherpa-onnx Windows x64 shared recipe: ONNX Runtime 1.28.2.",
            "This source default is not a final native-payload version receipt.",
            "Verify actual build inputs and their dependency notices before release.",
        ),
    )
    emit_native(
        "sherpa-onnx",
        "Xiaomi Corporation / k2-fsa",
        "Apache-2.0",
        "https://github.com/k2-fsa/sherpa-onnx",
        APACHE_FILE,
        version="1.13.8",
        provenance=(
            "Upstream: 11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf",
            "Payload : official sherpa-onnx and sherpa-onnx-core Windows wheels",
            "Inventory: scripts/sherpa-source-wheels.json",
            "Native eSpeak NG and piper-phonemize terms are listed separately below.",
        ),
    )
    emit_native(
        "eSpeak NG", "Jonathan Duddington, Reece H. Dunn and contributors",
        "GPL-3.0-or-later", "https://github.com/csukuangfj/espeak-ng",
        LICENSE_TEXTS_DIR / "espeak-ng-gpl-3.0.txt",
        version="ed530aa113046142eb5115cf2fc9157854d0ffe1",
        provenance=("Source: https://github.com/csukuangfj/espeak-ng/tree/ed530aa113046142eb5115cf2fc9157854d0ffe1",),
    )
    emit_native(
        "piper-phonemize", "Michael Hansen", "MIT",
        "https://github.com/csukuangfj/piper-phonemize",
        LICENSE_TEXTS_DIR / "piper-phonemize.txt",
        version="f3ff95afc03640bc1399e113e83361192a2fafb4",
        provenance=("Source: https://github.com/csukuangfj/piper-phonemize/tree/f3ff95afc03640bc1399e113e83361192a2fafb4",),
    )
    emit_native(
        "uni-algo (piper-phonemize dependency)", "uni-algo contributors",
        "Unlicense OR MIT", "https://github.com/csukuangfj/piper-phonemize",
        LICENSE_TEXTS_DIR / "uni-algo.txt",
        provenance=("Notice: licenses/uni-algo/LICENSE.md in the pinned piper-phonemize source",),
    )
    for component, notice, spdx in (
        ("eSpeak NG getopt compatibility", "espeak-ng-bsd2.txt", "BSD-2-Clause"),
        ("eSpeak NG Unicode data", "espeak-ng-ucd.txt", "Unicode-DFS-2016"),
        ("eSpeak NG ucd-tools", "ucd-tools.txt", "GPL-3.0-or-later"),
    ):
        emit_native(
            component, "Copyright holders identified in the following notice",
            spdx, "https://github.com/csukuangfj/espeak-ng",
            LICENSE_TEXTS_DIR / notice,
            provenance=("Source revision: ed530aa113046142eb5115cf2fc9157854d0ffe1",),
        )
    emit_native(
        "PortAudio",
        "Ross Bencina, Phil Burk, and contributors",
        "MIT (PortAudio variant)",
        "https://github.com/PortAudio/portaudio",
        PORTAUDIO_FILE,
    )

    ok("Native library attributions written")


# ---------------------------------------------------------------------------
# SECTION 3: Frontend (npm) dependencies
# ---------------------------------------------------------------------------

def _locked_npm_packages() -> dict[str, dict]:
    """Refuse stale/incomplete production installs before reading license bodies."""
    frontend = ROOT / "frontend"
    try:
        lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
        manifest = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
        packages = lock["packages"]
        if not isinstance(packages, dict) or "" not in packages:
            raise ValueError("missing packages table")
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            if packages[""].get(field, {}) != manifest.get(field, {}):
                raise ValueError(f"package.json {field} differ from the lockfile")
        for path, entry in packages.items():
            if not path or entry.get("dev"):
                continue
            installed = frontend / path / "package.json"
            if not installed.is_file() and entry.get("optional"):
                continue  # npm omits optional packages for other OS/CPU targets.
            actual = json.loads(installed.read_text(encoding="utf-8"))
            if actual.get("version") != entry.get("version"):
                raise ValueError(f"{path}: installed {actual.get('version')}, locked {entry.get('version')}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _fail(f"Frontend installation does not match package-lock.json: {exc}",
              "Run 'npm --prefix frontend ci', then regenerate licenses.")
    return packages


def _validate_npm_inventory(npm_data: dict, packages: dict[str, dict]) -> None:
    """Reject extraneous or mismatched entries reported by license-checker."""
    frontend = (ROOT / "frontend").resolve()
    for key, value in npm_data.items():
        try:
            path = Path(value["path"]).resolve().relative_to(frontend).as_posix()
            locked = packages[path]
            _, version = lic.split_npm_key(key)
            if locked.get("version") != version:
                raise ValueError("version differs from lockfile")
        except (KeyError, TypeError, ValueError) as exc:
            _fail(f"license-checker entry {key} does not match package-lock.json: {exc}",
                  "Run 'npm --prefix frontend ci', then regenerate licenses.")


def _section3_npm(txt: TxtBuilder, rows: Rows) -> None:
    banner("Section 3: Frontend dependencies (license-checker)")
    packages = _locked_npm_packages()

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 3: Frontend (npm) Dependencies")
    txt.line(SEPARATOR)
    txt.line("")

    info("Running license-checker (production only) in frontend/...")
    lc = _run_capture(
        [
            "npx", "--yes", "license-checker",
            "--production",
            "--json",
            "--excludePrivatePackages",
        ],
        cwd=ROOT / "frontend",
    )
    if lc.returncode != 0:
        err("license-checker failed")
        if lc.stderr:
            print(lc.stderr, file=sys.stderr, end="")
        raise GeneratorError(1)
    try:
        npm_data = json.loads(lc.stdout) if lc.stdout.strip() else {}
    except json.JSONDecodeError:
        npm_data = {}
    if not isinstance(npm_data, dict):
        npm_data = {}
    npm_count = len(npm_data)
    ok(f"license-checker reported {npm_count} packages")

    # Defensive guard: license-checker returns {} if frontend/node_modules missing.
    if npm_count == 0:
        npm_declared = _count_npm_production_deps(ROOT / "frontend" / "package.json")
        if npm_declared > 0:
            _fail(
                f"license-checker returned 0 packages but frontend/package.json declares {npm_declared} production dependencies.",
                "This means frontend/node_modules is missing or incomplete.",
                "Fix: run 'cd frontend && npm ci' from the repo root, then re-run this script.",
            )
        warn("license-checker returned 0 packages and frontend/package.json declares no production deps; continuing.")

    npm_rows_written = 0
    # Keep output ordering independent of license-checker's traversal order.
    _validate_npm_inventory(npm_data, packages)
    for key, value in sorted(npm_data.items()):
        if not isinstance(value, dict):
            value = {}
        licenses = value.get("licenses") or ""
        # license-checker may report licenses as a list; jq's `// ""` on a
        # non-string still emits the value. The bash `jq -r '.value.licenses'`
        # would render a list as its JSON-ish form, but in practice
        # license-checker emits a string here. Coerce non-strings defensively.
        if isinstance(licenses, list):
            licenses = " ".join(str(x) for x in licenses)
        elif not isinstance(licenses, str):
            licenses = str(licenses)
        repository = value.get("repository") or ""
        license_file_path = value.get("licenseFile") or ""

        name, version = lic.split_npm_key(key)

        license_spdx = licenses
        flag = lic.classify_flag(license_spdx)
        rows.add("npm", name, version, license_spdx, repository, flag)
        npm_rows_written += 1

        txt.line(SUB_SEP)
        txt.line(f"Package : {name}")
        txt.line(f"Version : {version}")
        txt.line(f"License : {license_spdx}")
        txt.line(f"URL     : {repository}")
        txt.line(SUB_SEP)
        if license_file_path and Path(license_file_path).is_file():
            txt.raw(Path(license_file_path).read_text(encoding="utf-8", errors="replace"))
        else:
            txt.line("(no license file reported by license-checker)")
        txt.line("")

    # Row-count guard: npm rows must equal NPM_COUNT exactly.
    if npm_rows_written != npm_count:
        _fail(
            f"license-checker reported {npm_count} packages but {npm_rows_written} npm rows landed in the audit table.",
            "The npm section loop dropped entries (likely a jq/pipefail interaction).",
            "Re-run the script; if this keeps happening, inspect the license-checker output for malformed entries.",
        )
    ok(f"npm section: {npm_rows_written} rows landed in audit table")


def _count_npm_production_deps(package_json: Path) -> int:
    """Count keys in the `dependencies` object of frontend/package.json.

    Mirrors `jq '(.dependencies // {}) | length'` (generate-licenses.sh 464).
    """
    if not package_json.is_file():
        return 0
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0
    deps = data.get("dependencies") or {}
    if isinstance(deps, dict):
        return len(deps)
    return 0


# ---------------------------------------------------------------------------
# SECTION 4: Rust crates (Tauri desktop shell)
# ---------------------------------------------------------------------------

_ABOUT_TOML = """\
accepted = [
    "Apache-2.0",
    "Apache-2.0 WITH LLVM-exception",
    "MIT",
    "MIT-0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "Zlib",
    "Unicode-DFS-2016",
    "Unicode-3.0",
    "CC0-1.0",
    "CDLA-Permissive-2.0",
    "OpenSSL",
    "MPL-2.0",
]
ignore-build-dependencies = true
ignore-dev-dependencies = true
workarounds = ["ring"]
# This is a third-party inventory. The unpublished workspace app has its own
# owner-controlled license; do not omit any dependency's license check.
[private]
ignore = true
"""

# Use each distinct license text, preserving crate attribution and literal text.
# `overview` contains license categories, not the per-crate copyright variants.
_ABOUT_HBS = """\
{{#each licenses}}
------------------------------------------------------------
License: {{name}} ({{id}})
------------------------------------------------------------
Used by:
{{#each used_by}}
  - {{crate.name}} {{crate.version}}
{{/each}}

{{{text}}}

{{/each}}
"""


def _section4_rust(txt: TxtBuilder, rows: Rows, work: Path) -> str:
    """Emit Section 4; return the rust tool used (for the report Notes)."""
    banner("Section 4: Rust crates (cargo-about or cargo-license)")

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 4: Desktop Shell (Rust crates)")
    txt.line(SEPARATOR)
    txt.line("")

    rust_tool_used = ""
    cargo_license_json_text = ""

    manifest = ROOT / "src-tauri" / "Cargo.toml"

    # Probe cargo about --version.
    about_probe = _run_capture(["cargo", "about", "--version"])
    if about_probe.returncode == 0:
        info("cargo-about detected - generating full Rust license bundle")
        rust_tool_used = "cargo-about"

        about_toml = work / "about.toml"
        about_toml.write_text(_ABOUT_TOML, encoding="utf-8")
        about_hbs = work / "about.hbs"
        about_hbs.write_text(_ABOUT_HBS, encoding="utf-8")

        about_output = work / "rust-licenses.txt"
        about_gen = _run_capture(
            [
                "cargo", "about", "generate",
                "--manifest-path", str(manifest),
                "--config", str(about_toml),
                "--output-file", str(about_output),
                str(about_hbs),
            ]
        )
        if about_gen.returncode == 0:
            about_text = about_output.read_text(encoding="utf-8")
            ok(f"cargo-about generated {about_text.count(chr(10))} lines")
            txt.line("(license text generated via cargo-about)")
            txt.line("")
            txt.raw(about_text)
        else:
            warn("cargo-about failed; falling back to cargo-license")
            if about_gen.stderr:
                print(about_gen.stderr, file=sys.stderr, end="")
            rust_tool_used = ""

    if not rust_tool_used:
        info("Using cargo-license fallback (no license text bodies)")
        rust_tool_used = "cargo-license"
        cl = _run_capture(
            ["cargo", "license", "--manifest-path", str(manifest), "--json"]
        )
        if cl.returncode != 0:
            err("cargo-license also failed. Install one of:")
            err("  cargo install cargo-about   (preferred, full license texts)")
            err("  cargo install cargo-license (fallback, metadata only)")
            if cl.stderr:
                print(cl.stderr, file=sys.stderr, end="")
            raise GeneratorError(1)
        cargo_license_json_text = cl.stdout
        ok(f"cargo-license reported {_json_len(cargo_license_json_text)} crates")

        txt.line("(license text not extracted - install cargo-about for full text)")
        txt.line("")

    # Always parse cargo-license JSON (if available) for the audit-report rows.
    if not cargo_license_json_text.strip():
        info("Running cargo-license (for audit-report rows)...")
        cl2 = _run_capture(
            ["cargo", "license", "--manifest-path", str(manifest), "--json"]
        )
        if cl2.returncode == 0 and cl2.stdout.strip():
            cargo_license_json_text = cl2.stdout
        else:
            warn("cargo-license unavailable for row extraction; Rust rows will be limited")
            cargo_license_json_text = "[]"

    try:
        crates = json.loads(cargo_license_json_text) if cargo_license_json_text.strip() else []
    except json.JSONDecodeError:
        crates = []
    if not isinstance(crates, list):
        crates = []
    rust_expected = len(crates)

    rust_rows_written = 0
    for crate in crates:
        if not isinstance(crate, dict):
            crate = {}
        name = crate.get("name") or ""
        version = crate.get("version") or ""
        license_ = crate.get("license") or ""
        repository = crate.get("repository") or ""

        # Manual license elections (r-efi tri-license -> MIT).
        if name in lic.MANUAL_RUST_OVERRIDES:
            license_ = lic.MANUAL_RUST_OVERRIDES[name]

        flag = lic.classify_flag(license_)
        rows.add("Rust", name, version, license_, repository, flag)
        rust_rows_written += 1

        if rust_tool_used == "cargo-license":
            txt.line(SUB_SEP)
            txt.line(f"Crate   : {name}")
            txt.line(f"Version : {version}")
            txt.line(f"License : {license_}")
            txt.line(f"URL     : {repository}")
            txt.line(SUB_SEP)
            txt.line("")

    # Row-count guard.
    if rust_rows_written != rust_expected:
        _fail(
            f"cargo-license reported {rust_expected} crates but {rust_rows_written} Rust rows landed in the audit table.",
            "The Rust section loop dropped entries (likely a jq/pipefail interaction).",
            "Re-run the script; if this keeps happening, inspect the cargo-license output for malformed entries.",
        )
    ok(f"Rust section: {rust_rows_written} rows landed in audit table")

    info(f"Rust audit tool used: {rust_tool_used}")
    return rust_tool_used


def _json_len(text: str) -> int:
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return 0
    if isinstance(data, (list, dict)):
        return len(data)
    return 0


# ---------------------------------------------------------------------------
# SECTION 5: NVIDIA Parakeet TDT 0.6B v3 model license
# ---------------------------------------------------------------------------

def _fetch_url(url: str, *, timeout: int = 60) -> "bytes | None":
    """Fetch a URL via urllib (replaces curl -fsSL --max-time). Returns bytes or None.

    Sends an explicit curl-style User-Agent. The bash original used curl, whose
    default UA (curl/<ver>) is accepted by creativecommons.org. urllib's default
    UA ("Python-urllib/<ver>") is blocked there with HTTP 403, which would make
    this fall back to the staged cc-by-4.0.txt and diverge from both the bash
    output and the committed THIRD_PARTY_LICENSES.txt (whose CC-BY body is the
    live legalcode.txt). A curl-style UA restores curl's behavior so the live
    fetch succeeds. Any network/HTTP error still returns None and triggers the
    staged-file fallback, exactly like a curl failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001  (network/HTTP errors -> fall back, like curl failure)
        return None


def _section5_parakeet(txt: TxtBuilder, rows: Rows) -> None:
    banner("Section 5: NVIDIA Parakeet model license")

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 5: NVIDIA Parakeet TDT 0.6B v3 Model")
    txt.line(SEPARATOR)
    txt.line("")

    parakeet_license_id = ""

    info("Fetching HuggingFace model card metadata...")
    card_bytes = _fetch_url(PARAKEET_CARD_URL, timeout=60)
    if card_bytes is not None:
        try:
            card = json.loads(card_bytes.decode("utf-8", errors="replace"))
            parakeet_license_id = (card.get("cardData") or {}).get("license") or ""
        except (json.JSONDecodeError, AttributeError):
            parakeet_license_id = ""
        shown = parakeet_license_id if parakeet_license_id else "<unknown>"
        ok(f"HuggingFace reports license: {shown}")
    else:
        warn("Could not fetch HuggingFace model card metadata (network?). Assuming cc-by-4.0 from model card.")
        parakeet_license_id = "cc-by-4.0"

    if not parakeet_license_id:
        parakeet_license_id = "cc-by-4.0"

    parakeet_license_up = parakeet_license_id.upper()

    if parakeet_license_up == "CC-BY-4.0":
        parakeet_license_spdx = "CC-BY-4.0"
        parakeet_license_name = "Creative Commons Attribution 4.0 International (CC-BY-4.0)"

        info(f"Fetching CC-BY-4.0 legal code from {CCBY_URL}")
        ccby_bytes = _fetch_url(CCBY_URL, timeout=60)
        if ccby_bytes is not None and len(ccby_bytes) > 0:
            nvidia_license_text = ccby_bytes.decode("utf-8", errors="replace")
            ok(f"Fetched CC-BY-4.0 legal code ({len(nvidia_license_text.encode('utf-8'))} bytes)")
        else:
            warn("Could not fetch CC-BY-4.0 from creativecommons.org; using staged copy")
            nvidia_license_text = CCBY40_FILE.read_text(encoding="utf-8", errors="replace")
            ok(f"Using staged CC-BY-4.0 text ({len(nvidia_license_text.encode('utf-8'))} bytes)")
    else:
        # Unexpected license identifier - fail loudly rather than guess.
        _fail(
            f"Unexpected Parakeet TDT v3 license identifier: '{parakeet_license_id}'",
            "This script only knows how to bundle cc-by-4.0. Update the generator for the new license.",
        )
        return  # unreachable; keeps type-checkers happy

    txt.line("NVIDIA Parakeet TDT 0.6B v3 - Model License")
    txt.line("")
    txt.line("Source    : https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3")
    txt.line(f"License   : {parakeet_license_name}")
    txt.line(f"Identifier: {parakeet_license_spdx}")
    txt.line("")
    txt.line(f"Attribution: {PARAKEET_ATTRIBUTION}")
    txt.line("")
    txt.line("The Parakeet TDT 0.6B v3 model is released on HuggingFace under the")
    txt.line("Creative Commons Attribution 4.0 International license. The governing")
    txt.line("terms appear verbatim below.")
    txt.line("Whisperstream uses csukuangfj's sherpa-onnx INT8 ONNX conversion,")
    txt.line("not the original training-format weights. Conversion and INT8")
    txt.line("quantization are modifications of the NVIDIA source model.")
    txt.line("Export: https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
    txt.line("Revision: 2bda32ec70b097a55adaa07d9a7173915b43cc78")
    txt.line("")
    txt.line("NVIDIA Parakeet TDT 0.6B v2 - Additional Model Attribution")
    txt.line("Copyright holder: NVIDIA Corporation; original model by NVIDIA.")
    txt.line("Source: https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2")
    txt.line("License: CC-BY-4.0, the same full legal code reproduced below.")
    txt.line("Whisperstream also offers csukuangfj's sherpa-onnx INT8 ONNX")
    txt.line("conversion of v2. Conversion and INT8 quantization modify the")
    txt.line("NVIDIA source model; this attribution does not imply endorsement.")
    txt.line("Export: https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
    txt.line("Revision: 1ab9323565ddb038682214b292f588070a538ce2")
    txt.line("")
    txt.line(SUB_SEP)
    txt.raw(nvidia_license_text)
    txt.line("")
    txt.line(SUB_SEP)
    txt.line("")

    rows.add(
        "Model",
        "nvidia/parakeet-tdt-0.6b-v3",
        "v3",
        parakeet_license_spdx,
        "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
        "Custom",
    )

    rows.add(
        "Model", "nvidia/parakeet-tdt-0.6b-v2", "v2", "CC-BY-4.0",
        "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2", "Custom",
    )


# ---------------------------------------------------------------------------
# SECTION 6: Bundled Fonts
# ---------------------------------------------------------------------------

def _section6_fonts(txt: TxtBuilder, rows: Rows) -> None:
    banner("Section 6: Bundled fonts (Outfit, Inter)")

    ofl_file = LICENSE_TEXTS_DIR / "outfit-ofl.txt"
    inter_file = LICENSE_TEXTS_DIR / "inter-ofl.txt"

    for f in (ofl_file, inter_file):
        if not (f.is_file() and f.stat().st_size > 0):
            _fail(
                f"missing or empty font license text data file: {f}",
                "Task 1.1 should have staged this. Restore it before re-running.",
            )

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 6: Bundled Fonts")
    txt.line(SEPARATOR)
    txt.line("")
    txt.line("The Whisperstream desktop app bundles two font families locally under")
    txt.line("frontend/public/fonts/ and loads them via @font-face rules in")
    txt.line("frontend/src/styles/globals.css. No font-CDN requests are made at runtime.")
    txt.line("")

    # ---- Block A: Outfit (OFL 1.1) ----
    txt.line(SUB_SEP)
    txt.line("Family           : Outfit")
    txt.line("Weights          : 300, 400, 500, 600")
    txt.line("Source           : https://fonts.google.com/specimen/Outfit")
    txt.line("License          : SIL Open Font License 1.1")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The Outfit font files are bundled under frontend/public/fonts/ and")
    txt.line("redistributed under the SIL Open Font License 1.1. The governing terms")
    txt.line("appear verbatim below.")
    txt.line("")
    txt.line(SUB_SEP)
    txt.raw(ofl_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")
    txt.line(SUB_SEP)
    txt.line("")

    rows.add(
        "Font", "Outfit", "bundled", "OFL-1.1",
        "https://fonts.google.com/specimen/Outfit", "Custom",
    )

    # ---- Block B: Inter (SIL Open Font License) ----
    txt.line(SUB_SEP)
    txt.line("Family  : Inter")
    txt.line("Weights : 100-900 variable")
    txt.line("Source  : https://rsms.me/inter/")
    txt.line("License : SIL Open Font License 1.1")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The Inter font files are bundled under frontend/public/fonts/ and")
    txt.line("redistributed under the SIL Open Font License 1.1. The governing terms")
    txt.line("appear verbatim below.")
    txt.line("")
    txt.line(SUB_SEP)
    txt.raw(inter_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")
    txt.line(SUB_SEP)
    txt.line("")

    rows.add(
        "Font", "Inter", "bundled", "OFL-1.1",
        "https://rsms.me/inter/", "Custom",
    )

    ok("Bundled font attributions written")


# ---------------------------------------------------------------------------
# SECTION 7: Bundled binary + downloadable model attributions
# ---------------------------------------------------------------------------

def _section7_bundled(txt: TxtBuilder, rows: Rows) -> None:
    banner("Section 7: Bundled binary + downloadable model attributions")

    llama_cpp_mit_file = LICENSE_TEXTS_DIR / "llama-cpp-mit.txt"
    llvm_openmp_file = LICENSE_TEXTS_DIR / "llvm-openmp.txt"
    openrail_m_file = LICENSE_TEXTS_DIR / "openrail-m.txt"
    stream_mini_license_file = ROOT / "vendor" / "stream-1-mini" / "LICENSE.txt"
    stream_mini_notice_file = ROOT / "vendor" / "stream-1-mini" / "NOTICE.txt"
    kokoro_upstream_notice_file = ROOT / "vendor" / "kokoro" / "UPSTREAM-NOTICE.txt"
    kokoro_notice_file = ROOT / "vendor" / "kokoro" / "NOTICE.txt"
    nltk_perceptron_mit_file = (
        ROOT / "vendor" / "kokoro" / "NLTK-PERCEPTRON-MIT.txt"
    )
    taylorchu_mit_file = (
        ROOT / "vendor" / "kokoro" / "TAYLORCHU-KOKORO-ONNX-MIT.txt"
    )
    thewh1teagle_mit_file = (
        ROOT / "vendor" / "kokoro" / "THEWH1TEAGLE-KOKORO-ONNX-MIT.txt"
    )
    kokoro_training_attribution_file = (
        ROOT / "vendor" / "kokoro" / "TRAINING-DATA-ATTRIBUTION.txt"
    )

    for f in (
        llama_cpp_mit_file,
        llvm_openmp_file,
        openrail_m_file,
        stream_mini_license_file,
        stream_mini_notice_file,
        kokoro_notice_file,
        nltk_perceptron_mit_file,
        taylorchu_mit_file,
        thewh1teagle_mit_file,
        kokoro_training_attribution_file,
    ):
        if not (f.is_file() and f.stat().st_size > 0):
            _fail(
                f"missing or empty license text data file: {f}",
                "Task 3.3 should have staged this. Restore it before re-running.",
            )

    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 7: Bundled Binary and Downloadable Model Attributions")
    txt.line(SEPARATOR)
    txt.line("")
    txt.line("This section covers:")
    txt.line("  - llama-server.exe and its runtime DLLs (bundled in the installer)")
    txt.line("  - Qwen3-ASR-0.6B INT8 ONNX model (downloaded for speech recognition)")
    txt.line("  - Cohere Transcribe INT8 ONNX model (downloaded for speech recognition)")
    txt.line("  - Gemma 4 E2B Instruct GGUF model (downloaded on first use)")
    txt.line("  - Stream-1 Mini by Whisperstream GGUF model (downloaded for local cleanup)")
    txt.line("  - Supertonic 3 TTS voice model (downloaded only if the optional")
    txt.line("    voice feature is enabled)")
    txt.line("  - Kokoro 82M TTS model and its supporting English frontend data")
    txt.line("    (installed models usable in standard builds; new downloads experimental)")
    txt.line("")

    # ---- 7a: llama.cpp (MIT) ----
    txt.line(SUB_SEP)
    txt.line("Component : llama.cpp (llama-server.exe + runtime DLLs)")
    txt.line("Holder    : The ggml authors")
    txt.line("License   : MIT")
    txt.line(f"Release   : {LLAMA_CPP_RELEASE_TAG}")
    txt.line("URL       : https://github.com/ggml-org/llama.cpp")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The llama-server.exe binary and its companion DLLs (ggml.dll,")
    txt.line("llama.dll, llama-server-impl.dll, and ggml-cpu-*.dll) are shipped")
    txt.line("inside the Whisperstream installer. They are built from the llama.cpp")
    txt.line(f"project, pinned to release {LLAMA_CPP_RELEASE_TAG}. The governing MIT")
    txt.line("license terms appear verbatim below.")
    txt.line("")
    txt.raw(llama_cpp_mit_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    rows.add(
        "Binary", "llama.cpp", LLAMA_CPP_RELEASE_TAG, "MIT",
        "https://github.com/ggml-org/llama.cpp", "",
    )

    txt.line(SUB_SEP)
    txt.line("Component : LLVM OpenMP (libomp.dll)")
    txt.line("License   : Apache-2.0 WITH LLVM-exception")
    txt.line("Version   : 20.1.8")
    txt.line("URL       : https://github.com/llvm/llvm-project/tree/llvmorg-20.1.8/openmp")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The companion OpenMP runtime is separately licensed from llama.cpp.")
    txt.line("Its complete upstream license, including third-party notices, follows.")
    txt.line("")
    txt.raw(llvm_openmp_file.read_text(encoding="utf-8"))
    txt.line("")
    rows.add(
        "Binary", "LLVM OpenMP", "20.1.8", "Apache-2.0 WITH LLVM-exception",
        "https://github.com/llvm/llvm-project/tree/llvmorg-20.1.8/openmp", "",
    )

    # ---- 7b: Qwen3-ASR-0.6B INT8 ONNX (Apache-2.0) ----
    txt.line(SUB_SEP)
    txt.line("Component : Qwen3-ASR-0.6B (INT8 ONNX, user-downloaded)")
    txt.line("Upstream holder : Copyright (c) Alibaba Cloud")
    txt.line("Upstream license: Apache-2.0")
    txt.line("Export license  : Apache-2.0")
    txt.line("Upstream  : https://huggingface.co/Qwen/Qwen3-ASR-0.6B")
    txt.line("Export    : https://huggingface.co/csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25")
    txt.line("Artifact  : https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx")
    txt.line("Converter : https://github.com/Wasser1462/Qwen3-ASR-onnx")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("Qwen3-ASR-0.6B is provided by Alibaba Cloud under the Apache")
    txt.line("License 2.0. The INT8 ONNX model is downloaded on demand to")
    txt.line("Whisperstream's model directory and is not bundled in the installer.")
    txt.line("The Apache License 2.0 appears verbatim below.")
    txt.line("Modification notice: the export converts the upstream model to")
    txt.line("INT8 ONNX files arranged for sherpa-onnx inference.")
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    rows.add(
        "Model", "Qwen3-ASR-0.6B", "INT8 ONNX",
        "Apache-2.0",
        "https://huggingface.co/Qwen/Qwen3-ASR-0.6B", "",
    )

    # ---- 7c: Cohere Transcribe INT8 ONNX (Apache-2.0; owner-reviewed chain) ----
    txt.line(SUB_SEP)
    txt.line("Component : Cohere Transcribe 03-2026 (INT8 ONNX, user-downloaded)")
    txt.line("Upstream holder : Cohere Labs")
    txt.line("Upstream license: Apache-2.0")
    txt.line("Converter/publisher: Fangjun Kuang and the k2-fsa sherpa-onnx project")
    txt.line("Risk decision   : owner-accepted intermediary provenance uncertainty")
    txt.line("Upstream  : https://huggingface.co/CohereLabs/cohere-transcribe-03-2026")
    txt.line("Revision  : b1eacc2686a3d08ceaae5f24a88b1d519620bc09")
    txt.line("Payload commit: https://modelscope.cn/models/csukuangfj/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01/summary?Revision=567cdf8d435d0902cbd15ea1e761652e6ccce64f")
    txt.line("Export method reference: https://huggingface.co/tristanripke/cohere-transcribe-onnx-int8/tree/9ecc3a5e64b132ab094bada232650e49e4340ad2")
    txt.line("Export    : https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01.tar.bz2")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The Cohere Transcribe INT8 ONNX speech-recognition export is intended")
    txt.line("for on-demand download to Whisperstream's model directory and is not")
    txt.line("bundled in the installer. The official upstream model is distributed")
    txt.line("by Cohere Labs under the Apache License 2.0. Fangjun Kuang published")
    txt.line("the compatible four-file payload through a merged k2-fsa release")
    txt.line("workflow and an immutable ModelScope Git commit. A separate")
    txt.line("Apache-2.0 repository preserves the mel-contract exporter, decoder")
    txt.line("exporter, tokenizer generator, and INT8 quantizer used for this model")
    txt.line("family. The compatible payload repository itself does not contain a")
    txt.line("standalone license or a complete build environment. Lanreal's owner")
    txt.line("accepted that narrow intermediary provenance uncertainty for this")
    txt.line("exact checksum-pinned payload; this notice does not describe the")
    txt.line("artifact as independently attorney-cleared. Modification notice: this")
    txt.line("payload is published as an INT8 ONNX conversion and dynamic")
    txt.line("quantization of the upstream model, arranged for sherpa-onnx")
    txt.line("inference; Lanreal has not independently reproduced the exact")
    txt.line("converted bytes. Test WAV files are excluded.")
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    rows.add(
        "Model", "Cohere Transcribe 03-2026", "INT8 ONNX",
        "Apache-2.0 (owner-accepted intermediary provenance risk)",
        "https://huggingface.co/CohereLabs/cohere-transcribe-03-2026", "",
    )

    # ---- 7d: Gemma 4 E2B Instruct (Apache-2.0) ----
    #
    # Gemma 4 is Apache-2.0, NOT the custom Gemma Terms of Use that governed
    # Gemma 1/2/3. The Terms page (ai.google.dev/gemma/terms) scopes itself to
    # "Gemma models listed in the Appendix" and says "For Gemma 4 terms, see
    # the Gemma 4 license", which resolves to ai.google.dev/gemma/apache_2.
    # Verified against three live primary sources.
    # Final-edition GGUF files download directly from pinned HF sources.
    # Include the license/attribution for both supported quantizations.
    # The reviewed Google QAT tree has no separate LICENSE or NOTICE file;
    # its pinned model card links the official Apache-2.0 license.
    txt.line(SUB_SEP)
    txt.line("Component : Gemma 4 E2B Instruct (GGUF, user-downloaded)")
    txt.line("Holder    : Google LLC")
    txt.line("License   : Apache-2.0")
    txt.line("URL       : https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF")
    txt.line("Terms     : https://ai.google.dev/gemma/docs/gemma_4_license")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The Gemma 4 E2B Instruct GGUF model is downloaded on demand to")
    txt.line("%LOCALAPPDATA%\\Whisperstream\\local-llm\\ and is not bundled in the")
    txt.line("installer. It is downloaded directly from its pinned Hugging Face source")
    txt.line("with Q4_K_M requantization by Unsloth AI from Google")
    txt.line("LLC's Gemma 4 E2B Instruct release. Gemma 4 is distributed under the")
    txt.line("Apache License 2.0. The governing license terms appear verbatim")
    txt.line("below.")
    txt.line("")
    txt.line("Customizable automatically falls back to this verified artifact")
    txt.line("if its primary source is unavailable: Google's official Gemma 4")
    txt.line("E2B Instruct QAT Q4_0 GGUF, unchanged. Its weights differ from")
    txt.line("Unsloth Q4_K_M; both are presented as one Customizable model.")
    txt.line("Source    : https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf")
    txt.line("Revision  : 675cff42a74c774d6cb76f76d8eacb49b48c9b93")
    txt.line("Artifact  : gemma-4-E2B_q4_0-it.gguf (3349516256 bytes)")
    txt.line("SHA-256   : fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634")
    txt.line("Google DeepMind publishes the QAT conversion; Lanreal does not")
    txt.line("modify, requantize or host it. No vision projector is downloaded.")
    txt.line("This source attribution does not imply Google endorsement of")
    txt.line("Whisperstream. Both Gemma editions use the Apache License 2.0 below.")
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    rows.add(
        "Model", "gemma-4-E2B-it-google-qat", "GGUF Q4_0", "Apache-2.0",
        "https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf", "",
    )
    rows.add(
        "Model", "gemma-4-E2B-it", "GGUF", "Apache-2.0",
        "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF", "",
    )

    # First-party fine-tune of Qwen/Qwen3-0.6B-Base. Custom terms (Apache-2.0
    # plus an identification condition), not plain Apache-2.0. The pinned
    # LICENSE and NOTICE are vendored verbatim from the published revision;
    # vendor/stream-1-mini/provenance.json records the shipped artifact.
    txt.line(SUB_SEP)
    txt.line("Component : Stream-1 Mini by Whisperstream (GGUF, user-downloaded)")
    txt.line("Holder    : Copyright 2026 Lanreal Technologies Inc.")
    txt.line("Base      : Qwen/Qwen3-0.6B-Base, Copyright 2024 Alibaba Cloud")
    txt.line("License   : Stream-1 Mini License, Version 1.0 (stream-1-mini-license),")
    txt.line("            includes the Apache License 2.0 for Qwen/Qwen3-0.6B-Base")
    txt.line("Source    : https://huggingface.co/whisperstream/stream-1-mini")
    txt.line("Revision  : ee30dd90bd346d781d0e420d3c6c23244798e7b9")
    txt.line("Artifact  : stream-1-mini-vocabulary-q4_k_m.gguf (484215648 bytes)")
    txt.line("SHA-256   : 32bef61bdbc98787ac278d96a2aad120a9fc61f588f1f0a4746441e9d6d89ae7")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("Stream-1 Mini is Whisperstream's dictation cleanup model, fine-tuned")
    txt.line("from Qwen/Qwen3-0.6B-Base and downloaded from the pinned Hugging Face")
    txt.line("repository for on-device text cleanup. It is offered under the")
    txt.line("Stream-1 Mini License, Version 1.0, which combines the Apache License")
    txt.line("2.0 with an identification condition. The upstream Qwen work remains")
    txt.line("under the Apache License 2.0. The published LICENSE, NOTICE and the")
    txt.line("Apache License 2.0 text follow verbatim.")
    txt.line("")
    txt.raw(stream_mini_license_file.read_text(encoding="utf-8"))
    txt.line("")
    txt.raw(stream_mini_notice_file.read_text(encoding="utf-8"))
    txt.line("")
    txt.line("LICENSE-APACHE-2.0:")
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    rows.add(
        "Model", "Stream-1 Mini by Whisperstream", "Q4_K_M GGUF",
        "LicenseRef-Stream-1-Mini-License-1.0 (Stream-1 Mini License, Version 1.0; includes Apache-2.0 for Qwen/Qwen3-0.6B-Base)",
        "https://huggingface.co/whisperstream/stream-1-mini", "Custom",
    )

    # ---- 7f: Supertonic 3 TTS voice model (OpenRAIL-M) ----
    #
    # Use-restricted but permissive: not copyleft, commercial use permitted.
    # OpenRAIL-M section 4.a requires the Attachment A behavioural use
    # restrictions to flow down to end users as an enforceable term, which
    # EULA Section 27 does, and requires every recipient of the model to be
    # given a copy of the license, which this verbatim block does. The
    # upstream bundle's own LICENSE file is MIT (Supertone's sample CODE) and
    # must NOT be passed through as the model's license.
    txt.line(SUB_SEP)
    txt.line("Component : Supertonic 3 TTS (voice weights, user-downloaded)")
    txt.line("Holder    : Copyright (c) 2026 Supertone Inc.")
    txt.line("License   : OpenRAIL-M (BigScience Open RAIL-M License, 2022-08-18)")
    txt.line("URL       : https://huggingface.co/Supertone/supertonic-3")
    txt.line("Terms     : https://huggingface.co/Supertone/supertonic-3/blob/main/LICENSE")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The Supertonic 3 text-to-speech model (text_encoder /")
    txt.line("duration_predictor / vector_estimator / vocoder ONNX weights,")
    txt.line("voice.bin, unicode_indexer.bin, tts.json) is downloaded on demand")
    txt.line("for the optional voice feature and is not bundled in the installer.")
    txt.line("The weights are distributed by Supertone Inc. under the BigScience")
    txt.line("Open RAIL-M License. This is a use-restricted (but permissive,")
    txt.line("non-copyleft, commercial-use-permitting) license: use of the model")
    txt.line("is subject to the behavioural use restrictions in its Attachment A,")
    txt.line("reproduced in full below and flowed down to you as an enforceable")
    txt.line("term by Section 27 of the Whisperstream End User License Agreement.")
    txt.line("Supertone's sample code is MIT-licensed and is NOT redistributed")
    txt.line("with Whisperstream. The governing OpenRAIL-M license terms,")
    txt.line("including Attachment A, appear verbatim below.")
    txt.line("")
    txt.raw(openrail_m_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    rows.add(
        "Model", "supertonic-3", "int8", "OpenRAIL-M",
        "https://huggingface.co/Supertone/supertonic-3", "Custom",
    )

    # Current upstream staging path, followed by retained legacy notices.
    txt.line(SUB_SEP)
    txt.line("Component : Upstream Kokoro v1.0 English runtime (user-downloaded)")
    txt.line("License   : Apache-2.0 model/lexicon, GPL-3.0-or-later eSpeak data")
    txt.line(SUB_SEP)
    txt.raw(kokoro_upstream_notice_file.read_text(encoding="utf-8"))
    txt.line("")
    rows.add("Model", "sherpa Kokoro v1.0", "f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b",
             "Apache-2.0", "https://huggingface.co/csukuangfj/kokoro-multi-lang-v1_0", "")
    rows.add("Model Data", "Kokoro eSpeak English data", "f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b",
             "GPL-3.0-or-later", "https://github.com/espeak-ng/espeak-ng", "GPL")

    # ---- 7g: Kokoro 82M optional voice archive ----
    #
    # The archive combines separately pinned artifacts. Apache-2.0 governs the
    # model/voices, Misaki lexicons, and PeterReid G2P model. Exact MIT notices
    # are retained for the NLTK-derived tagger implementation/data and the
    # conversion/release lineage. Koniwa and SIWIS are not distributed as
    # datasets or selected voice presets, but their upstream training-source
    # attribution is retained conservatively for the shared Kokoro weights.
    # No upstream Apache NOTICE file was found in the reviewed snapshots.
    txt.line(SUB_SEP)
    txt.line("Component : Legacy Kokoro 82M archive (existing caches only)")
    txt.line("License   : Apache-2.0, MIT notices, conservative CC attribution")
    txt.line("Bundle    : kokoro-82m-fp32-en-us-2026-07-28")
    txt.line("Source    : https://huggingface.co/hexgrad/Kokoro-82M")
    txt.line(SUB_SEP)
    txt.line("")
    txt.line("The optional archive contains the Kokoro 82M FP32 ONNX model, nine")
    txt.line("American English preset embeddings, two pinned Misaki English")
    txt.line("lexicons, a pinned PeterReid neural G2P model exported to ONNX, and")
    txt.line("three pinned NLTK English perceptron JSON data files. Whisperstream")
    txt.line("uses its own modified, inference-only frontend and the ONNX Runtime")
    txt.line("already distributed with the application.")
    txt.line("")
    txt.line("Whisperstream does not distribute the Misaki, NLTK, kokoro-onnx,")
    txt.line("Torch, Transformers, spaCy, eSpeak, phonemizer, Piper, or num2words")
    txt.line("runtime packages in this archive. Exact source pins, modifications,")
    txt.line("and the absence of upstream Apache NOTICE files are recorded below.")
    txt.line("The nine presets are: af_heart, af_bella, af_nicole, af_aoede,")
    txt.line("af_kore, af_sarah, am_fenrir, am_michael, and am_puck.")
    txt.line("Kokoro is Apache-2.0; Supertonic's OpenRAIL-M terms do not govern")
    txt.line("this Kokoro component.")
    txt.line("")
    txt.line("The following historical Kokoro notice describes that archive and")
    txt.line("its frontend, not the complete application environment. The official")
    txt.line("sherpa-onnx wheels separately contain eSpeak NG and piper-phonemize")
    txt.line("native code, attributed in Section 2.")
    txt.line("")
    txt.raw(kokoro_notice_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("Kokoro 82M model and nine curated preset embeddings")
    txt.line("License: Apache License 2.0")
    txt.line("Pinned source revision: f3ff3571791e39611d31c381e3a41a3af07b4987")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("Misaki US English lexicons and modified frontend behavior")
    txt.line("License: Apache License 2.0")
    txt.line("Pinned source revision: fba1236595f2d2bf21d414ba6e57d25256afada3")
    txt.line("Modification: only US lexicons are copied; Whisperstream replaces")
    txt.line("the package dependency closure with a narrow attributed frontend.")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("PeterReid English neural G2P model, exported ONNX graph")
    txt.line("License: Apache License 2.0")
    txt.line("Pinned source revision: a5631b285d18d59483c32c0c3379cb9fac924f4b")
    txt.line("Modification: exported as an opset-17 no-cache ONNX forward graph.")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(APACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("NLTK averaged perceptron implementation and English model data")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(
        nltk_perceptron_mit_file.read_text(
            encoding="utf-8", errors="replace"
        )
    )
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("kokoro-onnx conversion and release lineage")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(taylorchu_mit_file.read_text(encoding="utf-8", errors="replace"))
    txt.line("")
    txt.raw(
        thewh1teagle_mit_file.read_text(
            encoding="utf-8", errors="replace"
        )
    )
    txt.line("")

    txt.line(SUB_SEP)
    txt.line("Conservative Kokoro training-data attribution")
    txt.line(SUB_SEP)
    txt.line("")
    txt.raw(
        kokoro_training_attribution_file.read_text(
            encoding="utf-8", errors="replace"
        )
    )
    txt.line("")

    rows.add(
        "Model", "Kokoro-82M", "fp32-onnx+9-presets", "Apache-2.0",
        "https://huggingface.co/hexgrad/Kokoro-82M", "",
    )
    rows.add(
        "Model Data", "Misaki English lexicons",
        "fba1236595f2d2bf21d414ba6e57d25256afada3", "Apache-2.0",
        "https://github.com/hexgrad/misaki", "",
    )
    rows.add(
        "Model", "PeterReid/graphemes_to_phonemes_en_us",
        "a5631b285d18d59483c32c0c3379cb9fac924f4b", "Apache-2.0",
        "https://huggingface.co/PeterReid/graphemes_to_phonemes_en_us", "",
    )
    rows.add(
        "Model Data", "NLTK averaged_perceptron_tagger_eng",
        "550b6625bcef1f2abff2ff770a5a0d272c9c6b2a", "MIT",
        "https://github.com/nltk/nltk_data", "",
    )
    rows.add(
        "Tooling", "taylorchu/kokoro-onnx", "2025", "MIT",
        "https://github.com/taylorchu/kokoro-onnx", "",
    )
    rows.add(
        "Tooling", "thewh1teagle/kokoro-onnx", "model-files-v1.0", "MIT",
        "https://github.com/thewh1teagle/kokoro-onnx", "",
    )
    rows.add(
        "Training Data Attribution", "Koniwa tnc", "upstream training source",
        "CC-BY-3.0", "https://github.com/koniwa/koniwa", "Custom",
    )
    rows.add(
        "Training Data Attribution", "SIWIS", "2016", "CC-BY-4.0",
        "https://doi.org/10.7488/ds/1705", "Custom",
    )

    ok("Bundled binary and downloadable model attributions written")


# ---------------------------------------------------------------------------
# Locale-aware row sort (replicates `sort -t $'\t' -k1,1 -k2,2`).
# The bash original ran under LC_COLLATE=en_US.UTF-8 (Git Bash default), so its
# All-Dependencies table is collated case-insensitively-primary with case as a
# tiebreak (glibc en_US ordering): "Pygments" sorts near "pyg", "PyYAML" near
# "pyy", not ahead of every lowercase name as pure codepoint sorting would put
# them. To byte-reproduce the committed audit-report.md the port must use the
# same collation, so we sort via locale.strxfrm under en_US.UTF-8. If that
# locale is unavailable on the host we fall back to a casefold-primary key
# (close to glibc en_US for ASCII), and as a last resort to codepoint order.
#
# Tiebreak: GNU sort was invoked WITHOUT -s, so when the -k1,1 -k2,2 keys are
# equal (duplicate ecosystem+package, e.g. two `phf_codegen` or `wasi` crate
# versions) it falls back to comparing the ENTIRE line. We reproduce that with a
# third key component: the strxfrm of the full tab-joined RAW row. Without it,
# Python's stable sort would keep cargo-license insertion order for duplicates,
# which differs from the bash version-field collation.
#
# Keys are computed on the RAW (still-sentineled) field values, matching the
# bash sort over the raw TSV before unsentinel.
# ---------------------------------------------------------------------------

def _sort_rows_like_bash(
    all_rows: "list[tuple[str, str, str, str, str, str]]",
) -> "list[tuple[str, str, str, str, str, str]]":
    saved = locale.setlocale(locale.LC_COLLATE)
    using_strxfrm = False
    for loc in ("en_US.UTF-8", "en_US.utf8"):
        try:
            locale.setlocale(locale.LC_COLLATE, loc)
            using_strxfrm = True
            break
        except locale.Error:
            continue
    try:
        if using_strxfrm:
            # sort -k1,1 -k2,2 (no -s): key is field 1, then field 2, then the
            # whole tab-joined line as the GNU-sort last-resort tiebreak.
            key = lambda r: (
                locale.strxfrm(r[0]),
                locale.strxfrm(r[1]),
                locale.strxfrm("\t".join(r)),
            )
        else:
            # Fallback: casefold primary, original as tiebreak (ASCII-close to
            # glibc en_US). Logged so a host without the locale is diagnosable.
            warn(
                "locale en_US.UTF-8 unavailable for collation; "
                "falling back to casefold sort (may differ from bash on non-ASCII names)"
            )
            key = lambda r: (
                (r[0].casefold(), r[0]),
                (r[1].casefold(), r[1]),
                ("\t".join(r).casefold(), "\t".join(r)),
            )
        return sorted(all_rows, key=key)
    finally:
        with contextlib.suppress(locale.Error):
            locale.setlocale(locale.LC_COLLATE, saved)


# ---------------------------------------------------------------------------
# Audit report (audit-report.md)
# ---------------------------------------------------------------------------

def _build_report(
    rows: Rows,
    timestamp: str,
    rust_tool_used: str,
    compilation_note: str,
    uv_tree_text: str,
) -> str:
    """Build the full audit-report.md text (mirrors lines 1035-1142)."""
    py_rows = rows.count_eco("Python")
    native_rows = rows.count_eco("Native")
    npm_rows = rows.count_eco("npm")
    rust_rows = rows.count_eco("Rust")
    model_rows = rows.count_eco("Model")
    font_rows = rows.count_eco("Font")
    total_rows = rows.total()
    flagged_rows = rows.flagged()

    out: list[str] = []

    def w(s: str = "") -> None:
        out.append(s + "\n")

    w("# Whisperstream Third-Party License Audit")
    w("")
    w(f"_Generated: {timestamp}_")
    w("")
    w("Auto-generated by `scripts/generate_licenses.py`. Do not edit by hand.")
    w("")
    w("## Summary")
    w("")
    w("| Ecosystem | Count |")
    w("|-----------|------:|")
    w(f"| Python    | {py_rows} |")
    w(f"| Native    | {native_rows} |")
    w(f"| npm       | {npm_rows} |")
    w(f"| Rust      | {rust_rows} |")
    w(f"| Model     | {model_rows} |")
    w(f"| Font      | {font_rows} |")
    w(f"| **Total** | **{total_rows}** |")
    w("")
    w(f"Flagged entries: **{flagged_rows}**")
    w("")
    w(f"Rust audit tool: `{rust_tool_used}`")
    w("")
    w("## Flagged Dependencies")
    w("")

    all_rows = rows.all_rows()

    if flagged_rows == 0:
        w("_None._")
        w("")
    else:
        w("| Ecosystem | Package | Version | License | Flag | Obligation |")
        w("|-----------|---------|---------|---------|------|------------|")
        # awk '$6!="-" && $6!=""' preserves insertion order (no sort).
        for (eco, pkg, ver, lic_, src, flag) in all_rows:
            if not (flag != "-" and flag != ""):
                continue
            eco_d = lic.unsentinel(eco)
            pkg_d = lic.unsentinel(pkg)
            ver_d = lic.unsentinel(ver)
            lic_d = lic.unsentinel(lic_)
            flag_d = lic.unsentinel(flag)
            obl = lic.obligation_for_flag(flag_d)
            if not lic_d:
                lic_d = "UNKNOWN"
            w(
                f"| {lic.md_escape(eco_d)} | {lic.md_escape(pkg_d)} | "
                f"{lic.md_escape(ver_d)} | {lic.md_escape(lic_d)} | "
                f"{lic.md_escape(flag_d)} | {lic.md_escape(obl)} |"
            )
        w("")

    w("## All Dependencies")
    w("")
    w("| Ecosystem | Package | Version | License (SPDX) | Source | Flags |")
    w("|-----------|---------|---------|----------------|--------|-------|")

    # sort -t $'\t' -k1,1 -k2,2 under LC_COLLATE=en_US.UTF-8 (see helper above).
    sorted_rows = _sort_rows_like_bash(all_rows)
    for (eco, pkg, ver, lic_, src, flag) in sorted_rows:
        eco_d = lic.unsentinel(eco)
        pkg_d = lic.unsentinel(pkg)
        ver_d = lic.unsentinel(ver)
        lic_d = lic.unsentinel(lic_)
        src_d = lic.unsentinel(src)
        flag_d = lic.unsentinel(flag)
        if not lic_d:
            lic_d = "UNKNOWN"
        w(
            f"| {lic.md_escape(eco_d)} | {lic.md_escape(pkg_d)} | "
            f"{lic.md_escape(ver_d)} | {lic.md_escape(lic_d)} | "
            f"{lic.md_escape(src_d)} | {lic.md_escape(flag_d)} |"
        )

    w("")
    w("## Notes")
    w("")
    w("- Python closure: locked `uv tree --no-dev --python-version` snapshot using `.python-version`.")
    w(f"- {compilation_note}")
    w("- Frontend closure: `license-checker --production --excludePrivatePackages` (devDeps excluded).")
    w(f"- Rust closure: extracted via `{rust_tool_used}` against `src-tauri/Cargo.toml`.")
    w("- Native libs (ONNX Runtime, sherpa-onnx, PortAudio) are manually attributed in SECTION 2 of `THIRD_PARTY_LICENSES.txt`.")
    w("- NVIDIA Parakeet TDT 0.6B v3 model license is fetched from HuggingFace at audit time.")
    w(f"- Bundled binaries (llama.cpp/llama-server.exe, MIT, release {LLAMA_CPP_RELEASE_TAG}; LLVM OpenMP 20.1.8, Apache-2.0 WITH LLVM-exception) and downloadable models (Qwen3 Apache-2.0, Cohere Transcribe Apache-2.0, Gemma 4 E2B Apache-2.0, Stream-1 Mini by Whisperstream under the Stream-1 Mini License 1.0, Supertonic 3 OpenRAIL-M, Kokoro 82M and supporting data) are manually attributed in SECTION 7.")
    w("")
    w("## Python runtime closure (uv tree --no-dev)")
    w("")
    w("```")
    # cat "$UV_TREE_FILE": emit verbatim. The captured stdout already ends in a
    # newline, so append it raw rather than via w() (which would add a 2nd \n).
    out.append(uv_tree_text)
    w("```")

    return "".join(out)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _section8_silero(txt: TxtBuilder, rows: Rows) -> None:
    """Bundled speech detector and adapted ONNX inference contract."""
    license_file = ROOT / "src/whisperstream/assets/silero-vad-LICENSE.txt"
    if not license_file.is_file() or not license_file.stat().st_size:
        _fail("Missing Silero VAD license", "Restore the pinned upstream notice.")
    txt.line("")
    txt.line(SEPARATOR)
    txt.line("SECTION 8: Silero VAD Speech Detector")
    txt.line(SEPARATOR)
    txt.line("Component : Silero VAD v6.2.3 (16 kHz ONNX, bundled)")
    txt.line("Holder    : Copyright (c) 2020-present Silero Team")
    txt.line("License   : MIT")
    txt.line("Source    : https://github.com/snakers4/silero-vad")
    txt.line("Revision  : 5cd7945676eb32225748052e2e6a0580e4686a08")
    txt.line("SHA256    : 7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49")
    txt.line("")
    txt.line("The unmodified silero_vad_16k_op15.onnx model is bundled as")
    txt.line("silero_vad.onnx. Its inference contract is adapted in speech_gate.py")
    txt.line("to use NumPy and the existing ONNX Runtime without PyTorch.")
    txt.line("Detection runs locally with no model download or telemetry.")
    txt.line("")
    txt.raw(license_file.read_text(encoding="utf-8"))
    txt.line("")
    rows.add("Model", "Silero VAD", "6.2.3", "MIT", "https://github.com/snakers4/silero-vad", "")


def _write_outputs(out_text: str, report: str, timestamp: str) -> None:
    """Keep the previous timestamp when the generated content is unchanged."""
    outputs = {OUT_TXT: out_text, OUT_STAGED: out_text, OUT_REPORT: report}
    if all(path.is_file() for path in outputs):
        previous = {path: path.read_text(encoding="utf-8") for path in outputs}
        match = re.search(r"^Generated: (.+)$", previous[OUT_TXT], re.MULTILINE)
        if match:
            unchanged = {
                path: content.replace(timestamp, match.group(1), 1)
                for path, content in outputs.items()
            }
            if unchanged == previous:
                ok("License outputs unchanged")
                return
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")


def generate_licenses() -> int:
    """Generate the three license outputs. Returns 0 on success.

    Importable entrypoint used by build.py Step 5. Raises GeneratorError
    (a SystemExit subclass) to abort with a non-zero code, mirroring the bash
    `exit 1` semantics.
    """
    # ---- Tool prerequisites (uv, npx, cargo). jq/curl no longer needed. ----
    _require_tool("uv", "install uv: https://docs.astral.sh/uv/getting-started/installation/")
    _require_tool("npx", "install Node.js 22.13+ (npm ships with it)")
    _require_tool("cargo", "install Rust toolchain: https://rustup.rs")
    _locked_npm_packages()

    # ---- Data files for native libs (must exist + be non-empty). ----
    for f in (APACHE_FILE, MIT_FILE, PORTAUDIO_FILE, CCBY40_FILE):
        if not (f.is_file() and f.stat().st_size > 0):
            _fail(
                f"missing or empty license text data file: {f}",
                "Task 1a should have staged this. Restore it before re-running.",
            )

    # ---- Output dirs. ----
    OUT_STAGED.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = Rows()
    txt = TxtBuilder()

    # ---- Main output header. ----
    txt.line("Whisperstream Third-Party Licenses")
    txt.line(f"Generated: {timestamp}")
    txt.line("")
    txt.line("This file is auto-generated by scripts/generate_licenses.py. Do not edit by hand.")
    txt.line("Re-run the generator after dependency changes.")
    txt.line("")

    # TemporaryDirectory replaces mktemp + trap cleanup.
    with tempfile.TemporaryDirectory(prefix="whisperstream-licenses-") as work_str:
        work = Path(work_str)

        uv_tree_text, compilation_note = _section1_python(txt, rows, work)
        _section2_native(txt, rows)
        _section3_npm(txt, rows)
        rust_tool_used = _section4_rust(txt, rows, work)
        _section5_parakeet(txt, rows)
        _section6_fonts(txt, rows)
        _section7_bundled(txt, rows)
        _section8_silero(txt, rows)

        # ---- Stage the bundle copy and build the audit report. ----
        banner("Merging outputs")

        out_text = txt.text()
        report = _build_report(rows, timestamp, rust_tool_used, compilation_note, uv_tree_text)
        _write_outputs(out_text, report, timestamp)
        ok(f"Staged {OUT_STAGED}")
        ok(f"Wrote {OUT_REPORT}")

    # ---- Final summary (mirrors the bash banner + ecosystem counts). ----
    banner("Done")

    py_rows = rows.count_eco("Python")
    native_rows = rows.count_eco("Native")
    npm_rows = rows.count_eco("npm")
    rust_rows = rows.count_eco("Rust")
    model_rows = rows.count_eco("Model")
    font_rows = rows.count_eco("Font")
    total_rows = rows.total()
    flagged_rows = rows.flagged()

    print("")
    print("Ecosystem counts:")
    print(f"  Python : {py_rows}")
    print(f"  Native : {native_rows}")
    print(f"  npm    : {npm_rows}")
    print(f"  Rust   : {rust_rows}")
    print(f"  Model  : {model_rows}")
    print(f"  Font   : {font_rows}")
    print(f"  Total  : {total_rows}")
    print("")
    print(f"Flagged : {flagged_rows}")
    print("")
    print("Outputs:")
    print(f"  {OUT_TXT}")
    print(f"  {OUT_STAGED}")
    print(f"  {OUT_REPORT}")

    return 0


def main(argv: "list[str] | None" = None) -> int:
    try:
        return generate_licenses()
    except GeneratorError as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return code


if __name__ == "__main__":
    sys.exit(main())
