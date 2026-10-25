"""Tests for gpu_benchmark.py — Tasks 1.3 + 1.4: Vulkan device prefilter parsing (pure)
and decode verdict threshold + benchmark runner spawn args.

Table-driven test cases cover:
- parse_vulkan_devices: real sample lines, extra columns, blank lines, CRLF
- real_vulkan_devices: software-rasterizer filtering (llvmpipe/lavapipe/swiftshader/CPU)
- prefilter_verdict: zero-device -> instant_fail, one real device -> pass,
  iGPU-only -> pass (no hard-exclude for integrated), malformed/empty -> instant_fail
- decode_verdict: tri-state viable/marginal/unviable — viable at exactly 50 tok/s
  w/ enough tokens; marginal at 49.9 and at exactly 35 tok/s; unviable at 34.9 tok/s;
  unviable when predicted_n below min even at high tok/s; unviable on None inputs
- run_benchmark spawn args: device_index passed as --device VulkanN, child always
  terminated in finally, WS_VK_DEVICE env override accepted
- recommend_tiers: full-ladder recommendation from one measured rung — size-scaled
  estimates for the other rungs, RAM/disk + speed viability gate, largest-clearing-
  rung recommendation, "Needs a faster card" / "Needs more memory" reasons
"""

from __future__ import annotations

import asyncio
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests.legacy_model_fixtures import LEGACY_TIER_MODELS as LOCAL_BUNDLED_MODELS
from whisperstream.gpu_benchmark import (
    MARGINAL_DECODE_TPS,
    BenchmarkResult,
    VulkanDevice,
    decode_verdict,
    parse_vulkan_devices,
    prefilter_verdict,
    real_vulkan_devices,
    recommend_tiers,
    run_benchmark,
)


# ---------------------------------------------------------------------------
# Sample output captured from the real b9204 binary on the dev box
# ---------------------------------------------------------------------------

_REAL_SAMPLE = """\
Available devices:
  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)
  Vulkan1: AMD Radeon(TM) Graphics (16186 MiB, 15376 MiB free)
"""

_IGPU_ONLY = """\
Available devices:
  Vulkan0: AMD Radeon(TM) Graphics (8192 MiB, 7900 MiB free)
"""

_SOFTWARE_RASTERIZER_ONLY = """\
Available devices:
  Vulkan0: llvmpipe (software) (4096 MiB, 4096 MiB free)
"""

_MIXED_REAL_AND_SW = """\
Available devices:
  Vulkan0: NVIDIA GeForce RTX 3080 (10240 MiB, 9500 MiB free)
  Vulkan1: lavapipe (0 MiB, 0 MiB free)
  Vulkan2: SwiftShader Device (LLVM) (64 MiB, 64 MiB free)
"""

_NO_DEVICES = """\
Available devices:
"""

_EMPTY_STDOUT = ""

_MALFORMED_NO_MIB = """\
Available devices:
  Vulkan0: Some Weird Device (no memory info here)
"""

_CRLF_SAMPLE = (
    "Available devices:\r\n"
    "  Vulkan0: NVIDIA GeForce RTX 4070 Ti SUPER (16063 MiB, 15295 MiB free)\r\n"
    "  Vulkan1: AMD Radeon(TM) Graphics (16186 MiB, 15376 MiB free)\r\n"
)

_EXTRA_COLUMNS_SAMPLE = """\
Available devices:
  Vulkan0: NVIDIA GeForce RTX 4080 (16384 MiB, 15000 MiB free) [some extra info]
"""

_CPU_BARE_DEVICE = """\
Available devices:
  Vulkan0: CPU (Vulkan) (0 MiB, 0 MiB free)
"""

_SWIFTSHADER_VARIANT = """\
Available devices:
  Vulkan0: SwiftShader Device (LLVM 15.0.7) (256 MiB, 256 MiB free)
"""


class TestParseVulkanDevices(unittest.TestCase):
    """Tests for parse_vulkan_devices()."""

    def test_parses_real_sample_two_devices(self):
        """Real sample output yields two VulkanDevice records."""
        devices = parse_vulkan_devices(_REAL_SAMPLE)
        self.assertEqual(len(devices), 2)

    def test_parses_first_device_index_name_mib(self):
        """First device has correct index, name, and total_mib."""
        devices = parse_vulkan_devices(_REAL_SAMPLE)
        d0 = devices[0]
        self.assertEqual(d0.index, 0)
        self.assertEqual(d0.name, "NVIDIA GeForce RTX 4070 Ti SUPER")
        self.assertEqual(d0.total_mib, 16063)

    def test_parses_second_device_index_name_mib(self):
        """Second device has correct index, name, and total_mib."""
        devices = parse_vulkan_devices(_REAL_SAMPLE)
        d1 = devices[1]
        self.assertEqual(d1.index, 1)
        self.assertEqual(d1.name, "AMD Radeon(TM) Graphics")
        self.assertEqual(d1.total_mib, 16186)

    def test_empty_stdout_yields_empty_list(self):
        """Empty stdout returns empty list (no crash)."""
        devices = parse_vulkan_devices(_EMPTY_STDOUT)
        self.assertEqual(devices, [])

    def test_no_device_lines_yields_empty_list(self):
        """'Available devices:' header with no entries returns empty list."""
        devices = parse_vulkan_devices(_NO_DEVICES)
        self.assertEqual(devices, [])

    def test_crlf_line_endings_parsed_correctly(self):
        """CRLF line endings (Windows output) are tolerated."""
        devices = parse_vulkan_devices(_CRLF_SAMPLE)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].name, "NVIDIA GeForce RTX 4070 Ti SUPER")
        self.assertEqual(devices[1].name, "AMD Radeon(TM) Graphics")

    def test_extra_columns_after_mib_tolerated(self):
        """Extra text after the first MiB value is ignored gracefully."""
        devices = parse_vulkan_devices(_EXTRA_COLUMNS_SAMPLE)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "NVIDIA GeForce RTX 4080")
        self.assertEqual(devices[0].total_mib, 16384)

    def test_blank_lines_in_output_are_skipped(self):
        """Blank lines between entries are silently skipped."""
        stdout_with_blanks = (
            "Available devices:\n"
            "\n"
            "  Vulkan0: NVIDIA GeForce RTX 3090 (24576 MiB, 23000 MiB free)\n"
            "\n"
        )
        devices = parse_vulkan_devices(stdout_with_blanks)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "NVIDIA GeForce RTX 3090")

    def test_igpu_only_parsed_as_single_device(self):
        """iGPU-only output is parsed normally (not filtered at parse stage)."""
        devices = parse_vulkan_devices(_IGPU_ONLY)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "AMD Radeon(TM) Graphics")

    def test_software_rasterizer_parsed_as_device(self):
        """Software rasterizer IS returned by parse (filtering is in real_vulkan_devices)."""
        devices = parse_vulkan_devices(_SOFTWARE_RASTERIZER_ONLY)
        self.assertEqual(len(devices), 1)
        self.assertIn("llvmpipe", devices[0].name.lower())

    def test_malformed_no_mib_skipped(self):
        """A device line with no parseable MiB number is skipped defensively."""
        devices = parse_vulkan_devices(_MALFORMED_NO_MIB)
        # The line cannot be parsed for MiB → skip it rather than crash.
        self.assertEqual(devices, [])


