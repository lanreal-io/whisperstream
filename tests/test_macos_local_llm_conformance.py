"""Darwin conformance for the D-051/D-052 local-runtime boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whisperstream.advanced_capabilities import (
    AdvancedCapabilityState,
    create_advanced_capability_report,
)
from whisperstream.daily_use_capabilities import (
    CapabilityState,
    create_daily_use_capability_report,
)
from whisperstream.local_llm_runtime_contract import (
    LocalInferenceRequest,
    LocalRuntimeKind,
    RuntimeAcceleration,
    RuntimeFailure,
    RuntimeOwnership,
    RuntimeStage,
    RuntimeStartRequest,
)


class _Supervisor:
    def __init__(self, artifact: Path) -> None:
        self._gguf_path = artifact
        self._binary_path = artifact.with_name("llama-server")
        self.is_running = False
        self.is_ready = False
        self.stop_calls = 0
        self.restart_calls = 0

    async def start(self) -> None:
        self.is_running = True

    async def _warmup_inference(self) -> None:
        self.is_ready = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False
        self.is_ready = False

    async def restart_for_model(self, artifact: Path) -> None:
        self.restart_calls += 1
        self._gguf_path = artifact
        self.is_running = False
        self.is_ready = False


def _runtime_request(runtime: object, *, acceleration: RuntimeAcceleration):
    return RuntimeStartRequest(
        runtime.current_generation,
        LocalRuntimeKind.MANAGED_LLAMA,
        RuntimeOwnership.APPLICATION,
        runtime._model,
        acceleration,
        1.0,
    )


def test_darwin_capabilities_preserve_task_5_1_surfaces() -> None:
    advanced = {
        item.capability_id: item
        for item in create_advanced_capability_report("Darwin").capabilities
    }
    assert advanced["local-llm-runtime"].state is AdvancedCapabilityState.AVAILABLE
    assert advanced["finetune-registry-evaluation"].state is (
        AdvancedCapabilityState.DEVELOPMENT_ONLY
    )

    daily = {
        item.capability_id: item
        for item in create_daily_use_capability_report("Darwin").capabilities
    }
    assert daily["managed-local-enhancement"].state is CapabilityState.AVAILABLE


def test_selector_creates_darwin_metal_runtime_without_windows_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from whisperstream.local_llm_runtime import create_managed_runtime
    from whisperstream.macos_local_llm_runtime import MacosManagedLlamaRuntime

    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"GGUF")
    supervisor = _Supervisor(artifact)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")

    runtime = create_managed_runtime(
        supervisor,
        catalog_id="candidate-a",
        artifact_path=artifact,
        metal_probe=lambda _binary: True,
    )
    assert isinstance(runtime, MacosManagedLlamaRuntime)
    assert runtime.current_generation == 1


def test_metal_device_parser_accepts_native_llama_cpp_output() -> None:
    from whisperstream.gpu_benchmark import (
        MetalDevice,
        available_acceleration_devices,
        parse_metal_devices,
        prefilter_verdict,
    )

    output = """Available devices:
  MTL0: Apple M5 (18186 MiB, 18185 MiB free)
  BLAS: Accelerate (0 MiB, 0 MiB free)
