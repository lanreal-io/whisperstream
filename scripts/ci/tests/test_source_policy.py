"""Synthetic publication boundary regressions; no customer or credential fixtures."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "source_policy.py"
spec = importlib.util.spec_from_file_location("source_policy", SCRIPT)
policy_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy_module
spec.loader.exec_module(policy_module)
POLICY = policy_module.load_policy(SCRIPT.with_name("source-policy.json"))


class PolicyTests(unittest.TestCase):
    def check(self, path, data=b"ordinary source\n", mode="100644"):
        return policy_module.check_entries([policy_module.Entry(path, mode, data)], POLICY)

    def test_public_code_and_docs_allowed(self):
        for path in ("src/whisperstream/new_module.py", "docs/development/guide.md", "tests/test_module.py", "LICENSES/New-MIT.txt"):
            with self.subTest(path=path):
                self.assertEqual(self.check(path), [])

    def test_retired_campaign_probes_cannot_reenter(self):
        for path in ("src/whisperstream/macos_phase4_probe.py", "src-tauri/src/macos_phase4_probe.rs", "src-tauri/examples/conpty_diag.rs"):
            self.assertIn("excluded-path", {row["rule"] for row in self.check(path)})
        for path in ("src/whisperstream/macos_protected_storage_probe.py", "src/whisperstream/windows_protected_storage_probe.py", "tests/windows_context_probe.py"):
            self.assertEqual(self.check(path), [])

    def test_private_categories_rejected_case_insensitively(self):
        for path in ("Plans/note.md", "docs/tasks/a.md", ".agents/skills/x/SKILL.md", "site/a.ts", "docs/CLAUDE.md", "tests/fixture_studio.py", "docs/billing-export.csv", "scripts/tool.zip"):
            with self.subTest(path=path):
                self.assertIn("excluded-category", {x["rule"] for x in self.check(path)})

    def test_runtime_skills_are_allowed(self):
        for path in POLICY["allowed_skill_paths"]:
            self.assertEqual(self.check(path), [])

    def test_unknown_skill_needs_review(self):
        self.assertIn("unclassified-agent-skill", {x["rule"] for x in self.check("src/new/SKILL.md")})

    def test_unknown_root_needs_review(self):
        self.assertEqual(self.check("new-category/example.py")[0]["severity"], "review")

    def test_unsafe_paths(self):
        for path in ("../private.txt", "/absolute.txt", "docs\\file.txt", "docs/a:b.txt", "docs/a. /x.md", "docs/\uff30lans/x.md", "docs/x\n.md", "docs/CON.txt", "src/lpt1.py"):
            self.assertEqual(self.check(path)[0]["rule"], "unsafe-path")

    def test_case_collision(self):
        rows = [policy_module.Entry(p, "100644", b"a") for p in ("docs/A.md", "docs/a.md")]
        self.assertIn("case-or-unicode-collision", {x["rule"] for x in policy_module.check_entries(rows, POLICY)})

    def test_symlink_escape_and_missing_target(self):
        for target in (b"../../outside", b"C:/outside", b"missing.md"):
            self.assertIn("unsafe-symlink", {x["rule"] for x in self.check("docs/link.md", target, "120000")})

    def test_safe_symlink(self):
        rows = [policy_module.Entry("docs/link.md", "120000", b"guide.md"), policy_module.Entry("docs/guide.md", "100644", b"guide")]
        self.assertEqual(policy_module.check_entries(rows, POLICY), [])

    def test_submodule_and_lfs_require_review(self):
        self.assertIn("external-submodule", {x["rule"] for x in self.check("src/module", b"", "160000")})
        self.assertIn("external-lfs-object", {x["rule"] for x in self.check("src/asset.txt", b"version https://git-lfs.github.com/spec/v1\n")})

    def test_binary_pin_is_exact_path_and_content(self):
        p = copy.deepcopy(POLICY); data = b"\xff\x00opaque"
        p["binary_assets"].append({"path": "src/asset.bin", "sha256": policy_module.digest(data)})
        self.assertEqual(policy_module.check_entries([policy_module.Entry("src/asset.bin", "100644", data)], p), [])
        for path, value in (("src/other.bin", data), ("src/asset.bin", data + b"x")):
            self.assertEqual(policy_module.check_entries([policy_module.Entry(path, "100644", value)], p)[0]["rule"], "unreviewed-binary")

    def generated_case(self, pinned_path="docs/guide.pdf", data=b"%PDF-1.7\n\xff\x00generated", pins=None):
        p = copy.deepcopy(POLICY)
        p["generated_assets"] = {"pins_file": "scripts/generated-assets.json", "paths": [{"path": "docs/guide.pdf", "signature": "%PDF-"}]}
        pins = pins if pins is not None else json.dumps({"version": 1, "assets": [{"path": pinned_path, "sha256": policy_module.digest(data)}]}).encode()
        return p, [policy_module.Entry("docs/guide.pdf", "100644", data), policy_module.Entry("scripts/generated-assets.json", "100644", pins)]

    def test_shipped_policy_declares_generated_pdfs_outside_trusted_pins(self):
        declared = {row["path"] for row in POLICY["generated_assets"]["paths"]}
        self.assertIn("frontend/src/assets/documentation/Whisperstream-Documentation.pdf", declared)
        self.assertFalse(declared & {row["path"] for row in POLICY["binary_assets"]})
        self.assertNotIn(POLICY["generated_assets"]["pins_file"], ("scripts/source_policy.py", "scripts/source-policy.json", "scripts/source_controls.py"))

    def test_generated_output_accepted_from_ordinary_pins_file(self):
        p, items = self.generated_case()
        self.assertEqual(policy_module.check_entries(items, p), [])

    def test_generated_output_needs_matching_ordinary_pin(self):
        p, items = self.generated_case()
        changed = [policy_module.Entry("docs/guide.pdf", "100644", items[0].data + b"x"), items[1]]
        self.assertEqual({x["rule"] for x in policy_module.check_entries(changed, p)}, {"unreviewed-binary"})
        self.assertEqual({x["rule"] for x in policy_module.check_entries(items[:1], p)}, {"unreviewed-binary"})

    def test_generated_pin_requires_declared_signature(self):
        p, items = self.generated_case(data=b"\xff\x00not a pdf")
        self.assertEqual({x["rule"] for x in policy_module.check_entries(items, p)}, {"unreviewed-binary"})

    def test_ordinary_pins_file_cannot_pin_undeclared_paths(self):
        data = b"\xff\x00opaque"
        p, _ = self.generated_case()
        pins = json.dumps({"version": 1, "assets": [{"path": "src/asset.bin", "sha256": policy_module.digest(data)}]}).encode()
        items = [policy_module.Entry("src/asset.bin", "100644", data), policy_module.Entry("scripts/generated-assets.json", "100644", pins)]
        self.assertEqual({x["rule"] for x in policy_module.check_entries(items, p)}, {"unreviewed-binary", "undeclared-generated-asset-pin"})

    def test_invalid_pins_file_is_a_violation(self):
        bad_hash = json.dumps({"version": 1, "assets": [{"path": "docs/guide.pdf", "sha256": "ABC"}]}).encode()
        data = b"%PDF-1.7\n\xff\x00generated"
        duplicate = json.dumps({"version": 1, "assets": [{"path": "docs/guide.pdf", "sha256": policy_module.digest(data)},
                                                         {"path": "docs/guide.pdf", "sha256": policy_module.digest(b"%PDF-old")}]}).encode()
        for pins in (b"not json", b"[]", b'{"version": 2, "assets": []}', bad_hash, duplicate):
            with self.subTest(pins=pins[:20]):
                p, items = self.generated_case(pins=pins)
                self.assertEqual({x["rule"] for x in policy_module.check_entries(items, p)}, {"unreviewed-binary", "invalid-generated-asset-pins"})
        p, items = self.generated_case()
        linked = [items[0], policy_module.Entry("scripts/generated-assets.json", "120000", b"elsewhere.json"),
                  policy_module.Entry("scripts/elsewhere.json", "100644", items[1].data)]
        self.assertIn("invalid-generated-asset-pins", {x["rule"] for x in policy_module.check_entries(linked, p)})

    def test_malformed_generated_declaration_rejected(self):
        for generated in ({"pins_file": "x.json", "paths": []},
                          {"pins_file": "x.json", "paths": [{"path": "a.pdf", "signature": ""}]},
                          {"paths": [{"path": "a.pdf", "signature": "%PDF-"}]},
                          {"pins_file": "scripts/source-policy.json", "paths": [{"path": "a.pdf", "signature": "%PDF-"}]},
                          {"pins_file": "x.json", "paths": [{"path": POLICY["binary_assets"][0]["path"], "signature": "%PDF-"}]}):
            with self.subTest(generated=generated), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(POLICY); value["generated_assets"] = generated
                path = Path(temporary) / "policy.json"; path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(policy_module.AuditError):
                    policy_module.load_policy(path)

    def test_content_detection_has_no_test_exemption(self):
        private_url = "https://github.com/example/" + "application-" + "private/commit/example"
        for path in ("src/a.py", "tests/test_x.py", "docs/a.md"):
            self.assertIn("private-repository-reference", {x["rule"] for x in self.check(path, private_url.encode())})

    def test_metadata_checked(self):
        value = ("https://github.com/example/" + "application-" + "private/pull/1").encode()
        self.assertTrue(policy_module.check_metadata(value, POLICY))

    def test_private_reference_in_exact_json_value_and_uppercase_url(self):
        url = "https://github.com/example/" + "application-" + "private"
        for value in (json.dumps({"body": url}), json.dumps({"nested": [url.upper()]}), "[reference](" + url + ")", "`" + url + "`"):
            with self.subTest(format=value[:12]):
                self.assertTrue(policy_module.check_metadata(value.encode(), POLICY))

    def test_private_reference_rule_does_not_match_longer_public_name(self):
        url = "https://github.com/example/" + "application-" + "private-tools"
        self.assertEqual(policy_module.check_metadata(json.dumps({"body": url}).encode(), POLICY), [])

    def test_metadata_decodes_escaped_json_values_and_keys(self):
        url = "https://github.com/example/" + "application-" + "private"
        for value in ({"body": "prefix\n" + url}, {url: ["ordinary value"]}, {"nested": [{"link": url}]}):
            raw = json.dumps(value).replace("github", "\\u0067ithub").encode()
            self.assertNotIn(b"github", raw)
            self.assertTrue(policy_module.check_metadata(raw, POLICY))

    def test_metadata_preserves_raw_and_recursively_decoded_text(self):
        raw = b'{"nested": [{"\\u006bey": "first\\nsecond"}]}'
        expanded = policy_module.expand_metadata(raw)
        self.assertTrue(expanded.startswith(raw + b"\n"))
        self.assertIn(b"key\nfirst\nsecond", expanded)
        self.assertEqual(policy_module.expand_metadata(b"ordinary prose"), b"ordinary prose")

    def test_metadata_cli_secret_scan_receives_decoded_strings(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.json"
            raw = b'{"nested": [{"value": "synthetic\\n\\u0073canner-marker"}]}'
            metadata.write_bytes(raw)
            scanned = []
            def scanner(items, policy):
                scanned.extend(items)
                return []
            with patch.object(policy_module, "secret_scan", side_effect=scanner), patch("builtins.print"):
                result = policy_module.main(["metadata", "--file", str(metadata)])
            self.assertEqual(result, 0)
            self.assertEqual(len(scanned), 1)
            self.assertIn(b"synthetic\nscanner-marker", scanned[0].data)
            self.assertTrue(scanned[0].data.startswith(raw))

    def test_filename_content_checked(self):
        path = "docs/https://github.com/example/" + "application-" + "private/notes.md"
        self.assertIn("private-repository-reference", {x["rule"] for x in self.check(path)})

    def test_personal_paths(self):
        value = ("C:" + "/Users/" + "ExamplePerson/" + "Documents/project").encode()
        self.assertIn("personal-machine-path", {x["rule"] for x in self.check("docs/a.md", value)})

    def test_retired_update_and_mirror_references_cannot_reenter(self):
        rule = "retired-update-or-mirror-reference"
        values = (
            "https://" + "releases." + "whisperstream.io/opensource/latest.json",
            "https://" + "MODELS." + "whisperstream.io/manifest.json",
            '"@tauri-apps/plugin-' + 'updater": "^2.10.0"',
            "tauri-plugin-" + "updater = \"2\"",
            ".plugin(tauri_plugin_" + "updater::Builder::new().build())",
        )
        for value in values:
            for path in ("src/a.py", "tests/test_x.py", "docs/a.md"):
                with self.subTest(value=value[:24], path=path):
                    self.assertIn(rule, {x["rule"] for x in self.check(path, value.encode())})
        self.assertTrue(policy_module.check_metadata(("Restore " + "plugin-" + "updater").encode(), POLICY))
        for value in ("https://whisperstream.io/docs", "https://github.com/lanreal-io/whisperstream", "app_directory updater.exe stub"):
            with self.subTest(allowed=value):
                self.assertNotIn(rule, {x["rule"] for x in self.check("docs/a.md", value.encode())})
        policy_text = SCRIPT.with_name("source-policy.json").read_bytes()
        self.assertNotIn(rule, {x["rule"] for x in self.check("scripts/source-policy.json", policy_text)})

    def test_safe_diagnostics(self):
        personal = "Plans/" + "sensitive-customer-name.md"
        findings = self.check(personal)
        self.assertNotIn(personal, json.dumps(findings))
        self.assertNotIn("sensitive-customer", json.dumps(findings))

    def test_scanner_missing_fails_closed(self):
        with patch.object(policy_module.shutil, "which", return_value=None):
            with self.assertRaises(policy_module.AuditError):
                policy_module.secret_scan([], POLICY)

    def test_scanner_version_mismatch_fails_closed(self):
        with patch.object(policy_module.shutil, "which", return_value="scanner"), patch.object(policy_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "0.0.0", "")):
            with self.assertRaises(policy_module.AuditError):
                policy_module.secret_scan([], POLICY)

    def test_secret_finding_redacted_and_exception_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Simulate scanner protocol, never use a real secret fixture.
            def scanner(args, **kwargs):
                if args[1] == "version":
                    return subprocess.CompletedProcess(args, 0, POLICY["gitleaks_version"], "")
                report = Path(args[args.index("--report-path") + 1])
                report.write_text(json.dumps([{"File": "blob-0.txt", "RuleID": "synthetic-rule", "StartLine": 1, "Secret": "REDACTED"}]), encoding="utf-8")
                return subprocess.CompletedProcess(args, 10, b"", b"")
            entry = policy_module.Entry("docs/generated.json", "100644", b"synthetic checksum")
            p = copy.deepcopy(POLICY)
            with patch.object(policy_module.shutil, "which", return_value="scanner"), patch.object(policy_module.subprocess, "run", side_effect=scanner):
                self.assertEqual(policy_module.secret_scan([entry], p)[0]["rule"], "credential-detection")
                p["secret_exceptions"].append({"path": entry.path, "sha256": policy_module.digest(entry.data), "rule": "synthetic-rule", "line": 1})
                self.assertEqual(policy_module.secret_scan([entry], p), [])
                changed = policy_module.Entry(entry.path, entry.mode, entry.data + b" changed")
                self.assertTrue(policy_module.secret_scan([changed], p))

    def test_scanner_error_does_not_expose_output(self):
        responses = [subprocess.CompletedProcess([], 0, POLICY["gitleaks_version"], ""), subprocess.CompletedProcess([], 3, b"sensitive-output", b"sensitive-error")]
        with patch.object(policy_module.shutil, "which", return_value="scanner"), patch.object(policy_module.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(policy_module.AuditError, "Secret scanner failed") as caught:
                policy_module.secret_scan([], POLICY)
            self.assertNotIn("sensitive", str(caught.exception))

    def test_scanner_success_without_report_fails_closed(self):
        responses = [subprocess.CompletedProcess([], 0, POLICY["gitleaks_version"], ""), subprocess.CompletedProcess([], 0, b"", b"")]
        with patch.object(policy_module.shutil, "which", return_value="scanner"), patch.object(policy_module.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(policy_module.AuditError, "invalid report"):
                policy_module.secret_scan([], POLICY)


class GitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic Contributor")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write("README.md", "Public source\n")
        self.base = self.commit("Initial source")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_staged_bytes_not_worktree(self):
        self.write("README.md", "https://github.com/example/" + "application-" + "private/pull/1")
        self.git("add", "README.md")
        self.write("README.md", "Safe unstaged content")
        self.assertTrue(policy_module.audit_staged(self.repo, POLICY))

    def test_unstaged_private_bytes_do_not_override_safe_index(self):
        self.write("README.md", "https://github.com/example/" + "application-" + "private/pull/1")
        self.assertEqual(policy_module.audit_staged(self.repo, POLICY), [])

    def test_explicit_commit_index_is_honored(self):
        alternate = self.repo / ".git" / "alternate-index"
        with patch.dict(policy_module.os.environ, {"GIT_INDEX_FILE": str(alternate)}):
            self.git("read-tree", "HEAD")
            self.write("plans/note.md", "context")
            self.git("add", "plans/note.md")
            self.assertTrue(policy_module.audit_staged(self.repo, POLICY))
        self.assertEqual(policy_module.audit_staged(self.repo, POLICY), [])

    def test_option_like_revision_rejected(self):
        with self.assertRaises(policy_module.AuditError):
            policy_module.resolve(self.repo, "--all")

    def test_added_then_deleted_private_file_is_rejected(self):
        self.write("plans/note.md", "private context")
        self.commit("Add context")
        self.git("rm", "plans/note.md")
        head = self.commit("Remove context")
        self.assertEqual(policy_module.audit_tree(self.repo, head, POLICY), [])
        self.assertTrue(policy_module.audit_range(self.repo, self.base, head, POLICY))

    def test_existing_remote_file_removed_after_unchanged_intermediate_passes(self):
        self.write("plans/note.md", "already hosted preparation context")
        base = self.commit("Existing preparation baseline")
        self.write("src/app.py", "public = True")
        self.commit("Ordinary change before cleanup")
        self.git("rm", "plans/note.md")
        head = self.commit("Remove preparation context")
        self.assertEqual(policy_module.audit_range(self.repo, base, head, POLICY), [])

    def test_existing_file_deleted_readded_deleted_is_rejected(self):
        self.write("plans/note.md", "already hosted preparation context")
        base = self.commit("Existing preparation baseline")
        self.git("rm", "plans/note.md")
        self.commit("Remove preparation context")
        self.write("plans/note.md", "already hosted preparation context")
        self.commit("Accidental reintroduction")
        self.git("rm", "plans/note.md")
        head = self.commit("Remove again")
        self.assertTrue(policy_module.audit_range(self.repo, base, head, POLICY))

    def test_unchanged_forbidden_file_in_final_head_is_rejected(self):
        self.write("plans/note.md", "already hosted preparation context")
        base = self.commit("Existing preparation baseline")
        self.write("src/app.py", "public = True")
        head = self.commit("Ordinary change")
        self.assertTrue(policy_module.audit_range(self.repo, base, head, POLICY))
        self.assertTrue(policy_module.audit_range(self.repo, head, head, POLICY))

    def test_changed_existing_forbidden_file_then_deleted_is_rejected(self):
        self.write("plans/note.md", "already hosted preparation context")
        base = self.commit("Existing preparation baseline")
        self.write("plans/note.md", "new private context")
        self.commit("Modify preparation context")
        self.git("rm", "plans/note.md")
        head = self.commit("Remove context")
        self.assertTrue(policy_module.audit_range(self.repo, base, head, POLICY))

    def test_range_cli_scans_complete_final_tree_for_secrets(self):
        self.write("README.md", "synthetic scanner marker")
        base = self.commit("Existing remote baseline")
        self.write("src/app.py", "public = True")
        head = self.commit("Ordinary change")
        scanned = []
        def scanner(items, policy):
            scanned.extend(items)
            return [policy_module.finding("credential-detection", row.path) for row in items if row.data == b"synthetic scanner marker"]
        with patch.object(policy_module, "secret_scan", side_effect=scanner), patch("builtins.print"):
            result = policy_module.main(["--repo", str(self.repo), "range", "--base", base, "--head", head])
        self.assertEqual(result, 1)
        self.assertTrue(any(row.path == "README.md" for row in scanned))

    def test_range_cli_does_not_rescan_unchanged_removed_remote_blob(self):
        self.write("plans/note.md", "already hosted preparation context")
        base = self.commit("Existing remote preparation baseline")
        self.write("src/app.py", "public = True")
        self.commit("Ordinary change")
        self.git("rm", "plans/note.md")
        head = self.commit("Remove preparation context")
        scanned = []
        def scanner(items, policy):
            scanned.extend(items)
            return []
        with patch.object(policy_module, "secret_scan", side_effect=scanner), patch("builtins.print"):
            result = policy_module.main(["--repo", str(self.repo), "range", "--base", base, "--head", head])
        self.assertEqual(result, 0)
        self.assertFalse(any(row.path == "plans/note.md" for row in scanned))

    def test_commit_replacement_cannot_hide_original_forbidden_file(self):
        self.write("plans/note.md", "private context")
        dirty = self.commit("Unpublished context")
        self.git("replace", dirty, self.base)
        # Ordinary Git sees the clean replacement; the audit must see the object
        # that would actually be transmitted by a push of the original commit.
        self.assertNotIn("plans/note.md", self.git("ls-tree", "-r", "--name-only", dirty))
        self.assertTrue(policy_module.audit_tree(self.repo, dirty, POLICY))
        self.assertTrue(policy_module.audit_range(self.repo, self.base, dirty, POLICY))

    def test_blob_replacement_cannot_hide_original_content(self):
        clean_blob = self.git("rev-parse", "HEAD:README.md")
        self.write("README.md", "https://github.com/example/" + "application-" + "private/pull/1")
        dirty = self.commit("Update readme")
        dirty_blob = self.git("rev-parse", dirty + ":README.md")
        self.git("replace", dirty_blob, clean_blob)
        self.assertEqual(self.git("show", dirty + ":README.md"), "Public source")
        self.assertTrue(policy_module.audit_tree(self.repo, dirty, POLICY))
        self.assertTrue(policy_module.audit_staged(self.repo, POLICY))

    def test_shared_graft_file_blocks_linked_worktree_audit(self):
        linked = self.repo / "linked"
        self.git("worktree", "add", "-b", "linked-source", str(linked), self.base)
        graft = self.repo / ".git" / "info" / "grafts"
        graft.parent.mkdir(exist_ok=True)
        graft.write_text(self.base + "\n", encoding="utf-8")
        with self.assertRaisesRegex(policy_module.AuditError, "Git grafts"):
            policy_module.audit_tree(linked, "HEAD", POLICY)

    def test_history_override_environment_fails_closed(self):
        for name in ("GIT_GRAFT_FILE", "GIT_SHALLOW_FILE"):
            with patch.dict(policy_module.os.environ, {name: "untrusted-history"}):
                with self.assertRaisesRegex(policy_module.AuditError, "history overrides"):
                    policy_module.audit_range(self.repo, self.base, "HEAD", POLICY)

    def test_replacement_environment_cannot_reenable_substitution(self):
        with patch.dict(policy_module.os.environ, {"GIT_NO_REPLACE_OBJECTS": "0", "GIT_REPLACE_REF_BASE": "refs/custom-replacement/"}):
            env = policy_module.safe_env()
            self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertNotIn("GIT_REPLACE_REF_BASE", env)

    def test_private_merge_parent_rejected(self):
        self.write("src/module.py", "public = True")
        source = self.commit("Advance approved source")
        self.git("checkout", "-b", "private-side", self.base)
        self.write("docs/private-branch.md", "clean looking but wrong ancestry")
        foreign = self.commit("Foreign history")
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "-m", "Merge foreign history", foreign)
        with self.assertRaises(policy_module.AuditError):
            policy_module.audit_range(self.repo, source, "HEAD", POLICY)

    def test_routine_source_target_merge_allowed(self):
        self.git("checkout", "-b", "feature")
        self.write("src/feature.py", "feature = True")
        feature = self.commit("Feature")
        self.git("checkout", "main")
        self.write("src/target.py", "target = True")
        source = self.commit("Advance source target")
        self.git("checkout", "feature")
        self.git("merge", "--no-ff", "-m", "Merge source target", source)
        self.assertEqual(policy_module.audit_range(self.repo, feature, "HEAD", POLICY, source), [])

    def test_existing_feature_range_allowed(self):
        self.write("src/a.py", "a = True")
        old = self.commit("First public feature commit")
        self.write("src/b.py", "b = True")
        head = self.commit("Next public feature commit")
        self.assertEqual(policy_module.audit_range(self.repo, old, head, POLICY, self.base), [])

    def test_export_uses_same_policy(self):
        export = self.repo / "export"
        export.mkdir()
        (export / "README.md").write_text("Public", encoding="utf-8")
        self.assertEqual(policy_module.audit_export(export, POLICY), [])
        (export / "CLAUDE.md").write_text("Internal", encoding="utf-8")
        self.assertTrue(policy_module.audit_export(export, POLICY))

    def test_invalid_policy_fails_closed(self):
        path = self.repo / "invalid.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        with self.assertRaises(policy_module.AuditError):
            policy_module.load_policy(path)


if __name__ == "__main__":
    unittest.main()
