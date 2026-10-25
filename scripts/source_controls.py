"""Install reviewed source checks and run repository-scoped publishing operations.

Hooks execute a reviewed copy under the common Git directory, never candidate
code. Local state is deliberately not a credential store. Installation and trust
updates are explicit review decisions, not automatic consequences of a pull.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import urllib.request


class ControlError(RuntimeError):
    pass


def clean_env():
    env = {k: v for k, v in os.environ.items() if k.upper() not in {
        "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
        "GIT_ASKPASS", "SSH_ASKPASS", "PYTHONPATH", "PYTHONHOME", "GIT_NO_REPLACE_OBJECTS"}}
    # A replacement object is only a local view; pushes still send the original.
    # Every subprocess must inspect the original object bytes and ancestry.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def reject_grafts(repo, env):
    if any(key.upper() == "GIT_GRAFT_FILE" for key in env):
        raise ControlError("Local Git graft overrides are unsupported; remove them before publication inspection.")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise ControlError("Git repository inspection failed.")
    if Path(result.stdout.decode().strip()).exists():
        raise ControlError("Local Git graft overrides are unsupported; remove them before publication inspection.")


def git(repo, *args, data=None, check=True):
    env = clean_env()
    reject_grafts(repo, env)
    result = subprocess.run(["git", "-C", str(repo), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if check and result.returncode:
        raise ControlError("Git inspection failed; fetch complete history and check repository setup.")
    return result.stdout


def root(repo):
    return Path(git(repo, "rev-parse", "--show-toplevel").decode().strip())


def shell():
    executable = shutil.which("sh")
    if executable:
        return executable
    # Git for Windows does not normally expose sh.exe to PowerShell's PATH.
    git_executable = shutil.which("git")
    if git_executable:
        candidate = Path(git_executable).resolve().parent.parent / "bin/sh.exe"
        if candidate.is_file():
            return str(candidate)
    raise ControlError("Git's shell is unavailable; install Git with its shell support.")


def common(repo):
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())


def commit(repo, ref):
    if ref.startswith("-"):
        raise ControlError("Invalid revision.")
    return git(repo, "rev-parse", "--verify", ref + "^{commit}").decode().strip()


# Only these files decide what may be published. Changing any of them needs a
# review and an explicit trust-update before this machine will push it.
TRUSTED_CONTROLS = ("scripts/source_policy.py", "scripts/source-policy.json", "scripts/source_controls.py")
# Copied from the trusted revision so authenticated helpers never run candidate
# code. Changing them does not block pushes; a trust-update picks up a new copy.
SNAPSHOT_TOOLS = ("scripts/ci/merge_check.py",)
ATTRIBUTION_TRAILER = re.compile(r"(?im)^\s*co-authored-by\s*:")


def blocked_message(state, message):
    """Co-author trailers, plus any locally installed message patterns."""
    patterns = [ATTRIBUTION_TRAILER, *(re.compile(p, re.I | re.M) for p in state.get("blocked_patterns", []))]
    return any(pattern.search(message) for pattern in patterns)


def tracked_blobs(repo, ref, wanted):
    result = {}
    for record in git(repo, "ls-tree", "-rz", ref).split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        path = name.decode("utf-8", "strict")
        if path in wanted:
            mode, kind, oid = meta.decode().split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ControlError("Control files must be ordinary tracked files.")
            result[path] = {"oid": oid, "mode": mode}
    return result


def control_blobs(repo, ref):
    result = tracked_blobs(repo, ref, TRUSTED_CONTROLS)
    if set(result) != set(TRUSTED_CONTROLS):
        raise ControlError("Reviewed revision is missing a required control file.")
    return result


def load(repo):
    directory = common(repo) / "source-controls"
    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        snapshot = directory / state["reviewed_ref"]
        for path, digest in state["sha256"].items():
            if hashlib.sha256((snapshot / path).read_bytes()).hexdigest() != digest:
                raise ControlError("Installed controls changed; review and explicitly reinstall them.")
    except (OSError, ValueError, KeyError):
        raise ControlError("Reviewed source controls are unavailable; run explicit installation.") from None
    return state, snapshot


def hook_text(common_dir, name):
    helper = common_dir / "source-controls" / "runner.py"
    return ("#!/bin/sh\n# source-controls managed chain\nexec " + shlex.quote(sys.executable)
        + " -I " + shlex.quote(helper.as_posix()) + " hook " + name + ' "$@"\n').encode()


def install(repo, reviewed_ref, source_base=None, account=None, author_name=None, author_email=None, update=False, credit_commits=(), blocked_patterns=()):
    repo = root(repo)
    common_dir = common(repo)
    hooks_path = git(repo, "config", "--get", "core.hooksPath", check=False).strip()
    if hooks_path:
        raise ControlError("core.hooksPath is already configured; integrate its hooks explicitly before installation.")
    reviewed = commit(repo, reviewed_ref)
    if reviewed_ref != reviewed:
        raise ControlError("Pass the full reviewed commit SHA, not a moving branch or tag.")
    directory = common_dir / "source-controls"
    state_file = directory / "state.json"
    previous = json.loads(state_file.read_text()) if state_file.exists() else None
    if previous and not update:
        raise ControlError("Controls are already installed; use trust-update after reviewing the new revision.")
    if update and not previous:
        raise ControlError("Install controls before trust-update.")
    if source_base is None:
        source_base = previous["source_base"] if previous else None
    if not source_base:
        raise ControlError("An explicit reviewed source history baseline is required.")
    baseline = commit(repo, source_base)
    if source_base != baseline:
        raise ControlError("Source baseline must be a full commit SHA.")
    git(repo, "merge-base", "--is-ancestor", baseline, reviewed)
    blobs = control_blobs(repo, reviewed)
    remote = git(repo, "remote", "get-url", "--push", "origin").decode().strip()
    repository = github_repository(remote)
    identity = {"account": account, "author_name": author_name, "author_email": author_email}
    if previous:
        identity = {k: v or previous.get(k) for k, v in identity.items()}
    if any(identity.values()) and not all(identity.values()):
        raise ControlError("Supply account, author name and author email together.")
    hooks = common_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    # Check every destination before changing any hook. Existing hooks are saved
    # intact and receive the original arguments/stdin after these checks pass.
    for name in ("pre-commit", "pre-push"):
        target = hooks / name
        backup = hooks / (name + ".before-source-controls")
        if target.exists() and target.read_bytes() != hook_text(common_dir, name):
            if previous or backup.exists():
                raise ControlError("An existing hook changed; reconcile it explicitly before installation.")
    directory.mkdir(exist_ok=True)
    snapshot = directory / reviewed
    snapshot.mkdir(exist_ok=True)
    hashes = {}
    for path, info in {**tracked_blobs(repo, reviewed, SNAPSHOT_TOOLS), **blobs}.items():
        data = git(repo, "cat-file", "blob", info["oid"])
        output = snapshot / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        hashes[path] = hashlib.sha256(data).hexdigest()
    credits = sorted(set((previous or {}).get("credit_commits", [])) | {commit(repo, value) for value in credit_commits})
    patterns = sorted(set((previous or {}).get("blocked_patterns", [])) | set(blocked_patterns))
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ControlError(f"Blocked message pattern does not compile: {error}") from None
    state = {"reviewed_ref": reviewed, "source_base": baseline, "remote": remote, "credit_commits": credits,
             "blocked_patterns": patterns, "repository": repository, "blobs": blobs, "sha256": hashes, **identity}
    (directory / "state.tmp").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    (directory / "state.tmp").replace(state_file)
    # Stable tiny launcher loads only locally reviewed, integrity-checked code.
    runner = ('import hashlib,json,pathlib,runpy,sys\n'
        'd=pathlib.Path(__file__).parent\n'
        's=json.loads((d/"state.json").read_text())\n'
        'p=d/s["reviewed_ref"]/"scripts/source_controls.py"\n'
        'assert hashlib.sha256(p.read_bytes()).hexdigest()==s["sha256"]["scripts/source_controls.py"],"Installed controls changed"\n'
        'sys.argv=[str(p)]+sys.argv[1:]\nrunpy.run_path(str(p),run_name="__main__")\n')
    (directory / "runner.py").write_text(runner, encoding="utf-8")
    for name in ("pre-commit", "pre-push"):
        target = hooks / name
        if target.exists() and target.read_bytes() != hook_text(common_dir, name):
            target.rename(hooks / (name + ".before-source-controls"))
        target.write_bytes(hook_text(common_dir, name))
        target.chmod(0o755)
    return state


def github_repository(remote):
    url = urlsplit(remote)
    if url.scheme != "https" or url.hostname != "github.com" or url.password or url.port or url.query or url.fragment:
        raise ControlError("Scoped publishing requires a credential-managed GitHub HTTPS origin.")
    repository = url.path.strip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ControlError("Invalid GitHub repository URL.")
    return repository


def check_snapshot(repo, state, ref):
    if control_blobs(repo, ref) != state["blobs"]:
        raise ControlError("Candidate changes publication controls. Review and trust-update the exact commit before pushing.")


def scan(repo, snapshot, *args, emit=False):
    command = [sys.executable, "-I", str(snapshot / "scripts/source_policy.py"), "--repo", str(repo),
               "--policy", str(snapshot / "scripts/source-policy.json"), *args]
    result = subprocess.run(command, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # The trusted checker emits redacted JSON diagnostics, never matched text.
        if result.stdout:
            sys.stderr.buffer.write(result.stdout)
        raise ControlError("Source policy inspection failed; resolve reported findings before publication.")
    if emit and result.stdout:
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
    return result.stdout


def receipt_path(repo, value):
    path = Path(value).resolve()
    if path.is_relative_to(common(repo).resolve()):
        raise ControlError("Audit receipts cannot overwrite Git state or installed controls.")
    if path.is_relative_to(root(repo).resolve()):
        relative = path.relative_to(root(repo).resolve()).as_posix()
        if git(repo, "ls-files", "--", relative).strip() or not git(repo, "check-ignore", "--", relative, check=False).strip():
            raise ControlError("Store audit receipts outside the tracked source tree or in an ignored directory.")
    if not path.parent.is_dir():
        raise ControlError("Create the private receipt directory before auditing.")
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or not {"mode", "status", "policy_sha256", "checker_sha256"} <= existing.keys():
                raise ValueError()
        except (ValueError, OSError):
            raise ControlError("Receipt output already exists and is not an audit receipt.") from None
    return path


def audit(repo, arguments):
    """Rebuild accepted checker arguments; never forward caller policy/repo flags."""
    state, snapshot = load(repo)
    require_installed(snapshot)
    parser = argparse.ArgumentParser(prog="source-controls audit", allow_abbrev=False)
    parser.add_argument("--receipt")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("tree", allow_abbrev=False).add_argument("--ref", default="HEAD")
    modes.add_parser("staged", allow_abbrev=False)
    command = modes.add_parser("range", allow_abbrev=False)
    command.add_argument("--base", required=True)
    command.add_argument("--head", required=True)
    command.add_argument("--source-base")
    modes.add_parser("export", allow_abbrev=False).add_argument("--directory", required=True)
    modes.add_parser("metadata", allow_abbrev=False).add_argument("--file", required=True)
    arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
    args = parser.parse_args(arguments)
    checker_args = []
    if args.receipt:
        checker_args += ["--receipt", str(receipt_path(repo, args.receipt))]
    checker_args.append(args.mode)
    if args.mode == "tree":
        head = commit(repo, args.ref)
        check_snapshot(repo, state, head)
        checker_args += ["--ref", head]
    elif args.mode == "range":
        head, base = commit(repo, args.head), commit(repo, args.base)
        check_snapshot(repo, state, head)
        if args.source_base and commit(repo, args.source_base) != state["source_base"]:
            raise ControlError("Audit source baseline must match the installed reviewed history baseline.")
        checker_args += ["--base", base, "--head", head, "--source-base", state["source_base"]]
    elif args.mode == "export":
        check_snapshot(repo, state, commit(repo, "HEAD"))
        checker_args += ["--directory", str(Path(args.directory).resolve())]
    elif args.mode == "metadata":
        checker_args += ["--file", str(Path(args.file).resolve())]
    return scan(repo, snapshot, *checker_args, emit=True)


def request(token, endpoint, method="GET", data=None):
    req = urllib.request.Request("https://api.github.com/" + endpoint,
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "source-publication-controls"})
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ControlError("GitHub redirect refused; use the canonical repository endpoint.")
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except Exception:
        raise ControlError("GitHub request failed; check account access and request parameters.") from None


def authenticate(repo, state):
    if not state.get("account"):
        raise ControlError("Configure a local publishing account with install/trust-update before authenticated operations.")
    env = dict(clean_env(), GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "-C", str(repo), "credential", "fill"],
        input=("url=" + state["remote"] + "\n\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    fields = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
    token = fields.get("password")
    if result.returncode or not token:
        raise ControlError("The repository-scoped credential is unavailable.")
    if request(token, "user").get("login") != state["account"]:
        raise ControlError("Authenticated account does not match the locally configured publishing account.")
    info = request(token, "repos/" + state["repository"])
    if not info.get("permissions", {}).get("push"):
        raise ControlError("The configured account has no push permission for this repository.")
    return token


def configured_identity(repo, state):
    if not state.get("author_name"):
        return
    prefix = f'{state["author_name"]} <{state["author_email"]}> '
    for name in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if not git(repo, "var", name).decode().startswith(prefix):
            raise ControlError("Configured author/committer differs from the local publishing identity.")


def commit_identity(repo, state, revisions):
    if not state.get("author_name"):
        return
    expected = [state["author_name"], state["author_email"]]
    for revision in revisions:
        values = git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", revision).decode().strip().split("\0")
        credited = revision in state.get("credit_commits", [])
        author_allowed = values[:2] == expected or credited
        if not author_allowed or values[2:] != expected:
            raise ControlError("Outgoing commit identity requires review. Preserve truthful third-party credit; configure an exact local exception.")
        # Co-author trailers (including GitHub's squash additions) need the same
        # exact reviewed exception as a third-party author.
        if not credited and blocked_message(state, git(repo, "show", "-s", "--format=%B", revision).decode()):
            raise ControlError("Outgoing commit message carries a blocked trailer. Remove it, or configure an exact credit exception for genuine third-party credit.")


def outgoing(repo, state, remote_name, remote_url, data):
    if remote_name != "origin" or remote_url != state["remote"]:
        raise ControlError("Push destination differs from the reviewed repository remote.")
    if git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ControlError("Shallow history cannot be audited; fetch complete history.")
    advertised = {}
    for line in git(repo, "ls-remote", "--refs", remote_url, "refs/heads/*", "refs/tags/*").decode().splitlines():
        oid, ref = line.split("\t")
        advertised[ref] = oid
    remote_commits = []
    for oid in set(advertised.values()):
        # Require all advertised history locally; never assume missing objects safe.
        remote_commits.append(commit(repo, oid))
    updates = []
    zero = "0" * 40
    for line in data.decode().splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ControlError("Malformed pre-push ref input.")
        local_ref, local_oid, remote_ref, old_oid = fields
        if not remote_ref.startswith(("refs/heads/", "refs/tags/")):
            raise ControlError("Only branch and tag pushes are supported.")
        if advertised.get(remote_ref, zero) != old_oid:
            raise ControlError("Remote refs changed during inspection; fetch and retry.")
        if local_oid == zero:
            continue
        head = commit(repo, local_oid)
        base = commit(repo, old_oid) if old_oid != zero else state["source_base"]
        git(repo, "merge-base", "--is-ancestor", state["source_base"], head)
        new = git(repo, "rev-list", head, "--not", *remote_commits).decode().splitlines() if remote_commits else git(repo, "rev-list", head).decode().splitlines()
        updates.append((base, head, new, local_oid))
    return updates


def hook(repo, name, arguments, data):
    state, snapshot = load(repo)
    if git(repo, "config", "--get", "core.hooksPath", check=False).strip():
        raise ControlError("Configured hooksPath bypasses this reviewed hook chain.")
    if name == "pre-commit":
        scan(repo, snapshot, "staged")
    elif name == "pre-push":
        if len(arguments) != 2:
            raise ControlError("Missing push destination.")
        configured_identity(repo, state)
        with tempfile.TemporaryDirectory(prefix="source-refs-audit-") as temporary:
            path = Path(temporary) / "refs.txt"
            path.write_text("\n".join(line.split()[2] for line in data.decode().splitlines() if len(line.split()) == 4), encoding="utf-8")
            scan(repo, snapshot, "metadata", "--file", str(path))
        for base, head, revisions, object_id in outgoing(repo, state, *arguments, data):
            check_snapshot(repo, state, head)
            commit_identity(repo, state, revisions)
            scan(repo, snapshot, "range", "--base", base, "--head", head, "--source-base", state["source_base"])
            while object_id != head:
                # Annotated tag messages/tagger metadata are separately exposed.
                with tempfile.TemporaryDirectory(prefix="source-tag-audit-") as temporary:
                    path = Path(temporary) / "tag.txt"
                    tag = git(repo, "cat-file", "-p", object_id)
                    path.write_bytes(tag)
                    scan(repo, snapshot, "metadata", "--file", str(path))
                    first_line = tag.splitlines()[0].decode()
                    if not re.fullmatch(r"object [0-9a-f]{40,64}", first_line):
                        raise ControlError("Malformed annotated tag.")
                    object_id = first_line.split()[1]
        if state.get("account"):
            authenticate(repo, state)
    else:
        raise ControlError("Unknown hook.")
    previous = common(repo) / "hooks" / (name + ".before-source-controls")
    if previous.exists():
        result = subprocess.run([shell(), "-c", 'exec "$@"', "source-hook", str(previous), *arguments], input=data, cwd=repo, env=clean_env())
        if result.returncode:
            raise ControlError("Preserved repository hook blocked this operation.")


def doctor(repo, online=False):
    state, snapshot = load(repo)
    if git(repo, "config", "--get", "core.hooksPath", check=False).strip():
        raise ControlError("hooksPath bypasses the installed chain.")
    for name in ("pre-commit", "pre-push"):
        if (common(repo) / "hooks" / name).read_bytes() != hook_text(common(repo), name):
            raise ControlError("An installed hook changed; reconcile the chain.")
    for executable in ("git", "gitleaks"):
        if not shutil.which(executable):
            raise ControlError("Required executable missing: " + executable)
    shell()
    if git(repo, "remote", "get-url", "--push", "origin").decode().strip() != state["remote"]:
        raise ControlError("Configured push remote changed.")
    configured_identity(repo, state)
    if online:
        authenticate(repo, state)
    return {"reviewed_ref": state["reviewed_ref"], "source_base": state["source_base"],
            "account": state.get("account"), "online_verified": online}


def require_installed(snapshot):
    if Path(__file__).resolve() != (snapshot / "scripts/source_controls.py").resolve():
        raise ControlError("Run trusted operations through the installed .git/source-controls/runner.py.")


def publication_strings(value):
    """Return decoded strings and keys, including nested JSON payloads."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def scoped_api(repo, endpoint, method, input_file=None):
    state, snapshot = load(repo)
    require_installed(snapshot)
    if endpoint.startswith("/") or ".." in endpoint or ":" in endpoint or "\\" in endpoint or "%" in endpoint:
        raise ControlError("Use a repository-relative API endpoint.")
    if not re.fullmatch(r"[A-Za-z0-9_./?=&-]+", endpoint):
        raise ControlError("Unsupported API endpoint characters.")
    data = None
    if input_file:
        # Freeze the exact request bytes before scanning. A concurrent edit to
        # the original draft must not substitute unreviewed text after the scan.
        data = json.loads(Path(input_file).read_bytes())
        # Canonical JSON still escapes newlines/backslashes. Include decoded
        # nested values and keys so scanners see the exact displayed strings.
        raw = (json.dumps(data, ensure_ascii=False) + "\n"
               + "\n".join(publication_strings(data)) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="source-api-audit-") as temporary:
            frozen = Path(temporary) / "request.txt"
            frozen.write_bytes(raw)
            scan(repo, snapshot, "metadata", "--file", str(frozen))
        # PR titles, bodies and comments are published like commit messages.
        if blocked_message(state, raw.decode("utf-8")):
            raise ControlError("Request text carries a blocked trailer or message pattern.")
    token = authenticate(repo, state)
    return request(token, "repos/" + state["repository"] + "/" + endpoint, method, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "trust-update"):
        command = commands.add_parser(action)
        command.add_argument("--reviewed-ref", required=True)
        command.add_argument("--source-base")
        command.add_argument("--account")
        command.add_argument("--author-name")
        command.add_argument("--author-email")
        command.add_argument("--credit-commit", action="append", default=[], help="Exact reviewed third-party commit; preserves its author, still checks committer")
        command.add_argument("--blocked-pattern", action="append", default=[], help="Extra case-insensitive message pattern to reject in outgoing commits and merges; kept in local state only")
    commands.add_parser("doctor").add_argument("--online", action="store_true")
    command = commands.add_parser("hook")
    command.add_argument("name", choices=("pre-commit", "pre-push"))
    command.add_argument("arguments", nargs="*")
    command = commands.add_parser("api")
    command.add_argument("endpoint")
    command.add_argument("--method", default="GET", choices=("GET", "POST", "PATCH", "PUT", "DELETE"))
    command.add_argument("--input")
    command = commands.add_parser("merge-check")
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    command = commands.add_parser("audit", allow_abbrev=False)
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    repo = root(args.repo)
    if args.action in {"install", "trust-update"}:
        state = install(repo, args.reviewed_ref, args.source_base, args.account, args.author_name,
                        args.author_email, update=args.action == "trust-update", credit_commits=args.credit_commit,
                        blocked_patterns=args.blocked_pattern)
        print(json.dumps({"installed_reviewed_ref": state["reviewed_ref"]}))
    elif args.action == "doctor":
        print(json.dumps(doctor(repo, args.online)))
    elif args.action == "hook":
        hook(repo, args.name, args.arguments, sys.stdin.buffer.read() if args.name == "pre-push" else b"")
    elif args.action == "api":
        print(json.dumps(scoped_api(repo, args.endpoint, args.method, args.input)))
    elif args.action == "audit":
        audit(repo, args.arguments)
    elif args.action == "merge-check":
        state, snapshot = load(repo)
        require_installed(snapshot)
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        if not arguments or arguments[0] not in {"plan", "run", "verify", "merge"}:
            raise ControlError("Pass a merge-check action and its arguments.")
        token = authenticate(repo, state)
        result = subprocess.run([sys.executable, "-I", str(snapshot / "scripts/ci/merge_check.py"), *arguments],
            cwd=repo, env=dict(clean_env(), GH_TOKEN=token, GH_REPO=state["repository"], GH_HOST="github.com",
                MERGE_AUTHOR_NAME=state.get("author_name") or "", MERGE_AUTHOR_EMAIL=state.get("author_email") or "",
                MERGE_BLOCKED_PATTERNS=json.dumps(state.get("blocked_patterns", []))))
        if result.returncode:
            raise ControlError("Scoped merge-check failed.")


if __name__ == "__main__":
    try:
        main()
    except (ControlError, OSError, ValueError) as error:
        print("Source controls blocked: " + str(error), file=sys.stderr)
        sys.exit(1)