class TestRealVulkanDevices(unittest.TestCase):
    """Tests for real_vulkan_devices() — software rasterizer filtering."""

    def test_real_sample_both_real_devices_pass(self):
        """Both devices in the real sample are NOT software rasterizers."""
        devices = parse_vulkan_devices(_REAL_SAMPLE)
        real = real_vulkan_devices(devices)
        self.assertEqual(len(real), 2)

    def test_igpu_passes_filter(self):
        """Integrated GPUs are NOT filtered out (the benchmark judges them)."""
        devices = parse_vulkan_devices(_IGPU_ONLY)
        real = real_vulkan_devices(devices)
        self.assertEqual(len(real), 1)

    def test_llvmpipe_filtered_out(self):
        """'llvmpipe' is classified as software rasterizer and removed."""
        devices = parse_vulkan_devices(_SOFTWARE_RASTERIZER_ONLY)
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_lavapipe_filtered_out(self):
        """'lavapipe' is classified as software rasterizer and removed."""
        devices = [VulkanDevice(index=0, name="lavapipe Mesa Software", total_mib=0)]
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_swiftshader_filtered_out(self):
        """'SwiftShader' (any case) is classified as software rasterizer and removed."""
        devices = parse_vulkan_devices(_SWIFTSHADER_VARIANT)
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_bare_cpu_device_filtered_out(self):
        """A device named 'CPU ...' is classified as software rasterizer and removed."""
        devices = parse_vulkan_devices(_CPU_BARE_DEVICE)
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_mixed_real_and_sw_keeps_only_real(self):
        """Mixed output: only the real GPU survives; lavapipe and SwiftShader are removed."""
        devices = parse_vulkan_devices(_MIXED_REAL_AND_SW)
        real = real_vulkan_devices(devices)
        self.assertEqual(len(real), 1)
        self.assertIn("NVIDIA GeForce RTX 3080", real[0].name)

    def test_case_insensitive_llvmpipe(self):
        """llvmpipe filter is case-insensitive (e.g. 'LLVMpipe' also matches)."""
        devices = [VulkanDevice(index=0, name="LLVMpipe (Software Rendering)", total_mib=4096)]
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_case_insensitive_swiftshader(self):
        """swiftshader filter is case-insensitive."""
        devices = [VulkanDevice(index=0, name="SWIFTSHADER Device", total_mib=64)]
        real = real_vulkan_devices(devices)
        self.assertEqual(real, [])

    def test_nvidia_not_accidentally_filtered(self):
        """A name containing 'cpu' as substring of a brand name is NOT filtered."""
        # "Graphics Compute Unit Plus" contains 'cpu' if we substring-match carelessly.
        # The filter must match only a device whose name *starts* with 'CPU' or
        # the rasterizer keywords, not arbitrary substrings of normal GPU names.
        devices = [VulkanDevice(index=0, name="NVIDIA GeForce RTX 4090", total_mib=24576)]
        real = real_vulkan_devices(devices)
        self.assertEqual(len(real), 1)


