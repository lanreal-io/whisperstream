"""Native Windows freeze tests for the Phase 3 shared core contracts."""

from __future__ import annotations

import ast
import json
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from whisperstream.capture_contract import (
    CAPTURE_CHANNELS,
    CAPTURE_DTYPE,
    CAPTURE_SAMPLE_RATE,
    CaptureCompletion,
    CaptureCompletionStatus,
    CaptureDevice,
    CaptureFailure,
    CaptureFormat,
    CaptureInventory,
    CaptureRequest,
    CaptureSelection,
    CaptureSelectionResolution,
    CaptureSession,
    CaptureStartOutcome,
)
from whisperstream.delivery_contract import (
    DeliveryClaimLedger,
    DeliveryClaimStatus,
    DeliveryFailure,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRoute,
    DeliveryStatus,
    DeliveryStyle,
    DeliveryTarget,
    DeliveryWarning,
)
from whisperstream.permission_contract import (
    PermissionAction,
    PermissionActionFailure,
    PermissionActionOutcome,
    PermissionActionStatus,
    PermissionIntent,
    PermissionIntentRegistry,
    PermissionIntentUse,
    PermissionKind,
    PermissionRecoveryProvider,
    PermissionRequester,
    PermissionState,
    PermissionStatusProvider,
)
from whisperstream.shortcut_contract import (
    CanonicalShortcut,
    ShortcutAction,
    ShortcutBinding,
    ShortcutCaptureOutcome,
    ShortcutCaptureStatus,
    ShortcutFailure,
    ShortcutGesture,
    ShortcutGestureState,
    ShortcutRegistrationOutcome,
    ShortcutRegistrationStatus,
    ShortcutTransition,
)


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="native Windows Phase 3 contract freeze",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_permission_request_contract_requires_one_shot_explicit_intent() -> None:
    assert {
        name for name in PermissionStatusProvider.__dict__ if not name.startswith("_")
    } == {"query_permissions"}
    assert {
        name for name in PermissionRequester.__dict__ if not name.startswith("_")
    } == {"request_permission"}
    assert {
        name
        for name in PermissionRecoveryProvider.__dict__
        if not name.startswith("_")
    } == {"open_permission_settings"}
    registry = PermissionIntentRegistry()
    intent = registry.issue(PermissionKind.MICROPHONE, PermissionAction.REQUEST)
    assert registry.consume(intent) is PermissionIntentUse.ACCEPTED
    assert registry.consume(intent) is PermissionIntentUse.REUSED
    forged = PermissionIntent(
        intent_id="not-issued",
        kind=PermissionKind.MICROPHONE,
        action=PermissionAction.REQUEST,
    )
    assert registry.consume(forged) is PermissionIntentUse.UNKNOWN

    outcome = PermissionActionOutcome(
        kind=PermissionKind.MICROPHONE,
        action=PermissionAction.REQUEST,
        status=PermissionActionStatus.DECLINED,
        before=PermissionState.NOT_DETERMINED,
        after=PermissionState.DENIED,
    )
    serialized = outcome.as_dict()
    assert serialized["status"] == "declined"
    assert "intent" not in json.dumps(serialized)
    with pytest.raises(ValueError, match="failure must match"):
        PermissionActionOutcome(
            kind=PermissionKind.ACCESSIBILITY,
            action=PermissionAction.OPEN_SETTINGS,
            status=PermissionActionStatus.FAILED,
            before=PermissionState.DENIED,
            after=PermissionState.DENIED,
        )
    failure = PermissionActionOutcome(
        kind=PermissionKind.ACCESSIBILITY,
        action=PermissionAction.OPEN_SETTINGS,
        status=PermissionActionStatus.UNSUPPORTED,
        before=PermissionState.DENIED,
        after=PermissionState.DENIED,
        failure=PermissionActionFailure.RECOVERY_UNAVAILABLE,
    )
    assert failure.as_dict()["failure"] == "recovery-unavailable"


