"""The automatic GitHub star prompt: session accounting and the Lockdown gate."""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from whisperstream.main import WhisperstreamApp
from tests.test_eula_server_boundary import ACCEPTED, _make_server
from tests.test_onboarding_enhancement_result import RAW, _enhancer, _make_app as _pipeline_app


def _app(*, managed=False, voluntary=False):
    app = object.__new__(WhisperstreamApp)
    app._require_acceptance = lambda *a, **k: True
    app.managed_lockdown_policy = SimpleNamespace(enabled=managed)
    app.config = SimpleNamespace(lockdown_mode=voluntary)
    app.review_state = MagicMock()
    app.review_state.should_show_prompt.return_value = True
    app._broadcast = MagicMock()
    return app


def test_eligible_prompt_broadcasts_without_license_state(tmp_path):
    app = _app()
    with patch("whisperstream.main.get_app_dir", return_value=tmp_path):
        app._check_review_prompt()
    app._broadcast.assert_called_once_with({"type": "review.prompt"})
    app.review_state.record_prompt_shown.assert_called_once_with()


def test_ineligible_prompt_is_not_broadcast_or_counted(tmp_path):
    app = _app()
    app.review_state.should_show_prompt.return_value = False
    with patch("whisperstream.main.get_app_dir", return_value=tmp_path):
        app._check_review_prompt()
    app._broadcast.assert_not_called()
    app.review_state.record_prompt_shown.assert_not_called()


@pytest.mark.parametrize("managed,voluntary,envelope", [(True, False, False), (False, True, False), (False, False, True)])
def test_lockdown_suppresses_prompt(tmp_path, managed, voluntary, envelope):
    app = _app(managed=managed, voluntary=voluntary)
    with patch("whisperstream.main.get_app_dir", return_value=tmp_path), \
         patch("whisperstream.main.lockdown_keystore.is_lockdown_enabled", return_value=envelope):
        app._check_review_prompt()
    app._broadcast.assert_not_called()
    app.review_state.should_show_prompt.assert_not_called()
    app.review_state.record_prompt_shown.assert_not_called()


@pytest.mark.usefixtures("accepted_eula")
def test_completed_dictation_counts_one_session():
    app = _pipeline_app(inject_suppressed=False)
    app.llm_enhancer = _enhancer()
    app._enhance_then_inject(RAW, 1.0)
    app.review_state.record_session.assert_called_once_with()


@pytest.mark.parametrize("reason,method", [
    ("later", "record_later"),
    ("not-now", "record_dismissal"),
    ("never", "record_permanent_dismissal"),
    ("github", "record_submission"),
])
def test_prompt_outcomes_record_local_state_only(reason, method):
    server, app, _ = _make_server(ACCEPTED)
    response = TestClient(server.fastapi).post("/api/review/dismiss", json={"reason": reason})
    assert response.status_code == 200
    getattr(app.review_state, method).assert_called_once_with()


def test_prompt_handoffs_are_exact_and_data_free():
    popup = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "ReviewPopup.tsx").read_text(encoding="utf-8")
    urls = {
        match.group(0).rstrip("`'\")],.")
        for match in re.finditer(r"https?://[^\s'\"<>]+", popup)
    }
    assert urls == {
        "https://github.com/lanreal-io/whisperstream",
        "https://github.com/lanreal-io/whisperstream/issues/new/choose",
    }
    assert all("?" not in url and "#" not in url for url in urls)