"""
    expected = MetalDevice(0, "Apple M5", 18186)
    assert parse_metal_devices(output) == [expected]
    assert available_acceleration_devices(output) == [expected]
    assert prefilter_verdict(output) == "pass"


def test_real_cycle_can_bind_retained_llama_library_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from whisperstream.llama_server import llama_library_dir

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    stage = tmp_path / "retained-stage/src-tauri"
    binary = stage / "binaries/llama-server-aarch64-apple-darwin"
    library = stage / "resources/llama-cpp"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"Mach-O")
    library.mkdir(parents=True)
    (library / "libggml.dylib").write_bytes(b"Mach-O")
    monkeypatch.setenv("WHISPERSTREAM_BUNDLE_RESOURCES_DIR", str(library.parent))

    assert llama_library_dir(binary) == library


@pytest.mark.asyncio
async def test_supervisor_forces_full_metal_offload_without_vulkan_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from whisperstream.llama_server import LlamaServerSupervisor

    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"GGUF")
    binary = tmp_path / "llama-server"
    binary.write_bytes(b"Mach-O")
    process = MagicMock(returncode=None, pid=42)
    process.wait = AsyncMock(return_value=0)
    captured: list[str] = []

    async def spawn(*args, **_kwargs):
        captured.extend(args)
        return process

    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "ok"}
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("whisperstream.llama_server._create_subprocess_exec", spawn)
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: context)

    supervisor = LlamaServerSupervisor(
        artifact,
        binary_path=binary,
        n_gpu_layers_provider=lambda: 0,
        device_index_provider=lambda: 4,
        acceleration=RuntimeAcceleration.METAL,
    )
    await supervisor.start()
    assert captured[captured.index("--n-gpu-layers") + 1] == "99"
    assert "--device" not in captured
    await supervisor.stop()


@pytest.mark.asyncio
async def test_metal_runtime_health_gate_fallback_recovery_and_cleanup(
    tmp_path: Path,
) -> None:
    from whisperstream.macos_local_llm_runtime import MacosManagedLlamaRuntime

    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"GGUF")
    supervisor = _Supervisor(artifact)
    runtime = MacosManagedLlamaRuntime(
        supervisor,
        catalog_id="candidate-a",
        artifact_path=artifact,
        metal_probe=lambda _binary: True,
    )

    wrong = await runtime.start(
        _runtime_request(runtime, acceleration=RuntimeAcceleration.CPU)
    )
    assert wrong.failure is RuntimeFailure.UNSUPPORTED_ACCELERATION

    started = await runtime.start(
        _runtime_request(runtime, acceleration=RuntimeAcceleration.METAL)
    )
    assert started.stage is RuntimeStage.LOADING
    cold = await runtime.infer(
        LocalInferenceRequest("cold", 1, 1.0, "private raw"),
        AsyncMock(return_value="must not run"),
    )
    assert cold.failure is RuntimeFailure.UNAVAILABLE

    warmed = await runtime.warm(1)
    assert warmed.stage is RuntimeStage.READY

    async def transport(_request: LocalInferenceRequest) -> str:
        return "enhanced"

    success = await runtime.infer(
        LocalInferenceRequest("ready", 1, 1.0, "private raw"), transport
    )
    assert success.enhanced_text == "enhanced"
    assert "private raw" not in repr(success)

    supervisor.is_running = False
    supervisor.is_ready = False
    crashed = await runtime.infer(
        LocalInferenceRequest("crashed", 1, 1.0, "private raw"), transport
    )
    assert crashed.failure is RuntimeFailure.UNAVAILABLE
    recovered = await runtime.start(
        _runtime_request(runtime, acceleration=RuntimeAcceleration.METAL)
    )
    assert recovered.stage is RuntimeStage.LOADING
    assert (await runtime.warm(1)).stage is RuntimeStage.READY

    await runtime.restart_for_model(artifact, catalog_id="candidate-a")
    stale = await runtime.infer(
        LocalInferenceRequest("stale", 1, 1.0, "private raw"), transport
    )
    assert stale.failure is RuntimeFailure.STALE_GENERATION
    await runtime.close()
    await runtime.close()
    assert runtime._active == {}
    assert supervisor.stop_calls == 1


@pytest.mark.asyncio
async def test_metal_probe_failure_is_typed_and_does_not_spawn(tmp_path: Path) -> None:
    from whisperstream.macos_local_llm_runtime import MacosManagedLlamaRuntime

    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"GGUF")
    supervisor = _Supervisor(artifact)
    runtime = MacosManagedLlamaRuntime(
        supervisor,
        catalog_id="candidate-a",
        artifact_path=artifact,
        metal_probe=lambda _binary: False,
    )
    snapshot = await runtime.start(
        _runtime_request(runtime, acceleration=RuntimeAcceleration.METAL)
    )
    assert snapshot.failure is RuntimeFailure.UNSUPPORTED_ACCELERATION
    assert supervisor.is_running is False


@pytest.mark.asyncio
async def test_metal_start_timeout_cleans_a_partially_started_owner(
    tmp_path: Path,
) -> None:
    from whisperstream.macos_local_llm_runtime import MacosManagedLlamaRuntime

    artifact = tmp_path / "public.gguf"
    artifact.write_bytes(b"GGUF")
    supervisor = _Supervisor(artifact)

    async def partial_start() -> None:
        supervisor.is_running = True
        await asyncio.sleep(1)

    supervisor.start = partial_start
    runtime = MacosManagedLlamaRuntime(
        supervisor,
        catalog_id="candidate-a",
        artifact_path=artifact,
        metal_probe=lambda _binary: True,
    )
    request = RuntimeStartRequest(
        runtime.current_generation,
        LocalRuntimeKind.MANAGED_LLAMA,
        RuntimeOwnership.APPLICATION,
        runtime._model,
        RuntimeAcceleration.METAL,
        0.01,
    )
    snapshot = await runtime.start(request)
    assert snapshot.failure is RuntimeFailure.START_TIMEOUT
    assert supervisor.is_running is False
    assert supervisor.stop_calls == 1


def test_pinned_macos_sidecar_stage_is_exact_and_content_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import fetch_macos_llama_server as staging

    archive = tmp_path / "llama.tar.gz"
    payloads = {
        "llama-test/llama-server": b"Mach-O sidecar",
        "llama-test/LICENSE": b"MIT",
        "llama-test/libggml.dylib": b"Mach-O dylib",
    }
    with tarfile.open(archive, "w:gz") as bundle:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(staging, "LLAMA_CPP_SHA256", digest)
    monkeypatch.setattr(staging, "REQUIRED_DYLIBS", ("libggml.dylib",))
    checkout = tmp_path / "checkout"
    result = staging._stage_archive(archive, checkout)
    sidecar = (
        checkout
        / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    )
    resources = checkout / "src-tauri/resources/llama-cpp"
    assert sidecar.read_bytes() == b"Mach-O sidecar"
    if os.name == "posix":
        assert sidecar.stat().st_mode & 0o100
    assert (resources / "libggml.dylib").read_bytes() == b"Mach-O dylib"
    assert result["dylib_count"] == 1


def test_macos_bundle_contract_requires_pinned_llama_sidecar() -> None:
    config = json.loads(
        (
            Path(__file__).parents[1]
            / "src-tauri/tauri.macos.conf.json"
        ).read_text(encoding="utf-8")
    )
    assert "binaries/llama-server" in config["bundle"]["externalBin"]
    assert "resources/llama-cpp/*" in config["bundle"]["resources"]


def test_darwin_discovers_the_target_suffixed_dev_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import whisperstream.llama_server as module

    source = tmp_path / "src/whisperstream/llama_server.py"
    source.parent.mkdir(parents=True)
    source.write_text("# identity\n", encoding="utf-8")
    sidecar = (
        tmp_path
        / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"Mach-O")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("sys.executable", str(tmp_path / "missing-python"))
    monkeypatch.setattr(module, "__file__", str(source))
    assert module.find_bundled_llama_server() == sidecar


