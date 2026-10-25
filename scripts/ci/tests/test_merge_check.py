"""Merge gates must never accept partial, stale, or incorrectly selected evidence."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("merge_check", Path(__file__).parents[1] / "merge_check.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


@pytest.mark.parametrize("paths,expected", [
    (["docs/development/contributing.md", "CONTRIBUTING.md"], {"convention"}),
    (["docs/features/hotkeys.md"], {"convention"}),
    (["docs/user/guide.md"], set(ci.LANES)),
    (["legal/EULA.md"], set(ci.LANES)),
    (["frontend/src/components/Button.tsx"], {"convention", "frontend"}),
    (["src/whisperstream/audio.py"], {"convention", "python"}),
    (["src/whisperstream/server/app.py"], {"convention", "python", "frontend"}),
    (["frontend/src/api/client.ts"], {"convention", "python", "frontend"}),
    (["docs/offline/toolchain/src/lib/docs.ts"], {"convention", "docs"}),
    (["migrations/20261012.sql"], {"convention", "docs"}),
    (["pyproject.toml"], {"convention", "python", "frontend"}),
    (["unknown.toml"], set(ci.LANES)),
    (["plans/fixture.json"], set(ci.LANES)),
    ([".github/workflows/python-check.yml"], {"convention", "python"}),
    (["scripts/ci/merge_check.py"], {"convention"}),
    (["scripts/source_controls.py", "scripts/source_policy.py", "scripts/source-policy.json"], {"convention"}),
    (["scripts/generated-assets.json"], {"convention"}),
    (["scripts/check-sherpa-source.py"], set(ci.LANES)),
    (["tests/test_ci_workflows.py"], {"convention", "python"}),
    # --no-renames supplies both paths, including the removed runtime input.
    (["src/whisperstream/audio.py", "plans/audio.md"], {"convention", "python"}),
])
def test_selection(paths, expected):
    assert set(ci.select(paths)) == expected


def test_all_override():
    assert set(ci.select(["README.md"], full=True)) == set(ci.LANES)


@pytest.fixture
def pr():
    return {"head": {"sha": "abc", "ref": "chore/test"}, "base": {"sha": "def", "ref": "main"}, "title": "[chore] Fix it"}


def run(pr, **overrides):
    return dict(id=1, head_sha="abc", head_branch="chore/test", event="workflow_dispatch",
        path=".github/workflows/python-check.yml", display_title=ci.title("python", "def"),
        status="completed", conclusion="success", html_url="https://example.test/run", **overrides)


@pytest.mark.parametrize("field,value", [
    ("head_sha", "old"), ("head_branch", "other"), ("event", "push"),
    ("path", ".github/workflows/other.yml"), ("display_title", "python-check / old-base"),
])
def test_reject_wrong_evidence(pr, field, value):
    record = run(pr)
    record[field] = value
    assert ci.latest_run([record], "python", pr) is None


def test_latest_failed_run_supersedes_old_success(pr, monkeypatch):
    passed = run(pr)
    failed = {**passed, "id": 2, "conclusion": "failure"}
    assert ci.latest_run([passed, failed], "python", pr) == failed
    monkeypatch.setattr(ci, "get_run", lambda *a: failed)
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    with pytest.raises(ValueError, match="incomplete"):
        ci.verify("owner/repo", 1, pr, {"python": []})


@pytest.mark.parametrize("status,conclusion", [("queued", None), ("in_progress", None), ("completed", "cancelled"), ("completed", "skipped")])
def test_non_success_blocks_merge(pr, monkeypatch, status, conclusion):
    record = {**run(pr), "status": status, "conclusion": conclusion}
    monkeypatch.setattr(ci, "get_run", lambda *a: record)
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    with pytest.raises(ValueError, match="incomplete"):
        ci.verify("owner/repo", 1, pr, {"python": []})


def test_missing_evidence(pr, monkeypatch):
    monkeypatch.setattr(ci, "get_run", lambda *a: None)
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    with pytest.raises(ValueError, match="missing"):
        ci.verify("owner/repo", 1, pr, {"python": []})


@pytest.mark.parametrize("side", ["head", "base"])
def test_branch_moves_block_merge(pr, monkeypatch, side):
    current = {**pr, side: {**pr[side], "sha": "new"}}
    monkeypatch.setattr(ci, "context", lambda *a: current)
    with pytest.raises(ValueError, match="changed"):
        ci.unchanged("owner/repo", 1, pr)


def test_complete_success(pr, monkeypatch):
    monkeypatch.setattr(ci, "get_run", lambda *a: run(pr))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    ci.verify("owner/repo", 1, pr, {"python": []})


def test_success_is_reused_without_dispatch(pr, monkeypatch):
    monkeypatch.setattr(ci, "get_run", lambda *a: run(pr))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    monkeypatch.setattr(ci, "command", lambda *a: pytest.fail("must not dispatch"))
    ci.run_checks("owner/repo", 1, pr, {"python": []})


def test_failed_run_is_not_automatically_retried(pr, monkeypatch):
    failed = {**run(pr), "conclusion": "failure"}
    monkeypatch.setattr(ci, "get_run", lambda *a: failed)
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    monkeypatch.setattr(ci, "command", lambda *a: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="failed/cancelled"):
        ci.run_checks("owner/repo", 1, pr, {"python": []})


def test_dispatch_binds_head_and_base(pr, monkeypatch):
    calls = []
    records = iter([None, run(pr), run(pr)])
    monkeypatch.setattr(ci, "get_run", lambda *a: next(records))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    monkeypatch.setattr(ci, "command", lambda *a: calls.append(a))
    monkeypatch.setattr(ci.time, "sleep", lambda *a: None)
    monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0})())
    ci.run_checks("owner/repo", 1, pr, {"python": []})
    assert calls == [("gh", "workflow", "run", "python-check.yml", "--repo", "owner/repo",
        "--ref", "chore/test", "-f", "ci_head_sha=abc", "-f", "ci_base_sha=def")]


def test_plan_uses_complete_diff_and_requires_current_base(pr, monkeypatch):
    calls = []
    def command(*args):
        calls.append(args)
        if args[1] == "remote":
            return "https://github.com/owner/repo.git"
        return "src/whisperstream/audio.py\0plans/audio.md\0" if args[1] == "diff" else ""
    monkeypatch.setattr(ci, "command", command)
    assert set(ci.plan("owner/repo", pr, False)) == {"convention", "python"}
    assert calls[1] == ("git", "fetch", "origin", "abc", "def")
    assert calls[2] == ("git", "merge-base", "--is-ancestor", "def", "abc")
    assert calls[3] == ("git", "diff", "--name-only", "--no-renames", "-z", "def...abc")


def test_wrong_remote_blocks_fetch(pr, monkeypatch):
    monkeypatch.setattr(ci, "command", lambda *a: "https://github.com/other/repo.git")
    with pytest.raises(ValueError, match="origin must match"):
        ci.plan("owner/repo", pr, False)


@pytest.mark.parametrize("remote,allowed", [
    ("https://contributor@github.com/owner/repo.git", True),
    ("https://github.com/owner/repo", True),
    ("git@github.com:owner/repo.git", True),
    ("ssh://git@github.com/owner/repo.git", True),
    ("https://contributor:password@github.com/owner/repo", False),
    ("https://github.com.evil.invalid/owner/repo", False),
    ("https://github.com/other/repo", False),
    ("https://github.com/owner/repo?redirect=other", False),
    ("https://github.com/owner/repo#fragment", False),
    ("https://github.com:443/owner/repo", False),
])
def test_origin_identity_allows_only_scoped_transport(remote, allowed):
    assert ci.matches_origin(remote, "owner/repo") is allowed


def test_context_uses_live_refs_instead_of_stale_pr_snapshots(monkeypatch):
    calls = []
    def api(repo, path):
        calls.append(path)
        if path == "pulls/1":
            return {"state": "open",
                "head": {"ref": "chore/topic", "sha": "stale-head", "repo": {"full_name": repo}},
                "base": {"ref": "build_opensource", "sha": "stale-base"}}
        return {"object": {"sha": "live-head" if path.endswith("chore%2Ftopic") else "live-base"}}
    monkeypatch.setattr(ci, "api", api)
    current = ci.context("owner/repo", 1)
    assert ci.identity(current) == ("live-head", "live-base")
    assert calls == ["pulls/1", "git/ref/heads/chore%2Ftopic", "git/ref/heads/build_opensource"]


@pytest.mark.parametrize("state,head_repo", [("closed", {"full_name": "owner/repo"}), ("open", None), ("open", {"full_name": "fork/repo"})])
def test_context_rejects_ineligible_pr_before_resolving_refs(monkeypatch, state, head_repo):
    def api(repo, path):
        assert path == "pulls/1"
        return {"state": state, "head": {"repo": head_repo}}
    monkeypatch.setattr(ci, "api", api)
    with pytest.raises(ValueError, match="open PR"):
        ci.context("owner/repo", 1)


def test_merge_requires_evidence_and_passes_exact_head(pr, monkeypatch):
    pr.update(draft=False, mergeable=True)
    calls = []
    monkeypatch.setattr(ci.sys, "argv", ["merge_check.py", "merge", "--pr", "1"])
    monkeypatch.setattr(ci, "command", lambda *a: '{"nameWithOwner":"owner/repo"}')
    monkeypatch.setattr(ci, "context", lambda *a: pr)
    monkeypatch.setattr(ci, "plan", lambda *a: {"python": []})
    monkeypatch.setattr(ci, "get_run", lambda *a: run(pr))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    clean_merge(monkeypatch, calls)
    ci.main()
    assert calls == [MERGE_CALL]


MERGE_CALL = ["gh", "pr", "merge", "1", "--repo", "owner/repo", "--squash", "--match-head-commit", "abc",
    "--subject", "Fix it (#1)", "--body-file", "body"]


def clean_merge(monkeypatch, calls, errors=()):
    monkeypatch.setenv("MERGE_AUTHOR_NAME", "Publisher")
    monkeypatch.setenv("MERGE_AUTHOR_EMAIL", "pub@example.test")
    monkeypatch.setattr(ci, "squash_message", lambda *a: ("Fix it (#1)", "* Fix it"))
    monkeypatch.setattr(ci, "merged_commit_errors", lambda *a: list(errors))
    def record(args, **kwargs):
        if "--body-file" in args:
            index = args.index("--body-file") + 1
            assert ci.Path(args[index]).read_text(encoding="utf-8") == "* Fix it"
            args = [*args[:index], "body", *args[index + 1:]]
        calls.append(args)
    monkeypatch.setattr(ci.subprocess, "run", record)


def merge_main(pr, monkeypatch):
    pr.update(draft=False, mergeable=True)
    monkeypatch.setattr(ci.sys, "argv", ["merge_check.py", "merge", "--pr", "1"])
    monkeypatch.setattr(ci, "command", lambda *a: '{"nameWithOwner":"owner/repo"}')
    monkeypatch.setattr(ci, "context", lambda *a: pr)
    monkeypatch.setattr(ci, "plan", lambda *a: {"python": []})
    monkeypatch.setattr(ci, "get_run", lambda *a: run(pr))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)


def test_merge_requires_publishing_identity(pr, monkeypatch):
    merge_main(pr, monkeypatch)
    monkeypatch.delenv("MERGE_AUTHOR_EMAIL", raising=False)
    monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: pytest.fail("must not merge"))
    with pytest.raises(ValueError, match="publishing identity"):
        ci.main()


def test_merge_reports_unclean_squash_commit(pr, monkeypatch):
    merge_main(pr, monkeypatch)
    calls = []
    clean_merge(monkeypatch, calls, errors=["merge commit m message carries a blocked trailer"])
    with pytest.raises(ValueError, match="history correction"):
        ci.main()
    assert calls == [MERGE_CALL]


def test_merge_refuses_empty_squash_body(pr, monkeypatch):
    merge_main(pr, monkeypatch)
    calls = []
    clean_merge(monkeypatch, calls)
    monkeypatch.setattr(ci, "squash_message", lambda *a: ("Fix it (#1)", " \n"))
    with pytest.raises(ValueError, match="body is empty"):
        ci.main()
    assert calls == []


def test_squash_message_drops_merges_trailers_and_routing_tags(monkeypatch):
    commits = [
        {"parents": 1, "message": "Add the header\n\nBody line.\n\nCo-authored-by: Someone <s@example.test>"},
        {"parents": 2, "message": "Merge remote-tracking branch 'origin/main'"},
        {"parents": 1, "message": "Fix pin\n\nLocal-Trailer: value"},
    ]
    monkeypatch.setenv("MERGE_BLOCKED_PATTERNS", ci.json.dumps(["^local-trailer:"]))
    monkeypatch.setattr(ci, "command", lambda *a: "\n".join(ci.json.dumps(c) for c in commits))
    subject, body = ci.squash_message("owner/repo", 7, "[chore] Add the header")
    assert subject == "Add the header (#7)"
    assert body == "* Add the header\n\nBody line.\n\n* Fix pin"


def test_merged_commit_errors_check_author_and_trailers(monkeypatch):
    commit = {"author": {"name": "Publisher", "email": "pub@example.test"}, "message": "Fix (#1)\n\n* Fix"}
    monkeypatch.setattr(ci, "api", lambda repo, path: {"merge_commit_sha": "m"} if path.startswith("pulls") else {"commit": commit})
    assert ci.merged_commit_errors("owner/repo", 1, "Publisher", "pub@example.test") == []
    commit.update(author={"name": "Publisher", "email": "other@example.test"},
        message="Fix (#1)\n\nCo-authored-by: Publisher <pub@example.test>")
    assert len(ci.merged_commit_errors("owner/repo", 1, "Publisher", "pub@example.test")) == 2


def receipt(**overrides):
    record = {"version": ci.EVIDENCE_VERSION, "repo": "owner/repo", "pr": 1, "head": "abc", "base": "def",
        "helper_sha256": ci.HELPER_SHA256, "selection": ["convention", "python"],
        "lanes": {"convention": {"status": "pass"}, "python": {"status": "pass"}}}
    record.update(overrides)
    return record


LOCAL_LANES = {"convention": [], "python": []}


def test_local_receipt_accepts_complete_exact_evidence(pr):
    assert ci.receipt_errors(receipt(), "owner/repo", 1, pr, LOCAL_LANES) == []


@pytest.mark.parametrize("field,value", [
    ("head", "old"), ("base", "old-base"), ("repo", "other/repo"), ("pr", 2),
    ("helper_sha256", "edited-helper"), ("version", 0), ("selection", ["convention"]),
])
def test_local_receipt_rejects_mismatched_binding(pr, field, value):
    assert ci.receipt_errors(receipt(**{field: value}), "owner/repo", 1, pr, LOCAL_LANES)


@pytest.mark.parametrize("lanes", [{"convention": {"status": "pass"}},
    {"convention": {"status": "pass"}, "python": {"status": "fail"}}])
def test_local_receipt_rejects_missing_or_failed_lane(pr, lanes):
    errors = ci.receipt_errors(receipt(lanes=lanes), "owner/repo", 1, pr, LOCAL_LANES)
    assert errors == [f"python: {lanes.get('python', {}).get('status', 'missing')}"]


def test_missing_local_receipt_blocks_merge(pr, monkeypatch):
    monkeypatch.setattr(ci, "load_receipt", lambda *a: None)
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    with pytest.raises(ValueError, match="missing local receipt"):
        ci.verify_local("owner/repo", 1, pr, LOCAL_LANES)


@pytest.fixture
def local(pr, monkeypatch, tmp_path):
    """Run run_local against a fake evidence directory, git and gh."""
    calls = {"git": [], "comments": 0, "lanes": []}
    monkeypatch.setattr(ci, "evidence_dir", lambda *a: tmp_path / "evidence")
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    monkeypatch.setattr(ci, "tool_versions", lambda: {"os": "test"})
    monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: None)

    def command(*args):
        if args[:2] == ("gh", "pr"):
            calls["comments"] += 1
        else:
            calls["git"].append(args)
        return ""
    monkeypatch.setattr(ci, "command", command)
    return calls


def test_local_run_uses_exact_head_and_records_receipt(pr, local, monkeypatch, tmp_path):
    def run_lane(lane, *a):
        local["lanes"].append(lane)
        return {"status": "pass", "steps": [{"step": "x", "seconds": 1.0}]}
    monkeypatch.setattr(ci, "run_lane", run_lane)
    ci.run_local("owner/repo", 1, pr, LOCAL_LANES)
    assert local["git"][0][:5] == ("git", "worktree", "add", "--quiet", "--detach") and local["git"][0][-1] == "abc"
    assert local["lanes"] == ["convention", "python"] and local["comments"] == 1
    saved = ci.load_receipt("owner/repo", 1, pr)
    assert ci.receipt_errors(saved, "owner/repo", 1, pr, LOCAL_LANES) == [] and saved["commented"]
    # A repeated run reuses the passing lanes and does not comment again.
    ci.run_local("owner/repo", 1, pr, LOCAL_LANES)
    assert local["lanes"] == ["convention", "python"] and local["comments"] == 1


def test_local_failure_stops_and_blocks_without_comment(pr, local, monkeypatch):
    monkeypatch.setattr(ci, "run_lane", lambda lane, *a: local["lanes"].append(lane) or {"status": "fail", "steps": []})
    with pytest.raises(ValueError, match="convention: fail"):
        ci.run_local("owner/repo", 1, pr, LOCAL_LANES)
    assert local["lanes"] == ["convention"] and local["comments"] == 0


def test_local_evidence_from_another_helper_is_rerun(pr, local, monkeypatch):
    monkeypatch.setattr(ci, "run_lane", lambda lane, *a: local["lanes"].append(lane) or {"status": "pass", "steps": []})
    ci.run_local("owner/repo", 1, pr, LOCAL_LANES)
    monkeypatch.setattr(ci, "HELPER_SHA256", "changed")
    ci.run_local("owner/repo", 1, pr, LOCAL_LANES)
    assert local["lanes"] == ["convention", "python"] * 2 and local["comments"] == 2


def test_run_lane_stops_at_first_failed_step(pr, monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(ci, "local_steps", lambda *a: [
        ("ok", ".", [sys.executable, "-c", "print('ok')"]),
        ("fails", ".", [sys.executable, "-c", "raise SystemExit(3)"]),
        ("skipped", ".", [sys.executable, "-c", "print('never')"]),
    ])
    result = ci.run_lane("python", pr, tmp_path, tmp_path, tmp_path / "python.log")
    assert result["status"] == "fail"
    assert [(step["step"], step["exit"]) for step in result["steps"]] == [("ok", 0), ("fails", 3)]
    assert "never" not in (tmp_path / "python.log").read_text(encoding="utf-8")



def test_conventions_require_nonempty_notices(pr, tmp_path):
    pr["title"] = "untagged"
    (tmp_path / "LICENSE").write_text("x")
    (tmp_path / "NOTICE").write_text("")
    output = io.StringIO()
    assert ci.conventions(pr, tmp_path, tmp_path, output) == 1
    assert output.getvalue().splitlines() == ["WARNING: PR title does not start with [app], [site] or [chore].",
        "ERROR: LICENSES/Chirp-MIT.txt is missing or empty.", "ERROR: NOTICE is missing or empty.",
        "ERROR: AUTHORS.md is missing or empty."]


def test_convention_lane_runs_head_controls_with_workflow_source_base(pr, tmp_path):
    workflow = tmp_path / ".github/workflows/convention-check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("SOURCE_BASE: " + "b" * 40 + "\n")
    scratch = tmp_path / "scratch"
    steps = {label: args for label, _, args in ci.local_steps("convention", pr, tmp_path, scratch)}
    assert "controls pin" not in steps and steps["pr metadata"] is ci.pr_metadata
    audit = steps["publication audit"]
    assert audit[2:6] == [str(tmp_path / "scripts/source_policy.py"), "--repo", ".", "--policy"]
    assert audit[6] == str(tmp_path / "scripts/source-policy.json")
    assert audit[-6:] == ["--base", "def", "--head", "HEAD", "--source-base", "b" * 40]
    assert steps["metadata audit"][-2:] == ["--file", str(scratch / "pr-metadata.json")]
    workflow.write_text("SOURCE_BASE: unpinned\n")
    with pytest.raises(ValueError, match="SOURCE_BASE pin is missing"):
        ci.local_steps("convention", pr, tmp_path, scratch)


def test_pr_metadata_stages_title_body_and_branches(pr, tmp_path):
    pr["body"] = "Body"
    assert ci.pr_metadata(pr, tmp_path, tmp_path, io.StringIO()) == 0
    assert json.loads((tmp_path / "pr-metadata.json").read_text()) == [
        {"title": "[chore] Fix it", "body": "Body", "head": "chore/test", "base": "main"}]

def test_local_merge_uses_receipt_and_passes_exact_head(pr, monkeypatch):
    pr.update(draft=False, mergeable=True)
    calls = []
    monkeypatch.setattr(ci.sys, "argv", ["merge_check.py", "merge", "--pr", "1", "--local"])
    monkeypatch.setattr(ci, "command", lambda *a: '{"nameWithOwner":"owner/repo"}')
    monkeypatch.setattr(ci, "context", lambda *a: pr)
    monkeypatch.setattr(ci, "plan", lambda *a: LOCAL_LANES)
    monkeypatch.setattr(ci, "load_receipt", lambda *a: receipt())
    monkeypatch.setattr(ci, "evidence_dir", lambda *a: ci.Path("evidence"))
    monkeypatch.setattr(ci, "get_run", lambda *a: pytest.fail("local merge must not read Actions"))
    monkeypatch.setattr(ci, "unchanged", lambda *a: pr)
    clean_merge(monkeypatch, calls)
    ci.main()
    assert calls == [MERGE_CALL]


def test_lanes_do_not_inherit_github_tokens(pr, monkeypatch, tmp_path):
    import sys
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "1")
    monkeypatch.setattr(ci, "local_steps", lambda *a: [("env", ".", [sys.executable, "-c",
        "import os; raise SystemExit(bool({'GH_TOKEN', 'CI', 'GIT_NO_REPLACE_OBJECTS'} & set(os.environ)) or 'WHISPERSTREAM_DATA_DIR' not in os.environ)"])])
    assert ci.run_lane("python", pr, tmp_path, tmp_path, tmp_path / "python.log")["status"] == "pass"


@pytest.mark.parametrize(("platform", "builds_worker"), [("darwin", True), ("linux", False)])
def test_python_lane_builds_native_mac_worker_only_on_macos(pr, monkeypatch, tmp_path, platform, builds_worker):
    monkeypatch.setattr(ci.sys, "platform", platform)
    labels = [label for label, _cwd, _args in ci.local_steps("python", pr, tmp_path, tmp_path)]
    assert ("macos stt worker" in labels) is builds_worker
    assert labels[-1] == "pytest"
