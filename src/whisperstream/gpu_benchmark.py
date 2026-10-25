"""GPU benchmark module for Whisperstream — Tasks 1.3 + 1.4 + 2.1.

Provides:
  Task 1.3 (pure parsing):
  - VulkanDevice         dataclass for a parsed ``--list-devices`` entry
  - parse_vulkan_devices parser for llama-server ``--list-devices`` stdout
  - real_vulkan_devices  filter that removes software rasterizers
  - prefilter_verdict    cheap pass/instant_fail gate (no subprocess yet)

  Task 1.4 (verdict threshold + async runner):
  - MIN_DECODE_TPS       the 50 tok/s latency-budget floor (shared with recommend_tiers)
  - MARGINAL_DECODE_TPS  the 35 tok/s marginal-band floor (shared with recommend_tiers)
  - BenchmarkResult      dataclass for the one-shot decode result
  - decode_verdict       tri-state: viable (>=50 tok/s), marginal (35..<50 tok/s),
                         unviable (<35 tok/s, or <24 output tokens, or None input)
  - run_benchmark        one-shot async runner (spawn → health → /completion → terminate)
  - WS_VK_DEVICE env var: integer Vulkan device index override (matches bench_gpu.py L54-60)

  Task 2.1 (full-ladder tier recommender, pure):
  - recommend_tiers      measure one rung, size-scale estimates for the rest, gate on
                         RAM/disk + speed, recommend the largest clearing rung

Parsing notes
-------------
The ``--list-devices`` output format (captured from b9204 on the dev box):

  Available devices:
    Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)
    Vulkan1: AMD Radeon(TM) Graphics (16186 MiB, 15376 MiB free)

The parser is intentionally defensive:
- strips CRLF so Windows-line-ending output is handled correctly
- skips blank lines and non-matching lines without raising
- ignores extra tokens after the first ``<N> MiB`` group
- skips any device line where the MiB number cannot be parsed (fail closed)

Software-rasterizer filter
--------------------------
A device is classified as a software rasterizer if its name (case-insensitive)
begins with one of the known software-renderer prefixes or exactly matches the
bare keyword ``cpu``.  Integrated GPUs are explicitly NOT hard-excluded; the
real benchmark (Task 1.4) is the authoritative judge.

Filtered names (case-insensitive prefix / exact match):
  llvmpipe, lavapipe, swiftshader, CPU (bare / CPU-prefix device entries)

run_benchmark notes
-------------------
- Uses /apply-template + /completion (NOT /v1/chat) because b9204's OAI endpoint
  returns no timings (confirmed in bench_gpu.py workstream notes).
- Task 1.6: /completion is called with ``stream:true`` (SSE) so an optional
  ``on_tick`` callback can be fed live words/s measured from generated content
  (throttled ~200ms) for live-fill animation. The FINAL SSE chunk (the one
  carrying ``timings``) stays the sole authoritative source for the token/s
  verdict number — live words/s is animation-only and never substitutes for it.
- Always terminates the child process in ``finally`` regardless of outcome.
- Maps healthcheck-timeout / completion-error / empty output -> "unviable" (fail closed).
- WS_VK_DEVICE env var (integer) overrides ``device_index`` when the kwarg is None,
  matching the bench_gpu.py L54-60 pattern. An explicit ``device_index`` kwarg takes
  precedence over the env var.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .enhancement_models import LOCAL_BUNDLED_MODELS
from .enhancement_prompts import STREAM_MINI_PROMPT_KEYS

# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

@dataclass
class VulkanDevice:
    """A single Vulkan device enumerated by llama-server --list-devices."""

    index: int
    name: str
    total_mib: int


@dataclass
class MetalDevice:
    """A Metal device enumerated by the Darwin llama.cpp sidecar."""

    index: int
    name: str
    total_mib: int
    backend: str = "metal"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Matches lines like:
#   "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)"
# Capture groups: (index, name, total_mib)
# The name may contain any characters except the opening parenthesis.
# The first number followed by " MiB" is the total VRAM figure.
_DEVICE_RE = re.compile(
    r"^\s*Vulkan(\d+):\s+(.+?)\s+\((\d+)\s+MiB",
    re.IGNORECASE,
)
_METAL_DEVICE_RE = re.compile(
    r"^\s*MTL(\d+):\s+(.+?)\s+\((\d+)\s+MiB",
    re.IGNORECASE,
)

# Known software-rasterizer name prefixes (case-insensitive).
# These are matched against the start of the device name so that a GPU whose
# brand/model happens to contain "cpu" as a substring is not accidentally
# filtered (e.g. a hypothetical "ACPU-branded GPU"). The bare-cpu rule
# additionally catches entries whose entire name is "CPU" or starts with
# "CPU " / "CPU(" etc.
_SW_RASTERIZER_PREFIXES = (
    "llvmpipe",
    "lavapipe",
    "swiftshader",
)


def parse_vulkan_devices(stdout: str) -> list[VulkanDevice]:
    """Parse ``llama-server --list-devices`` stdout into structured records.

    Parameters
    ----------
    stdout:
        Raw stdout string from the binary (any line endings).

    Returns
    -------
    list[VulkanDevice]
        One entry per successfully-parsed ``VulkanN:`` line.
        Lines that do not match the expected format are silently skipped.
        An empty list is returned when stdout is empty or has no device lines.
    """
    devices: list[VulkanDevice] = []
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip("\r")  # strip CRLF in case of Windows output
        m = _DEVICE_RE.match(line)
        if m is None:
            continue
        index = int(m.group(1))
        name = m.group(2).strip()
        try:
            total_mib = int(m.group(3))
        except ValueError:
            # Should not happen given the regex, but be defensive.
            continue
        devices.append(VulkanDevice(index=index, name=name, total_mib=total_mib))
    return devices


def parse_metal_devices(stdout: str) -> list[MetalDevice]:
    """Parse native llama.cpp ``MTLN:`` rows from ``--list-devices``."""

    devices: list[MetalDevice] = []
    for raw_line in stdout.splitlines():
        match = _METAL_DEVICE_RE.match(raw_line.rstrip("\r"))
        if match is None:
            continue
        devices.append(
            MetalDevice(
                index=int(match.group(1)),
                name=match.group(2).strip(),
                total_mib=int(match.group(3)),
            )
        )
    return devices


def available_acceleration_devices(
    stdout: str,
) -> list[VulkanDevice] | list[MetalDevice]:
    """Return hardware Vulkan rows, otherwise native Metal rows."""

    vulkan = real_vulkan_devices(parse_vulkan_devices(stdout))
    return vulkan if vulkan else parse_metal_devices(stdout)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _is_software_rasterizer(name: str) -> bool:
    """Return True if *name* identifies a known software rasterizer.

    The check is case-insensitive.  Only prefix and bare-keyword matches are
    used so that real GPU model names containing the word "cpu" somewhere in
    the middle are not affected.
    """
    lower = name.lower().strip()

    # Known explicit prefixes.
    for prefix in _SW_RASTERIZER_PREFIXES:
        if lower.startswith(prefix):
            return True

    # Bare "CPU" device (e.g. "CPU (Vulkan)" emitted by some Mesa builds).
    # Match when the name *starts* with "cpu" followed by space, "(" or end.
    if re.match(r"^cpu(\s|\(|$)", lower):
        return True

    return False


def real_vulkan_devices(devices: list[VulkanDevice]) -> list[VulkanDevice]:
    """Filter *devices* to only those backed by real GPU hardware.

    Software rasterizers (llvmpipe, lavapipe, SwiftShader, bare CPU entries)
    are removed.  Integrated GPUs are explicitly **not** filtered here; the
    real latency benchmark is the authoritative viability judge.

    Parameters
    ----------
    devices:
        List of parsed ``VulkanDevice`` records.

    Returns
    -------
    list[VulkanDevice]
        Subset of *devices* excluding software rasterizers.
    """
    return [d for d in devices if not _is_software_rasterizer(d.name)]


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def prefilter_verdict(stdout: str) -> str:
    """Run the cheap Vulkan device prefilter against raw ``--list-devices`` stdout.

    This is the gate that runs BEFORE any model download.  It costs only the
    time to spawn the binary with ``--list-devices`` (no model load).

    Returns
    -------
    "pass"
        At least one real (non-software-rasterizer) Vulkan device is present.
        The real benchmark (Task 1.4) will judge whether it is fast enough.
    "instant_fail"
        Zero real Vulkan devices, or the output is empty/malformed.
        The caller should switch to Gemini without downloading the model.
    """
    if available_acceleration_devices(stdout):
        return "pass"
    return "instant_fail"


def list_devices_stdout(binary_path: Path, *, timeout_s: float = 15.0) -> str:
    """Run ``llama-server --list-devices`` and return its combined stdout/stderr.

    This is the cheap prefilter probe: it spawns the bundled Vulkan binary with
    ``--list-devices`` (no ``-m`` model load), so it costs only process-startup
    time. The returned text is fed straight into :func:`prefilter_verdict`.

    Some llama.cpp builds print the device table to stderr rather than stdout,
    so both streams are merged before returning. On any spawn error or timeout
    an empty string is returned, which :func:`prefilter_verdict` treats as
    ``instant_fail`` (fail closed).

    Parameters
    ----------
    binary_path:
        Path to the llama-server executable (Vulkan build).
    timeout_s:
        Hard ceiling for the probe. ``--list-devices`` returns near-instantly;
        the timeout only guards against a wedged process.

    Returns
    -------
    str
        Combined stdout + stderr text, or ``""`` on failure.
    """
    import subprocess

    from .llama_server import llama_library_dir, llama_subprocess_environment

    creationflags = (
        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
    )
    try:
        proc = subprocess.run(
            [str(binary_path), "--list-devices"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
            # Packaging Bug B: llama.cpp loads its ggml backend DLLs relative to
            # the exe dir / CWD. The DLLs ship next to llama-server.exe (dev:
            # src-tauri/binaries/) or in the binaries/ subdir (packaged), so pin
            # CWD to whichever holds them or --list-devices enumerates zero
            # devices (the prefilter would instant-fail on capable hardware).
            cwd=str(llama_library_dir(binary_path)),
            env=llama_subprocess_environment(binary_path),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# Task 1.5: ngl_for_verdict — map cached verdict to n_gpu_layers
# ---------------------------------------------------------------------------


def ngl_for_verdict(verdict: str) -> int:
    """Return the ``--n-gpu-layers`` value for the given cached GPU verdict.

    This is the single source of truth for the verdict-to-NGL mapping so both
    ``LlamaServerSupervisor`` construction sites in ``server.py`` stay identical.

    Parameters
    ----------
    verdict:
        The cached GPU verdict (per-model map value, see ``config_manager``).
        Recognised values: ``"viable"``, ``"marginal"``, ``"not_run"``,
        ``"unviable"``, ``""``. Any unrecognised string also maps to ``0``
        (fail closed).

    Returns
    -------
    int
        ``99`` when ``verdict`` is ``"viable"`` or ``"marginal"`` (full GPU
        offload). Marginal still runs on the GPU — it only failed the speed
        budget, not the runnability bar — so it gets the same offload as
        viable; the "just slower" experience is a consented tradeoff, not a
        reason to fall back to CPU.
        ``0`` for every other value (CPU path / benchmark not yet run).
    """
    return 99 if verdict in ("viable", "marginal") else 0


def gpu_fingerprint(devices: list[VulkanDevice] | list[MetalDevice]) -> str:
    """Order-independent signature of a set of real GPUs.

    Includes each device's name + total VRAM so that adding, removing, or
    swapping a card changes the signature, while mere re-ordering does not.
    Used on each launch to decide whether a cached benchmark verdict is still
    valid (a changed signature -> re-benchmark). Empty string for no devices.
    """
    return "|".join(sorted(f"{d.name}:{d.total_mib}" for d in devices))


# ---------------------------------------------------------------------------
# Task 1.4: decode verdict threshold (pure)
# ---------------------------------------------------------------------------

# The single source of truth for the decode-throughput latency budget: ~50
# tok/s long-form decode keeps wall-clock cleanup under ~2-3s for typical
# ~150-word transcripts (benchmark calibration data, scope.md). Both
# decode_verdict (the tri-state GPU-viability gate) and recommend_tiers (the
# Task 2.1 full-ladder recommender) key off this one constant so the floor
# can't drift between the two call sites.
MIN_DECODE_TPS = 50.0

# The marginal-band floor: below this, decode is slow enough that the
# feature is not worth offering at all ("unviable"); at or above it but
# still under MIN_DECODE_TPS, the model runs but slowly enough to warrant a
# "marginal" consented tradeoff rather than a silent pass (scope.md tiered
# verdict decision). Shared by decode_verdict and recommend_tiers.
MARGINAL_DECODE_TPS = 35.0


def decode_verdict(
    predicted_per_second: Optional[float],
    predicted_n: Optional[int],
    *,
    min_output_tokens: int = 24,
    min_tps: float = MIN_DECODE_TPS,
    min_marginal_tps: float = MARGINAL_DECODE_TPS,
) -> str:
    """Return a tri-state verdict from decode throughput and output length.

    The thresholds are set from the benchmark calibration data (scope.md):
    ~50 tok/s long-form decode keeps wall-clock cleanup under ~2-3s for typical
    ~150-word transcripts ("viable"); 35-50 tok/s still runs but slowly enough
    to warrant an explicit "marginal" consented tradeoff instead of a silent
    pass. Fail closed on any None input or short/slow output.

    Parameters
    ----------
    predicted_per_second:
        Decode throughput from ``timings.predicted_per_second`` in the
        ``/completion`` response.  ``None`` when the response had no timings.
    predicted_n:
        Number of output tokens from ``timings.predicted_n``.  ``None`` when
        the response had no timings.  A short output (< ``min_output_tokens``)
        indicates the model stopped early (empty/garbage completion) and should
        not be taken as evidence of adequate throughput.
    min_output_tokens:
        Minimum number of generated tokens required for the timing to be
        meaningful.  Default 24 (about one sentence of output).
    min_tps:
        Minimum decode tokens-per-second for a ``"viable"`` verdict.  Default 50.0.
    min_marginal_tps:
        Minimum decode tokens-per-second for a ``"marginal"`` verdict (the
        floor below which the result is ``"unviable"`` instead).  Default 35.0.

    Returns
    -------
    "viable"
        The device cleared both the throughput (``>= min_tps``) and
        minimum-output-token bars.
    "marginal"
        Throughput is between ``min_marginal_tps`` (inclusive) and ``min_tps``
        (exclusive), with enough output tokens to trust the measurement.
    "unviable"
        Any input is ``None``, output was too short, or throughput was below
        ``min_marginal_tps``.
    """
    if predicted_per_second is None or predicted_n is None:
        return "unviable"
    if predicted_n < min_output_tokens:
        return "unviable"
    if predicted_per_second < min_marginal_tps:
        return "unviable"
    if predicted_per_second < min_tps:
        return "marginal"
    return "viable"


# ---------------------------------------------------------------------------
# Task 2.1: recommend_tiers — full-ladder tier recommender (pure)
# ---------------------------------------------------------------------------


def recommend_tiers(
    measured_model_id: str,
    measured_tok_per_s: float,
    catalog: list[dict] = LOCAL_BUNDLED_MODELS,
    *,
    available_ram_mb: float,
    free_disk_mb: float,
    min_tps: float = MIN_DECODE_TPS,
    min_marginal_tps: float = MARGINAL_DECODE_TPS,
) -> dict:
    """Recommend the largest catalog rung a machine can run within budget.

    Decode throughput is measured ONCE (on ``measured_model_id`` — in
    practice the heaviest rung, Gemma at full GPU offload). Every other
    rung's tok/s is an ESTIMATE derived by scaling the measured value with
    ``disk_mb`` as a monotonic size proxy: tok/s is monotonic-decreasing in
    model size, so a smaller GGUF is strictly faster::

        est_tps = measured_tps * (measured_disk_mb / rung_disk_mb)

    The rung whose ``id`` matches ``measured_model_id`` keeps its real
    measured value unscaled and is tagged ``measured=True``; every other
    rung is ``measured=False`` (an estimate, "est.").

    Each rung gets a tri-state ``verdict`` (mirrors ``decode_verdict``'s
    bands, plus the memory/disk gate):
      - ``"unviable"`` when the memory/disk gate fails
        (``available_ram_mb < rung["min_system_ram_mb"]`` OR
        ``free_disk_mb < rung["disk_mb"]``), REGARDLESS of speed — a rung
        that can't even be downloaded/loaded is never "just slow".
      - otherwise banded by its (measured or estimated) tok/s:
        ``"viable"`` at ``>= min_tps``, ``"marginal"`` at
        ``min_marginal_tps <= tok/s < min_tps``, ``"unviable"`` below
        ``min_marginal_tps``.

    ``"clears"`` is ``verdict in ("viable", "marginal")`` — i.e. the rung is
    runnable at all (with consent for the marginal case), not necessarily
    fast. ``"reason"`` is non-null only when ``verdict == "unviable"``:
    ``"Needs more memory"`` when the memory/disk gate failed, else
    ``"Needs a faster card"`` (memory was fine, decode was just too slow).

    The recommendation is the LARGEST rung whose verdict is ``"viable"``
    (marginal does NOT count — a marginal rung is never auto-recommended,
    only manually selectable) that is also ELIGIBLE for auto-recommendation
    (``rung.get("auto_recommend", True)``). ``catalog`` order is the
    preference order (``LOCAL_BUNDLED_MODELS``), so the first viable eligible
    entry encountered in iteration order is the answer. Gemma is
    ``auto_recommend=False`` (the customizable/opt-in general model, picked
    manually), so the ladder recommends Stream-1 Mini when it fits and never
    auto-recommends Gemma. Every rung, including Gemma,
    still appears in ``tiers`` with its own ``verdict``/``clears`` state so
    the picker can offer it as a manual choice. A rung with no
    ``auto_recommend`` key defaults to eligible so a synthetic catalog
    behaves as before.

    This function does NOT collapse the whole result to unviable when no
    rung is viable — it stays a pure per-rung report. Any "no runnable rung
    at all -> fall back to the plain unviable result shape" decision is made
    by the caller (the benchmark worker), not here.

    Parameters
    ----------
    measured_model_id:
        The catalog ``id`` of the rung that was actually benchmarked.
    measured_tok_per_s:
        The real decode throughput measured for ``measured_model_id``.
    catalog:
        The local model catalog to recommend from, in descending-capability
        order. Defaults to ``LOCAL_BUNDLED_MODELS``; tests may inject a
        synthetic catalog.
    available_ram_mb:
        Total system RAM available on the machine, in MB.
    free_disk_mb:
        Free disk space available for the model directory, in MB.
    min_tps:
        Minimum acceptable decode tokens-per-second for a ``"viable"``
        verdict — the same latency budget as ``decode_verdict``. Defaults
        to ``MIN_DECODE_TPS``.
    min_marginal_tps:
        Minimum decode tokens-per-second for a ``"marginal"`` verdict (the
        floor below which the rung is ``"unviable"`` instead). Defaults to
        ``MARGINAL_DECODE_TPS``.

    Returns
    -------
    dict
        ``{"recommended_model_id": str | None, "measured_model_id": str,
        "measured_tok_per_s": float, "measured_verdict": str, "tiers": [...]}``.
        ``measured_verdict`` is the tri-state ``verdict`` of the measured
        rung. Each tier entry is ``{"id", "label", "tok_per_s", "measured",
        "verdict", "clears", "reason"}``.
    """
    measured_entry = next((r for r in catalog if r["id"] == measured_model_id), None)
    measured_disk_mb = measured_entry["disk_mb"] if measured_entry is not None else None

    tiers: list[dict] = []
    recommended_model_id: Optional[str] = None
    measured_verdict = "unviable"

    for rung in catalog:
        is_measured = rung["id"] == measured_model_id
        if is_measured:
            tok_per_s = measured_tok_per_s
        elif measured_disk_mb:
            tok_per_s = measured_tok_per_s * (measured_disk_mb / rung["disk_mb"])
        else:
            # measured_model_id isn't in the catalog -- no reference size to
            # scale from. Fail closed rather than fabricate a number.
            tok_per_s = 0.0

        mem_ok = (
            available_ram_mb >= rung["min_system_ram_mb"]
            and free_disk_mb >= rung["disk_mb"]
        )

        if not mem_ok:
            verdict = "unviable"
        elif tok_per_s >= min_tps:
            verdict = "viable"
        elif tok_per_s >= min_marginal_tps:
            verdict = "marginal"
        else:
            verdict = "unviable"

        clears = verdict in ("viable", "marginal")

        if not mem_ok:
            reason: Optional[str] = "Needs more memory"
        elif verdict == "unviable":
            reason = "Needs a faster card"
        else:
            reason = None

        if is_measured:
            measured_verdict = verdict

        tiers.append(
            {
                "id": rung["id"],
                "label": rung["label"],
                "tok_per_s": round(tok_per_s, 1),
                "measured": is_measured,
                "verdict": verdict,
                "clears": clears,
                "reason": reason,
            }
        )

        # Auto-recommend only VIABLE, ELIGIBLE rungs. Marginal never counts
        # toward auto-recommendation (Gemma additionally opts out entirely
        # via auto_recommend=False — it stays a manual/customizable pick).
        # Missing key defaults to eligible so synthetic test catalogs are
        # unaffected.
        if (
            verdict == "viable"
            and recommended_model_id is None
            and rung.get("auto_recommend", True)
        ):
            recommended_model_id = rung["id"]

    return {
        "recommended_model_id": recommended_model_id,
        "measured_model_id": measured_model_id,
        "measured_tok_per_s": measured_tok_per_s,
        "measured_verdict": measured_verdict,
        "tiers": tiers,
    }


# ---------------------------------------------------------------------------
# Task 1.4: BenchmarkResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    """Result of one ``run_benchmark`` call."""

    verdict: str
    """``"viable"``, ``"marginal"``, or ``"unviable"`` (see ``decode_verdict``)."""

    predicted_per_second: Optional[float]
    """Decode throughput from ``/completion`` timings, or ``None`` on failure."""

    predicted_n: Optional[int]
    """Output token count from ``/completion`` timings, or ``None`` on failure."""

    error: Optional[str] = None
    """Short description of the failure reason when the run did not complete."""


# ---------------------------------------------------------------------------
# Task 1.4: fixed ~150-word prompt text (from bench_gpu.py L110-122)
# ---------------------------------------------------------------------------

# This is the xxl_~150w_brainstorm prompt used in the workstream-c benchmark.
# It represents a realistic long dictation that exercises the decode path
# at the upper end of typical transcript lengths.
_BENCHMARK_USER_TEXT = (
    "Okay so for the next sprint I want to focus on, um, three things "
    "really. First is the onboarding flow, we need to cut it from like "
    "seven screens down to three, no wait, four screens because legal "
    "wants the EULA on its own page. Second is the latency on the local "
    "model, which has been creeping up especially on longer transcripts "
    "like this one I'm dictating right now. And third, scratch that, "
    "third and fourth: we should split the dashboard work into a "
    "redesign track and an analytics track because they have different "
    "stakeholders. Also actually before I forget, can you remind me on "
    "Friday, I mean Thursday, to follow up with the designer about the "
    "icon set?"
)

# Fixed system prompt used to render the cleanup request via /apply-template.
# Minimal system prompt that tells the model to clean up the dictation.
_BENCHMARK_SYSTEM_PROMPT = (
    "You are a dictation cleanup assistant. Clean up the following spoken "
    "text: fix grammar, remove filler words (um, uh, like), and correct "
    "self-corrections. Output only the cleaned text, no commentary."
)

# WS_VK_DEVICE env var: integer Vulkan device index override.
# Matches the bench_gpu.py L54-60 pattern: parsed from the environment,
# stripped, and used when device_index kwarg is None.
_ENV_VK_DEVICE = "WS_VK_DEVICE"


# ---------------------------------------------------------------------------
# Task 1.4: internal helpers (patchable in tests)
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Pick a free TCP port on 127.0.0.1 (same pattern as bench_gpu.py L130-133)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_health(port: int, timeout_s: float = 60.0) -> bool:
    """Poll ``/health`` until the server is ready or timeout expires.

    Lifted from bench_gpu.py L156-168 with the same 0.25s poll interval.
    Uses a plain HTTP GET rather than the supervisor's httpx client so this
    module stays off the hot path with zero LlamaServerSupervisor coupling.
    """
    import httpx

    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
    return False


async def _render_prompt(port: int, prompt_key: str = "") -> str:
    """Render the fixed benchmark prompt via ``/apply-template``.

    Uses the same chat-message structure as bench_gpu.py L182-193.
    """
    import httpx

    payload = {
        "messages": [
            {"role": "system", "content": _BENCHMARK_SYSTEM_PROMPT},
            {"role": "user", "content": _BENCHMARK_USER_TEXT},
        ]
    }
    if prompt_key == "gemma":
        from .llm_enhancer import GEMMA_LOCAL_CHAT_PROMPT, format_user_content
        payload["messages"] = [
            {"role": "system", "content": GEMMA_LOCAL_CHAT_PROMPT},
            {"role": "user", "content": format_user_content(_BENCHMARK_USER_TEXT, None)},
        ]
    if prompt_key in STREAM_MINI_PROMPT_KEYS:
        from .llm_enhancer import format_registered_user_content, resolve_registered_system_prompt
        payload["messages"] = [
            {"role": "system", "content": resolve_registered_system_prompt(
                prompt_key, "chat", "wrap-v2",
            )},
            {"role": "user", "content": format_registered_user_content(
                _BENCHMARK_USER_TEXT, prompt_key=prompt_key,
                profile_mode="chat", serving_contract="wrap-v2",
            )},
        ]
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"http://127.0.0.1:{port}/apply-template", json=payload)
        r.raise_for_status()
        return r.json()["prompt"]


async def _do_completion(
    port: int,
    rendered_prompt: str,
    on_tick: Callable[[float], None] | None = None,
    prompt_key: str = "",
) -> dict:
    """POST one streamed ``/completion`` request; return the final timings chunk.

    Task 1.6: switched from a single blocking POST to llama.cpp SSE
    ``stream:true`` so generated words/s can be reported live. Only nonterminal,
    non-empty ``content`` is accumulated; progress/metadata objects and the
    terminal ``tokens_predicted`` count never affect the live rate. The clock
    starts when the first generated content arrives, excluding prompt
    evaluation. When *on_tick* is given, it receives cumulative generated
    words/s at most once every ~200ms plus one final tick at completion.

    The terminating SSE chunk (the one carrying ``"stop": true``) also carries
    the AUTHORITATIVE ``timings`` object (``predicted_per_second``,
    ``predicted_n``) — that chunk, as a plain ``dict``, is what this function
    returns. *on_tick*'s wall-clock words/s is for live-fill ANIMATION only and
    is never treated as the verdict number: if the stream ends without a
    terminating chunk (dropped connection, early EOF), the returned dict has
    no ``"timings"`` key, so the caller's ``decode_verdict(None, None)`` call
    falls through to ``"unviable"`` exactly as it did before this change
    (fail-closed).

    Uses the same production sampling parameters as bench_gpu.py L127.
    Timeout is generous (120s) to accommodate slow/marginal devices.
    """
    import httpx

    payload = {
        "prompt": rendered_prompt,
        "n_predict": 256,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "cache_prompt": False,
        "stream": True,
    }

    if prompt_key in STREAM_MINI_PROMPT_KEYS:
        payload.update({"temperature": 0.0, "top_k": 1, "top_p": 1.0})

    generation_start: float | None = None
    last_tick: float | None = None
    generated_parts: list[str] = []
    final_chunk: dict = {}

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST", f"http://127.0.0.1:{port}/completion", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue  # blank SSE separator line / comment — skip
                raw = line[len("data:"):].strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue  # malformed chunk — skip, keep waiting for more

                if chunk.get("stop"):
                    final_chunk = chunk
                    if (
                        on_tick is not None
                        and generation_start is not None
                        and generated_parts
                    ):
                        now = time.monotonic()
                        elapsed = now - generation_start
                        word_count = len("".join(generated_parts).split())
                        if elapsed > 0 and word_count > 0:
                            on_tick(word_count / elapsed)
                    break

                # b9204 partial chunks carry content/tokens/stop. Count only
                # actual generated content; metadata/progress chunks may carry
                # token fields but are not output and must not move the meter.
                content = chunk.get("content")
                if not isinstance(content, str) or not content:
                    continue

                now = time.monotonic()
                if generation_start is None:
                    generation_start = now
                    last_tick = now
                generated_parts.append(content)

                if (
                    on_tick is not None
                    and last_tick is not None
                    and (now - last_tick) >= 0.2
                ):
                    elapsed = now - generation_start
                    word_count = len("".join(generated_parts).split())
                    if elapsed > 0 and word_count > 0:
                        on_tick(word_count / elapsed)
                    last_tick = now

    return final_chunk


# ---------------------------------------------------------------------------
# Task 1.4: async one-shot benchmark runner
# ---------------------------------------------------------------------------


async def run_benchmark(
    *,
    binary_path: Path,
    gguf_path: Path,
    device_index: Optional[int] = None,
    on_tick: Callable[[float], None] | None = None,
    acceleration: str = "vulkan",
    prompt_key: str = "",
) -> BenchmarkResult:
    """Run a one-shot GPU decode benchmark and return a viability verdict.

    Spawns ``llama-server`` with full GPU offload (``-ngl 99``), renders the
    fixed ~150-word dictation prompt via ``/apply-template``, streams one
    ``/completion`` request, reads ``timings.predicted_per_second`` and
    ``timings.predicted_n`` off the FINAL streamed chunk, and always
    terminates the child in ``finally``.

    The device Vulkan index is resolved in this order:
    1. ``device_index`` kwarg (explicit caller choice).
    2. ``WS_VK_DEVICE`` environment variable (integer string).
    3. No ``--device`` flag (llama.cpp picks its default device).

    Parameters
    ----------
    binary_path:
        Path to the llama-server executable (Vulkan build).
    gguf_path:
        Path to the gemma GGUF model file.
    device_index:
        Vulkan device index to pin via ``--device VulkanN``.  ``None`` defers
        to the ``WS_VK_DEVICE`` env var, or no pin if that is also unset.
    on_tick:
        Task 1.6: optional callback fed generated words/s (throttled to ~200ms)
        while the ``/completion`` SSE stream is being consumed, so a caller
        (the benchmark WS loop) can animate a live fill.
        Threaded straight through to ``_do_completion`` — see its docstring
        for the fail-closed contract (live words/s is NEVER the verdict
        number; only the final ``timings`` chunk is).

    Returns
    -------
    BenchmarkResult
        ``verdict="viable"`` iff decoded >= 50 tok/s with >= 24 output tokens;
        ``verdict="unviable"`` on timeout, error, short output, or slow decode.
    """
    # Resolve the effective Vulkan device index.
    effective_device: Optional[int] = device_index
    if acceleration == "vulkan" and effective_device is None:
        env_val = os.environ.get(_ENV_VK_DEVICE, "").strip()
        if env_val:
            try:
                effective_device = int(env_val)
            except ValueError:
                pass  # malformed env var -> no device pin

    port = _pick_free_port()

    # Build spawn args (matches bench_gpu.py L331-346 pattern).
    args = [
        str(binary_path),
        "-m", str(gguf_path),
        "--host", "127.0.0.1",
        "--port", str(port),
        "-c", "4096",
        "--n-gpu-layers", "99",
        "--jinja",
        "--reasoning", "off",
    ]
    if prompt_key == "gemma":
        import hashlib
        from .llama_server import find_bundled_gemma_chat_template, _GEMMA_4_TEMPLATE_SHA256
        template = find_bundled_gemma_chat_template()
        if template is None or hashlib.sha256(template.read_bytes()).hexdigest() != _GEMMA_4_TEMPLATE_SHA256:
            raise RuntimeError("Gemma 4 benchmark template failed integrity verification")
        args += ["--chat-template-file", str(template), "--no-repack"]
    if prompt_key in STREAM_MINI_PROMPT_KEYS:
        args += ["--chat-template-kwargs", '{"enable_thinking":false}']
    if acceleration == "vulkan" and effective_device is not None:
        args += ["--device", f"Vulkan{effective_device}"]

    # Spawn the server (suppress its window on Windows).
    import subprocess as _subprocess

    from .llama_server import llama_library_dir, llama_subprocess_environment

    creationflags = (
        _subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
    )
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
        # Packaging Bug B: pin CWD to the dir that holds the ggml backend DLLs
        # (exe dir in dev, binaries/ subdir when packaged) so llama.cpp loads them.
        cwd=str(llama_library_dir(binary_path)),
        env=llama_subprocess_environment(binary_path),
    )

    try:
        # Health poll — 60s ceiling (GPU cold load is ~2s; 60s is generous).
        healthy = await _wait_for_health(port, timeout_s=60.0)
        if not healthy:
            return BenchmarkResult(
                verdict="unviable",
                predicted_per_second=None,
                predicted_n=None,
                error="healthcheck_timeout",
            )

        # Render the fixed benchmark prompt.
        try:
            rendered = await _render_prompt(port, **({"prompt_key": prompt_key} if prompt_key else {}))
        except Exception as exc:
            return BenchmarkResult(
                verdict="unviable",
                predicted_per_second=None,
                predicted_n=None,
                error=f"template_error: {exc!r}",
            )

        # Stream one /completion and read timings off the final SSE chunk.
        try:
            data = await _do_completion(port, rendered, on_tick=on_tick, **({"prompt_key": prompt_key} if prompt_key else {}))
        except Exception as exc:
            return BenchmarkResult(
                verdict="unviable",
                predicted_per_second=None,
                predicted_n=None,
                error=f"completion_error: {exc!r}",
            )

        timings = data.get("timings") or {}
        tps: Optional[float] = timings.get("predicted_per_second")
        pred_n: Optional[int] = timings.get("predicted_n")

        verdict = decode_verdict(tps, pred_n)
        return BenchmarkResult(
            verdict=verdict,
            predicted_per_second=tps,
            predicted_n=pred_n,
        )

    finally:
        # Always terminate the child — never leave an orphaned llama-server.
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception:
            pass