class TestPrefilterVerdict(unittest.TestCase):
    """Tests for prefilter_verdict()."""

    def test_two_real_gpus_returns_pass(self):
        """stdout with two real Vulkan devices yields 'pass'."""
        self.assertEqual(prefilter_verdict(_REAL_SAMPLE), "pass")

    def test_igpu_only_returns_pass(self):
        """iGPU-only stdout yields 'pass' (no integrated hard-exclude)."""
        self.assertEqual(prefilter_verdict(_IGPU_ONLY), "pass")

    def test_software_rasterizer_only_returns_instant_fail(self):
        """llvmpipe-only stdout yields 'instant_fail' (no real device)."""
        self.assertEqual(prefilter_verdict(_SOFTWARE_RASTERIZER_ONLY), "instant_fail")

    def test_no_device_lines_returns_instant_fail(self):
        """'Available devices:' with no entries yields 'instant_fail'."""
        self.assertEqual(prefilter_verdict(_NO_DEVICES), "instant_fail")

    def test_empty_stdout_returns_instant_fail(self):
        """Empty stdout yields 'instant_fail' (fail closed)."""
        self.assertEqual(prefilter_verdict(_EMPTY_STDOUT), "instant_fail")

    def test_malformed_stdout_returns_instant_fail(self):
        """Malformed stdout (no parseable MiB) yields 'instant_fail' (fail closed)."""
        self.assertEqual(prefilter_verdict(_MALFORMED_NO_MIB), "instant_fail")

    def test_mixed_real_and_sw_returns_pass(self):
        """stdout with one real GPU plus software rasterizers yields 'pass'."""
        self.assertEqual(prefilter_verdict(_MIXED_REAL_AND_SW), "pass")

    def test_cpu_only_device_returns_instant_fail(self):
        """stdout with only a bare 'CPU' Vulkan device yields 'instant_fail'."""
        self.assertEqual(prefilter_verdict(_CPU_BARE_DEVICE), "instant_fail")

    def test_crlf_real_sample_returns_pass(self):
        """CRLF line endings do not break the verdict computation."""
        self.assertEqual(prefilter_verdict(_CRLF_SAMPLE), "pass")


# ---------------------------------------------------------------------------
# Task 1.4: decode_verdict threshold tests (TDD — written before implementation)
# ---------------------------------------------------------------------------


class TestDecodeVerdict(unittest.TestCase):
    """Tests for decode_verdict() — pure tri-state threshold logic.

    Bands: viable >= MIN_DECODE_TPS (50.0); marginal in
    [MARGINAL_DECODE_TPS, MIN_DECODE_TPS) i.e. [35.0, 50.0); unviable below
    MARGINAL_DECODE_TPS, or on None/short-output input (fail closed).
    """

    # Default thresholds: min_tps=50.0, min_marginal_tps=35.0, min_output_tokens=24

    def test_viable_at_exactly_50_tps_and_sufficient_tokens(self):
        """Exactly at the 50 tok/s threshold with enough tokens -> viable."""
        self.assertEqual(decode_verdict(50.0, 24), "viable")

    def test_viable_above_50_tps_and_sufficient_tokens(self):
        """Well above threshold (e.g. 179 tok/s dGPU) -> viable."""
        self.assertEqual(decode_verdict(179.0, 200), "viable")

    def test_marginal_at_49_point_9_tps(self):
        """49.9 tok/s — just below viable, above the marginal floor -> marginal."""
        self.assertEqual(decode_verdict(49.9, 100), "marginal")

    def test_marginal_at_exactly_35_tps(self):
        """Exactly at the marginal floor (35.0) -> marginal (inclusive lower bound)."""
        self.assertEqual(decode_verdict(MARGINAL_DECODE_TPS, 100), "marginal")
        self.assertEqual(decode_verdict(35.0, 100), "marginal")

    def test_unviable_at_34_point_9_tps(self):
        """34.9 tok/s — just below the marginal floor — is unviable."""
        self.assertEqual(decode_verdict(34.9, 100), "unviable")

    def test_unviable_at_zero_tps(self):
        """0 tok/s is unviable."""
        self.assertEqual(decode_verdict(0.0, 50), "unviable")

    def test_unviable_when_predicted_n_below_min(self):
        """High tok/s but predicted_n below min_output_tokens -> unviable."""
        # Even 1000 tok/s doesn't pass if the output was too short.
        self.assertEqual(decode_verdict(1000.0, 23), "unviable")

    def test_unviable_when_predicted_n_is_zero(self):
        """predicted_n=0 (no output) is unviable regardless of tok/s."""
        self.assertEqual(decode_verdict(100.0, 0), "unviable")

    def test_unviable_on_none_tps(self):
        """None predicted_per_second is unviable (fail closed)."""
        self.assertEqual(decode_verdict(None, 100), "unviable")

    def test_unviable_on_none_predicted_n(self):
        """None predicted_n is unviable (fail closed)."""
        self.assertEqual(decode_verdict(100.0, None), "unviable")

    def test_unviable_on_both_none(self):
        """Both None inputs is unviable (fail closed)."""
        self.assertEqual(decode_verdict(None, None), "unviable")

    def test_custom_min_tps_respected(self):
        """Custom min_tps kwarg overrides the default 50.0 viable floor."""
        # At min_tps=100, 90 tok/s is now in the marginal band (still >= the
        # default 35.0 marginal floor), not viable.
        self.assertEqual(decode_verdict(90.0, 100, min_tps=100.0), "marginal")
        self.assertEqual(decode_verdict(100.0, 100, min_tps=100.0), "viable")
        # Below the marginal floor even with a raised min_tps -> unviable.
        self.assertEqual(decode_verdict(30.0, 100, min_tps=100.0), "unviable")

    def test_custom_min_marginal_tps_respected(self):
        """Custom min_marginal_tps kwarg overrides the default 35.0 floor."""
        self.assertEqual(decode_verdict(20.0, 100, min_marginal_tps=10.0), "marginal")
        self.assertEqual(decode_verdict(5.0, 100, min_marginal_tps=10.0), "unviable")

    def test_custom_min_output_tokens_respected(self):
        """Custom min_output_tokens kwarg overrides the default."""
        # At min_output_tokens=5, predicted_n=5 is viable if tps passes.
        self.assertEqual(decode_verdict(60.0, 5, min_output_tokens=5), "viable")
        # predicted_n=4 is still unviable.
        self.assertEqual(decode_verdict(60.0, 4, min_output_tokens=5), "unviable")

    def test_viable_exactly_at_both_minimums(self):
        """Exactly at both thresholds simultaneously -> viable."""
        self.assertEqual(decode_verdict(50.0, 24, min_tps=50.0, min_output_tokens=24), "viable")

    def test_unviable_one_below_token_threshold(self):
        """One token below min_output_tokens at exactly min_tps -> unviable."""
        self.assertEqual(decode_verdict(50.0, 23, min_tps=50.0, min_output_tokens=24), "unviable")


