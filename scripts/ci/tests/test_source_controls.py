"""Publication controls use reviewed code, explicit identities and all pushed refs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SPEC = importlib.util.spec_from_file_location("source_controls", Path(__file__).parents[2] / "source_controls.py")
controls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controls)


def run(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    run(repo, "init")
    run(repo, "config", "user.name", "Publisher")
    run(repo, "config", "user.email", "publisher@example.test")
    run(repo, "remote", "add", "origin", "https://publisher@github.com/example/project.git")
    (repo / "scripts").mkdir()
    (repo / "scripts/source_controls.py").write_bytes(Path(controls.__file__).read_bytes())
    (repo / "scripts/source_policy.py").write_text('print("checked")\n')
    (repo / "scripts/source-policy.json").write_text('{}\n')
    (repo / "README.md").write_text("Public guide\n")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "Public source baseline")
    return repo


def setup(repo, **kwargs):
    sha = run(repo, "rev-parse", "HEAD")
    return controls.install(repo, sha, sha, **kwargs)


def change(repo, path="README.md", content="Updated guide\n"):
    (repo / path).write_text(content)
    run(repo, "add", path)
    # Test fixtures deliberately create history without invoking installed hooks.
    run(repo, "-c", "core.hooksPath=/dev/null", "commit", "-m", "Update public input")
    return run(repo, "rev-parse", "HEAD")


def test_install_preserves_hooks_and_worktree_shares_snapshot(repository, tmp_path):
    hook = repository / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    original = hook.read_bytes()
    state = setup(repository)
    assert hook.with_name("pre-push.before-source-controls").read_bytes() == original
    assert b"source-controls managed chain" in hook.read_bytes()
    worktree = tmp_path / "linked"
    run(repository, "worktree", "add", "-b", "feature", str(worktree))
    assert controls.load(worktree)[0] == state
    assert controls.common(worktree) == controls.common(repository)


def test_install_refuses_custom_hooks_path_without_mutation(repository):
    run(repository, "config", "core.hooksPath", "custom-hooks")
    with pytest.raises(controls.ControlError, match="hooksPath"):
        setup(repository)
    assert not (repository / ".git/source-controls").exists()


def test_actual_git_precommit_chains_preserved_hook(repository):
    hook = repository / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 23\n")
    hook.chmod(0o755)
    setup(repository)
    result = subprocess.run(["git", "-C", str(repository), "hook", "run", "pre-commit"], capture_output=True)
    assert result.returncode != 0
    assert b"Preserved repository hook blocked" in result.stderr


def test_missing_scanner_blocks_doctor(repository, monkeypatch):
    setup(repository)
    original = controls.shutil.which
    monkeypatch.setattr(controls.shutil, "which", lambda name: None if name == "gitleaks" else original(name))
    with pytest.raises(controls.ControlError, match="Required executable missing: gitleaks"):
        controls.doctor(repository)


def test_install_requires_immutable_review(repository):
    with pytest.raises(controls.ControlError, match="full reviewed"):
        controls.install(repository, "HEAD", run(repository, "rev-parse", "HEAD"))


def test_changed_controls_require_explicit_update(repository):
    state = setup(repository)
    changed = change(repository, "scripts/source-policy.json", '{"version":2}\n')
    with pytest.raises(controls.ControlError, match="trust-update"):
        controls.check_snapshot(repository, state, changed)
    updated = controls.install(repository, changed, update=True)
    controls.check_snapshot(repository, updated, changed)
    assert updated["source_base"] == state["source_base"]


@pytest.mark.parametrize("path", ["scripts/ci/merge_check.py", ".github/workflows/convention-check.yml",
    "scripts/export-source.py", ".gitleaks.toml"])
def test_tooling_and_workflow_changes_need_no_trust_update(repository, path):
    state = setup(repository)
    (repository / path).parent.mkdir(parents=True, exist_ok=True)
    changed = change(repository, path, "changed tooling\n")
    controls.check_snapshot(repository, state, changed)


def test_snapshot_keeps_reviewed_merge_helper_with_integrity_check(repository):
    (repository / "scripts/ci").mkdir()
    reviewed = change(repository, "scripts/ci/merge_check.py", "reviewed helper\n")
    controls.install(repository, reviewed, reviewed)
    _, snapshot = controls.load(repository)
    helper = snapshot / "scripts/ci/merge_check.py"
    assert helper.read_text() == "reviewed helper\n"
    candidate = change(repository, "scripts/ci/merge_check.py", "candidate helper\n")
    controls.check_snapshot(repository, controls.load(repository)[0], candidate)
    assert helper.read_text() == "reviewed helper\n"
    helper.write_text("tampered")
    with pytest.raises(controls.ControlError, match="Installed controls changed"):
        controls.load(repository)


def test_installed_checker_tampering_fails(repository):
    setup(repository)
    _, snapshot = controls.load(repository)
    (snapshot / "scripts/source_policy.py").write_text("changed")
    with pytest.raises(controls.ControlError, match="Installed controls changed"):
        controls.load(repository)


def test_preserved_hook_receives_original_input(repository, monkeypatch):
    setup(repository)
    old = repository / ".git/hooks/pre-push.before-source-controls"
    old.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(controls, "outgoing", lambda *a: [])
    monkeypatch.setattr(controls, "scan", lambda *a: None)
    monkeypatch.setattr(controls, "shell", lambda: "sh")
    seen = []
    original = subprocess.run
    def execute(command, **kwargs):
        if command[0] == "sh":
            seen.append((command, kwargs["input"]))
            return subprocess.CompletedProcess(command, 0)
        return original(command, **kwargs)
    monkeypatch.setattr(controls.subprocess, "run", execute)
    controls.hook(repository, "pre-push", ["origin", "url"], b"original refs\n")
    assert seen == [(["sh", "-c", 'exec "$@"', "source-hook", str(old), "origin", "url"], b"original refs\n")]


def mock_advertisement(monkeypatch, refs):
    original = controls.git
    def git(repo, *args, **kwargs):
        if args[0] == "ls-remote":
            return "".join(f"{sha}\t{ref}\n" for ref, sha in refs.items()).encode()
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(controls, "git", git)


def test_all_refs_and_annotated_tags_are_inspected(repository, monkeypatch):
    state = setup(repository)
    base = state["source_base"]
    head = change(repository)
    run(repository, "tag", "-a", "release", "-m", "Public tag")
    tag = run(repository, "rev-parse", "release")
    mock_advertisement(monkeypatch, {"refs/heads/main": base})
    zero = "0" * 40
    lines = f"refs/heads/feature {head} refs/heads/feature {zero}\nrefs/tags/release {tag} refs/tags/release {zero}\n"
    updates = controls.outgoing(repository, state, "origin", state["remote"], lines.encode())
    assert [(item[0], item[1]) for item in updates] == [(base, head), (base, head)]
    assert all(item[2] == [head] for item in updates)
    assert updates[1][3] == tag


def test_remote_race_and_unknown_history_fail(repository, monkeypatch):
    state = setup(repository)
    base = state["source_base"]
    mock_advertisement(monkeypatch, {"refs/heads/main": base})
    with pytest.raises(controls.ControlError, match="Remote refs changed"):
        controls.outgoing(repository, state, "origin", state["remote"],
            f"refs/heads/main {base} refs/heads/main {'0' * 40}\n".encode())


def test_shallow_history_fails_before_network(repository, monkeypatch):
    state = setup(repository)
    original = controls.git
    def git(repo, *args, **kwargs):
        if args == ("rev-parse", "--is-shallow-repository"):
            return b"true\n"
        if args[0] == "ls-remote":
            pytest.fail("shallow history must fail before remote inspection")
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(controls, "git", git)
    with pytest.raises(controls.ControlError, match="Shallow history"):
        controls.outgoing(repository, state, "origin", state["remote"], b"")


def test_unknown_remote_object_fails(repository, monkeypatch):
    state = setup(repository)
    mock_advertisement(monkeypatch, {"refs/heads/unknown": "a" * 40})
    with pytest.raises(controls.ControlError, match="Git inspection failed"):
        controls.outgoing(repository, state, "origin", state["remote"], b"")


def test_unrelated_history_fails(repository, monkeypatch):
    state = setup(repository)
    mock_advertisement(monkeypatch, {})
    run(repository, "checkout", "--orphan", "unrelated")
    head = change(repository)
    with pytest.raises(controls.ControlError, match="Git inspection failed"):
        controls.outgoing(repository, state, "origin", state["remote"],
            f"refs/heads/unrelated {head} refs/heads/new {'0' * 40}\n".encode())


def test_commit_identity_not_only_current_configuration(repository):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    run(repository, "config", "user.name", "Other")
    head = change(repository)
    run(repository, "config", "user.name", "Publisher")
    controls.configured_identity(repository, state)
    with pytest.raises(controls.ControlError, match="Outgoing commit identity"):
        controls.commit_identity(repository, state, [head])


def test_exact_credit_exception_preserves_author_requires_committer(repository):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    env = dict(os.environ, GIT_AUTHOR_NAME="Contributor", GIT_AUTHOR_EMAIL="contributor@example.test")
    run(repository, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "Contributor fix", env=env)
    head = run(repository, "rev-parse", "HEAD")
    state["credit_commits"] = [head]
    controls.commit_identity(repository, state, [head])
    state["credit_commits"] = []
    with pytest.raises(controls.ControlError, match="Outgoing commit identity"):
        controls.commit_identity(repository, state, [head])


@pytest.mark.parametrize("trailer,patterns", [
    ("Co-authored-by: Publisher <publisher@example.test>", []),
    ("Local-Trailer: value", ["^local-trailer:"]),
])
def test_blocked_trailer_requires_credit_exception(repository, trailer, patterns):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    state["blocked_patterns"] = patterns
    env = dict(os.environ, GIT_AUTHOR_NAME="Publisher", GIT_AUTHOR_EMAIL="publisher@example.test",
        GIT_COMMITTER_NAME="Publisher", GIT_COMMITTER_EMAIL="publisher@example.test")
    run(repository, "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "Fix", "-m", trailer, env=env)
    head = run(repository, "rev-parse", "HEAD")
    with pytest.raises(controls.ControlError, match="blocked trailer"):
        controls.commit_identity(repository, state, [head])
    state["credit_commits"] = [head]
    controls.commit_identity(repository, state, [head])


@pytest.mark.parametrize("remote",["https://github.com.evil.test/a/b", "https://u:secret@github.com/a/b", "https://github.com/a/b?token=bad", "ssh://git@github.com/a/b"])
def test_scoped_remote_rejects_credential_or_host_confusion(remote):
    with pytest.raises(controls.ControlError):
        controls.github_repository(remote)


def test_auth_mismatch_does_not_expose_credential(repository, monkeypatch):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    monkeypatch.setattr(controls.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, b"password=synthetic-credential\n", b""))
    monkeypatch.setattr(controls, "request", lambda *a, **k: {"login": "wrong"})
    with pytest.raises(controls.ControlError, match="Authenticated account") as exc:
        controls.authenticate(repository, state)
    assert "synthetic-credential" not in str(exc.value)


def test_missing_credentials_fail_without_api_request(repository, monkeypatch):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    monkeypatch.setattr(controls.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, b"", b""))
    monkeypatch.setattr(controls, "request", lambda *a, **k: pytest.fail("no request without credentials"))
    with pytest.raises(controls.ControlError, match="credential is unavailable"):
        controls.authenticate(repository, state)


def test_scan_environment_has_no_publishing_token(repository, monkeypatch):
    setup(repository)
    _, snapshot = controls.load(repository)
    monkeypatch.setenv("GH_TOKEN", "synthetic-credential")
    seen = []
    monkeypatch.setattr(controls.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or subprocess.CompletedProcess([], 0, b"", b""))
    controls.scan(repository, snapshot, "staged")
    assert "GH_TOKEN" not in seen[0][1]["env"]
    assert str(snapshot / "scripts/source_policy.py") in seen[0][0]
    assert "-I" in seen[0][0]


def test_checkout_cannot_run_authenticated_helper(repository):
    setup(repository)
    _, snapshot = controls.load(repository)
    with pytest.raises(controls.ControlError, match="installed"):
        controls.require_installed(snapshot)


def test_api_body_scanned_before_authentication(repository, monkeypatch, tmp_path):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    monkeypatch.setattr(controls, "scan", lambda *a: (_ for _ in ()).throw(controls.ControlError("blocked metadata")))
    monkeypatch.setattr(controls, "authenticate", lambda *a: pytest.fail("credentials must not be loaded"))
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"body": "Public description"}))
    with pytest.raises(controls.ControlError, match="blocked metadata"):
        controls.scoped_api(repository, "pulls", "POST", body)


def test_api_sends_exact_scanned_bytes_despite_concurrent_draft_change(repository, monkeypatch, tmp_path):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    body = tmp_path / "body.json"
    body.write_text('{"body":"Reviewed"}')
    def scan(*args, **kwargs):
        assert json.loads(Path(args[-1]).read_text().splitlines()[0]) == {"body": "Reviewed"}
        body.write_text('{"body":"Changed after scan"}')
    monkeypatch.setattr(controls, "scan", scan)
    monkeypatch.setattr(controls, "authenticate", lambda *a: "synthetic-credential")
    monkeypatch.setattr(controls, "request", lambda token, endpoint, method, data: data)
    assert controls.scoped_api(repository, "pulls", "POST", body) == {"body": "Reviewed"}


@pytest.mark.parametrize("text", ["Fix\n\nCo-authored-by: Someone <s@example.test>", "Fix\n\nLocal-Trailer: value"])
def test_api_rejects_blocked_patterns_in_request_text(repository, monkeypatch, tmp_path, text):
    state = setup(repository)
    state["blocked_patterns"] = ["^local-trailer:"]
    monkeypatch.setattr(controls, "load", lambda repo: (state, tmp_path))
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    monkeypatch.setattr(controls, "scan", lambda *a, **k: None)
    monkeypatch.setattr(controls, "authenticate", lambda *a: pytest.fail("blocked before credentials"))
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"title": "Fix", "body": text}))
    with pytest.raises(controls.ControlError, match="blocked trailer or message pattern"):
        controls.scoped_api(repository, "pulls", "POST", body)


@pytest.mark.parametrize("decoded", ["https://internal.example.test/restricted", "synthetic-credential-detection-value"])
def test_api_audits_decoded_json_values(repository, monkeypatch, tmp_path, decoded):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    body = tmp_path / "escaped.json"
    body.write_text('{"body":"' + ''.join("\\u%04x" % ord(character) for character in decoded) + '"}')
    def scan(*args, **kwargs):
        assert decoded in Path(args[-1]).read_text()
        raise controls.ControlError("decoded metadata blocked")
    monkeypatch.setattr(controls, "scan", scan)
    monkeypatch.setattr(controls, "authenticate", lambda *a: pytest.fail("blocked before credentials"))
    with pytest.raises(controls.ControlError, match="decoded metadata blocked"):
        controls.scoped_api(repository, "pulls", "POST", body)


@pytest.mark.parametrize("location", ["body", "nested_value", "nested_key"])
def test_api_decoded_strings_hit_real_metadata_rules(repository, monkeypatch, tmp_path, location):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    policy_path = Path(controls.__file__).with_name("source_policy.py")
    spec = importlib.util.spec_from_file_location("source_policy_for_api_controls", policy_path)
    policy_checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = policy_checker
    spec.loader.exec_module(policy_checker)
    policy = json.loads(policy_path.with_name("source-policy.json").read_text())
    link = "https://github.com/example/project-" + "private"
    text = link + "\nNotes"
    payload = ({"body": text} if location == "body" else
               {"details": [{"body": text}]} if location == "nested_value" else
               {"details": [{text: "public value"}]})
    body = tmp_path / "escaped-nested.json"
    body.write_text(json.dumps(payload).replace(link, ''.join("\\u%04x" % ord(character) for character in link)))
    def scan(*args, **kwargs):
        findings = policy_checker.check_metadata(Path(args[-1]).read_bytes(), policy)
        assert any(item["rule"] == "private-repository-reference" for item in findings)
        raise controls.ControlError("decoded metadata blocked")
    monkeypatch.setattr(controls, "scan", scan)
    monkeypatch.setattr(controls, "authenticate", lambda *a: pytest.fail("blocked before credentials"))
    with pytest.raises(controls.ControlError, match="decoded metadata blocked"):
        controls.scoped_api(repository, "pulls", "POST", body)


@pytest.mark.parametrize("endpoint", ["https://example.test", "../other", "/user", "pulls/%2e%2e", "pulls\\other"])
def test_api_cannot_escape_repository(repository, monkeypatch, endpoint):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    with pytest.raises(controls.ControlError, match="repository-relative"):
        controls.scoped_api(repository, endpoint, "GET")


@pytest.mark.parametrize("arguments", [
    ["--policy", "other.json", "tree"], ["--repo", "other", "tree"],
    ["tree", "--policy", "other.json"], ["tree", "--repo", "other"],
    ["tree", "--r", "HEAD"], ["metadata", "--file", "body.txt", "--policy", "other.json"],
])
def test_audit_rejects_checker_override_arguments(repository, monkeypatch, arguments):
    setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    monkeypatch.setattr(controls, "scan", lambda *a, **k: pytest.fail("invalid arguments reached scanner"))
    with pytest.raises(SystemExit):
        controls.audit(repository, arguments)


def test_audit_tree_emits_trusted_receipt_and_pins_revision(repository, monkeypatch, tmp_path):
    state = setup(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    seen = []
    monkeypatch.setattr(controls, "scan", lambda *a, **k: seen.append((a, k)))
    receipt = tmp_path / "audit.json"
    controls.audit(repository, ["--", "--receipt", str(receipt), "tree", "--ref", "HEAD"])
    assert seen[0][0][2:] == ("--receipt", str(receipt.resolve()), "tree", "--ref", state["reviewed_ref"])
    assert seen[0][1] == {"emit": True}


def test_audit_rejects_unreviewed_control_changes(repository, monkeypatch):
    setup(repository)
    change(repository, "scripts/source-policy.json", '{"changed":true}\n')
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    monkeypatch.setattr(controls, "scan", lambda *a, **k: pytest.fail("unreviewed policy reached scanner"))
    with pytest.raises(controls.ControlError, match="trust-update"):
        controls.audit(repository, ["tree"])


def test_audit_range_cannot_replace_approved_history_baseline(repository, monkeypatch):
    state = setup(repository)
    new = change(repository)
    monkeypatch.setattr(controls, "require_installed", lambda *a: None)
    with pytest.raises(controls.ControlError, match="history baseline"):
        controls.audit(repository, ["range", "--base", state["source_base"], "--head", new, "--source-base", new])


def test_receipt_cannot_overwrite_git_state_or_source(repository):
    setup(repository)
    for path in (repository / ".git/source-controls/state.json", repository / "README.md", repository / "new.py"):
        with pytest.raises(controls.ControlError):
            controls.receipt_path(repository, path)


def test_receipt_can_replace_only_existing_audit_json(repository, tmp_path):
    setup(repository)
    path = tmp_path / "record.json"
    path.write_text('{"business":"record"}')
    with pytest.raises(controls.ControlError, match="not an audit receipt"):
        controls.receipt_path(repository, path)
    path.write_text(json.dumps(dict(mode="tree", status="pass", policy_sha256="a", checker_sha256="b")))
    assert controls.receipt_path(repository, path) == path.resolve()


def test_successful_scan_outputs_receipt(repository, monkeypatch, capsys):
    setup(repository)
    _, snapshot = controls.load(repository)
    output = b'{"status":"pass"}\n'
    monkeypatch.setattr(controls.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, output, b""))
    assert controls.scan(repository, snapshot, "tree", "--ref", "HEAD", emit=True) == output
    assert capsys.readouterr().out == output.decode()


def test_replacement_cannot_hide_changed_control_blobs(repository):
    state = setup(repository)
    dirty = change(repository, "scripts/source-policy.json", '{"weakened":true}\n')
    run(repository, "replace", dirty, state["reviewed_ref"])
    assert run(repository, "show", dirty + ":scripts/source-policy.json") == "{}"
    with pytest.raises(controls.ControlError, match="trust-update"):
        controls.check_snapshot(repository, state, dirty)


def test_replacement_cannot_hide_wrong_commit_author(repository):
    state = setup(repository, account="publisher", author_name="Publisher", author_email="publisher@example.test")
    run(repository, "config", "user.name", "Other")
    dirty = change(repository)
    run(repository, "replace", dirty, state["reviewed_ref"])
    assert run(repository, "show", "-s", "--format=%an", dirty) == "Publisher"
    with pytest.raises(controls.ControlError, match="Outgoing commit identity"):
        controls.commit_identity(repository, state, [dirty])


def test_install_copies_original_blobs_despite_replacement(repository):
    original = run(repository, "rev-parse", "HEAD")
    replacement = change(repository, "scripts/source-policy.json", '{"replacement":true}\n')
    run(repository, "replace", original, replacement)
    controls.install(repository, original, original)
    _, snapshot = controls.load(repository)
    assert (snapshot / "scripts/source-policy.json").read_text() == '{}\n'


def test_local_graft_file_blocks_inspection(repository):
    head = run(repository, "rev-parse", "HEAD")
    (repository / ".git/info/grafts").write_text(head + "\n")
    with pytest.raises(controls.ControlError, match="graft overrides"):
        controls.commit(repository, "HEAD")


def test_environment_graft_override_blocks_inspection(repository, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_GRAFT_FILE", str(tmp_path / "alternate-grafts"))
    with pytest.raises(controls.ControlError, match="graft overrides"):
        controls.commit(repository, "HEAD")
