"""Local model benchmark and runtime status HTTP contracts."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from ..benchmark_operation import BenchmarkOperation
from ...config_manager import get_local_gpu_verdict


def register_local_model_status_routes(
    api: FastAPI, operation: BenchmarkOperation, *, current_config: Callable,
    current_supervisor: Callable, current_download_thread: Callable,
    logger: logging.Logger,
) -> None:
    @api.post("/api/enhancement/benchmark/start")
    async def start_benchmark(request: Request):
        """Kick off a GPU viability benchmark in one of two modes.

        Accepts an OPTIONAL JSON body ``{"mode": "auto"|"manual",
        "model_id": "<id>"}``. A missing/empty body (or a missing field)
        defaults to ``mode="auto"`` with ``model_id=None`` -- the
        fully-automatic check of Stream-1 Mini (Gemma excluded), with zero
        user decisions. ``mode="manual"`` benches
        EXACTLY the one ``model_id`` the user selected (Gemma included) and
        can return ``marginal``. See ``_run_benchmark`` for the full state
        machine and event shapes.

        Both modes broadcast ``benchmark.progress`` / ``benchmark.result``
        (plus the ``download.*`` ``kind:"local_llm"`` stream during the
        download leg) over the shared WebSocket.

        Refuses a second concurrent benchmark with 409. The download leg
        shares ``_download_thread`` / ``_download_cancel_event`` with the
        local-download endpoints so ``/api/enhancement/local-download/cancel``
        aborts a mid-download benchmark (no config write on cancel).
        """
        # Parse the optional body DEFENSIVELY: a missing/empty body, a
        # non-JSON body, or a non-object body all fall through to the auto
        # default (existing callers POST with no body at all).
        mode = "auto"
        model_id: Optional[str] = None
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            raw_mode = body.get("mode")
            if raw_mode in ("auto", "manual"):
                mode = raw_mode
            raw_model_id = body.get("model_id")
            if isinstance(raw_model_id, str) and raw_model_id:
                model_id = raw_model_id

        logger.info(
            "[Benchmark] start requested mode=%s requested_model=%s active_model=%s",
            mode,
            model_id or "none",
            current_config().enhancement_local_model or "none",
        )

        # Candidate artifacts are developer supplied and bypass every
        # production download/benchmark path. They are selected through
        # /api/enhancement/local-activate after a local availability check.
        if mode == "manual" and model_id:
            from ...enhancement_models import get_local_model

            catalog_entry = get_local_model(model_id)
            if (
                catalog_entry is not None
                and catalog_entry.get("source") == "candidate"
            ):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "candidate_benchmark_not_supported",
                        "model_id": str(catalog_entry["id"]),
                    },
                )

        if not operation.try_start(mode=mode, model_id=model_id):
            return JSONResponse(status_code=409, content={"status": "busy"})
        return {"status": "started"}

    @api.get("/api/enhancement/benchmark/verdict")
    async def get_benchmark_verdict():
        """Return the cached GPU benchmark verdict + device for the frontend seed.

        Read-only: surfaces the active local model's per-model verdict
        (one of ``not_run`` / ``viable`` / ``marginal`` / ``unviable``) and
        ``enhancement_local_gpu_device`` from config so the Enhancement page
        can render the re-check affordance and the correct provider state on
        mount, before any ``benchmark.result`` WebSocket event arrives.

        ``recheck_pending`` is True exactly once after a launch-time GPU-set
        change invalidated the verdict (read-and-clear), telling the frontend
        to auto-run the benchmark with progress.
        """
        cfg = current_config()
        recheck = operation.recheck_pending
        operation.recheck_pending = False
        return {
            "verdict": get_local_gpu_verdict(cfg, cfg.enhancement_local_model),
            "device": getattr(cfg, "enhancement_local_gpu_device", ""),
            "recheck_pending": recheck,
        }

    @api.get("/api/enhancement/local-recovery")
    async def get_local_recovery():
        """Disk recovery is distinct from an ordinarily cold supervisor."""
        from ...config_manager import is_loopback_endpoint
        from ...enhancement_models import get_local_model

        cfg = current_config()
        endpoint = cfg.enhancement_local_endpoint.strip()
        honor_endpoint = bool(endpoint) and is_loopback_endpoint(endpoint)
        model = None
        if cfg.enhancement_enabled and cfg.effective_provider() == "local" and not honor_endpoint:
            entry = get_local_model(cfg.enhancement_local_model)
            if entry and entry["downloadable"] and not entry["artifact_available"]:
                model = {key: entry[key] for key in ("id", "label", "disk_mb")}
        return {
            "model": model,
            "busy": any(
                thread is not None and thread.is_alive()
                for thread in (current_download_thread(), operation.thread)
            ),
        }

    @api.get("/api/llama/status")
    async def get_llama_status():
        """Return a snapshot of the production local-LLM supervisor state.

        Used by the Enhancement page on mount to hydrate the status badge
        before any WebSocket ``llama.status`` events arrive (late-connect
        seed — see research.md "Constraints Discovered"). The endpoint is
        read-only and has no side effects.

        Returns:
            {"state": "absent"} if no supervisor exists (pre-download,
            endpoint-override, or unknown model-id scenarios).

            {"state": <state>, "model": <gguf_filename>, "port": <int|None>}
            otherwise, where <state> is one of:
              "ready"   — supervisor.is_ready is True
              "warming" — subprocess alive but warmup not yet complete
              "dormant" — subprocess not running
        """
        supervisor = current_supervisor()
        if supervisor is None:
            return {"state": "absent"}
        if supervisor.is_ready:
            state = "ready"
        elif supervisor.is_running and not supervisor.is_ready:
            state = "warming"
        else:
            state = "dormant"
        return {
            "state": state,
            "model": supervisor._gguf_path.name,
            "port": supervisor.port if supervisor.port else None,
        }
