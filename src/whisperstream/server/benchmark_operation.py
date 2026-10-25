"""GPU benchmark operation, measurement and durable result publication."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict, Optional

from ..enhancement_models import get_local_model

# The only model the automatic first-setup speed check measures.
AUTO_BENCHMARK_MODEL_ID = "stream-1-mini"


class BenchmarkOperation:
    """Own benchmark execution while borrowing the shared model download slot."""

    def __init__(
        self, *, current_config: Callable, save_config: Callable,
        reload_config: Callable, fetch_gguf: Callable,
        set_download_activity: Callable, broadcast: Callable,
        logger: logging.Logger,
    ) -> None:
        self._current_config = current_config
        self._save_config = save_config
        self._reload_config = reload_config
        self._fetch_gguf = fetch_gguf
        self._set_download_activity = set_download_activity
        self.broadcast = broadcast
        self._logger = logger
        self.thread: threading.Thread | None = None
        self.recheck_pending = False

    def try_start(self, *, mode: str, model_id: str | None) -> bool:
        existing = self.thread
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(
            target=self.run, kwargs={"mode": mode, "model_id": model_id},
            daemon=True, name="WhisperstreamBenchmark",
        )
        self.thread = thread
        thread.start()
        return True

    def publish(self, event: Dict[str, Any]) -> None:
        """Log and send one content-free benchmark event payload.

        Benchmark events contain only routing, device, speed, and verdict
        metadata. Keeping this helper scoped to that contract avoids ever
        logging arbitrary shared-WebSocket events, which may contain transcript
        content.
        """
        event_type = event.get("type")
        if event_type not in ("benchmark.progress", "benchmark.result"):
            raise ValueError("benchmark broadcaster received a non-benchmark event")
        log = (
            self._logger.info
            if event_type == "benchmark.result"
            else self._logger.debug
        )
        log(
            "[Benchmark] event send type=%s phase=%s verdict=%s mode=%s",
            event_type,
            event.get("phase", "none"),
            event.get("verdict", "none"),
            event.get("mode", "none"),
        )
        self.broadcast(event)

    def bench_one_rung(self, binary_path, gguf_path, real_devices, rung_id: str, *, cancel_event: threading.Event | None = None):
        """Bench ONE model across each real Vulkan device; pick the best.

        Broadcasts a device-start ``benchmark.progress`` event before each
        serial measurement, streams live generated words/s ticks tagged with
        the same model and Vulkan device identity, then broadcasts an explicit
        ``device_complete`` progress event with the authoritative terminal
        llama.cpp timing and verdict. Benches every real device and returns the
        highest-tps measured device, including below-cutoff results. If no subprocess
        produces a measurement, raises so the caller can report infrastructure
        failure instead of a measured hardware verdict.

        The ``on_tick`` value is ANIMATION ONLY; the authoritative verdict
        number comes from the FINAL ``run_benchmark`` timings (fail-closed -- see
        ``gpu_benchmark._do_completion``).

        Returns
        -------
        tuple
            ``(best_device, best_tps, best_verdict)`` -- ``best_device`` is a
            ``VulkanDevice`` (or ``None`` when none produced a measurement),
            ``best_tps`` its measured decode tok/s (or ``None``), and
            ``best_verdict`` the tri-state verdict of the winning measured
            device (``"unviable"`` when measurements completed but none ran).
        """
        import asyncio as _asyncio

        from ..gpu_benchmark import run_benchmark
        from ..model_downloader import DownloadCancelled

        self._logger.info(
            "[Benchmark] decode start devices=%d", len(real_devices)
        )

        best_device = None  # VulkanDevice with the highest runnable tok/s
        best_tps: Optional[float] = None
        best_verdict = "unviable"
        fastest_measured_tps: Optional[float] = None
        completed_measurement = False
        for dev in real_devices:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("benchmark cancelled")
            # This is both the rung/device handoff and the explicit meter reset
            # signal. A rate is intentionally absent until this device produces
            # generated output.
            self.publish({
                "type": "benchmark.progress",
                "phase": "running",
                "model_id": rung_id,
                "device_name": dev.name,
                "device_index": dev.index,
            })

            def _on_tick(wps: float, *, _dev=dev) -> None:
                self.publish({
                    "type": "benchmark.progress",
                    "phase": "running",
                    "model_id": rung_id,
                    "device_name": _dev.name,
                    "device_index": _dev.index,
                    "wps": wps,
                })

            try:
                benchmark_kwargs = {
                    "binary_path": Path(binary_path),
                    "gguf_path": gguf_path,
                    "device_index": dev.index,
                    "on_tick": _on_tick,
                }
                if getattr(dev, "backend", "vulkan") == "metal":
                    benchmark_kwargs["device_index"] = None
                    benchmark_kwargs["acceleration"] = "metal"
                if rung_id in {"gemma4-e2b-instruct", "gemma4-e2b-google-qat"}:
                    benchmark_kwargs["prompt_key"] = "gemma"
                elif rung_id == AUTO_BENCHMARK_MODEL_ID:
                    benchmark_kwargs["prompt_key"] = get_local_model(rung_id)["prompt_key"]
                result = _asyncio.run(
                    run_benchmark(**benchmark_kwargs)
                )
            except Exception as exc:
                self._logger.error(
                    "[Benchmark] decode failed device_ordinal=%d "
                    "error_type=%s action=skip",
                    dev.index,
                    type(exc).__name__,
                )
                # A failed subprocess is still a completed serial-device
                # attempt. Surface the fail-closed terminal state so the
                # frontend can give it the same deliberate handoff beat as a
                # measured result. ``None`` is authoritative here: llama.cpp
                # produced no usable terminal timing.
                self.publish({
                    "type": "benchmark.progress",
                    "phase": "device_complete",
                    "model_id": rung_id,
                    "device_name": dev.name,
                    "device_index": dev.index,
                    "measured_tok_per_s": None,
                    "predicted_n": None,
                    "verdict": "unviable",
                })
                continue
            self.publish({
                "type": "benchmark.progress",
                "phase": "device_complete",
                "model_id": rung_id,
                "device_name": dev.name,
                "device_index": dev.index,
                "measured_tok_per_s": result.predicted_per_second,
                "predicted_n": result.predicted_n,
                "verdict": result.verdict,
            })
            completed_measurement = completed_measurement or result.predicted_per_second is not None
            if result.predicted_per_second is not None:
                fastest_measured_tps = max(
                    fastest_measured_tps or 0.0, result.predicted_per_second,
                )
            self._logger.info(
                "[Benchmark] decode device_ordinal=%d verdict=%s tps=%s",
                dev.index,
                result.verdict,
                f"{result.predicted_per_second:.1f}"
                if result.predicted_per_second is not None else "none",
            )
            if (
                result.predicted_per_second is not None
                and (best_tps is None or result.predicted_per_second > best_tps)
            ):
                best_tps = result.predicted_per_second
                best_device = dev
                best_verdict = result.verdict
        if not completed_measurement:
            raise RuntimeError("benchmark produced no completed measurement")
        # Preserve each model's measured device/rate for comparison. The
        # verdict still gates activation; measurement never enables cleanup.
        return best_device, best_tps if best_tps is not None else fastest_measured_tps, best_verdict

    def broadcast_infrastructure_error(self, mode: str) -> None:
        """End a benchmark that could not produce a hardware measurement."""
        self.publish({
            "type": "benchmark.result",
            "verdict": "unviable",
            "outcome": "infrastructure_error",
            "model_downloaded": False,
            "freshly_downloaded": False,
            "mode": mode,
        })

    def run(
        self, *, mode: str = "auto", model_id: Optional[str] = None
    ) -> None:
        """Measure the selected model; auto (and bodyless) requests measure Stream-1 Mini.

        Infrastructure failures never become cached hardware verdicts.
        """
        try:
            if mode == "manual":
                self.run_manual(model_id)
            else:
                self.run_auto()
        finally:
            self.thread = None

    def run_auto(self) -> None:
        """Measure only the default Stream-1 Mini; Customizable is manual opt-in."""
        results: list[dict] = []
        self.run_manual(AUTO_BENCHMARK_MODEL_ID, _results=results)
        if not results:  # Download cancelled.
            return
        result = dict(results[-1])
        result["mode"] = "auto"
        self.publish(result)

    def run_manual(self, model_id: Optional[str], *, _results: list[dict] | None = None, _cancel_event: threading.Event | None = None) -> None:
        """MANUAL SINGLE-MODEL: bench exactly the one selected model.

        Benches EXACTLY ``model_id`` (Gemma included), downloading it without
        changing the active model. The frontend applies a successful explicit
        choice after the result. It measures every real device and CAN return
        ``marginal`` (which arms the manual consent dialogs). No descend or
        ladder.
        """
        from ..enhancement_models import (
            LOCAL_BUNDLED_MODELS,
            get_local_bundled_model,
            resolve_local_model_path,
        )
        from ..gpu_benchmark import (
            gpu_fingerprint,
            list_devices_stdout,
            prefilter_verdict,
            available_acceleration_devices,
        )
        from ..llama_server import find_bundled_llama_server
        from ..setup import DownloadCancelled

        # Resolve the model to bench. Fall back to the active local model, then
        # the catalog default, when the caller passed nothing / an unknown id.
        resolved = (
            model_id
            or self._current_config().enhancement_local_model
            or LOCAL_BUNDLED_MODELS[0]["id"]
        )
        catalog_entry = get_local_bundled_model(resolved)
        if catalog_entry is None:
            resolved = LOCAL_BUNDLED_MODELS[0]["id"]
            catalog_entry = LOCAL_BUNDLED_MODELS[0]

        def emit_result(payload):
            payload.setdefault("measured_model_id", resolved)
            if _results is not None:
                _results.append(payload)
            else:
                self.publish(payload)

        def infrastructure_error():
            emit_result({
                "type": "benchmark.result", "verdict": "unviable",
                "outcome": "infrastructure_error", "model_downloaded": False,
                "freshly_downloaded": False, "mode": "manual",
            })

        try:
            binary_path = find_bundled_llama_server()
            if binary_path is None:
                self._logger.warning(
                    "[Benchmark] binary_not_found; benchmark unavailable"
                )
                infrastructure_error()
                return

            # --- Step 1: prefilter (cheap, no model load) ---
            self.publish(
                {"type": "benchmark.progress", "phase": "prefilter"}
            )
            self._logger.info(
                "[Benchmark] manual prefilter start model_id=%s", resolved
            )
            stdout = list_devices_stdout(Path(binary_path))
            verdict = prefilter_verdict(stdout)
            self._logger.info("[Benchmark] manual prefilter verdict=%s", verdict)

            if verdict != "pass":
                emit_result({
                    "type": "benchmark.result",
                    "verdict": "unviable",
                    "model_downloaded": False,
                    "freshly_downloaded": False,
                    "mode": "manual",
                })
                return

            # --- Step 2: download model X without changing active config.
            # Shares _download_thread / _download_cancel_event so cancel
            # aborts here with NO config write. ---
            cancel_event = _cancel_event if _cancel_event is not None else threading.Event()
            self._set_download_activity(self.thread, cancel_event)

            gguf_path = resolve_local_model_path(catalog_entry)
            assert gguf_path is not None
            self._logger.info(
                "[Benchmark] manual download start model_id=%s", resolved
            )
            try:
                ok, existed_before = self._fetch_gguf(
                    resolved, cancel_event
                )
                if cancel_event.is_set():
                    self._logger.info(
                        "[Benchmark] manual aborted reason=download_cancelled"
                    )
                    return
                if not ok:
                    self._logger.warning(
                        "[Benchmark] manual aborted "
                        "reason=gguf_missing_after_download"
                    )
                    infrastructure_error()
                    return
            except DownloadCancelled:
                self._logger.info(
                    "[Benchmark] manual aborted reason=download_cancelled"
                )
                return
            finally:
                if _cancel_event is None:
                    self._set_download_activity(None, None)

            if _cancel_event is None:
                self.broadcast({"type": "download.complete", "kind": "local_llm"})

            # --- Step 3: measure model X across each real device ---
            # A failed primary download may have selected the verified Google GGUF.
            gguf_path = resolve_local_model_path(catalog_entry)
            assert gguf_path is not None
            from .. import gemma_artifacts
            measured_artifact = (
                gemma_artifacts.receipt(gguf_path.parent)
                if resolved == gemma_artifacts.PUBLIC_ID else ""
            )
            real = available_acceleration_devices(stdout)
            fingerprint = gpu_fingerprint(real)

            best_device, best_tps, best_verdict = self.bench_one_rung(
                binary_path, gguf_path, real, resolved,
                **({"cancel_event": _cancel_event} if _cancel_event is not None else {}),
            )

            if cancel_event.is_set():
                return

            if best_device is not None:
                device_name = best_device.name
                device_index = best_device.index
                verdict_final = best_verdict
            else:
                device_name = ""
                device_index = -1
                verdict_final = "unviable"

            self._logger.info(
                "[Benchmark] manual selected verdict=%s device_selected=%s tps=%s",
                verdict_final,
                str(device_index >= 0).lower(),
                f"{best_tps:.1f}" if best_tps is not None else "none",
            )

            # --- Step 4: single config write ---
            cfg = self._current_config()
            if resolved == gemma_artifacts.PUBLIC_ID:
                if not measured_artifact or measured_artifact != gemma_artifacts.receipt(gguf_path.parent):
                    raise RuntimeError("Benchmark model changed during measurement")
                cfg.enhancement_local_gpu_artifacts[resolved] = measured_artifact
            cfg.enhancement_local_gpu_verdicts[resolved] = verdict_final
            cfg.enhancement_local_gpu_devices[resolved] = device_index
            cfg.enhancement_local_gpu_device_names[resolved] = device_name
            if cfg.enhancement_local_model == resolved:
                cfg.enhancement_local_gpu_device = device_name
                cfg.enhancement_local_gpu_device_index = device_index
            cfg.enhancement_local_gpu_fingerprint = fingerprint
            self._save_config(cfg)
            self._reload_config(cfg)

            # --- Step 5: terminal result (viable | marginal | unviable) ---
            emit_result({
                "type": "benchmark.result",
                "verdict": verdict_final,
                "model_downloaded": True,
                "freshly_downloaded": not existed_before,
                "device": device_name,
                "device_index": device_index,
                "measured_model_id": resolved,
                "measured_tok_per_s": best_tps,
                "verdicts": {resolved: verdict_final},
                "mode": "manual",
            })

        except DownloadCancelled:
            return
        except Exception as exc:
            self._logger.error(
                "[Benchmark] manual failed error_type=%s action=retry",
                type(exc).__name__,
            )
            infrastructure_error()
