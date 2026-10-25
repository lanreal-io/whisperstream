"""Llama-server lifecycle supervisor.

Owns the ``llama-server.exe`` subprocess (llama.cpp) that provides an
OpenAI-compatible HTTP endpoint for local LLM enhancement.

Lifecycle (lazy + idle-offload)
-------------------------------
1. The instance is created cold (no subprocess yet) by the server.
2. The dictation pipeline calls ``kick_warm(loop)`` synchronously from a
   non-loop thread when the user starts recording. The supervisor
   schedules a background coroutine that spawns ``llama-server`` and
   issues one tiny warmup inference. ``is_ready`` flips True once warmup
   completes; until then, ``enhance()`` sees ``is_ready == False`` and
   skips the LLM call (returns raw text immediately, no wait).
3. ``mark_active()`` resets an idle timer on every recording start. After
   ``idle_timeout_seconds`` of inactivity, the idle timer schedules
   ``stop()`` on the loop, the subprocess is terminated, and ``is_ready``
   flips back to False.
4. ``stop()`` is also called on config deactivation and FastAPI shutdown.
   Windows children are created inside a backend-owned kill-on-close Job Object.
   macOS children are owned by a guardian watching a backend-only lifetime pipe.

This module owns no event loop of its own — the server hands one in via
``kick_warm(loop)``. The idle ``threading.Timer`` schedules coroutines on
that captured loop via ``call_soon_threadsafe``.

Binary discovery
----------------
In a packaged build (Nuitka onefile) the binary lives next to the
extracted exe. In dev it lives at ``src-tauri/binaries/``. Use the
``find_bundled_llama_server()`` helper or pass ``binary_path`` directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import httpx

if TYPE_CHECKING:
    from .windows_child_process import WindowsJobProcess
    from .macos_child_process import MacOSGuardianProcess


_TAG = "[LlamaServer]"
_HEALTH_POLL_INTERVAL = 0.25  # seconds
_HEALTH_TIMEOUT = 30.0        # seconds total before giving up
_KILL_WAIT = 3.0              # seconds after SIGTERM before forceful kill
_CONTEXT_SIZE = 4096          # -c: bounded context keeps RAM predictable for transcripts


def _thinking_off_model(model_id: str | None) -> bool:
    """Qwen-family fixed-prompt models must start with thinking disabled."""
    if not model_id:
        return False
    from .enhancement_models import get_local_model
    from .enhancement_prompts import STREAM_MINI_PROMPT_KEYS
    entry = get_local_model(model_id)
    return bool(entry and entry.get("prompt_key") in STREAM_MINI_PROMPT_KEYS)
_WARMUP_TIMEOUT = 60.0        # First inference does model load + prefill of the full
                              # system prompt (~750 tokens). Empirically ~12s on
                              # the user's CPU; 60s is a generous ceiling.
                              # GPU offload (n_gpu_layers=99): ~2.2s cold load
                              # (health_ready ~14s incl. model map), warmup ~27s
                              # observed on RTX 4070 Ti SUPER — well under ceiling.

# Google's canonical Gemma 4 template is pinned independently of the GGUF so
# existing installs and in-place upstream GGUF replacements serialize chat
# identically. Source: google/gemma-4-E2B-it commit
# 899537982545a3e55ce64d34462b2efa5af85232.
_GEMMA_4_MODEL_ID = "gemma4-e2b-instruct"
_GEMMA_4_MODEL_IDS = {_GEMMA_4_MODEL_ID, "gemma4-e2b-google-qat"}
_GEMMA_4_TEMPLATE_FILENAME = "gemma-4-e2b-chat-template.jinja"
_GEMMA_4_TEMPLATE_SHA256 = (
    "0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5"
)


@dataclass(frozen=True)
class LlamaServerRuntimeMetadata:
    """Content-free facts about the currently loaded llama.cpp process."""

    device_mode: str
    device: Optional[str]
    n_gpu_layers: Optional[int]
    load_duration_ms: Optional[int]


def _onefile_install_dir() -> Optional[Path]:
    """Resolve the directory of the ORIGINAL onefile exe in a packaged build.

    Under the Nuitka onefile sidecar, ``sys.executable`` points into the temp
    extraction dir (``%LOCALAPPDATA%/Whisperstream-runtime/<VERSION>/...``), NOT
    the install dir where Tauri's ``externalBin`` deposits ``llama-server.exe``
    next to ``whisperstream-engine.exe`` (e.g. ``C:\\Program Files\\Whisperstream``).
    Nuitka exposes the original onefile path via ``NUITKA_ONEFILE_BINARY``; fall
    back to ``sys.argv[0]`` which is also the original launcher path. Returns
    ``None`` in dev (no onefile env var; ``sys.argv[0]`` is the python module).
    """
    onefile = os.environ.get("NUITKA_ONEFILE_BINARY")
    if onefile:
        try:
            return Path(onefile).resolve().parent
        except OSError:
            pass
    # Fallback: argv[0] is the launched onefile exe in a packaged build.
    try:
        argv0 = Path(sys.argv[0]).resolve()
        if argv0.suffix.lower() == ".exe" or (
            platform.system() == "Darwin" and argv0.name == "whisperstream-engine"
        ):
            return argv0.parent
    except (OSError, IndexError):
        pass
    return None


def find_bundled_llama_server() -> Optional[Path]:
    """Return the platform llama-server sidecar, or ``None`` if not found.

    Search order:
    1. Next to the ORIGINAL onefile exe (the real install dir; ``sys.executable``
       is the temp extraction dir under the Nuitka onefile, so we resolve the
       install dir via ``NUITKA_ONEFILE_BINARY`` / ``sys.argv[0]``). This is the
       packaging-Bug-A fix: without it the installed app logs ``binary_not_found``.
    2. Next to ``sys.executable`` (covers a non-onefile / standalone layout where
       Tauri's ``externalBin`` sits beside the running exe).
    3. ``src-tauri/binaries/llama-server-x86_64-pc-windows-msvc.exe`` relative to
       the repo root (dev runs using the source tree).
    """
    _logger = logging.getLogger(__name__)
    candidate_dirs: list[Path] = []

    # 1. Packaged onefile: binary lives next to the ORIGINAL onefile exe.
    install_dir = _onefile_install_dir()
    if install_dir is not None:
        candidate_dirs.append(install_dir)

    # 2. Standalone / non-onefile: next to sys.executable.
    candidate_dirs.append(Path(sys.executable).parent)

    names = (
        ("llama-server", "llama-server.exe")
        if platform.system() == "Darwin"
        else ("llama-server.exe", "llama-server")
    )
    for directory in candidate_dirs:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                _logger.debug(
                    "%s found_bundled_llama_server dir=%s", _TAG, directory
                )
                return candidate

    # 3. Dev: walk up from this file to the repo root.
    # Path(__file__) = <repo>/src/whisperstream/llama_server.py
    #   parents[2] = <repo>   <- repo root
    try:
        repo_root = Path(__file__).resolve().parents[2]
        binary_dir = repo_root / "src-tauri" / "binaries"
        dev_names = (
            (
                "llama-server-aarch64-apple-darwin",
                "llama-server-x86_64-pc-windows-msvc.exe",
            )
            if platform.system() == "Darwin"
            else (
                "llama-server-x86_64-pc-windows-msvc.exe",
                "llama-server-aarch64-apple-darwin",
            )
        )
        for name in dev_names:
            candidate_dev = binary_dir / name
            if candidate_dev.is_file():
                return candidate_dev
    except (IndexError, OSError):
        pass

    _logger.warning(
        "%s find_bundled_llama_server: not found; searched %s + dev tree",
        _TAG,
        [str(d) for d in candidate_dirs],
    )
    return None


def find_bundled_gemma_chat_template() -> Optional[Path]:
    """Return the pinned Gemma 4 chat-template path, or ``None`` if absent.

    Search order mirrors the installed-resource convention used by the EULA:

    1. ``resources/`` beside the original Nuitka onefile executable (Tauri
       installer layout), then beside the executable itself (portable layout).
    2. The same two locations beside ``sys.executable`` for standalone/frozen
       builds.
    3. ``src-tauri/resources/`` relative to the source tree for development,
       including isolated git worktrees.

    The template is an external Tauri resource rather than a file in Nuitka's
    temporary extraction directory. That gives the llama-server subprocess a
    stable path for its full lifetime.
    """
    candidate_dirs: list[Path] = []

    configured_resources = os.environ.get(
        "WHISPERSTREAM_BUNDLE_RESOURCES_DIR", ""
    ).strip()
    if configured_resources:
        candidate = (
            Path(configured_resources).expanduser().resolve()
            / _GEMMA_4_TEMPLATE_FILENAME
        )
        if candidate.is_file():
            return candidate

    install_dir = _onefile_install_dir()
    if install_dir is not None:
        candidate_dirs.append(install_dir)

    if getattr(sys, "frozen", False):
        candidate_dirs.append(Path(sys.executable).parent)

    for directory in candidate_dirs:
        for candidate in (
            directory.parent / "Resources" / _GEMMA_4_TEMPLATE_FILENAME,
            directory / "resources" / _GEMMA_4_TEMPLATE_FILENAME,
            directory / _GEMMA_4_TEMPLATE_FILENAME,
        ):
            if candidate.is_file():
                return candidate

    try:
        repo_root = Path(__file__).resolve().parents[2]
        candidate_dev = (
            repo_root
            / "src-tauri"
            / "resources"
            / _GEMMA_4_TEMPLATE_FILENAME
        )
        if candidate_dev.is_file():
            return candidate_dev
    except (IndexError, OSError):
        pass

    return None


def llama_dll_dir(binary_path: Path) -> Path:
    """Return the directory that holds llama.cpp's ggml backend DLLs.

    Packaging Bug B: llama.cpp resolves both its load-time imports (``llama.dll``,
    ``ggml.dll``, ``ggml-base.dll``) and its dynamically loaded backend DLLs
    (``ggml-vulkan.dll`` etc.) relative to the exe dir / CWD. The DLLs and the
    exe are NOT always co-located:

    - **dev:** ``fetch-llama-server.sh`` stages the exe AND the DLLs together in
      ``src-tauri/binaries/`` → DLLs sit in ``binary_path.parent``.
    - **packaged (NSIS):** Tauri's ``externalBin`` puts ``llama-server.exe`` in
      the install ROOT, but the ggml DLLs (declared as ``binaries/*.dll``
      resources) land in the ``binaries/`` SUBDIR → DLLs sit in
      ``binary_path.parent / "binaries"``.

    Pinning the spawn CWD to whichever dir actually contains the DLLs makes both
    layouts work (CWD is on the Windows DLL search path, so even the load-time
    imports resolve). Probed via a sentinel DLL (``ggml-vulkan.dll``); falls back
    to the exe dir.
    """
    here = binary_path.parent
    if (here / "ggml-vulkan.dll").exists():
        return here
    sub = here / "binaries"
    if (sub / "ggml-vulkan.dll").exists():
        return sub
    return here


def llama_library_dir(binary_path: Path) -> Path:
    """Resolve the stable directory containing llama.cpp runtime libraries."""

    if platform.system() != "Darwin":
        return llama_dll_dir(binary_path)

    configured = os.environ.get("WHISPERSTREAM_BUNDLE_RESOURCES_DIR", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser().resolve() / "llama-cpp")
    candidates.extend(
        (
            binary_path.parent / "llama-cpp",
            binary_path.parent.parent / "Resources" / "resources" / "llama-cpp",
        )
    )
    try:
        candidates.append(
            Path(__file__).resolve().parents[2]
            / "src-tauri"
            / "resources"
            / "llama-cpp"
        )
    except (IndexError, OSError):
        pass
    for candidate in candidates:
        if (candidate / "libggml.dylib").is_file():
            return candidate
    return binary_path.parent


def llama_subprocess_environment(binary_path: Path) -> dict[str, str]:
    """Return an inherited environment with the bundled Darwin dylibs visible."""

    env = dict(os.environ)
    if platform.system() == "Darwin":
        library_dir = str(llama_library_dir(binary_path))
        existing = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            item for item in (library_dir, existing) if item
        )
    return env


def runtime_acceleration_for_platform():
    """Return the managed llama.cpp accelerator selected by this platform."""

    from .local_llm_runtime_contract import RuntimeAcceleration

    if platform.system() == "Darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }:
        return RuntimeAcceleration.METAL
    return RuntimeAcceleration.VULKAN


def _pick_free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, then close and return it.

    Slightly race-prone (the port could be claimed between close and
    llama-server's bind), but the window is small and the healthcheck
    retry loop covers an unlucky reuse.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _create_subprocess_exec(*args, **kwargs):
    if os.name == "nt":
        # File-only/import-only backends must not load native Windows APIs.
        from .windows_child_process import spawn_windows_job_process

        # No await between atomic job assignment and returning ownership.
        return spawn_windows_job_process(args, cwd=kwargs["cwd"], env=kwargs["env"])
    if sys.platform == "darwin":
        from .macos_child_process import spawn_macos_guardian_process

        return await spawn_macos_guardian_process(args, cwd=kwargs["cwd"], env=kwargs["env"])
    return await asyncio.create_subprocess_exec(*args, **kwargs)


class LlamaServerSupervisor:
    """Manages a single ``llama-server`` child process.

    Parameters
    ----------
    gguf_path:
        Path to the GGUF model file to serve.
    binary_path:
        Absolute path to the ``llama-server`` executable.
    logger:
        Optional Python logger; if None a module-level logger is used.
    idle_timeout_seconds:
        Seconds of inactivity (no ``mark_active`` call) before the
        subprocess is auto-stopped. ``0.0`` disables auto-offload (keeps
        the model resident forever, matching the "keep in memory" toggle).
    n_gpu_layers:
        Value passed as ``--n-gpu-layers`` to llama-server. ``0`` keeps all
        layers on CPU (default, preserves existing behavior for callers that
        do not pass this kwarg). ``99`` fully offloads a model that fits in
        GPU VRAM (use when the GPU benchmark verdict is ``"viable"``).
    n_gpu_layers_provider:
        Optional zero-arg callable returning the ``--n-gpu-layers`` value,
        evaluated by ``start()`` at spawn time. Takes precedence over
        ``n_gpu_layers`` when set. Pass this (rather than a fixed
        ``n_gpu_layers``) when the value derives from a config field that may be
        written after construction (e.g. the GPU benchmark verdict), so the spawn
        reads the current value instead of a stale construction-time snapshot.
    model_id_provider:
        Optional zero-arg callable returning the current local model id. The
        canonical production Gemma id enables the pinned external chat template
        at spawn time. Reading at spawn time keeps model switches correct while
        leaving Stream models and arbitrary development candidates unchanged.
    """

    def __init__(
        self,
        gguf_path: Path,
        *,
        binary_path: Path,
        logger: Optional[logging.Logger] = None,
        idle_timeout_seconds: float = 0.0,
        on_state_change: Optional[Callable[[dict], None]] = None,
        n_gpu_layers: int = 0,
        n_gpu_layers_provider: Optional[Callable[[], int]] = None,
        device_index_provider: Optional[Callable[[], int]] = None,
        model_id_provider: Optional[Callable[[], str]] = None,
        acceleration=None,
    ) -> None:
        self._gguf_path = gguf_path
        self._binary_path = binary_path
        self._logger = logger or logging.getLogger(__name__)
        self._on_state_change = on_state_change
        self._n_gpu_layers = n_gpu_layers
        # Resolved at spawn time (not construction) when provided, so a GPU
        # verdict committed AFTER this supervisor is built still takes effect.
        # The benchmark flow constructs the supervisor during the download step,
        # BEFORE it writes verdict="viable"; a construction-time ngl would freeze
        # at 0 -> CPU. See _resolve_n_gpu_layers / start().
        self._n_gpu_layers_provider = n_gpu_layers_provider
        # Resolved at spawn time: the Vulkan device index the benchmark selected
        # as best, used to pin ``--device VulkanN`` so enhancement runs on the
        # validated GPU rather than llama.cpp's default. -1 / no provider -> no pin.
        self._device_index_provider = device_index_provider
        self._model_id_provider = model_id_provider
        self._acceleration = acceleration
        self._process: Optional[asyncio.subprocess.Process | WindowsJobProcess | MacOSGuardianProcess] = None
        self.port: int = 0
        self._runtime_metadata = LlamaServerRuntimeMetadata(
            device_mode="unknown",
            device=None,
            n_gpu_layers=None,
            load_duration_ms=None,
        )
        # is_ready flips True only after a warmup inference completes —
        # /health returning 200 means the GGUF loaded, not that the model
        # is warm enough to serve a sub-second real call. Without this gate
        # the first user dictation pays ~12s cold-start prefill and blows
        # past the httpx timeout.
        self._is_ready: bool = False
        # One shared task backs both fire-and-forget recording warmups and
        # callers that explicitly await readiness. The generation invalidates
        # callbacks/tasks that were queued before an explicit stop.
        self._warm_task: Optional[asyncio.Task[bool]] = None
        self._warm_generation = 0
        self._idle_timeout_seconds = float(idle_timeout_seconds)
        self._idle_timer: Optional[threading.Timer] = None
        self._idle_generation = 0
        # Runtime-only residency suppresses idle offload without rewriting the
        # configured timeout.
        self._residency_pinned = False
        # The asyncio loop the subprocess + httpx work runs on. Captured
        # the first time kick_warm is called; reused for the idle timer's
        # cross-thread scheduling.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        # Emit initial dormant state so late-connecting listeners get a seed.
        # port is None here because no subprocess has been spawned yet.
        self._emit("dormant")

    # ------------------------------------------------------------------
    # Internal emit helper — research.md line 33
    # ------------------------------------------------------------------

    def _emit(self, state: str, *, message: Optional[str] = None) -> None:
        """Invoke on_state_change with a llama.status payload.

        Mirrors the _broadcast swallow pattern from main.py:155-161: a
        broken callback MUST NOT crash the lifecycle. The port field reflects
        self.port at emit time — dormant emits from __init__ carry port=None
        (0 is treated as None in the payload), while spawning and later emits
        carry the actual port assigned by _pick_free_port() in start().
        """
        if self._on_state_change is None:
            return
        payload: dict = {
            "type": "llama.status",
            "state": state,
            "model": self._gguf_path.name,
            "port": self.port if self.port else None,
        }
        if message is not None:
            payload["message"] = message
        try:
            self._on_state_change(payload)
        except Exception:
            self._logger.debug(
                "%s on_state_change callback raised (swallowed) state=%s",
                _TAG, state,
            )

    # ------------------------------------------------------------------
    # Public API — lazy lifecycle
    # ------------------------------------------------------------------

    def kick_warm(self, loop: asyncio.AbstractEventLoop) -> None:
        """Ensure the subprocess is spawned + warming, scheduled on *loop*.

        Thread-safe and idempotent. Safe to call from a hotkey-handler
        thread on every recording start. The first call captures the loop
        for use by the idle timer's cross-thread scheduling. Subsequent
        calls are no-ops if already running or already warming.

        Does not block — the caller does not wait for warmup.
        """
        with self._lock:
            if self._loop is None:
                self._loop = loop
            if self._is_ready and self.is_running:
                return
            self._is_ready = False
            if self._warm_task is not None and not self._warm_task.done():
                return
            generation = self._warm_generation
            # Schedule on the captured loop. Use call_soon_threadsafe so
            # this is safe whether the caller is on the loop thread or not.
            self._loop.call_soon_threadsafe(self._schedule_warm, generation)

    def _schedule_warm(self, generation: int) -> None:
        """Create the warm task on the loop. Runs on the loop thread."""
        # Re-check under lock — another thread may have raced us.
        with self._lock:
            if generation != self._warm_generation:
                return
            if self._is_ready and self.is_running:
                return
            self._is_ready = False
            if self._warm_task is not None and not self._warm_task.done():
                return
            self._warm_task = asyncio.create_task(self._full_warm(generation))

    async def ensure_ready(self) -> bool:
        """Await one coalesced spawn and warmup attempt.

        Returns ``True`` only when the child is alive and warm. Expected load
        failures are represented as ``False`` and a content-free lifecycle
        error state, so callers never need model- or transcript-bearing error
        text to recover.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is None:
                self._loop = loop
            elif self._loop is not loop:
                raise RuntimeError(
                    "llama-server supervisor is bound to another event loop"
                )

            process = self._process
            if (
                self._is_ready
                and process is not None
                and process.returncode is None
            ):
                return True

            self._is_ready = False
            task = self._warm_task
            if task is None or task.done():
                generation = self._warm_generation
                task = loop.create_task(self._full_warm(generation))
                self._warm_task = task

        # Cancelling one waiter must not cancel the shared warm attempt for all
        # other callers. Explicit stop() cancels the task itself and awaits it.
        return await asyncio.shield(task)

    async def _full_warm(self, generation: Optional[int] = None) -> bool:
        """Spawn (if needed) + warmup inference. Sets is_ready on success."""
        if generation is None:
            with self._lock:
                generation = self._warm_generation
        current_task = asyncio.current_task()
        try:
            if not self.is_running:
                await self.start()
            await self._warmup_inference()
            with self._lock:
                stale = generation != self._warm_generation
                if not stale:
                    self._is_ready = True
                    self._reset_idle_timer_locked()
            if stale:
                await self._terminate_and_clear_process()
                return False
            # Emit ready AFTER releasing the lock so listeners see the
            # final is_ready=True state immediately.
            self._emit("ready")
            return True
        except asyncio.CancelledError:
            self._logger.info("%s warm cancelled", _TAG)
            await self._terminate_and_clear_process()
            return False
        except Exception as exc:
            self._logger.warning(
                "%s warm failed reason=%s; enhancement will skip until next attempt",
                _TAG, type(exc).__name__,
            )
            self._emit("error", message=type(exc).__name__)
            await self._terminate_and_clear_process()
            return False
        finally:
            with self._lock:
                if self._warm_task is current_task:
                    self._warm_task = None

    def pin_resident(self) -> None:
        """Keep this runtime resident until explicit shutdown.

        The configured idle timeout remains untouched so normal behavior is
        preserved for supervisors that are never pinned.
        """
        with self._lock:
            self._residency_pinned = True
            self._cancel_idle_timer_locked()

    @property
    def is_residency_pinned(self) -> bool:
        """Whether runtime residency currently suppresses idle offload."""
        with self._lock:
            return self._residency_pinned

    def mark_active(self) -> None:
        """Reset the idle timer. Call from the recording-start hook.

        No-op if the idle timeout is disabled (0.0). Thread-safe.
        """
        with self._lock:
            self._reset_idle_timer_locked()

    def _cancel_idle_timer_locked(self) -> None:
        self._idle_generation += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _reset_idle_timer_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if self._idle_timeout_seconds <= 0 or self._residency_pinned:
            return
        self._idle_timer = threading.Timer(
            self._idle_timeout_seconds, self._on_idle_expired,
            args=(self._idle_generation,),
        )
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _on_idle_expired(self, generation: Optional[int] = None) -> None:
        """Idle timer fired — schedule an async stop on the captured loop."""
        with self._lock:
            if generation is None:
                generation = self._idle_generation
            if generation != self._idle_generation:
                return
            self._idle_timer = None
            if self._residency_pinned or self._idle_timeout_seconds <= 0:
                return
            loop = self._loop
            process = self._process
            if loop is None or process is None or process.returncode is not None:
                return
        self._logger.info(
            "%s idle_offload triggered after %.0fs", _TAG, self._idle_timeout_seconds,
        )
        try:
            loop.call_soon_threadsafe(
                self._schedule_idle_stop, generation
            )
        except RuntimeError:
            # Loop is shutting down — best-effort sync kill as a last resort.
            with self._lock:
                proc = self._process
                if generation != self._idle_generation or self._residency_pinned:
                    return
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass

    def _schedule_idle_stop(self, generation: int) -> None:
        asyncio.create_task(self._stop_and_reset(generation))

    async def _stop_and_reset(self, generation: int) -> None:
        """Stop the subprocess and clear ready/task state."""
        await self.stop(_idle_generation=generation)

    def set_idle_timeout(self, seconds: float) -> None:
        """Reconfigure the idle-offload window. Called when config changes."""
        with self._lock:
            self._idle_timeout_seconds = float(seconds)
            if self._is_ready and self.is_running:
                self._reset_idle_timer_locked()
            else:
                self._cancel_idle_timer_locked()

    # ------------------------------------------------------------------
    # Public API — direct spawn (used by the new lazy path and by tests)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Pick a free port, spawn llama-server, and wait for /health 200.

        Does NOT run warmup — that is a separate stage gated by ``is_ready``.
        The lazy path (``kick_warm``) calls ``start`` then ``_warmup_inference``.

        Raises
        ------
        FileNotFoundError
            If the llama-server binary does not exist.
        RuntimeError
            If the server fails to respond within ``_HEALTH_TIMEOUT`` seconds.
        """
        load_started = time.perf_counter()
        if not self._binary_path.exists():
            raise FileNotFoundError(
                f"llama-server binary not found; reinstall Whisperstream "
                f"(looked at: {self._binary_path})"
            )

        # A replacement child must earn readiness through its own warmup. The
        # prior process may have crashed after becoming ready, so retaining the
        # bit here would let inference cross the new child's health gate.
        with self._lock:
            self._is_ready = False

        model_id = (
            self._model_id_provider()
            if self._model_id_provider is not None
            else None
        )
        template_path: Optional[Path] = None
        if model_id in _GEMMA_4_MODEL_IDS:
            template_path = find_bundled_gemma_chat_template()
            if template_path is None:
                raise FileNotFoundError(
                    "Gemma 4 chat template not found; reinstall Whisperstream "
                    f"(expected {_GEMMA_4_TEMPLATE_FILENAME})"
                )
            template_sha256 = hashlib.sha256(template_path.read_bytes()).hexdigest()
            if template_sha256 != _GEMMA_4_TEMPLATE_SHA256:
                raise RuntimeError(
                    "Gemma 4 chat template failed integrity verification"
                )

        self.port = _pick_free_port()
        # Resolve --n-gpu-layers at spawn time: a provider (set by the server
        # from the current GPU verdict) lets a verdict written after this
        # supervisor was constructed take effect. Falls back to the fixed value.
        n_gpu_layers = (
            self._n_gpu_layers_provider()
            if self._n_gpu_layers_provider is not None
            else self._n_gpu_layers
        )
        # Pin the GPU the benchmark selected (--device VulkanN); -1 = no pin
        # (llama.cpp default device).
        device_index = (
            self._device_index_provider()
            if self._device_index_provider is not None
            else -1
        )
        from .local_llm_runtime_contract import RuntimeAcceleration

        if self._acceleration is RuntimeAcceleration.METAL:
            n_gpu_layers = 99
            device_index = -1
        with self._lock:
            self._runtime_metadata = LlamaServerRuntimeMetadata(
                device_mode="gpu" if n_gpu_layers > 0 else "cpu",
                device=("Metal" if self._acceleration is RuntimeAcceleration.METAL else f"Vulkan{device_index}" if device_index >= 0 else None),
                n_gpu_layers=n_gpu_layers,
                load_duration_ms=None,
            )
        # Long-form flags only: on macOS these arguments pass through the
        # packaged engine's guardian entry, and Nuitka's self-execution guard
        # rejects an argv containing interpreter flags such as -m or -c.
        args = [
            str(self._binary_path),
            "--model", str(self._gguf_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--ctx-size", str(_CONTEXT_SIZE),
            "--n-gpu-layers", str(n_gpu_layers),
            "--jinja",                # Required for gemma 4's chat template (PLE-aware)
            "--no-repack",            # gemma 4 E2B: avoids ~1 GiB CPU-repacked weight copy
            "--reasoning", "off",     # gemma 4 E2B ships thinking-on by default; routes
                                      # output to content (not reasoning_content) field
            "--log-disable",
        ]
        if _thinking_off_model(model_id):
            args += ["--chat-template-kwargs", '{"enable_thinking":false}']
        if template_path is not None:
            args += ["--chat-template-file", str(template_path)]
        if device_index >= 0 and self._acceleration is not RuntimeAcceleration.METAL:
            args += ["--device", f"Vulkan{device_index}"]

        self._logger.info(
            "%s spawn model=%s port=%d n_gpu_layers=%d device=%s",
            _TAG, self._gguf_path.name, self.port, n_gpu_layers,
            (
                "Metal"
                if self._acceleration is RuntimeAcceleration.METAL
                else f"Vulkan{device_index}" if device_index >= 0 else "default"
            ),
        )

        # Emit spawning before the subprocess is created so the frontend
        # can show "Loading" as soon as the spawn is in flight.
        self._emit("spawning")

        self._process = await _create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(llama_library_dir(self._binary_path)),
            env=llama_subprocess_environment(self._binary_path),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._logger.info("%s spawn pid=%d port=%d", _TAG, self._process.pid, self.port)

        # Poll until the /health endpoint responds with {"status":"ok"}
        health_url = f"http://127.0.0.1:{self.port}/health"
        deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT
        last_exc: Optional[Exception] = None

        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                # Check if the process died while we waited
                if self._process.returncode is not None:
                    self._emit("error", message="ProcessDied")
                    raise RuntimeError(
                        f"llama-server failed to come up "
                        f"(exited with code {self._process.returncode})"
                    )
                try:
                    resp = await client.get(health_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "ok":
                            load_duration_ms = int(
                                (time.perf_counter() - load_started) * 1000
                            )
                            with self._lock:
                                self._runtime_metadata = LlamaServerRuntimeMetadata(
                                    device_mode=(
                                        "gpu" if n_gpu_layers > 0 else "cpu"
                                    ),
                                    device=(
                                        "Metal"
                                        if self._acceleration is RuntimeAcceleration.METAL
                                        else f"Vulkan{device_index}"
                                        if device_index >= 0
                                        else None
                                    ),
                                    n_gpu_layers=n_gpu_layers,
                                    load_duration_ms=load_duration_ms,
                                )
                            self._logger.info(
                                "%s health_ready port=%d duration_ms=%d",
                                _TAG,
                                self.port,
                                load_duration_ms,
                            )
                            self._emit("health_ok")
                            return
                except (httpx.RequestError, Exception) as exc:
                    last_exc = exc

                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        # Timed out — kill the child and raise
        await self._terminate_process()
        self._emit("error", message="HealthcheckTimeout")
        raise RuntimeError(
            f"llama-server failed to come up within {_HEALTH_TIMEOUT}s"
            + (f" (last error: {last_exc})" if last_exc else "")
        )

    def _warmup_request(self) -> dict:
        """Return the messages and options of a Chat dictation for this model.

        A registered model such as Stream-1 Mini has its own system prompt and
        reuses it through ``cache_prompt``, so a generic prompt would leave the
        first real dictation cold. Unregistered models keep the generic prompt.
        """
        # Imported here to avoid a module-level cycle (llm_enhancer ↔ llama_server).
        from whisperstream.enhancement_models import get_local_model
        from whisperstream.enhancement_policy import resolve_model_policy
        from whisperstream.llm_enhancer import CHAT_PROMPT

        model_id = self._model_id_provider() if self._model_id_provider is not None else ""
        try:
            policy = resolve_model_policy("local", get_local_model(model_id or ""), model_id=model_id or "")
            resolution = policy.prepare("chat", None)
            request = {
                "messages": [
                    {"role": "system", "content": resolution.system_prompt},
                    {"role": "user", "content": policy.user_content("warmup", profile_mode="chat")},
                ],
                **policy.generation_options(),
            }
        except Exception as exc:  # warmup must never fail on prompt resolution
            self._logger.warning("%s warmup_prompt_fallback reason=%s", _TAG, type(exc).__name__)
            request = {
                "messages": [
                    {"role": "system", "content": CHAT_PROMPT},
                    {"role": "user", "content": "warmup"},
                ],
                "temperature": 1.0, "top_p": 0.95, "top_k": 64,
            }
        return request

    async def _warmup_inference(self) -> None:
        """Issue one tiny inference call so the first user dictation is warm.

        ``/health`` returning 200 only means the model file loaded, not that
        the inference path is warm. The first real call costs ~12s on a CPU
        backend (mostly prefill of the ~750-token system prompt). Pay that
        cost here, in the lazy-warm flow, so the user's first dictation
        injects raw immediately and the *second* dictation gets cleaned.

        Uses the served model's own Chat request shape (system prompt, chat
        template options, prompt caching) so warmup populates the same KV
        cache the real first call will hit.
        """
        # Emit warming at the very top, before any HTTP work.
        self._emit("warming")

        url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        payload = {"model": self._gguf_path.stem, **self._warmup_request(), "max_tokens": 1}
        t0 = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=_WARMUP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        elapsed_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        self._logger.info("%s warmup_ok port=%d duration_ms=%d",
                          _TAG, self.port, elapsed_ms)

    async def stop(self, *, _idle_generation: Optional[int] = None) -> None:
        """Gracefully terminate the llama-server child process.

        Sends SIGTERM (``terminate()``), waits up to ``_KILL_WAIT`` seconds,
        then forcefully kills if still alive. Idempotent.
        """
        # Cancel any pending idle timer so it doesn't fire after stop.
        with self._lock:
            if _idle_generation is not None and (
                _idle_generation != self._idle_generation
                or self._residency_pinned
                or self._idle_timeout_seconds <= 0
            ):
                return
            self._cancel_idle_timer_locked()
            self._is_ready = False
            self._warm_generation += 1
            warm_task = self._warm_task
        # Emit stopping BEFORE the early-return guard so a stop call on a
        # non-running supervisor still emits stopping → dormant.
        self._emit("stopping")
        current_task = asyncio.current_task()
        if (
            warm_task is not None
            and warm_task is not current_task
            and not warm_task.done()
        ):
            warm_task.cancel()
            try:
                await warm_task
            except asyncio.CancelledError:
                # A task cancelled before its coroutine body starts cannot run
                # _full_warm's cleanup/finally blocks. stop() owns cleanup here.
                pass

        await self._terminate_and_clear_process()
        with self._lock:
            if self._warm_task is warm_task:
                self._warm_task = None
        self._emit("dormant")

    async def restart_for_model(self, gguf_path: Path) -> None:
        """Stop the current server and update the GGUF path.

        Does NOT eagerly restart — the next ``kick_warm`` call will spawn
        with the new GGUF. This matches the lazy lifecycle: nothing eager,
        ever. Callers that want the new model warm immediately should
        ``kick_warm`` themselves.
        """
        await self.stop()
        self._gguf_path = gguf_path
        # Re-emit dormant after updating _gguf_path so the model field in
        # the payload reflects the new GGUF name for the next supervisor state.
        self._emit("dormant")

    @property
    def base_url(self) -> str:
        """OpenAI-compatible base URL, e.g. ``http://127.0.0.1:PORT/v1``."""
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def gguf_path(self) -> Path:
        """The model artifact bound to this supervisor."""

        return self._gguf_path

    @property
    def runtime_metadata(self) -> LlamaServerRuntimeMetadata:
        """Return a stable snapshot of process device and load timing facts."""

        with self._lock:
            return self._runtime_metadata

    @property
    def is_running(self) -> bool:
        """True if the child process is alive (has not exited)."""
        if self._process is None:
            return False
        return self._process.returncode is None

    @property
    def is_ready(self) -> bool:
        """True if the subprocess is alive AND warmup has completed.

        The enhancer checks this (not ``is_running``) to decide whether to
        actually call the local LLM or fast-path the raw transcript.
        """
        return self._is_ready and self.is_running

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _terminate_and_clear_process(self) -> None:
        """Best-effort terminate the current child and release its state."""
        process = self._process
        if process is None:
            with self._lock:
                self.port = 0
                self._is_ready = False
            return
        try:
            await self._terminate_process()
        except Exception as exc:
            self._logger.warning(
                "%s shutdown failed reason=%s",
                _TAG,
                type(exc).__name__,
            )
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
                    self.port = 0
                self._is_ready = False

    async def _terminate_process(self) -> None:
        """Terminate the child process: SIGTERM then SIGKILL after 3 s."""
        proc = self._process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except OSError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_WAIT)
            self._logger.info(
                "%s shutdown pid=%d exit_code=%s",
                _TAG, proc.pid, proc.returncode,
            )
        except asyncio.TimeoutError:
            self._logger.warning(
                "%s kill pid=%d reason=did_not_exit_after_%.0fs",
                _TAG, proc.pid, _KILL_WAIT,
            )
            try:
                proc.kill()
                await proc.wait()
            except OSError:
                pass
            self._logger.info(
                "%s shutdown pid=%d exit_code=%s reason=forced_kill",
                _TAG, proc.pid, proc.returncode,
            )
