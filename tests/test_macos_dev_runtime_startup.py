"""Focused startup regressions for the non-file-only macOS backend."""

from __future__ import annotations

import logging
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from whisperstream.main import WhisperstreamApp
from whisperstream.audio_policy import (
    PlatformRecordingAudioPolicyFactory,
    UnavailableRecordingAudioPolicyAdapter,
)
from whisperstream.audio_policy_contract import AudioPolicyResult, AudioPolicyStage
from whisperstream.macos_shortcuts import MacosShortcutAdapter
from whisperstream.permission_contract import (
    PermissionKind,
    PermissionReport,
    PermissionSnapshot,
    PermissionState,
)
from whisperstream.protected_secret import UnavailableProtectedSecretStore
from whisperstream.server import WhisperstreamServer


@pytest.fixture(autouse=True)
def _fail_closed_protected_storage(monkeypatch):
    """Keep this startup suite completely outside Security.framework."""

    import whisperstream.key_encryption as key_encryption
    import whisperstream.transcript_crypto as transcript_crypto

    store = UnavailableProtectedSecretStore()
    monkeypatch.setattr(key_encryption, "get_protected_secret_store", lambda: store)
    monkeypatch.setattr(transcript_crypto, "get_protected_secret_store", lambda: store)


def _mock_server_app(adapter: MacosShortcutAdapter) -> MagicMock:
    app = MagicMock()
    app.keyboard = adapter
    app._agent_reply_router = None
    app.set_agent_reply_router = MethodType(
        WhisperstreamApp.set_agent_reply_router,
        app,
    )
    app.dictation_state_snapshot.return_value = None
    return app


def _shortcut_report(state: PermissionState) -> PermissionReport:
    return PermissionReport(
        platform="macos",
        permissions=tuple(
            PermissionSnapshot(
                kind=kind,
                state=(
                    PermissionState.NOT_DETERMINED
                    if kind is PermissionKind.MICROPHONE
                    else state
                ),
                query_supported=True,
                request_supported=True,
            )
            for kind in PermissionKind
        ),
    )


@pytest.mark.parametrize("register_hotkeys", [False, True])
@pytest.mark.parametrize(
    "shortcut_state",
    [PermissionState.NOT_DETERMINED, PermissionState.DENIED],
)
def test_server_construction_only_associates_router_without_native_listener(
    register_hotkeys: bool,
    shortcut_state: PermissionState,
) -> None:
    permission_reporter = MagicMock(
        return_value=_shortcut_report(shortcut_state)
    )
    listener_factory = MagicMock(
        side_effect=AssertionError("server construction started an event tap")
    )
    adapter = MacosShortcutAdapter(
        logger=logging.getLogger("test.macos-startup"),
        listener_factory=listener_factory,
        permission_reporter=permission_reporter,
        physical_key_state=lambda _keycode: False,
    )
    app = _mock_server_app(adapter)

    try:
        server = WhisperstreamServer(
            app,
            port=0,
            register_hotkeys=register_hotkeys,
        )

        assert app._agent_reply_router is server
        assert adapter._early_handlers == {}
        permission_reporter.assert_not_called()
        listener_factory.assert_not_called()
    finally:
        adapter.close()


def test_macos_early_handlers_are_passive_until_ordinary_binding_registration() -> None:
    permission_reporter = MagicMock(
        return_value=_shortcut_report(PermissionState.NOT_DETERMINED)
    )
    listener_factory = MagicMock(
        side_effect=AssertionError("denied permission must precede tap creation")
    )
    adapter = MacosShortcutAdapter(
        logger=logging.getLogger("test.macos-startup"),
        listener_factory=listener_factory,
        permission_reporter=permission_reporter,
        physical_key_state=lambda _keycode: False,
    )

    try:
        adapter.register_early_key_handler("escape", lambda: False, lambda: None)
        adapter.register_early_key_handler("enter", lambda: False, lambda: None)

        permission_reporter.assert_not_called()
        listener_factory.assert_not_called()

        with pytest.raises(PermissionError, match="denied"):
            adapter.register_push_to_talk(
                "right shift",
                lambda: None,
                lambda: None,
            )

        permission_reporter.assert_called_once_with()
        listener_factory.assert_not_called()
        assert set(adapter._early_handlers) == {"esc", "enter"}
    finally:
        adapter.close()


def test_ordinary_hotkey_registration_installs_router_handlers_first() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app.keyboard = MagicMock()
    app._agent_reply_router = MagicMock()
    app.config = SimpleNamespace(
        push_to_talk_shortcut="right shift",
        toggle_shortcut="",
        paste_last_shortcut="ctrl+alt+v",
        paste_last_original_shortcut="",
        claude_view_shortcut="",
    )
    app._register_profile_hotkeys = MagicMock()

    app._register_hotkey()

    assert app.keyboard.method_calls[:3] == [
        call.register_early_key_handler(
            "escape",
            app._should_consume_escape,
            app._on_escape_press,
        ),
        call.register_early_key_handler(
            "enter",
            app._agent_reply_router.should_consume_response_enter,
            app._on_agent_enter_press,
        ),
        call.register_push_to_talk(
            "right shift",
            app._on_hotkey_press,
            app._on_hotkey_release,
        ),
    ]


def test_no_hotkeys_engine_wait_does_not_register_any_handler_or_listener() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app._file_only = False
    app.logger = MagicMock(spec=logging.Logger)
    app.keyboard = MagicMock()

    app.run(register_hotkeys=False)

    app.keyboard.wait.assert_called_once_with()
    app.keyboard.register_early_key_handler.assert_not_called()
    app.keyboard.register_push_to_talk.assert_not_called()


def test_non_windows_volume_paths_do_not_import_windows_audio_stack() -> None:
    with patch(
        "whisperstream.macos_audio_policy._NativeCoreAudioOutputBridge",
        return_value=MagicMock(),
    ):
        adapter = PlatformRecordingAudioPolicyFactory(
            feedback=MagicMock(),
            logger=MagicMock(spec=logging.Logger),
            start_path=lambda: None,
            stop_path=lambda: None,
            system="Darwin",
            machine="arm64",
        ).create()

    assert adapter.__class__.__name__ == "MacosRecordingAudioPolicyAdapter"


def test_windows_volume_path_still_uses_existing_pycaw_endpoint() -> None:
    adapter = PlatformRecordingAudioPolicyFactory(
        feedback=MagicMock(),
        logger=MagicMock(spec=logging.Logger),
        start_path=lambda: None,
        stop_path=lambda: None,
        system="Windows",
        machine="AMD64",
    ).create()

    assert type(adapter).__name__ == "WindowsRecordingAudioPolicyAdapter"
    assert getattr(adapter, "_active_endpoint") is None


def test_cleanup_closes_audio_policy_and_feedback_once() -> None:
    app = WhisperstreamApp.__new__(WhisperstreamApp)
    app.logger = MagicMock(spec=logging.Logger)
    app._recording = False
    app.audio_capture = MagicMock()
    app.audio_feedback = MagicMock()
    app.audio_policy = MagicMock()
    app.audio_policy.close.return_value = AudioPolicyResult(1, AudioPolicyStage.CLOSED)
    app._audio_policy_generation = 1
    app._audio_policy_transaction = None
    app.text_injector = MagicMock()
    app.keyboard = MagicMock()
    app.stt_engine = MagicMock()

    app.cleanup()
    app.cleanup()

    app.audio_feedback.close.assert_called_once_with()
    app.audio_policy.close.assert_called_once_with(1)