# ---------------------------------------------------------------------------
# Task 1.4: run_benchmark spawn args (mock-based — no real binary needed)
# ---------------------------------------------------------------------------


class TestRunBenchmarkSpawnArgs(unittest.TestCase):
    """Assert that run_benchmark builds the correct spawn args for llama-server.

    The real subprocess spawn is mocked; the test verifies the args list and
    that the child process is always terminated in the finally block.
    """

    def _run(self, coro):
        """Run a coroutine synchronously in a fresh event loop."""
        return asyncio.run(coro)

    def _make_mock_proc(self):
        """Return a mock asyncio subprocess with the minimal interface."""
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = None
        # terminate() and kill() are synchronous on the mock
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.poll = MagicMock(return_value=0)
        return proc

    def _make_health_ok(self):
        """Return an async mock for _wait_for_health that immediately returns True."""
        return AsyncMock(return_value=True)

    def _make_render_template(self, rendered="<rendered-prompt>"):
        """Return an async mock for the /apply-template call."""
        return AsyncMock(return_value=rendered)

    def _make_completion_response(self, tps=179.0, predicted_n=150):
        """Return the final-SSE-chunk dict ``_do_completion`` now returns.

        Task 1.6: ``_do_completion`` streams SSE and returns the terminating
        chunk (the one with ``"stop": true`` + ``"timings"``) as a plain
        dict, not an httpx Response — these tests patch ``_do_completion``
        directly, so the mock's return value must match that new contract.
        """
        return {
            "content": "some output text",
            "stop": True,
            "timings": {
                "predicted_per_second": tps,
                "predicted_n": predicted_n,
            },
        }

    def test_no_device_index_omits_device_flag(self):
        """When device_index is None, --device flag is NOT in spawn args."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    device_index=None,
                )
            )

        self.assertNotIn("--device", captured_args)

    def test_device_index_0_adds_vulkan0_flag(self):
        """device_index=0 adds --device Vulkan0 to the spawn args."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    device_index=0,
                )
            )

        self.assertIn("--device", captured_args)
        device_idx = captured_args.index("--device")
        self.assertEqual(captured_args[device_idx + 1], "Vulkan0")

    def test_device_index_1_adds_vulkan1_flag(self):
        """device_index=1 adds --device Vulkan1 to the spawn args."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    device_index=1,
                )
            )

        self.assertIn("--device", captured_args)
        device_idx = captured_args.index("--device")
        self.assertEqual(captured_args[device_idx + 1], "Vulkan1")

    def test_required_flags_in_spawn_args(self):
        """Spawn args always include -ngl 99 --jinja --reasoning off -c 4096."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        # Must include full-offload flag
        self.assertIn("--n-gpu-layers", captured_args)
        ngl_idx = captured_args.index("--n-gpu-layers")
        self.assertEqual(captured_args[ngl_idx + 1], "99")

        # Must include jinja flag
        self.assertIn("--jinja", captured_args)

        # Must include reasoning off
        self.assertIn("--reasoning", captured_args)
        r_idx = captured_args.index("--reasoning")
        self.assertEqual(captured_args[r_idx + 1], "off")

        # Must include context size
        self.assertIn("-c", captured_args)
        c_idx = captured_args.index("-c")
        self.assertEqual(captured_args[c_idx + 1], "4096")

    def test_child_terminate_called_in_finally_on_success(self):
        """The child process is terminated in the finally block even on success."""
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        mock_resp = self._make_completion_response()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        proc.terminate.assert_called_once()

    def test_child_terminate_called_in_finally_on_health_timeout(self):
        """Even when health check times out, the child is terminated."""
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            AsyncMock(return_value=False),  # health timeout
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        proc.terminate.assert_called_once()
        self.assertEqual(result.verdict, "unviable")
        self.assertIsNone(result.predicted_per_second)

    def test_health_timeout_maps_to_unviable(self):
        """Health check timeout -> verdict 'unviable' (fail closed)."""
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            AsyncMock(return_value=False),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        self.assertEqual(result.verdict, "unviable")

    def test_completion_error_maps_to_unviable(self):
        """HTTP error from /completion -> verdict 'unviable' (fail closed)."""
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        async def raise_http_error(*args, **kwargs):
            raise Exception("HTTPStatusError")

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(side_effect=raise_http_error),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        self.assertEqual(result.verdict, "unviable")

    def test_viable_result_returned_on_success(self):
        """When timings exceed thresholds, BenchmarkResult.verdict is 'viable'."""
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        mock_resp = self._make_completion_response(tps=179.0, predicted_n=150)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            result = self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                )
            )

        self.assertEqual(result.verdict, "viable")
        self.assertAlmostEqual(result.predicted_per_second, 179.0)
        self.assertEqual(result.predicted_n, 150)

    def test_ws_vk_device_env_override_applied(self):
        """WS_VK_DEVICE env var overrides device_index when device_index is None."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch.dict(os.environ, {"WS_VK_DEVICE": "1"}), patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    device_index=None,  # no explicit index; env should override
                )
            )

        # WS_VK_DEVICE=1 should have injected --device Vulkan1
        self.assertIn("--device", captured_args)
        device_idx = captured_args.index("--device")
        self.assertEqual(captured_args[device_idx + 1], "Vulkan1")

    def test_explicit_device_index_takes_precedence_over_env(self):
        """An explicit device_index kwarg overrides the WS_VK_DEVICE env var."""
        captured_args = []

        async def fake_create_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return self._make_mock_proc()

        mock_resp = self._make_completion_response()

        with patch.dict(os.environ, {"WS_VK_DEVICE": "1"}), patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess,
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            self._make_health_ok(),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            self._make_render_template(),
        ), patch(
            "whisperstream.gpu_benchmark._do_completion",
            AsyncMock(return_value=mock_resp),
        ):
            self._run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    device_index=0,  # explicit 0 overrides env WS_VK_DEVICE=1
                )
            )

        self.assertIn("--device", captured_args)
        device_idx = captured_args.index("--device")
        self.assertEqual(captured_args[device_idx + 1], "Vulkan0")


# ---------------------------------------------------------------------------
# Task 1.6: live WPS streaming — SSE parsing, on_tick throttling, fail-closed
# terminal-timings verdict (mocks the SSE stream itself, not _do_completion)
# ---------------------------------------------------------------------------


class _FakeSSEStreamResponse:
    """Fakes an httpx streamed Response: sync raise_for_status + async aiter_lines.

    Advances a shared fake monotonic clock by *steps[i]* right before yielding
    the i-th ``data:`` line, so a test can control exactly how far apart each
    streamed chunk "arrives" without any real sleeping.
    """

    def __init__(self, lines: list, clock: dict, steps: list):
        self._lines = lines
        self._clock = clock
        self._steps = steps

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        step_iter = iter(self._steps)
        for line in self._lines:
            if line.startswith("data:"):
                self._clock["t"] += next(step_iter)
            yield line


class _FakeSSEStreamCM:
    """Fakes the async context manager returned by ``httpx.AsyncClient.stream``."""

    def __init__(self, response: _FakeSSEStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


# Realistic llama.cpp b9204 stream shape: non-output metadata while the prompt
# is evaluated, partial chunks carrying content/tokens/stop, another non-output
# progress chunk, then the terminal chunk carrying tokens_predicted + timings.
_SSE_LINES = [
    'data: {"content": "", "tokens": [], "stop": false, '
    '"generation_settings": {"n_predict": 256}}',
    "",
    'data: {"content": "", "tokens": [], "stop": false}',
    "",
    'data: {"content": "One", "tokens": [4114], "stop": false}',
    "",
    'data: {"content": " two", "tokens": [1403], "stop": false}',
    "",
    'data: {"content": " three", "tokens": [3405], "stop": false}',
    "",
    'data: {"content": "", "tokens": [], "stop": false, '
    '"completion_probabilities": []}',
    "",
    'data: {"content": " four", "tokens": [2519], "stop": false}',
    "",
    'data: {"content": "", "tokens": [], "stop": true, '
    '"tokens_predicted": 200, "timings": '
    '{"predicted_per_second": 62.5, "predicted_n": 200}}',
    "",
]

# The first 2.4s is prompt evaluation and must not dilute live throughput.
# Generation starts when "One" arrives at t=2.5. Content ticks then land at
# generation elapsed 0.22s and 0.52s; terminal metadata lands at 0.82s and
# produces one final accurate tick without counting tokens_predicted.
_SSE_STEPS = [0.4, 2.0, 0.1, 0.1, 0.12, 0.2, 0.1, 0.3]


class TestDoCompletionSSEStreaming(unittest.TestCase):
    """_do_completion: generated-word timing, throttling, fail-closed verdict."""

    def _patched_stream(self, fake_cm):
        return patch("httpx.AsyncClient.stream", return_value=fake_cm)

    def test_live_wps_uses_content_only_and_excludes_prompt_evaluation(self):
        """Live WPS counts partial content only from the first generated text.

        Metadata/progress objects are ignored, and terminal tokens_predicted
        cannot replace the generated-word count or cause a terminal spike.
        """
        from whisperstream.gpu_benchmark import _do_completion

        clock = {"t": 0.0}
        fake_response = _FakeSSEStreamResponse(_SSE_LINES, clock, list(_SSE_STEPS))
        fake_cm = _FakeSSEStreamCM(fake_response)

        ticks: list = []

        def _on_tick(wps: float) -> None:
            ticks.append(wps)

        async def _go():
            with self._patched_stream(fake_cm), patch(
                "whisperstream.gpu_benchmark.time.monotonic", lambda: clock["t"]
            ):
                return await _do_completion(9999, "<rendered-prompt>", on_tick=_on_tick)

        result = asyncio.run(_go())

        # Two cadence ticks from content plus one final completion tick. Neither
        # non-output object emits a tick even though each crosses 200ms.
        self.assertEqual(len(ticks), 3)
        self.assertAlmostEqual(ticks[0], 3 / 0.22, places=5)
        self.assertAlmostEqual(ticks[1], 4 / 0.52, places=5)
        self.assertAlmostEqual(ticks[2], 4 / 0.82, places=5)

        # The verdict-bearing numbers come ONLY from the terminating SSE
        # chunk's timings — never from any on_tick wall-clock estimate.
        self.assertEqual(result["timings"]["predicted_per_second"], 62.5)
        self.assertEqual(result["timings"]["predicted_n"], 200)
        self.assertNotIn(62.5, ticks)
        self.assertLess(max(ticks), 20)

    def test_on_tick_not_required(self):
        """Passing no on_tick still returns the terminal chunk's timings."""
        from whisperstream.gpu_benchmark import _do_completion

        clock = {"t": 0.0}
        fake_response = _FakeSSEStreamResponse(_SSE_LINES, clock, list(_SSE_STEPS))
        fake_cm = _FakeSSEStreamCM(fake_response)

        async def _go():
            with self._patched_stream(fake_cm), patch(
                "whisperstream.gpu_benchmark.time.monotonic", lambda: clock["t"]
            ):
                return await _do_completion(9999, "<rendered-prompt>")

        result = asyncio.run(_go())
        self.assertEqual(result["timings"]["predicted_per_second"], 62.5)

    def test_stream_ending_without_stop_chunk_fails_closed(self):
        """If the stream ends without a terminating `stop`/`timings` chunk,
        _do_completion returns {} — the caller's decode_verdict(None, None)
        then falls through to 'unviable', same as any other failure mode."""
        from whisperstream.gpu_benchmark import _do_completion

        truncated_lines = [
            'data: {"content": "hello", "stop": false}',
            "",
            'data: {"content": " there", "stop": false}',
            "",
        ]
        clock = {"t": 0.0}
        fake_response = _FakeSSEStreamResponse(truncated_lines, clock, [0.05, 0.05])
        fake_cm = _FakeSSEStreamCM(fake_response)

        async def _go():
            with self._patched_stream(fake_cm), patch(
                "whisperstream.gpu_benchmark.time.monotonic", lambda: clock["t"]
            ):
                return await _do_completion(9999, "<rendered-prompt>")

        result = asyncio.run(_go())
        self.assertEqual(result, {})
        self.assertEqual(decode_verdict(result.get("timings", {}).get("predicted_per_second"),
                                         result.get("timings", {}).get("predicted_n")),
                          "unviable")