def test_device_capture_contract_freezes_format_fallback_and_generation() -> None:
    assert (CAPTURE_SAMPLE_RATE, CAPTURE_CHANNELS, CAPTURE_DTYPE) == (
        16_000,
        1,
        "float32",
    )
    assert CaptureFormat() == CaptureFormat(16_000, 1, "float32")
    with pytest.raises(ValueError, match="16000"):
        CaptureFormat(sample_rate=48_000)

    inventory = CaptureInventory(
        devices=(
            CaptureDevice("logical-default", "Default microphone", 1, 48_000, True),
            CaptureDevice("logical-selected", "Selected microphone", 2, 44_100, False),
        )
    )
    assert len(inventory.devices) == 2
    request = CaptureRequest(
        request_id="dictation-contract-test",
        generation=7,
        selection=CaptureSelection(legacy_name="Selected microphone"),
    )
    session = CaptureSession(
        request_id=request.request_id,
        generation=request.generation,
        device_id="logical-default",
        selection_resolution=CaptureSelectionResolution.FALLBACK_OPEN_FAILED,
    )
    assert CaptureStartOutcome(session=session).failure is None
    audio = np.zeros(320, dtype=np.float32)
    stopped = CaptureCompletion(
        request_id=request.request_id,
        generation=request.generation,
        status=CaptureCompletionStatus.STOPPED,
        audio=audio,
    )
    assert stopped.audio is not None
    assert stopped.audio.dtype == np.float32
    assert stopped.audio.shape == (320,)
    assert np.shares_memory(stopped.audio, audio)
    cancelled = CaptureCompletion(
        request_id=request.request_id,
        generation=request.generation,
        status=CaptureCompletionStatus.CANCELLED,
    )
    assert cancelled.audio is None
    failed = CaptureCompletion(
        request_id=request.request_id,
        generation=request.generation,
        status=CaptureCompletionStatus.FAILED,
        failure=CaptureFailure.DEVICE_LOST,
    )
    assert failed.failure is CaptureFailure.DEVICE_LOST


@pytest.mark.parametrize(
    ("gesture", "expected_press", "expected_release"),
    [
        (ShortcutGesture.HOLD, ShortcutAction.HOLD_BEGIN, ShortcutAction.HOLD_END),
        (ShortcutGesture.TOGGLE, ShortcutAction.TOGGLE, None),
        (ShortcutGesture.TAP, ShortcutAction.FIRE, None),
    ],
)
def test_hotkey_contract_freezes_press_release_toggle_and_tap(
    gesture: ShortcutGesture,
    expected_press: ShortcutAction,
    expected_release: ShortcutAction | None,
) -> None:
    state = ShortcutGestureState(gesture)
    assert state.apply(ShortcutTransition.PRESSED) == (expected_press,)
    assert state.apply(ShortcutTransition.PRESSED) == ()
    expected = (expected_release,) if expected_release else ()
    assert state.apply(ShortcutTransition.RELEASED) == expected
    assert state.apply(ShortcutTransition.RELEASED) == ()


def test_hotkey_contract_freezes_binding_conflict_capture_and_teardown() -> None:
    canonical = CanonicalShortcut(("ctrl", "alt", "v"))
    binding = ShortcutBinding(
        action_id="paste-last",
        gesture=ShortcutGesture.TAP,
        configured="ctrl+alt+v",
        shortcut=canonical,
    )
    assert binding.shortcut == canonical
    assert ShortcutRegistrationOutcome(
        action_id="toggle-recording",
        status=ShortcutRegistrationStatus.FAILED,
        failure=ShortcutFailure.CONFLICT,
    ).failure is ShortcutFailure.CONFLICT
    assert ShortcutCaptureOutcome(
        status=ShortcutCaptureStatus.CAPTURED,
        shortcut=CanonicalShortcut(("right shift",)),
    ).shortcut == CanonicalShortcut(("right shift",))
    state = ShortcutGestureState(ShortcutGesture.HOLD)
    state.apply(ShortcutTransition.PRESSED)
    assert state.reset() == (ShortcutAction.HOLD_END,)
    assert state.reset() == ()


