"""Select, run and verify merge checks remotely or locally. Requires git and gh."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import quote, urlsplit

LANES = ("convention", "python", "frontend", "docs")
DOC_FILES = {"README.md", "CONTRIBUTING.md", "PREPARATION.md"}


def select(paths: list[str], full: bool = False) -> dict[str, list[str]]:
    """Conservative surface selection; unknown and consumed inputs fail broad."""
    reasons: dict[str, list[str]] = {"convention": ["Required for every PR"]}
    for path in paths:
        suffix = PurePosixPath(path).suffix.lower()
        if path in DOC_FILES or (
            path.startswith(("docs/development/", "docs/features/", "plans/", "blockers/"))
            and suffix == ".md"
        ) or (path.startswith("screenshots/") and suffix in {".png", ".jpg", ".webp"}):
            lanes = ()
        # Convention pins, loads and tests the trusted controls; no app suite uses them.
        # Generated-output hashes are read only by Convention's publication audit.
        elif path.startswith("scripts/ci/") or path in {
                "scripts/source_controls.py", "scripts/source_policy.py", "scripts/source-policy.json",
                "scripts/generated-assets.json"}:
            lanes = ("convention",)
        elif path.startswith(".github/workflows/"):
            lane = PurePosixPath(path).stem.removesuffix("-check")
            lanes = (lane,) if lane in LANES else LANES
        elif path.startswith(("legal/", "src-tauri/", "vendor/")):
            lanes = LANES
        elif path.startswith("frontend/"):
            lanes = ("frontend",)
            if any(part in path.lower() for part in ("api", "websocket", "protocol", "types")):
                lanes = ("python", "frontend")
        elif path.startswith(("docs/offline/", "site/", "migrations/")):
            lanes = ("docs",)
        elif path.startswith("src/whisperstream/server/") or path == "src/whisperstream/config_manager.py":
            lanes = ("python", "frontend")
        elif path.startswith(("src/", "tests/")):
            lanes = ("python",)
        elif path in {"pyproject.toml", "uv.lock"}:
            lanes = ("python", "frontend")
        else:
            lanes = LANES
        for lane in lanes:
            reasons.setdefault(lane, []).append(path)
    if full:
        for lane in LANES:
            reasons.setdefault(lane, []).append("Explicit --all")
    return {lane: reasons[lane] for lane in LANES if lane in reasons}


# GitHub's default squash message appends Co-authored-by trailers whenever a
# branch commit's email differs from the merge author's. Public history carries
# no co-author trailers, so merges send an explicit message and identity.
TRAILER = re.compile(r"(?im)^\s*co-authored-by\s*:")


def blocked(text: str) -> bool:
    """Co-author trailers, plus message patterns from the installed controls."""
    extra = json.loads(os.environ.get("MERGE_BLOCKED_PATTERNS") or "[]")
    return any(p.search(text) for p in [TRAILER, *(re.compile(x, re.I | re.M) for x in extra)])


def squash_message(repo: str, number: int, title: str) -> tuple[str, str]:
    """Subject and body like GitHub's default, without merges or trailers."""
    lines = command("gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/commits",
        "--jq", ".[] | {parents: (.parents | length), message: .commit.message}").splitlines()
    entries = []
    for line in lines:
        commit = json.loads(line)
        if commit["parents"] > 1:
            continue
        text = "\n".join(l for l in commit["message"].strip().splitlines() if not blocked(l)).strip()
        if text:
            entries.append("* " + text)
    # PR titles carry routing tags for review; public history does not.
    subject = re.sub(r"^\[(app|site|chore)\]\s*", "", title)
    return f"{subject} (#{number})", "\n\n".join(entries)


def merged_commit_errors(repo: str, number: int, name: str, email: str) -> list[str]:
    sha = api(repo, f"pulls/{number}").get("merge_commit_sha")
    if not sha:
        return ["GitHub reported no merge commit"]
    commit = api(repo, f"commits/{sha}")["commit"]
    errors = []
    if [commit["author"]["name"], commit["author"]["email"]] != [name, email]:
        errors.append(f"merge commit {sha} author is {commit['author']['name']} <{commit['author']['email']}>")
    if blocked(commit["message"]):
        errors.append(f"merge commit {sha} message carries a blocked trailer")
    return errors


