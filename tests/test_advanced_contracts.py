"""Contract inventory for Phase 5 advanced platform boundaries."""

from __future__ import annotations

import ast
import json
from dataclasses import fields
from pathlib import Path

import pytest

from whisperstream.advanced_capabilities import (
    ADVANCED_CAPABILITY_SCHEMA_VERSION,
    AdvancedCapabilityPresentation,
    AdvancedCapabilityState,
    create_advanced_capability_report,
)
from whisperstream.agent_integration_contract import (
    AGENT_INTEGRATION_SCHEMA_VERSION,
    AgentFailure,
    AgentHookDisposition,
    AgentHookEvent,
    AgentHookRequest,
    AgentHookResult,
    AgentInteractionKind,
    AgentLifecycleAction,
    AgentLifecycleRequest,
    AgentPluginAdapter,
    AgentProvider,
    AgentSessionRef,
)
from whisperstream.app_context_contract import (
    APP_CONTEXT_SCHEMA_VERSION,
    ApplicationContextAdapter,
    ApplicationRef,
    BrowserPageRef,
    ContextFailure,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
    EnhancementProfileRoute,
    EnhancementRouteResolver,
    ProfileRouteTier,
)
from whisperstream.local_llm_runtime_contract import (
    LOCAL_LLM_RUNTIME_SCHEMA_VERSION,
    LocalInferenceRequest,
    LocalInferenceResult,
    LocalModelRef,
    LocalRuntimeAdapter,
    LocalRuntimeFactory,
    LocalRuntimeKind,
    RuntimeAcceleration,
    RuntimeFailure,
    RuntimeOwnership,
    RuntimeSnapshot,
    RuntimeStage,
    RuntimeStartRequest,
)
from whisperstream.tts_playback_contract import (
    TTS_PLAYBACK_SCHEMA_VERSION,
    SpeechBuffer,
    SpeechEngineChoice,
    SpeechFailure,
    SpeechPlaybackAdapter,
    SpeechReceipt,
    SpeechRequest,
    SpeechStage,
    SpeechSynthesisAdapter,
    SpeechTerminalReason,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_MODULES = (
    ROOT / "src" / "whisperstream" / "advanced_capabilities.py",
    ROOT / "src" / "whisperstream" / "agent_integration_contract.py",
    ROOT / "src" / "whisperstream" / "app_context_contract.py",
    ROOT / "src" / "whisperstream" / "local_llm_runtime_contract.py",
    ROOT / "src" / "whisperstream" / "tts_playback_contract.py",
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


def _field_names(*types: type[object]) -> set[str]:
    return {field.name.lower() for type_ in types for field in fields(type_)}


def test_advanced_inventory_is_complete_native_free_and_truthful() -> None:
    assert ADVANCED_CAPABILITY_SCHEMA_VERSION == 1
    assert all(path.is_file() for path in CONTRACT_MODULES)
    prohibited_imports = {
        "AppKit",
        "ApplicationServices",
        "AVFoundation",
        "CoreAudio",
        "Quartz",
        "ctypes",
        "pycaw",
        "sounddevice",
        "win32api",
        "win32com",
    }
    for path in CONTRACT_MODULES:
        assert not (_imports(path) & prohibited_imports), path.name

    prohibited_fields = {
        "audio_device",
        "automation_object",
        "browser_automation",
        "hook_path",
        "hwnd",
        "native_handle",
        "process_handle",
        "uia_element",
    }
    assert not (
        _field_names(
            LocalModelRef,
            RuntimeSnapshot,
            ApplicationRef,
            BrowserPageRef,
            SpeechRequest,
            SpeechBuffer,
            AgentSessionRef,
            AgentHookRequest,
        )
        & prohibited_fields
    )

    windows = create_advanced_capability_report("Windows")
    darwin = create_advanced_capability_report("Darwin")
    unknown = create_advanced_capability_report("Plan9")
    json.dumps(windows.as_dict())
    win = {item.capability_id: item for item in windows.capabilities}
    mac = {item.capability_id: item for item in darwin.capabilities}
    assert win["local-llm-runtime"].state is AdvancedCapabilityState.AVAILABLE
    assert win["app-path-browse"].state is AdvancedCapabilityState.AVAILABLE
    assert win["finetune-registry-evaluation"].state is (
        AdvancedCapabilityState.DEVELOPMENT_ONLY
    )
    assert win["finetune-registry-evaluation"].presentation is (
        AdvancedCapabilityPresentation.EXPLAIN
    )
    assert mac["local-llm-runtime"].state is AdvancedCapabilityState.AVAILABLE
    assert mac["app-path-browse"].state is AdvancedCapabilityState.UNAVAILABLE
    assert mac["app-path-browse"].reason_code == "raw-path-boundary-unavailable"
    assert mac["app-path-browse"].presentation is (
        AdvancedCapabilityPresentation.DISABLE
    )
    assert mac["finetune-registry-evaluation"].state is (
        AdvancedCapabilityState.DEVELOPMENT_ONLY
    )
    assert mac["app-identity"].state is AdvancedCapabilityState.AVAILABLE
    assert (
        mac["browser-page-identity"].state
        is AdvancedCapabilityState.AVAILABLE
    )
    assert all(
        item.state is AdvancedCapabilityState.NOT_IMPLEMENTED
        for key, item in mac.items()
        if key
        not in {
            "agent-hook-lifecycle",
            "agent-hud-integration",
            "app-identity",
            "app-path-browse",
            "browser-page-identity",
            "local-llm-runtime",
            "finetune-registry-evaluation",
            "tts-synthesis-playback",
        }
    )
    assert mac["tts-synthesis-playback"].state is (
        AdvancedCapabilityState.EXTERNAL_DEPENDENCY
    )
    assert (
        mac["tts-synthesis-playback"].reason_code
        == "supertonic-native-runtime-prerequisite"
    )
    assert mac["tts-synthesis-playback"].presentation is (
        AdvancedCapabilityPresentation.EXPLAIN
    )
    assert mac["agent-hook-lifecycle"].state is AdvancedCapabilityState.AVAILABLE
    assert mac["agent-hud-integration"].state is AdvancedCapabilityState.AVAILABLE
    assert all(
        item.state is AdvancedCapabilityState.UNAVAILABLE
        for item in unknown.capabilities
    )


def test_local_llm_contract_freezes_ownership_readiness_and_raw_fallback() -> None:
    assert LOCAL_LLM_RUNTIME_SCHEMA_VERSION == 1
    model = LocalModelRef("gemma-4-e2b", "storage:managed")
    request = RuntimeStartRequest(
        4,
        LocalRuntimeKind.MANAGED_LLAMA,
        RuntimeOwnership.APPLICATION,
        model,
        RuntimeAcceleration.VULKAN,
        30.0,
    )
    assert "storage:managed" not in repr(request)
    ready = RuntimeSnapshot(
        4,
        LocalRuntimeKind.MANAGED_LLAMA,
        RuntimeOwnership.APPLICATION,
        RuntimeStage.READY,
        healthy=True,
    )
    assert ready.as_dict()["healthy"] is True
    inference = LocalInferenceRequest("local-1", 4, 8.0, "private raw")
    assert "private raw" not in repr(inference)
    fallback = LocalInferenceResult(
        "local-1",
        4,
        True,
        failure=RuntimeFailure.INFERENCE_FAILED,
    )
    assert fallback.use_raw
    with pytest.raises(ValueError):
        RuntimeStartRequest(
            5,
            LocalRuntimeKind.EXTERNAL_OLLAMA,
            RuntimeOwnership.APPLICATION,
            model,
            RuntimeAcceleration.CPU,
            5.0,
        )
    with pytest.raises(ValueError):
        RuntimeSnapshot(
            4,
            LocalRuntimeKind.MANAGED_LLAMA,
            RuntimeOwnership.APPLICATION,
            RuntimeStage.READY,
            healthy=False,
        )

    class AdapterStub:
        def start(self, request: object) -> object:
            return request

        def warm(self, generation: int) -> object:
            return generation

        def infer(self, request: object) -> object:
            return request

        def cancel(self, request_id: str, generation: int) -> object:
            return (request_id, generation)

        def stop(self, generation: int) -> object:
            return generation

        def close(self) -> None:
            return None

    class FactoryStub:
        def create(self, kind: object) -> object:
            return kind

    assert isinstance(AdapterStub(), LocalRuntimeAdapter)
    assert isinstance(FactoryStub(), LocalRuntimeFactory)


def test_app_browser_context_contract_excludes_titles_urls_and_native_objects() -> None:
    assert APP_CONTEXT_SCHEMA_VERSION == 1
    application = ApplicationRef("app:logical", "instance:opaque")
    page = BrowserPageRef(application, "mail.example.test")
    snapshot = ContextSnapshot(
        6,
        ContextRequirement.APPLICATION_AND_BROWSER_PAGE,
        ContextStatus.RESOLVED,
        application,
        page,
    )
    assert "mail.example.test" not in repr(snapshot)
    route = EnhancementProfileRoute(6, ProfileRouteTier.SITE, "profile:opaque")
    assert "profile:opaque" not in repr(route)
    partial = ContextSnapshot(
        7,
        ContextRequirement.APPLICATION_AND_BROWSER_PAGE,
        ContextStatus.APPLICATION_ONLY,
        application,
    )
    assert partial.browser_page is None
    with pytest.raises(ValueError):
        BrowserPageRef(application, "https://example.test/path")
    failure = ContextSnapshot(
        8,
        ContextRequirement.APPLICATION,
        ContextStatus.FAILED,
        failure=ContextFailure.NATIVE_ERROR,
    )
    assert failure.failure is ContextFailure.NATIVE_ERROR

    class ContextStub:
        def capture(self, generation: int, requirement: object) -> object:
            return (generation, requirement)

        def inventory(self, scope: object) -> tuple[object, ...]:
            return ()

        def close(self) -> None:
            return None

    class ResolverStub:
        def resolve(self, snapshot: object) -> object:
            return snapshot

    assert isinstance(ContextStub(), ApplicationContextAdapter)
    assert isinstance(ResolverStub(), EnhancementRouteResolver)


def test_tts_playback_contract_separates_synthesis_output_and_interruption() -> None:
    assert TTS_PLAYBACK_SCHEMA_VERSION == 1
    request = SpeechRequest(
        "completion-1",
        11,
        SpeechEngineChoice.AUTOMATIC,
        "voice:opaque",
        1.0,
        0.8,
        "private speech",
    )
    assert "private speech" not in repr(request)
    buffer = SpeechBuffer(11, 0, 24000, 1, b"\x00" * 16, final=True)
    assert "pcm_f32le" not in repr(buffer)
    completed = SpeechReceipt(
        "completion-1",
        11,
        SpeechStage.COMPLETED,
        SpeechTerminalReason.COMPLETED,
        buffers_completed=1,
    )
    assert completed.as_dict()["terminal_reason"] == "completed"
    interrupted = SpeechReceipt(
        "completion-1",
        11,
        SpeechStage.INTERRUPTED,
        SpeechTerminalReason.SUPERSEDED,
    )
    assert interrupted.failure is None
    failed = SpeechReceipt(
        "completion-2",
        12,
        SpeechStage.FAILED,
        SpeechTerminalReason.UNAVAILABLE_OUTPUT,
        SpeechFailure.OUTPUT_UNAVAILABLE,
    )
    assert failed.failure is SpeechFailure.OUTPUT_UNAVAILABLE

    class SynthesisStub:
        def synthesize(self, request: object) -> tuple[object, ...]:
            return (request,)

        def close(self) -> None:
            return None

    class PlaybackStub:
        def play(self, request: object, buffers: object) -> object:
            return (request, buffers)

        def interrupt(self, generation: int, reason: object) -> None:
            return None

        def close(self) -> None:
            return None

    assert isinstance(SynthesisStub(), SpeechSynthesisAdapter)
    assert isinstance(PlaybackStub(), SpeechPlaybackAdapter)


def test_agent_integration_contract_is_bounded_private_and_fail_open() -> None:
    assert AGENT_INTEGRATION_SCHEMA_VERSION == 1
    session = AgentSessionRef(AgentProvider.CLAUDE_CODE, "session:opaque")
    hook = AgentHookRequest(
        "hook-1",
        13,
        session,
        AgentHookEvent.STOP,
        10.0,
        '{"private":"body"}',
    )
    assert "session:opaque" not in repr(hook)
    assert "private" not in repr(hook)
    failed_open = AgentHookResult(
        "hook-1",
        13,
        AgentHookDisposition.FAILED_OPEN,
        failure=AgentFailure.TIMEOUT,
    )
    assert failed_open.response_json == "{}"
    resolved = AgentHookResult(
        "hook-2",
        14,
        AgentHookDisposition.RESOLVED,
        AgentInteractionKind.PERMISSION,
        '{"decision":"allow"}',
    )
    assert "decision" not in repr(resolved)
    lifecycle = AgentLifecycleRequest(
        "plugin-1",
        15,
        AgentProvider.CLAUDE_CODE,
        AgentLifecycleAction.RECONCILE,
    )
    assert lifecycle.action is AgentLifecycleAction.RECONCILE
    with pytest.raises(ValueError):
        AgentHookResult(
            "hook-3",
            16,
            AgentHookDisposition.FAILED_OPEN,
            response_json='{ "decision": "allow" }',
            failure=AgentFailure.NATIVE_ERROR,
        )

    class PluginStub:
        def apply(self, request: object) -> object:
            return request

        def close(self) -> None:
            return None

    assert isinstance(PluginStub(), AgentPluginAdapter)
