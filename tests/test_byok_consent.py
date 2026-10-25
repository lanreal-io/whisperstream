"""Unit tests for byok_consent append-only consent log.

Covers the required consent fields
(provider, consent_given_at, privacy_policy_version, eula_version,
software_version, dialog_content_hash) plus the remove-provider behaviour
that underpins the "Remove key" control in the Enhancement tab.
"""

from __future__ import annotations

import json
import tempfile
import unittest

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


class BYOKConsentTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tempdir.name)
        self._patcher = mock.patch(
            "whisperstream.byok_consent.get_app_dir",
            return_value=tmp_path,
        )
        self._patcher.start()
        # Import after patching so get_consent_path() sees the temp dir.
        from whisperstream import byok_consent
        self.byok_consent = byok_consent

    def tearDown(self):
        self._patcher.stop()
        self._tempdir.cleanup()

    def test_record_consent_writes_all_six_fields(self):
        record = self.byok_consent.record_consent(
            "openai",
            "Privacy Notice\nOpenAI\n...",
        )
        self.assertEqual(record["provider"], "openai")
        self.assertIn("consent_given_at", record)
        from whisperstream.consent_disclosure import legal_resource_identity
        identity = legal_resource_identity()
        self.assertEqual(
            record["privacy_policy_version"],
            identity["privacy_policy_version"],
        )
        self.assertEqual(record["eula_version"], identity["eula_version"])
        self.assertIn("software_version", record)
        self.assertEqual(len(record["dialog_content_hash"]), 64)  # sha256 hex

    def test_dialog_content_hash_is_sha256_of_body_text(self):
        body = "Privacy Notice\nOpenAI\nsome body"
        record = self.byok_consent.record_consent("openai", body)
        self.assertEqual(
            record["dialog_content_hash"],
            self.byok_consent.hash_dialog_content(body),
        )

    def test_consent_given_at_is_iso8601_utc(self):
        fixed = datetime(2026, 4, 22, 15, 30, 0, tzinfo=timezone.utc)
        record = self.byok_consent.record_consent("openai", "body", now=fixed)
        self.assertEqual(record["consent_given_at"], "2026-04-22T15:30:00+00:00")

    def test_record_consent_is_append_only(self):
        self.byok_consent.record_consent("openai", "body one")
        self.byok_consent.record_consent("openai", "body two")
        records = self.byok_consent.list_records()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(
            records[0]["dialog_content_hash"], records[1]["dialog_content_hash"]
        )

    def test_has_consent_reflects_any_record_for_provider(self):
        self.assertFalse(self.byok_consent.has_consent("openai"))
        self.byok_consent.record_consent("openai", "body")
        self.assertTrue(self.byok_consent.has_consent("openai"))
        self.assertFalse(self.byok_consent.has_consent("anthropic"))

    def test_remove_provider_deletes_every_record_for_that_provider(self):
        self.byok_consent.record_consent("openai", "a")
        self.byok_consent.record_consent("openai", "b")
        self.byok_consent.record_consent("anthropic", "c")
        removed = self.byok_consent.remove_provider("openai")
        self.assertEqual(removed, 2)
        self.assertFalse(self.byok_consent.has_consent("openai"))
        self.assertTrue(self.byok_consent.has_consent("anthropic"))

    def test_missing_file_behaves_as_empty_log(self):
        self.assertEqual(self.byok_consent.list_records(), [])
        self.assertEqual(self.byok_consent.acknowledged_providers(), [])
        self.assertFalse(self.byok_consent.has_consent("openai"))

    def test_corrupt_file_is_treated_as_empty(self):
        path = self.byok_consent.get_consent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(self.byok_consent.list_records(), [])

    def test_file_written_as_indented_json_list(self):
        self.byok_consent.record_consent("openai", "body")
        raw = self.byok_consent.get_consent_path().read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, list)
        self.assertIn("\n", raw)  # indented, not one-line

    def test_acknowledged_providers_deduplicates(self):
        self.byok_consent.record_consent("openai", "a")
        self.byok_consent.record_consent("openai", "b")
        self.byok_consent.record_consent("anthropic", "c")
        self.assertEqual(
            self.byok_consent.acknowledged_providers(), ["openai", "anthropic"]
        )


class TestBYOKConsentHTTPEndpoint(unittest.TestCase):
    """HTTP-level smoke tests for POST /api/byok-consent via TestClient.

    Verifies that provider allow-list changes in server.py actually accept
    (or reject) a given provider string.
    """

    def _make_client(self):
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from whisperstream.server import WhisperstreamServer

        mock_app = MagicMock()
        from whisperstream.config_manager import WhisperstreamConfig
        mock_app.config = WhisperstreamConfig(enhancement_custom_base_url="https://synthetic.invalid/v1", enhancement_custom_model="synthetic")
        mock_app.stt_engine = MagicMock()
        mock_app.config_manager = MagicMock()
        mock_app.config_manager.save = MagicMock()
        mock_app.reload_config = MagicMock()
        mock_app.license_manager = MagicMock()
        mock_app.license_manager.status.return_value = {
            "is_pro": True,
            "trial_remaining_seconds": 0,
        }
        mock_app.license_manager.maybe_revalidate = AsyncMock(return_value=None)
        mock_app.load_stt_engine_async = MagicMock()
        mock_app.set_event_broadcaster = MagicMock()
        mock_app.llama_server = None
        server = WhisperstreamServer(
            mock_app, port=0, logger=None, register_hotkeys=False
        )
        return TestClient(server.fastapi, raise_server_exceptions=True)

    def test_custom_provider_byok_consent_returns_consent_record(self):
        """POST /api/byok-consent with provider='custom' must return a
        consent record (not 400).  Previously rejected because 'custom' was
        missing from the server allow-list."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch(
                "whisperstream.byok_consent.get_app_dir",
                return_value=tmp_path,
            ):
                client = self._make_client()
                context = client.post("/api/byok-consent/context", json={"provider": "custom"}).json()
                resp = client.post(
                    "/api/byok-consent",
                    json={"provider": "custom", "dialog_body_text": context["dialog_body_text"], "context_token": context["context_token"]},
                )
        self.assertEqual(resp.status_code, 200)
        record = resp.json()
        self.assertEqual(record["provider"], "custom")
        self.assertIn("consent_given_at", record)
        self.assertIn("dialog_content_hash", record)

    def test_unknown_provider_byok_consent_still_returns_400(self):
        """An unrecognised provider string must still be rejected."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch(
                "whisperstream.byok_consent.get_app_dir",
                return_value=tmp_path,
            ):
                client = self._make_client()
                resp = client.post(
                    "/api/byok-consent",
                    json={
                        "provider": "unknown_xyz",
                        "dialog_body_text": "some text",
                    },
                )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("system,expected,absent", [
    ("Darwin", "macOS Keychain", "Windows user account"),
    ("Windows", "Windows user account", "macOS Keychain"),
])
def test_consent_explains_current_platform_secret_storage(monkeypatch, system, expected, absent):
    from whisperstream import consent_disclosure
    from whisperstream.enhancement_security import ConsentTarget
    monkeypatch.setattr(consent_disclosure.platform, "system", lambda: system)
    text = "\n".join(consent_disclosure.disclosure_lines(
        ConsentTarget("openai", "https://api.openai.com/v1", "synthetic-model")
    ))
    assert expected in text
    assert absent not in text
