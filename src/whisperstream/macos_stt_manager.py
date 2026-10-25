"""Native-free parent manager for the macOS FluidAudio Parakeet worker."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .config_manager import get_log_dir
from .stt_contracts import SttCapabilities
from .stt_supervisor import SttWorkerSupervisor
from .supervised_stt_manager import SupervisedSttManager


MACOS_STT_MODEL_NAME = "parakeet-tdt-0.6b-v3"
MACOS_STT_WORKER_ENV_VAR = "WHISPERSTREAM_MACOS_STT_WORKER_BIN"
MACOS_STT_REQUIRED_ASSETS = (
    "Preprocessor.mlmodelc/model.mil",
    "Encoder.mlmodelc/model.mil",
    "Decoder.mlmodelc/model.mil",
    "JointDecisionv3.mlmodelc/model.mil",
    "parakeet_v3_vocab.json",
)


def default_macos_stt_worker_path() -> Path:
    """Return the internal source-build worker path without loading native state."""

    configured = os.environ.get(MACOS_STT_WORKER_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    # Tauri installs both external binaries in Contents/MacOS. Nuitka exposes
    # the original onefile launcher through this environment value while the
    # extracted Python runtime lives in a cache directory. Prefer the original
    # launcher so the engine can find its sibling worker without depending on
    # a source checkout or extraction path.
    onefile_binary = os.environ.get("NUITKA_ONEFILE_BINARY", "").strip()
    if onefile_binary:
        return Path(onefile_binary).expanduser().resolve().with_name(
            "whisperstream-macos-stt-worker"
        )

    try:
        launched = Path(sys.argv[0]).expanduser().resolve()
        if launched.is_file() and launched.name.lower() in {
            "whisperstream-engine",
            "whisperstream-engine.exe",
        }:
            return launched.with_name("whisperstream-macos-stt-worker")
    except (IndexError, OSError):
        pass

    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root
        / "native"
        / "macos-stt-worker"
        / "target"
        / "release"
        / "whisperstream-macos-stt-worker"
    )


def macos_stt_model_root() -> Path | None:
    """Resolve the lifecycle-owned FluidAudio cache without loading FluidAudio."""

    try:
        from .macos_model_lifecycle import create_macos_model_storage

        return create_macos_model_storage().active_model_root
    except (OSError, RuntimeError, ValueError):
        return None


def macos_stt_model_present() -> bool:
    """Return whether the exact prewarmed milestone cache is complete."""

    root = macos_stt_model_root()
    return root is not None and all(
        (root / relative).is_file() for relative in MACOS_STT_REQUIRED_ASSETS
    )


class MacosFluidAudioManager(SupervisedSttManager):
    """Parakeet contract backed by the native macOS protocol-v1 executable."""

    _log_tag = "macos-parakeet"
    engine_id = "parakeet"
    capabilities = SttCapabilities(final_alignment=True)

    def __init__(
        self,
        *,
        logger: logging.Logger,
        timeout: float = 300.0,
        worker_path: Path | None = None,
        worker_environment: Mapping[str, str] | None = None,
        lifecycle_probe_only: bool = False,
    ) -> None:
        if worker_environment is None:
            from .macos_model_lifecycle import create_macos_model_storage

            worker_environment = create_macos_model_storage().worker_environment()
        # FluidAudio uses this value to correct native token timestamps. Keep
        # final overlap ownership independent of the launcher's environment.
        worker_environment = {**worker_environment, "TDT_EMISSION_DELAY_FRAMES": "1"}
        executable = Path(worker_path or default_macos_stt_worker_path())
        supervisor = SttWorkerSupervisor(
            backend=self.engine_id,
            model_identifier=MACOS_STT_MODEL_NAME,
            diagnostic_dir=get_log_dir() / "stt-workers",
            logger=logger,
            command=(str(executable),),
            cwd=Path(__file__).resolve().parents[2],
            environment=worker_environment,
        )
        super().__init__(
            model_name=MACOS_STT_MODEL_NAME,
            threads=None,
            logger=logger,
            model_dir=Path("."),
            timeout=timeout,
            _supervisor=supervisor,
            _defer_initial_load=lifecycle_probe_only,
        )
        self._lifecycle_probe_only = lifecycle_probe_only

    def _load_body(self) -> dict[str, Any]:
        return {"model_name": MACOS_STT_MODEL_NAME}

    def start_worker_for_lifecycle_probe(self) -> int:
        """Start only the real worker handshake for internal process proof.

        This deliberately does not load FluidAudio model state. It exists so
        the Phase 2 file-only app can prove engine ownership and teardown of
        the packaged native child without opening any interaction surface.
        """

        if not self._lifecycle_probe_only:
            raise RuntimeError("lifecycle worker probe requires deferred model loading")
        self._supervisor.start()
        self._validate_worker_capabilities()
        worker_pid = self.worker_pid
        if worker_pid is None:
            raise RuntimeError("macOS lifecycle worker did not report a PID")
        self._logger.info(
            "macos lifecycle worker ready worker_pid=%s model_loaded=false",
            worker_pid,
        )
        return worker_pid

    def _log_loading(self, *, reason: str) -> None:
        self._logger.info(
            "macos parakeet model loading model=%s reason=%s",
            self._model_name,
            reason,
        )

    def _log_loaded(self, *, elapsed: float) -> None:
        self._logger.info(
            "macos parakeet model loaded latency=%.2fs worker_pid=%s",
            elapsed,
            self.worker_pid,
        )


__all__ = [
    "MACOS_STT_MODEL_NAME",
    "MACOS_STT_REQUIRED_ASSETS",
    "MACOS_STT_WORKER_ENV_VAR",
    "MacosFluidAudioManager",
    "default_macos_stt_worker_path",
    "macos_stt_model_present",
    "macos_stt_model_root",
]