class TestRunBenchmarkLiveWpsEndToEnd(unittest.TestCase):
    """run_benchmark threads on_tick down into a real (SSE-mocked) _do_completion,
    and the returned BenchmarkResult still verdicts off the final timings."""

    def _make_mock_proc(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    def test_on_tick_fires_through_run_benchmark_verdict_from_final_timings(self):
        proc = self._make_mock_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return proc

        clock = {"t": 0.0}
        fake_response = _FakeSSEStreamResponse(_SSE_LINES, clock, list(_SSE_STEPS))
        fake_cm = _FakeSSEStreamCM(fake_response)

        ticks: list = []

        def _on_tick(wps: float) -> None:
            ticks.append(wps)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_create_subprocess
        ), patch(
            "whisperstream.gpu_benchmark._wait_for_health",
            AsyncMock(return_value=True),
        ), patch(
            "whisperstream.gpu_benchmark._render_prompt",
            AsyncMock(return_value="<rendered-prompt>"),
        ), patch(
            "httpx.AsyncClient.stream", return_value=fake_cm
        ), patch(
            "whisperstream.gpu_benchmark.time.monotonic", lambda: clock["t"]
        ):
            result = asyncio.run(
                run_benchmark(
                    binary_path=Path("fake/llama-server.exe"),
                    gguf_path=Path("fake/model.gguf"),
                    on_tick=_on_tick,
                )
            )

        # on_tick actually fired (proves the wiring run_benchmark -> on_tick
        # kwarg -> _do_completion is live, not just accepted-and-dropped).
        self.assertGreater(len(ticks), 0)
        # The verdict is derived from the FINAL llama-server timings (62.5
        # tok/s, 200 tokens -> "viable"), never from a live WPS tick.
        self.assertEqual(result.verdict, "viable")
        self.assertEqual(result.predicted_per_second, 62.5)
        self.assertEqual(result.predicted_n, 200)
        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Task 1.5: ngl_for_verdict — TDD tests (written BEFORE implementation)