def command(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, encoding="utf-8", stdout=subprocess.PIPE).stdout.strip()


def api(repo: str, path: str) -> dict:
    return json.loads(command("gh", "api", f"repos/{repo}/{path}"))


def context(repo: str, number: int) -> dict:
    pr = api(repo, f"pulls/{number}")
    if pr["state"] != "open" or not pr["head"]["repo"] or pr["head"]["repo"]["full_name"] != repo:
        raise ValueError("Requires an open PR with a branch in this repository.")
    # Pull-request SHA snapshots can lag branch updates. Read the actual refs
    # for selection and every freshness check, including immediately pre-merge.
    for side in ("head", "base"):
        ref = quote(pr[side]["ref"], safe="")
        pr[side]["sha"] = api(repo, f"git/ref/heads/{ref}")["object"]["sha"]
    return pr


def identity(pr: dict) -> tuple[str, str]:
    return pr["head"]["sha"], pr["base"]["sha"]


def matches_origin(remote: str, repo: str) -> bool:
    """Permit a credential-manager username without weakening host/repo checks."""
    expected = repo.lower()
    normalized = remote.lower().rstrip("/").removesuffix(".git")
    if normalized in {f"git@github.com:{expected}", f"ssh://git@github.com/{expected}"}:
        return True
    try:
        url = urlsplit(normalized)
        return (url.scheme == "https" and url.hostname == "github.com"
            and url.password is None and url.port is None and not url.query
            and not url.fragment and url.path == "/" + expected)
    except ValueError:
        return False


def plan(repo: str, pr: dict, full: bool) -> dict[str, list[str]]:
    # Fetch immutable objects from the same repository gh selected. Never trust
    # origin to belong to that repository, nor GitHub's truncated file listing.
    head, base = identity(pr)
    remote = command("git", "remote", "get-url", "origin")
    if not matches_origin(remote, repo):
        raise ValueError("origin must match the repository selected by gh; check gh repo set-default.")
    # Preserve the clone's working authentication transport rather than
    # requiring SSH on an HTTPS checkout (or vice versa).
    command("git", "fetch", "origin", head, base)
    command("git", "merge-base", "--is-ancestor", base, head)
    paths = command("git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}").split("\0")
    return select([path for path in paths if path], full)


def title(lane: str, base: str) -> str:
    return f"{lane}-check / {base}"


def latest_run(runs: list[dict], lane: str, pr: dict) -> dict | None:
    head, base = identity(pr)
    matches = [run for run in runs if (
        run["head_sha"] == head
        and run["head_branch"] == pr["head"]["ref"]
        and run["event"] == "workflow_dispatch"
        and run["path"] == f".github/workflows/{lane}-check.yml"
        and run["display_title"] == title(lane, base)
    )]
    return max(matches, key=lambda run: run["id"], default=None)


def get_run(repo: str, lane: str, pr: dict) -> dict | None:
    # Pagination prevents a busy branch or many manual retries hiding evidence.
    result = command("gh", "api", "--paginate", "--slurp",
        f"repos/{repo}/actions/workflows/{lane}-check.yml/runs?head_sha={pr['head']['sha']}&event=workflow_dispatch&per_page=100")
    return latest_run([run for page in json.loads(result) for run in page["workflow_runs"]], lane, pr)


def unchanged(repo: str, number: int, pr: dict) -> dict:
    current = context(repo, number)
    if identity(current) != identity(pr):
        raise ValueError("PR head or target branch changed. Merge the target branch, push, then validate again.")
    return current


def verify(repo: str, number: int, pr: dict, lanes: dict) -> None:
    errors = []
    for lane in lanes:
        run = get_run(repo, lane, pr)
        if not run or run["status"] != "completed" or run["conclusion"] != "success":
            errors.append(f"{lane}: {run['html_url'] if run else 'missing current-head/current-base run'}")
        else:
            print(f"PASS {lane}: {run['html_url']}", flush=True)
    unchanged(repo, number, pr)
    if errors:
        raise ValueError("Merge validation incomplete:\n" + "\n".join(errors))