def test_delivery_focus_contract_is_exactly_once_content_free_and_partial_aware() -> None:
    ledger = DeliveryClaimLedger()
    assert ledger.claim("dictation-contract-test", 4) is DeliveryClaimStatus.CLAIMED
    assert ledger.claim("dictation-contract-test", 4) is DeliveryClaimStatus.REUSED
    assert ledger.claim("dictation-contract-test", 5) is DeliveryClaimStatus.CLAIMED

    target = DeliveryTarget(DeliveryRoute.FOCUSED, "private-native-target", 4)
    request = DeliveryRequest(
        request_id="dictation-contract-test",
        generation=4,
        target=target,
        style=DeliveryStyle.CLIPBOARD,
        processed_text="private-payload-must-not-serialize",
    )
    assert "private-native-target" not in repr(target)
    assert "private-payload" not in repr(request)

    receipt = DeliveryReceipt(
        request_id=request.request_id,
        generation=request.generation,
        route=DeliveryRoute.FOCUSED,
        status=DeliveryStatus.FAILED,
        failure=DeliveryFailure.FOCUS_MISMATCH,
        warnings=(DeliveryWarning.CLIPBOARD_RESTORE_FAILED,),
        units_attempted=3,
        units_completed=1,
        clipboard_restored=False,
        modifiers_restored=True,
    )
    serialized = json.dumps(receipt.as_dict(), sort_keys=True)
    assert receipt.partial is True
    assert "private-native-target" not in serialized
    assert "private-payload" not in serialized
    assert '"partial": true' in serialized

    focused = DeliveryReceipt(
        request_id="focused-best-effort",
        generation=1,
        route=DeliveryRoute.FOCUSED,
        status=DeliveryStatus.FOCUSED_ATTEMPTED,
        warnings=(DeliveryWarning.CLIPBOARD_COPY_FAILED,),
        units_attempted=1,
        units_completed=0,
    )
    assert focused.failure is None

    fallback = DeliveryReceipt(
        request_id="clipboard-fallback",
        generation=1,
        route=DeliveryRoute.FOCUSED,
        status=DeliveryStatus.FOCUSED_ATTEMPTED,
        warnings=(DeliveryWarning.CLIPBOARD_SNAPSHOT_FALLBACK,),
        units_attempted=1,
        units_completed=1,
        clipboard_restored=True,
    )
    assert fallback.as_dict()["warnings"] == ["clipboard-snapshot-fallback"]


def test_process_generation_contract_binds_native_identity_health_and_shutdown() -> None:
    lifecycle = (
        REPO_ROOT / "src-tauri/src/engine_lifecycle_contract.rs"
    ).read_text(encoding="utf-8")
    windows_process = (
        REPO_ROOT / "src-tauri/src/windows_process_adapter.rs"
    ).read_text(encoding="utf-8")
    windows_engine = (
        REPO_ROOT / "src-tauri/src/windows_engine_adapter.rs"
    ).read_text(encoding="utf-8")
    transport = (REPO_ROOT / "src-tauri/src/engine_http.rs").read_text(
        encoding="utf-8"
    )
    backend = (REPO_ROOT / "src/whisperstream/server/app.py").read_text(
        encoding="utf-8"
    )
    assert "pub generation: u64" in lifecycle
    for token in (
        "GetProcessTimes",
        "creation_time_100ns",
        "struct ProcessHandle",
        "WaitForSingleObject",
    ):
        assert token in windows_process
    for token in (
        "new_generation_token(next_generation)",
        '"--engine-generation"',
        "self.generation_token.as_deref()",
    ):
        assert token in windows_engine
    assert "x-whisperstream-engine-generation" in transport
    assert "expected_generation" in transport
    assert "response_generation" in transport
    assert "stale_engine_generation" in backend


def test_shared_core_contract_imports_are_native_free() -> None:
    paths = tuple(
        REPO_ROOT / f"src/whisperstream/{name}"
        for name in (
            "permission_contract.py",
            "capture_contract.py",
            "shortcut_contract.py",
            "delivery_contract.py",
        )
    )
    forbidden = {
        "AppKit",
        "AVFoundation",
        "ApplicationServices",
        "CoreAudio",
        "CoreGraphics",
        "Quartz",
        "sounddevice",
        "pynput",
        "pyperclip",
        "win32api",
        "win32clipboard",
        "win32gui",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        assert imports.isdisjoint(forbidden), f"native import in {path}"

    probe = """
import json
import sys
import whisperstream.permission_contract
import whisperstream.capture_contract
import whisperstream.shortcut_contract
import whisperstream.delivery_contract
forbidden = {
    "AppKit", "AVFoundation", "ApplicationServices", "CoreAudio",
    "CoreGraphics", "Quartz", "sounddevice", "pynput", "pyperclip",
    "win32api", "win32clipboard", "win32gui",
}
print(json.dumps(sorted(forbidden.intersection(sys.modules))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(completed.stdout.strip()) == []
