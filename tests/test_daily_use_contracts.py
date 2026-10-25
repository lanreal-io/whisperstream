"""Contract inventory for Phase 4 daily-use platform boundaries."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from whisperstream.audio_policy_contract import (
    AUDIO_POLICY_SCHEMA_VERSION,
    WINDOWS_REDUCE_TARGET_SCALAR,
    AudioOutputSnapshot,
    AudioPolicyFailure,
    AudioPolicyMode,
    AudioPolicyResult,
    AudioPolicyStage,
    AudioPolicyTransaction,
    OutputEndpointRef,
    RecordingAudioPolicyFactory,
)
from whisperstream.daily_use_capabilities import (
    DAILY_USE_CAPABILITY_SCHEMA_VERSION,
    CapabilityPresentation,
    CapabilityState,
    create_daily_use_capability_report,
)
from whisperstream.enhancement_provider_contract import (
    ENHANCEMENT_PROVIDER_SCHEMA_VERSION,
    EnhancementFailure,
    EnhancementDestination,
    EnhancementOutcome,
    EnhancementProviderKind,
    EnhancementRequest,
    provider_policy,
)
from whisperstream.history_security_contract import (
    HISTORY_SECURITY_SCHEMA_VERSION,
    WINDOWS_HISTORY_POLICY,
    FileSelection,
    FileWorkflowKind,
    FileWorkflowResult,
    HistoryFailure,
    LicenseSnapshot,
    LicenseState,
    HistorySecurityFactory,
    LockdownModeState,
    SecurityAvailability,
    SecuritySnapshot,
)
from whisperstream.history_security import UnavailableLicenseStateAdapter
from whisperstream.windows_history_security import WindowsLicenseStateAdapter
from whisperstream.model_lifecycle_contract import (
    MODEL_LIFECYCLE_SCHEMA_VERSION,
    ModelAssetRef,
    ModelFailure,
    ModelFamily,
    ModelLifecycleFactory,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelSnapshot,
    ModelState,
)
from whisperstream.window_lifecycle_contract import (
    WINDOW_LIFECYCLE_SCHEMA_VERSION,
    LaunchPresentation,
    LaunchAtLoginResult,
    LaunchAtLoginState,
    LogicalGeometry,
    WindowAction,
    WindowCloseDisposition,
    WindowFailure,
    WindowLifecycleRequest,
    WindowLifecycleResult,
    WindowLifecycleFactory,
    WindowPolicy,
    WindowRef,
    WindowRole,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_MODULES = (
    ROOT / "src" / "whisperstream" / "audio_policy_contract.py",
    ROOT / "src" / "whisperstream" / "daily_use_capabilities.py",
    ROOT / "src" / "whisperstream" / "enhancement_provider_contract.py",
    ROOT / "src" / "whisperstream" / "history_security_contract.py",
    ROOT / "src" / "whisperstream" / "model_lifecycle_contract.py",
    ROOT / "src" / "whisperstream" / "window_lifecycle_contract.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def test_daily_use_inventory_is_complete_and_native_free() -> None:
    assert all(path.is_file() for path in CONTRACT_MODULES)
    prohibited = {
        "AppKit",
        "CoreAudio",
        "Security",
        "ctypes",
        "pathlib",
        "pycaw",
        "sounddevice",
        "winreg",
    }
    for path in CONTRACT_MODULES:
        assert not (_imports(path) & prohibited), path.name



def test_daily_use_factories_are_native_free_protocols() -> None:
    class FactoryStub:
        def create(self, *args: object) -> object:
            return object()

        def history(self) -> object:
            return object()

        def files(self) -> object:
            return object()

        def licensing(self) -> object:
            return object()

    stub = FactoryStub()
    assert isinstance(stub, ModelLifecycleFactory)
    assert isinstance(stub, RecordingAudioPolicyFactory)
    assert isinstance(stub, WindowLifecycleFactory)
    assert isinstance(stub, HistorySecurityFactory)


def test_model_lifecycle_contract_is_opaque_and_explicit() -> None:
    assert MODEL_LIFECYCLE_SCHEMA_VERSION == 1
    asset = ModelAssetRef("parakeet-v3", ModelFamily.STT)
    ready = ModelSnapshot(asset, ModelState.READY, "storage:primary", verified=True)
    assert ready.verified
    request = ModelOperationRequest(
        7, ModelOperation.RELOCATE, asset, destination_token="storage:alternate"
    )
    result = ModelOperationResult(
        7,
        ModelOperation.RELOCATE,
        asset,
        ModelState.READY,
        ModelFailure.NONE,
        changed=True,
    )
    assert request.destination_token == "storage:alternate"
    assert result.changed
    with pytest.raises(ValueError):
        ModelOperationRequest(8, ModelOperation.RELOCATE, asset)
    with pytest.raises(ValueError):
        ModelOperationResult(
            8, ModelOperation.ACQUIRE, asset, ModelState.UNAVAILABLE
        )


def test_enhancement_provider_contract_preserves_failure_fallback() -> None:
    assert ENHANCEMENT_PROVIDER_SCHEMA_VERSION == 1
    cloud = provider_policy(EnhancementProviderKind.CLOUD)
    ollama = provider_policy(EnhancementProviderKind.OLLAMA)
    assert cloud.requires_protected_secret and cloud.network_bound
    assert cloud.destination is EnhancementDestination.CLOUD_SERVICE
    assert not ollama.requires_protected_secret and ollama.requires_endpoint
    assert ollama.destination is EnhancementDestination.CONFIGURED_ENDPOINT
    assert not cloud.allowed_in_lockdown and not ollama.allowed_in_lockdown
    assert cloud.retries == ollama.retries == 0
    request = EnhancementRequest(
        "request-1", "provider-1", EnhancementProviderKind.CLOUD, 8.0, "private"
    )
    assert "private" not in repr(request)
    fallback = EnhancementOutcome(
        "request-1", EnhancementFailure.TIMEOUT, use_raw=True
    )
    assert fallback.use_raw and fallback.enhanced_text is None
    with pytest.raises(ValueError):
        EnhancementOutcome(
            "request-1", EnhancementFailure.HTTP, use_raw=False
        )


def test_settings_capability_contract_is_truthful_and_serializable() -> None:
    assert DAILY_USE_CAPABILITY_SCHEMA_VERSION == 1
    windows = create_daily_use_capability_report("Windows")
    darwin = create_daily_use_capability_report(
        "Darwin", audio_policy_available=True
    )
    darwin_unconfirmed = create_daily_use_capability_report("Darwin")
    unknown = create_daily_use_capability_report("Plan9")
    windows_payload = windows.as_dict()
    json.dumps(windows_payload)
    windows_by_id = {item.capability_id: item for item in windows.capabilities}
    darwin_by_id = {item.capability_id: item for item in darwin.capabilities}
    assert windows_by_id["model-lifecycle"].state is CapabilityState.AVAILABLE
    assert windows_by_id["model-lifecycle"].presentation is CapabilityPresentation.ENABLE
    assert windows_by_id["ollama-enhancement"].state is CapabilityState.EXTERNAL_DEPENDENCY
    assert darwin_by_id["protected-storage"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["model-lifecycle"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["model-lifecycle"].reason_code == "fluidaudio-verified"
    assert darwin_by_id["managed-local-enhancement"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["managed-local-enhancement"].reason_code == "metal-runtime"
    assert darwin_by_id["auto-update"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["auto-update"].reason_code == "implemented"
    assert darwin_by_id["qwen-stt"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["audio-policy"].state is CapabilityState.AVAILABLE
    assert darwin_by_id["audio-policy"].reason_code == "coreaudio-output-policy"
    assert darwin_by_id["audio-policy"].presentation is CapabilityPresentation.ENABLE
    unconfirmed_by_id = {
        item.capability_id: item for item in darwin_unconfirmed.capabilities
    }
    assert unconfirmed_by_id["audio-policy"].state is CapabilityState.UNAVAILABLE
    assert unconfirmed_by_id["audio-policy"].reason_code == "output-control-unconfirmed"
    assert all(item.state is CapabilityState.UNAVAILABLE for item in unknown.capabilities)


def test_audio_policy_contract_binds_restore_to_captured_endpoint() -> None:
    assert AUDIO_POLICY_SCHEMA_VERSION == 1
    assert WINDOWS_REDUCE_TARGET_SCALAR == pytest.approx(0.20)
    endpoint = OutputEndpointRef("endpoint:recording-generation")
    snapshot = AudioOutputSnapshot(endpoint, 0.75, muted=False)
    transaction = AudioPolicyTransaction(3, AudioPolicyMode.REDUCE, snapshot)
    assert "endpoint:recording-generation" not in repr(transaction)
    restored = AudioPolicyResult(3, AudioPolicyStage.RESTORED, changed=True)
    assert restored.changed
    unsupported = AudioPolicyResult(
        4, AudioPolicyStage.IDLE, AudioPolicyFailure.UNSUPPORTED
    )
    assert unsupported.failure is AudioPolicyFailure.UNSUPPORTED
    with pytest.raises(ValueError):
        AudioPolicyResult(
            4, AudioPolicyStage.RESTORED, AudioPolicyFailure.RESTORE_FAILED, True
        )


def test_window_lifecycle_contract_keeps_native_identity_private() -> None:
    assert WINDOW_LIFECYCLE_SCHEMA_VERSION == 1
    policy = WindowPolicy(
        WindowRole.MAIN,
        WindowCloseDisposition.HIDE,
        focusable=True,
        resizable=False,
        visible_on_normal_launch=True,
        visible_on_minimized_launch=False,
    )
    assert policy.close_disposition is WindowCloseDisposition.HIDE
    assert LaunchPresentation.MINIMIZED.value == "minimized"
    window = WindowRef(WindowRole.HUD, "native-window-private")
    geometry = LogicalGeometry(10, 20, 480, 96, "display-private")
    request = WindowLifecycleRequest(5, window, WindowAction.SHOW, geometry)
    assert "native-window-private" not in repr(request)
    assert "display-private" not in repr(request)
    approval = LaunchAtLoginResult(LaunchAtLoginState.REQUIRES_APPROVAL)
    assert approval.state is LaunchAtLoginState.REQUIRES_APPROVAL
    with pytest.raises(ValueError):
        LaunchAtLoginResult(LaunchAtLoginState.UNAVAILABLE)
    unsupported = WindowLifecycleResult(
        5,
        WindowRole.HUD,
        WindowAction.SHOW,
        visible=False,
        focused=False,
        failure=WindowFailure.UNSUPPORTED,
    )
    assert unsupported.failure is WindowFailure.UNSUPPORTED


def test_history_security_contract_freezes_security_and_file_boundaries() -> None:
    assert HISTORY_SECURITY_SCHEMA_VERSION == 1
    assert WINDOWS_HISTORY_POLICY.cipher == "AES-256-GCM"
    assert WINDOWS_HISTORY_POLICY.zero_retention_means_forever
    assert not WINDOWS_HISTORY_POLICY.ordinary_delete_is_secure_erasure
    assert WINDOWS_HISTORY_POLICY.max_import_files == 100
    selection = FileSelection(
        "native-panel-selection", FileWorkflowKind.EXPORT_TRANSCRIPT, "export.txt"
    )
    success = FileWorkflowResult(HistoryFailure.NONE, selection, b"private")
    assert "native-panel-selection" not in repr(success)
    assert "private" not in repr(success)
    cache = LicenseSnapshot(
        LicenseState.PRO,
        cache_integrity_protected=True,
        cache_confidential=False,
        activation_available=True,
        offline_validation_available=True,
        deactivation_available=True,
    )
    assert cache.cache_integrity_protected and not cache.cache_confidential
    assert cache.offline_validation_available and cache.deactivation_available
    failed_defaults = LicenseSnapshot(
        LicenseState.UNAVAILABLE,
        cache_integrity_protected=False,
        cache_confidential=False,
        activation_available=False,
    )
    assert not failed_defaults.offline_validation_available
    assert not failed_defaults.deactivation_available
    generic = UnavailableLicenseStateAdapter().snapshot()
    assert not generic.offline_validation_available
    assert not generic.deactivation_available
    windows_unavailable = WindowsLicenseStateAdapter(
        manager_provider=lambda: None
    ).snapshot()
    assert not windows_unavailable.offline_validation_available
    assert not windows_unavailable.deactivation_available
    locked = SecuritySnapshot(
        SecurityAvailability.READY,
        SecurityAvailability.LOCKED,
        LockdownModeState.LOCKED,
        network_enhancement_allowed=False,
        diagnostic_export_allowed=False,
    )
    assert locked.lockdown is LockdownModeState.LOCKED
    with pytest.raises(ValueError):
        SecuritySnapshot(
            SecurityAvailability.READY,
            SecurityAvailability.LOCKED,
            LockdownModeState.LOCKED,
            network_enhancement_allowed=True,
            diagnostic_export_allowed=False,
        )
    with pytest.raises(ValueError):
        FileWorkflowResult(HistoryFailure.CANCELLED, selection)