def run_checks(repo: str, number: int, pr: dict, lanes: dict) -> None:
    pending = []
    for lane in lanes:
        unchanged(repo, number, pr)
        run = get_run(repo, lane, pr)
        if run and run["status"] == "completed" and run["conclusion"] == "success":
            print(f"Reuse {lane}: {run['html_url']}", flush=True)
            continue
        if run and run["status"] == "completed":
            raise ValueError(f"{lane} failed/cancelled. Inspect {run['html_url']}; fix and push, or use gh run rerun {run['id']} --failed for a transient failure.")
        if not run:
            print(f"Dispatch {lane}", flush=True)
            command("gh", "workflow", "run", f"{lane}-check.yml", "--repo", repo,
                "--ref", pr["head"]["ref"], "-f", f"ci_head_sha={pr['head']['sha']}",
                "-f", f"ci_base_sha={pr['base']['sha']}")
            deadline = time.monotonic() + 90
            while not run and time.monotonic() < deadline:
                time.sleep(3)
                run = get_run(repo, lane, pr)
            if not run:
                raise ValueError(f"{lane}: no matching run appeared. Check Actions and retry this command; do not merge.")
        pending.append(run)
    failed = False
    for run in pending:
        print(f"Watch {run['html_url']}", flush=True)
        result = subprocess.run(["gh", "run", "watch", str(run["id"]), "--repo", repo, "--exit-status", "--interval", "15"])
        failed |= result.returncode != 0
    verify(repo, number, pr, lanes)
    if failed:
        raise ValueError("A workflow watch failed. Re-run verify before merging.")


# Local evidence: the helper runs each lane itself, in a detached worktree of
# the exact head, and records a receipt bound to head, target, selection and
# this helper's bytes. It never accepts results reported by another tool.
EVIDENCE_VERSION = 1
HELPER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
COMMENT_MARKER = "<!-- merge-check-local"
# The installed controls disable Git replacements for their own reads. Lanes get
# CI's ordinary Git, which the replacement-hiding tests depend on.
LANE_ENV_EXCLUDED = {"CI", "NODE_OPTIONS", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN", "GH_REPO", "GH_HOST", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE"}


NOTICES = ("LICENSE", "LICENSES/Chirp-MIT.txt", "NOTICE", "AUTHORS.md")


def source_base(tree: Path) -> str:
    """The history boundary the Convention workflow pins."""
    workflow = (tree / ".github/workflows/convention-check.yml").read_text(encoding="utf-8")
    pin = re.search(r"SOURCE_BASE: ([0-9a-f]{40})\b", workflow)
    if not pin:
        raise ValueError("Convention workflow SOURCE_BASE pin is missing.")
    return pin.group(1)


