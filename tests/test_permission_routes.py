"""HTTP permission intent gates without invoking native permission APIs."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whisperstream.permission_contract import PermissionKind
from whisperstream.server.routes.permissions import register_permission_routes


@pytest.mark.parametrize("action,method", [
    ("request", "request_permission"), ("recovery", "open_permission_settings"),
])
def test_explicit_intent_precedes_lazy_native_service_resolution(action, method):
    api = FastAPI()
    outcome = SimpleNamespace(as_dict=lambda: {"result": "requested"})
    service = Mock()
    getattr(service, method).return_value = outcome
    resolve_service = Mock(return_value=service)
    register_permission_routes(
        api, current_shortcut_recovery=lambda: None,
        permission_actions=resolve_service,
    )
    headers = {"X-Whisperstream-User-Intent": "permission-action-v1"}
    with TestClient(api) as client:
        path = f"/api/permissions/{action}"
        payload = {"kind": "microphone"}
        assert client.post(path, json=payload).status_code == 403
        assert client.post(path, json=payload, headers={
            **headers, "Origin": "https://untrusted.example",
        }).status_code == 403
        resolve_service.assert_not_called()
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"result": "requested"}
    resolve_service.assert_called_once_with()
    getattr(service, method).assert_called_once_with(
        PermissionKind.MICROPHONE, explicit_user_action=True,
    )
