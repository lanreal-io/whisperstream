#!/usr/bin/env python3
"""Exercise the production Windows Gemma adapter through crash recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import sys
from pathlib import Path

import httpx
import psutil

from whisperstream.config_manager import get_local_llm_dir
from whisperstream.gpu_benchmark import (
    list_devices_stdout,
    parse_vulkan_devices,
    real_vulkan_devices,
)
from whisperstream.enhancement_models import get_local_model
from whisperstream.llama_server import (
    LlamaServerSupervisor,
    find_bundled_llama_server,
)
from whisperstream.local_llm_runtime import create_managed_runtime
from whisperstream.local_llm_runtime_contract import (
    LocalInferenceRequest,
    LocalRuntimeKind,
    RuntimeAcceleration,
    RuntimeFailure,
    RuntimeOwnership,
    RuntimeStage,
    RuntimeStartRequest,
)


CATALOG_ID = "gemma4-e2b-instruct"
PUBLIC_INPUT = "Please organize this public verification sentence into one concise note."


def _llama_pids() -> set[int]:
    found: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info.get("name") or "").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if name.startswith("llama-server"):
            found.add(int(process.info["pid"]))
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        print(json.dumps({"status": "invalid-host"}, sort_keys=True))
        return 2

    catalog = get_local_model(CATALOG_ID)
    binary = find_bundled_llama_server()
    if catalog is None or binary is None:
        print(
            json.dumps(
                {"availability": "not_available", "reason": "runtime_missing"},
                sort_keys=True,
            )
        )
        return 3
    artifact = get_local_llm_dir() / str(catalog["gguf_filename"])
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        print(
            json.dumps(
                {"availability": "not_available", "reason": "artifact_missing"},
                sort_keys=True,
            )
        )
        return 3
    devices = real_vulkan_devices(
        parse_vulkan_devices(list_devices_stdout(binary))
    )
    if not devices:
        print(
            json.dumps(
                {"availability": "not_available", "reason": "gpu_missing"},
                sort_keys=True,
            )
        )
        return 3

    before = _llama_pids()
    supervisor = LlamaServerSupervisor(
        artifact,
        binary_path=binary,
        idle_timeout_seconds=0,
        n_gpu_layers_provider=lambda: 99,
        device_index_provider=lambda: devices[0].index,
        model_id_provider=lambda: CATALOG_ID,
    )
    runtime = create_managed_runtime(
        supervisor,
        catalog_id=CATALOG_ID,
        artifact_path=artifact,
    )
    if runtime is None:
        raise RuntimeError("Windows managed runtime was not selected")

    async def infer_transport(request: LocalInferenceRequest) -> str:
        async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
            response = await client.post(
                f"{supervisor.base_url}/chat/completions",
                json={
                    "model": artifact.stem,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return one concise sentence without commentary.",
                        },
                        {"role": "user", "content": request.raw_text},
                    ],
                    "max_tokens": 32,
                    "temperature": 0.0,
                },
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])

    try:
        generation = runtime.current_generation
        start_request = RuntimeStartRequest(
            generation,
            LocalRuntimeKind.MANAGED_LLAMA,
            RuntimeOwnership.APPLICATION,
            runtime._model,
            RuntimeAcceleration.VULKAN,
            60.0,
        )
        started = await runtime.start(start_request)
        warmed = await runtime.warm(generation)
        if started.stage not in {RuntimeStage.LOADING, RuntimeStage.READY}:
            raise RuntimeError("initial managed runtime start failed")
        if warmed.stage is not RuntimeStage.READY:
            raise RuntimeError("initial managed runtime warm failed")

        first_request = LocalInferenceRequest(
            "unit10-before-crash", generation, 30.0, PUBLIC_INPUT
        )
        first_result = await runtime.infer(first_request, infer_transport)
        if first_result.use_raw or not first_result.enhanced_text:
            raise RuntimeError("initial production Gemma inference failed")

        first_process = supervisor._process
        if first_process is None:
            raise RuntimeError("managed process identity was not retained")
        first_pid = first_process.pid
        first_process.kill()
        await first_process.wait()
        await asyncio.sleep(0.1)

        down_request = LocalInferenceRequest(
            "unit10-while-down", generation, 5.0, PUBLIC_INPUT
        )
        down_result = await runtime.infer(down_request, infer_transport)
        if not down_result.use_raw or down_result.failure is not RuntimeFailure.UNAVAILABLE:
            raise RuntimeError("crashed runtime did not fail closed to raw text")

        restarted = await runtime.start(start_request)
        rewarmed = await runtime.warm(generation)
        if restarted.stage not in {RuntimeStage.LOADING, RuntimeStage.READY}:
            raise RuntimeError("managed runtime restart failed")
        if rewarmed.stage is not RuntimeStage.READY:
            raise RuntimeError("managed runtime recovery warm failed")
        second_process = supervisor._process
        if second_process is None or second_process.pid == first_pid:
            raise RuntimeError("crashed process identity was not replaced")

        recovered_request = LocalInferenceRequest(
            "unit10-after-recovery", generation, 30.0, PUBLIC_INPUT
        )
        recovered_result = await runtime.infer(recovered_request, infer_transport)
        if recovered_result.use_raw or not recovered_result.enhanced_text:
            raise RuntimeError("production Gemma inference did not recover")
        output_sha256 = hashlib.sha256(
            recovered_result.enhanced_text.encode("utf-8")
        ).hexdigest()
    finally:
        await runtime.close()
        await asyncio.sleep(0.25)

    leaked = sorted(_llama_pids() - before)
    print(
        json.dumps(
            {
                "availability": "available",
                "status": "pass" if not leaked else "failed",
                "model_class": "production-gemma4-e2b-instruct",
                "artifact_bytes": artifact.stat().st_size,
                "artifact_sha256": _sha256(artifact),
                "full_gpu_layers": 99,
                "real_device_count": len(devices),
                "same_generation_recovery": True,
                "raw_fallback_while_down": True,
                "owned_process_replaced": True,
                "output_sha256": output_sha256,
                "new_llama_processes_after_cleanup": leaked,
                "private_content_emitted": False,
            },
            sort_keys=True,
        )
    )
    return 0 if not leaked else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