def pr_metadata(pr: dict, tree: Path, scratch: Path, output) -> int:
    """Stage the PR title, body and branches for the metadata audit."""
    metadata = [{key: pr.get(key) for key in ("title", "body")} | {"head": pr["head"]["ref"], "base": pr["base"]["ref"]}]
    (scratch / "pr-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return 0


def conventions(pr: dict, tree: Path, scratch: Path, output) -> int:
    """Errors block; branch and title conventions only warn, as in CI."""
    if not re.fullmatch(r"(app|site|chore|feature|fix|bugfix|hotfix)/[a-z0-9._-]+", pr["head"]["ref"]):
        output.write(f"WARNING: branch {pr['head']['ref']} does not match the naming convention.\n")
    if not re.match(r"\[(app|site|chore)\] .+", pr.get("title", "")):
        output.write("WARNING: PR title does not start with [app], [site] or [chore].\n")
    errors = [f"{path} is missing or empty." for path in NOTICES
        if not (tree / path).is_file() or not (tree / path).stat().st_size]
    output.write("".join(f"ERROR: {error}\n" for error in errors))
    return 1 if errors else 0


def local_steps(lane: str, pr: dict, tree: Path, scratch: Path) -> list[tuple[str, str, list[str] | Callable]]:
    """Local equivalents of each workflow. A callable step runs in-process and returns an exit code."""
    npm = shutil.which("npm") or "npm"
    empty = str(scratch / "empty-pytest.ini")
    if lane == "convention":
        # The head's own checker: control changes are approved before the port
        # and the installed snapshot refuses pushes with unreviewed control bytes.
        policy = [sys.executable, "-I", str(tree / "scripts/source_policy.py"), "--repo", ".",
            "--policy", str(tree / "scripts/source-policy.json")]
        return [
            ("pr metadata", ".", pr_metadata),
            ("publication audit", ".", [*policy, "--receipt", str(scratch / "source-audit.json"), "range",
                "--base", pr["base"]["sha"], "--head", "HEAD", "--source-base", source_base(tree)]),
            ("metadata audit", ".", [*policy, "metadata", "--file", str(scratch / "pr-metadata.json")]),
            ("conventions", ".", conventions),
            ("ci tooling tests", ".", ["uv", "--no-config", "run", "--no-project", "--with", "pytest==8.4.2",
                "python", "-m", "pytest", "-c", empty, "-p", "no:cacheprovider",
                "--confcutdir=scripts/ci/tests", "scripts/ci/tests", "-q"]),
        ]
    return {
        "python": [
            ("uv sync", ".", ["uv", "sync", "--locked", "--extra", "dev", "--python", "3.13"]),
            ("sherpa source", ".", ["uv", "run", "--locked", "python", "scripts/check-sherpa-source.py"]),
            # CI runs this lane on Windows, where the native Mac worker tests skip.
            # On a Mac the fresh worktree has no worker binary, so build it first.
            *([("macos stt worker", ".", ["env", "MACOSX_DEPLOYMENT_TARGET=14.0", "cargo", "build", "--release",
                "--locked", "--manifest-path", "native/macos-stt-worker/Cargo.toml"])]
                if sys.platform == "darwin" else []),
            ("pytest", ".", ["uv", "run", "pytest", "tests/", "--ignore=tests/e2e/", "-q", "-n", "auto"]),
        ],
        "frontend": [
            ("npm ci", "frontend", [npm, "ci"]),
            ("test", "frontend", [npm, "test"]),
            ("build", "frontend", [npm, "run", "build"]),
        ],
        "docs": [
            ("npm ci", "docs/offline/toolchain", [npm, "ci"]),
            ("docs check", ".", [npm, "--prefix", "docs/offline/toolchain", "run", "docs:check"]),
            ("bundle inventory", ".", ["node", "scripts/check-offline-docs.mjs"]),
        ],
    }[lane]


def tool_versions() -> dict[str, str]:
    versions = {"os": platform.platform(), "python": platform.python_version()}
    for name, args in {"git": ["git", "--version"], "uv": ["uv", "--version"], "node": ["node", "--version"],
            "npm": [shutil.which("npm") or "npm", "--version"], "gitleaks": ["gitleaks", "version"]}.items():
        try:
            versions[name] = command(*args).splitlines()[0]
        except (OSError, subprocess.CalledProcessError, IndexError):
            versions[name] = "unavailable"
    return versions


def evidence_dir(repo: str, number: int, pr: dict) -> Path:
    common = Path(command("git", "rev-parse", "--path-format=absolute", "--git-common-dir"))
    head, base = identity(pr)
    return common / "merge-evidence" / repo.replace("/", "__") / f"pr{number}" / f"{head}-{base}"


def load_receipt(repo: str, number: int, pr: dict) -> dict | None:
    path = evidence_dir(repo, number, pr) / "receipt.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def receipt_errors(receipt: dict | None, repo: str, number: int, pr: dict, lanes: dict) -> list[str]:
    if not receipt:
        return ["missing local receipt for the current head/target; run with --local"]
    head, base = identity(pr)
    expected = {"version": EVIDENCE_VERSION, "repo": repo, "pr": number, "head": head, "base": base,
        "helper_sha256": HELPER_SHA256, "selection": sorted(lanes)}
    errors = [f"receipt {key} does not match the current {key}" for key, value in expected.items()
        if receipt.get(key) != value]
    results = receipt.get("lanes", {})
    errors += [f"{lane}: {results.get(lane, {}).get('status', 'missing')}" for lane in lanes
        if results.get(lane, {}).get("status") != "pass"]
    return errors


def run_lane(lane: str, pr: dict, tree: Path, scratch: Path, log: Path) -> dict:
    # Local resource limits stay active (no CI); each lane gets fresh app data.
    # Lanes run candidate code, so they never inherit the helper's GitHub token.
    env = {key: value for key, value in os.environ.items() if key.upper() not in LANE_ENV_EXCLUDED}
    data = scratch / f"data-{lane}"
    data.mkdir()
    env["WHISPERSTREAM_DATA_DIR"] = str(data)
    steps = []
    with log.open("w", encoding="utf-8") as output:
        for label, cwd, args in local_steps(lane, pr, tree, scratch):
            print(f"  {lane}: {label}", flush=True)
            output.write(f"$ [{cwd}] {label}: {'in-process' if callable(args) else args}\n")
            output.flush()
            started = time.monotonic()
            if callable(args):
                try:
                    code = args(pr, tree, scratch, output)
                except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                    output.write(f"ERROR: {exc}\n")
                    code = 1
            else:
                try:
                    code = subprocess.run(args, cwd=tree / cwd, env=env, stdout=output,
                        stderr=subprocess.STDOUT).returncode
                except OSError as exc:
                    output.write(f"ERROR: {exc}\n")
                    code = 127
            steps.append({"step": label, "cwd": cwd, "args": None if callable(args) else args, "exit": code,
                "seconds": round(time.monotonic() - started, 1)})
            if code:
                break
    return {"status": "fail" if any(step["exit"] for step in steps) else "pass", "steps": steps,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()}


def comment_body(receipt: dict) -> str:
    rows = "\n".join(f"| {lane} | {result['status']} | " + "; ".join(
        f"{step['step']} ({step['seconds']}s)" for step in result["steps"]) + " |"
        for lane, result in receipt["lanes"].items())
    tools = ", ".join(f"{name} {value}" for name, value in receipt["tools"].items())
    return (f"{COMMENT_MARKER} head={receipt['head']} base={receipt['base']} -->\n"
        f"## Local merge checks\n\nRun by `merge_check.py run --local` for head `{receipt['head']}` "
        f"against `{receipt['base_ref']}` `{receipt['base']}`.\n\n| Lane | Result | Steps |\n| --- | --- | --- |\n"
        f"{rows}\n\nTools: {tools}.\n")


def run_local(repo: str, number: int, pr: dict, lanes: dict) -> None:
    out = evidence_dir(repo, number, pr)
    head, base = identity(pr)
    receipt = {"version": EVIDENCE_VERSION, "repo": repo, "pr": number, "head": head, "base": base,
        "head_ref": pr["head"]["ref"], "base_ref": pr["base"]["ref"], "helper_sha256": HELPER_SHA256,
        "selection": sorted(lanes), "tools": tool_versions(), "lanes": {}}
    # Reuse passing lanes only from a receipt for this exact pair, selection and helper.
    previous = load_receipt(repo, number, pr) or {}
    bound = ("version", "repo", "pr", "head", "base", "helper_sha256", "selection")
    reusable = previous.get("lanes", {}) if all(previous.get(key) == receipt[key] for key in bound) else {}
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="merge-check-"))
    tree = scratch / "tree"
    try:
        command("git", "worktree", "add", "--quiet", "--detach", str(tree), head)
        (scratch / "empty-pytest.ini").write_text("", encoding="utf-8")
        for lane in lanes:
            unchanged(repo, number, pr)
            prior = reusable.get(lane)
            if prior and prior.get("status") == "pass":
                print(f"Reuse {lane}: passed locally for this head/target", flush=True)
                receipt["lanes"][lane] = prior
                continue
            print(f"Run {lane} locally", flush=True)
            receipt["lanes"][lane] = run_lane(lane, pr, tree, scratch, out / f"{lane}.log")
            print(f"{receipt['lanes'][lane]['status'].upper()} {lane}: {out / (lane + '.log')}", flush=True)
            if receipt["lanes"][lane]["status"] != "pass":
                break
    finally:
        receipt["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        shutil.rmtree(scratch, ignore_errors=True)
    verify_local(repo, number, pr, lanes)
    if reusable and all(reusable.get(lane) == receipt["lanes"][lane] for lane in lanes) and previous.get("commented"):
        print(f"Local evidence for this head/target is already on PR #{number}.", flush=True)
        receipt["commented"] = True
    else:
        body = out / "comment.md"
        body.write_text(comment_body(receipt), encoding="utf-8")
        command("gh", "pr", "comment", str(number), "--repo", repo, "--body-file", str(body))
        receipt["commented"] = True
        print(f"Recorded local evidence on PR #{number}.", flush=True)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def verify_local(repo: str, number: int, pr: dict, lanes: dict) -> None:
    errors = receipt_errors(load_receipt(repo, number, pr), repo, number, pr, lanes)
    if not errors:
        for lane in lanes:
            print(f"PASS {lane}: local receipt {evidence_dir(repo, number, pr) / 'receipt.json'}", flush=True)
    unchanged(repo, number, pr)
    if errors:
        raise ValueError("Merge validation incomplete:\n" + "\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "verify", "merge"))
    parser.add_argument("--pr", type=int, help="Defaults to the current branch's PR")
    parser.add_argument("--all", action="store_true", help="Select every suite, including for release preparation")
    parser.add_argument("--local", action="store_true",
        help="Run lanes on this machine and verify/merge from its receipt instead of GitHub Actions")
    args = parser.parse_args()
    repo = json.loads(command("gh", "repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
    number = args.pr or json.loads(command("gh", "pr", "view", "--json", "number"))["number"]
    pr = context(repo, number)
    lanes = plan(repo, pr, args.all)
    print(f"PR #{number}: head {pr['head']['sha']} against {pr['base']['ref']} at {pr['base']['sha']}", flush=True)
    for lane, reasons in lanes.items():
        print(f"  {lane}: {', '.join(reasons)}", flush=True)
    if args.action == "plan":
        return
    if args.action == "run":
        (run_local if args.local else run_checks)(repo, number, pr, lanes)
        return
    (verify_local if args.local else verify)(repo, number, pr, lanes)
    if args.action == "merge":
        current = unchanged(repo, number, pr)
        if current["draft"] or current["mergeable"] is not True:
            raise ValueError("PR must be non-draft and mergeable. An unknown mergeability state requires a retry.")
        # GitHub atomically protects the head; this plan cannot atomically lock
        # the base without supported branch protection/merge queues.
        name, email = os.environ.get("MERGE_AUTHOR_NAME"), os.environ.get("MERGE_AUTHOR_EMAIL")
        if not name or not email:
            raise ValueError("Merge requires the configured publishing identity; run it through source controls.")
        subject, body = squash_message(repo, number, current["title"])
        if blocked(subject):
            raise ValueError("PR title carries a blocked trailer.")
        # gh omits an empty body, which lets GitHub generate its default one.
        if not body.strip():
            raise ValueError("Squash message body is empty; GitHub would substitute its default.")
        with tempfile.TemporaryDirectory(prefix="merge-message-") as scratch:
            body_file = Path(scratch) / "body.txt"
            body_file.write_text(body, encoding="utf-8")
            subprocess.run(["gh", "pr", "merge", str(number), "--repo", repo, "--squash",
                "--match-head-commit", pr["head"]["sha"], "--subject", subject,
                "--body-file", str(body_file)], check=True)
        # GitHub rejects --author-email for the noreply address; the account's
        # private-email setting makes it the squash author, verified below.
        errors = merged_commit_errors(repo, number, name, email)
        if errors:
            raise ValueError("Merged, but the squash commit needs a reviewed history correction:\n" + "\n".join(errors))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}\nIf the target is ahead, merge it into the PR branch and push. Never rebase.", file=sys.stderr)
        sys.exit(1)
