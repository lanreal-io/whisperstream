"""Retired REST submissions cannot reach a transport or persist success."""
import httpx
import pytest
from starlette.testclient import TestClient
from whisperstream.review_manager import ReviewManager
from tests.test_eula_server_boundary import _make_server, ACCEPTED

@pytest.mark.parametrize("path,payload",[("/api/review",{"rating":5,"customer_email":"synthetic@example.test"}),("/api/feedback",{"feedback":"synthetic","customer_email":"synthetic@example.test"})])
def test_retired_submission_is_unavailable_and_does_not_write(monkeypatch,path,payload):
    server,app,_ = _make_server(ACCEPTED)
    server.review_manager = ReviewManager()
    real_client = httpx.AsyncClient
    def no_remote(*args,**kwargs):
        if not isinstance(kwargs.get("transport"), httpx.ASGITransport):
            pytest.fail("unexpected remote client")
        return real_client(*args,**kwargs)
    monkeypatch.setattr(httpx,"AsyncClient",no_remote)
    response=TestClient(server.fastapi).post(path,json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is False
    app.review_state.record_submission.assert_not_called()


@pytest.mark.parametrize("reason,method",[("later","record_later"),("not-now","record_dismissal"),("never","record_permanent_dismissal"),("github","record_submission")])
def test_prompt_outcomes_record_local_state_only(reason,method):
    server,app,_ = _make_server(ACCEPTED)
    response=TestClient(server.fastapi).post("/api/review/dismiss",json={"reason":reason})
    assert response.status_code == 200
    getattr(app.review_state,method).assert_called_once_with()