# ---------------------------------------------------------------------------


class TestNglForVerdict(unittest.TestCase):
    """Tests for ngl_for_verdict() — maps a cached verdict string to n_gpu_layers."""

    def test_viable_returns_99(self):
        """ngl_for_verdict('viable') == 99 (full GPU offload)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict("viable"), 99)

    def test_marginal_returns_99(self):
        """ngl_for_verdict('marginal') == 99 (same full GPU offload as viable;
        marginal is "runs, just slowly", not a reason to drop to CPU)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict("marginal"), 99)

    def test_not_run_returns_0(self):
        """ngl_for_verdict('not_run') == 0 (CPU path)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict("not_run"), 0)

    def test_unviable_returns_0(self):
        """ngl_for_verdict('unviable') == 0 (GPU failed benchmark, use CPU)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict("unviable"), 0)

    def test_empty_string_returns_0(self):
        """ngl_for_verdict('') == 0 (unknown/blank value defaults to CPU)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict(""), 0)

    def test_arbitrary_string_returns_0(self):
        """Any unrecognised verdict string returns 0 (fail closed)."""
        from whisperstream.gpu_benchmark import ngl_for_verdict
        self.assertEqual(ngl_for_verdict("some_future_value"), 0)


# ---------------------------------------------------------------------------
# Task 2.1: recommend_tiers — full-ladder tier recommender (TDD — written
# BEFORE implementation)
# ---------------------------------------------------------------------------


class TestRecommendTiers(unittest.TestCase):
    """Tests for recommend_tiers() — pure tier recommendation logic.

    Uses the real LOCAL_BUNDLED_MODELS catalog (gemma4-e2b-instruct
    disk_mb=2962/min_system_ram_mb=8192, stream-1 disk_mb=697/
    min_system_ram_mb=4096, stream-1-lite disk_mb=219/min_system_ram_mb=2048)
    for scenarios that map directly onto the shipped catalog, and a synthetic
    3-rung catalog where a scenario needs round, easy-to-verify numbers.
    """

    def test_fast_gemma_all_rungs_clear_recommends_largest_finetune(self):
        """Fast measured Gemma + ample RAM/disk -> all 3 rungs clear, but the
        recommendation is the largest FINE-TUNE (Stream-1), not Gemma: Gemma is
        the customizable/opt-in general model (auto_recommend=False) and is
        never the ladder's auto-recommendation, even though it clears."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            120.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        # Recommendation skips Gemma and lands on the largest clearing fine-tune.
        self.assertEqual(result["recommended_model_id"], "stream-1")
        self.assertEqual(result["measured_model_id"], "gemma4-e2b-instruct")
        self.assertEqual(result["measured_tok_per_s"], 120.0)
        self.assertEqual(len(result["tiers"]), 3)
        # Gemma still appears in the ladder as a clearing (manually selectable)
        # rung — the exclusion is only from the AUTO-recommendation.
        for tier in result["tiers"]:
            self.assertTrue(tier["clears"], tier)
            self.assertIsNone(tier["reason"])

    def test_gemma_never_auto_recommended_even_when_only_gemma_measured(self):
        """Even if only Gemma is benched and every rung clears, the recommended
        id is a fine-tune — Gemma is opt-in only."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            250.0,  # very fast: all rungs clear comfortably
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=65536,
            free_disk_mb=65536,
        )
        self.assertNotEqual(result["recommended_model_id"], "gemma4-e2b-instruct")
        self.assertEqual(result["recommended_model_id"], "stream-1")
        gemma_tier = next(
            t for t in result["tiers"] if t["id"] == "gemma4-e2b-instruct"
        )
        self.assertTrue(gemma_tier["clears"])  # clears, but not recommended

    def test_measured_rung_tagged_measured_others_estimated(self):
        """Only the benched rung is tagged measured=True; the rest are est."""
        gemma_tier, stream_tier, lite_tier = recommend_tiers(
            "gemma4-e2b-instruct",
            120.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )["tiers"]
        self.assertTrue(gemma_tier["measured"])
        self.assertEqual(gemma_tier["tok_per_s"], 120.0)
        self.assertFalse(stream_tier["measured"])
        self.assertFalse(lite_tier["measured"])

    def test_slow_gemma_scaled_lighter_rung_clears(self):
        """Gemma just below the viable budget lands in the marginal band (still
        mem_ok, so it "clears" as runnable-with-consent, but its verdict is
        marginal, not viable); scaled lighter rungs are comfortably viable."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            49.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        tiers_by_id = {t["id"]: t for t in result["tiers"]}
        self.assertEqual(tiers_by_id["gemma4-e2b-instruct"]["verdict"], "marginal")
        self.assertTrue(tiers_by_id["gemma4-e2b-instruct"]["clears"])
        self.assertIsNone(tiers_by_id["gemma4-e2b-instruct"]["reason"])
        self.assertEqual(tiers_by_id["stream-1"]["verdict"], "viable")
        self.assertTrue(tiers_by_id["stream-1"]["clears"])
        self.assertIsNone(tiers_by_id["stream-1"]["reason"])
        self.assertEqual(tiers_by_id["stream-1-lite"]["verdict"], "viable")
        self.assertTrue(tiers_by_id["stream-1-lite"]["clears"])
        # Recommendation is the LARGEST VIABLE rung (stream-1, not lite) —
        # Gemma's marginal band doesn't count toward auto-recommendation even
        # though it "clears" (is runnable).
        self.assertEqual(result["recommended_model_id"], "stream-1")
        self.assertEqual(result["measured_verdict"], "marginal")

    def test_ram_below_gate_marks_needs_more_memory_even_if_fast(self):
        """RAM below a rung's min_system_ram_mb -> non-clearing, memory reason,
        even though its scaled tok/s estimate is well above budget."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            120.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=3000,  # below stream-1's 4096 and gemma's 8192
            free_disk_mb=32768,
        )
        tiers_by_id = {t["id"]: t for t in result["tiers"]}
        self.assertFalse(tiers_by_id["gemma4-e2b-instruct"]["clears"])
        self.assertEqual(
            tiers_by_id["gemma4-e2b-instruct"]["reason"], "Needs more memory"
        )
        self.assertFalse(tiers_by_id["stream-1"]["clears"])
        self.assertEqual(tiers_by_id["stream-1"]["reason"], "Needs more memory")
        self.assertGreater(tiers_by_id["stream-1"]["tok_per_s"], 50.0)
        self.assertTrue(tiers_by_id["stream-1-lite"]["clears"])  # 3000 >= 2048
        self.assertEqual(result["recommended_model_id"], "stream-1-lite")

    def test_disk_below_gate_marks_needs_more_memory(self):
        """Free disk below a rung's disk_mb -> non-clearing, memory reason,
        even with ample RAM."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            120.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=500,  # below stream-1's 697 and gemma's 2962
        )
        tiers_by_id = {t["id"]: t for t in result["tiers"]}
        self.assertFalse(tiers_by_id["gemma4-e2b-instruct"]["clears"])
        self.assertEqual(
            tiers_by_id["gemma4-e2b-instruct"]["reason"], "Needs more memory"
        )
        self.assertFalse(tiers_by_id["stream-1"]["clears"])
        self.assertEqual(tiers_by_id["stream-1"]["reason"], "Needs more memory")
        self.assertTrue(tiers_by_id["stream-1-lite"]["clears"])  # 500 >= 219
        self.assertEqual(result["recommended_model_id"], "stream-1-lite")

    def test_no_rung_clears_recommends_none(self):
        """Tiny RAM + slow measured card -> no rung clears -> None recommendation."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            5.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=500,
            free_disk_mb=100,
        )
        self.assertIsNone(result["recommended_model_id"])
        for tier in result["tiers"]:
            self.assertFalse(tier["clears"])
            self.assertIsNotNone(tier["reason"])

    def test_estimate_scaling_is_monotonic_lighter_faster(self):
        """Lighter rungs (smaller disk_mb) get a strictly higher est tok/s."""
        synthetic_catalog = [
            {"id": "heavy", "label": "Heavy", "disk_mb": 3000, "min_system_ram_mb": 8192},
            {"id": "mid", "label": "Mid", "disk_mb": 1000, "min_system_ram_mb": 4096},
            {"id": "light", "label": "Light", "disk_mb": 100, "min_system_ram_mb": 2048},
        ]
        result = recommend_tiers(
            "heavy",
            100.0,
            synthetic_catalog,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        tps_by_id = {t["id"]: t["tok_per_s"] for t in result["tiers"]}
        self.assertLess(tps_by_id["heavy"], tps_by_id["mid"])
        self.assertLess(tps_by_id["mid"], tps_by_id["light"])

    def test_measured_rung_keeps_real_value_others_scaled(self):
        """Measuring a middle rung: its tok/s stays real; others scale off it."""
        result = recommend_tiers(
            "stream-1",
            77.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        tiers_by_id = {t["id"]: t for t in result["tiers"]}
        self.assertEqual(tiers_by_id["stream-1"]["tok_per_s"], 77.0)
        self.assertTrue(tiers_by_id["stream-1"]["measured"])
        # gemma is heavier (bigger disk_mb) -> scaled down from 77.
        self.assertLess(tiers_by_id["gemma4-e2b-instruct"]["tok_per_s"], 77.0)
        self.assertFalse(tiers_by_id["gemma4-e2b-instruct"]["measured"])
        # lite is lighter (smaller disk_mb) -> scaled up from 77.
        self.assertGreater(tiers_by_id["stream-1-lite"]["tok_per_s"], 77.0)
        self.assertFalse(tiers_by_id["stream-1-lite"]["measured"])

    # -- Task 1.2: per-rung tri-state verdict + measured_verdict --------------

    def test_measured_rung_verdict_bands_at_boundaries(self):
        """The measured rung's own verdict follows the same tri-state bands as
        decode_verdict: viable >= 50, marginal 35..<50, unviable < 35."""
        cases = [
            (50.0, "viable"),
            (49.9, "marginal"),
            (35.0, "marginal"),
            (34.9, "unviable"),
        ]
        for tps, expected in cases:
            result = recommend_tiers(
                "gemma4-e2b-instruct",
                tps,
                LOCAL_BUNDLED_MODELS,
                available_ram_mb=32768,
                free_disk_mb=32768,
            )
            measured_tier = next(
                t for t in result["tiers"] if t["id"] == "gemma4-e2b-instruct"
            )
            self.assertEqual(measured_tier["verdict"], expected, tps)
            self.assertEqual(result["measured_verdict"], expected, tps)

    def test_marginal_measured_rung_clears_but_is_not_auto_recommended(self):
        """Measured tok/s in the marginal band -> tier verdict 'marginal',
        clears True (runnable-with-consent), reason None; the recommendation
        still requires 'viable', so a lighter viable rung wins instead."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            45.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        gemma_tier = next(
            t for t in result["tiers"] if t["id"] == "gemma4-e2b-instruct"
        )
        self.assertEqual(gemma_tier["verdict"], "marginal")
        self.assertTrue(gemma_tier["clears"])
        self.assertIsNone(gemma_tier["reason"])
        self.assertEqual(result["measured_verdict"], "marginal")
        self.assertEqual(result["recommended_model_id"], "stream-1")

    def test_unviable_measured_rung_does_not_clear(self):
        """Measured tok/s below the marginal floor -> verdict 'unviable',
        clears False, 'Needs a faster card' (mem_ok but too slow)."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            20.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        gemma_tier = next(
            t for t in result["tiers"] if t["id"] == "gemma4-e2b-instruct"
        )
        self.assertEqual(gemma_tier["verdict"], "unviable")
        self.assertFalse(gemma_tier["clears"])
        self.assertEqual(gemma_tier["reason"], "Needs a faster card")
        self.assertEqual(result["measured_verdict"], "unviable")

    def test_mem_gate_forces_unviable_verdict_regardless_of_speed(self):
        """not mem_ok always -> verdict 'unviable' (and clears False), even
        when the tok/s estimate is extremely high — the memory gate wins."""
        result = recommend_tiers(
            "gemma4-e2b-instruct",
            250.0,
            LOCAL_BUNDLED_MODELS,
            available_ram_mb=500,
            free_disk_mb=100,
        )
        for tier in result["tiers"]:
            self.assertEqual(tier["verdict"], "unviable", tier)
            self.assertFalse(tier["clears"], tier)
            self.assertEqual(tier["reason"], "Needs more memory", tier)

    def test_recommendation_requires_viable_not_just_clears(self):
        """A rung that only clears via the marginal band is never the
        recommendation — recommended_model_id falls through to None when no
        rung reaches the full 'viable' band, even though every rung 'clears'."""
        synthetic_catalog = [
            {
                "id": "heavy",
                "label": "Heavy",
                "disk_mb": 3000,
                "min_system_ram_mb": 8192,
            },
            {
                "id": "light",
                "label": "Light",
                "disk_mb": 3000,
                "min_system_ram_mb": 8192,
            },
        ]
        # Identical disk_mb -> identical scaled tok/s for every rung, so a
        # marginal measured value keeps every rung marginal (never viable).
        result = recommend_tiers(
            "heavy",
            40.0,
            synthetic_catalog,
            available_ram_mb=32768,
            free_disk_mb=32768,
        )
        for tier in result["tiers"]:
            self.assertEqual(tier["verdict"], "marginal", tier)
            self.assertTrue(tier["clears"], tier)
        self.assertIsNone(result["recommended_model_id"])
        self.assertEqual(result["measured_verdict"], "marginal")


if __name__ == "__main__":
    unittest.main()
