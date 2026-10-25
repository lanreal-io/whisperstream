"""Exercise the real server GGUF dispatch with both platform storage owners."""
import threading
from unittest.mock import MagicMock, patch

import pytest

from whisperstream.macos_model_lifecycle import MacosModelLifecycleAdapter, MacosModelStorage
from whisperstream.model_downloader import DownloadCancelled
from whisperstream.server import WhisperstreamServer
from whisperstream.windows_model_lifecycle import WindowsModelLifecycleAdapter


@pytest.fixture(params=["Darwin", "Windows"])
def server(request, tmp_path):
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._model_generation_lock = threading.Lock()
    server._model_operation_generation = 0
    server.broadcast = MagicMock()
    server._model_lifecycle = (
        MacosModelLifecycleAdapter(storage=MacosModelStorage(
            default_home=tmp_path / 'home', state_path=tmp_path / 'state.json'))
        if request.param == 'Darwin' else WindowsModelLifecycleAdapter()
    )
    from whisperstream.server.gguf_download import RegisteredGGUFDownload
    server._gguf_download = RegisteredGGUFDownload(
        next_generation=server._next_model_generation,
        execute=server._execute_windows_model_operation,
        broadcast=server.broadcast,
    )
    return server


def test_registered_gguf_acquisition_calls_downloader_and_reports_progress(server, tmp_path):
    target = tmp_path / 'synthetic.gguf'
    cancelled = threading.Event()
    def acquire(model_id, path, *, progress_callback, cancel_event):
        assert model_id == 'gemma4-e2b-instruct'
        assert cancel_event is cancelled
        path.write_bytes(b'public-synthetic-gguf')
        progress_callback(21, 21)
    with patch('whisperstream.enhancement_models.resolve_local_model_path', return_value=target), patch('whisperstream.setup.download_llm_model', side_effect=acquire) as download:
        assert server._gguf_download.fetch('gemma4-e2b-instruct', cancelled) == (True, False)
        assert server._gguf_download.fetch('gemma4-e2b-instruct', cancelled) == (True, True)
        assert download.call_count == 2
    assert server.broadcast.call_args.args[0] == {
        'type':'download.progress', 'kind':'local_llm', 'downloaded':21,
        'total':21, 'speed_bps':0, 'model_id':'gemma4-e2b-instruct',
    }


def test_cancelled_gguf_request_never_calls_downloader(server, tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    with patch('whisperstream.enhancement_models.resolve_local_model_path', return_value=tmp_path/'synthetic.gguf'), patch('whisperstream.setup.download_llm_model') as download:
        with pytest.raises(DownloadCancelled):
            server._gguf_download.fetch('gemma4-e2b-instruct', cancelled)
        download.assert_not_called()


def test_gguf_download_preserves_original_error_for_caller(server, tmp_path):
    failure = OSError('synthetic download failure')
    with patch('whisperstream.enhancement_models.resolve_local_model_path', return_value=tmp_path/'synthetic.gguf'), patch('whisperstream.setup.download_llm_model', side_effect=failure):
        with pytest.raises(OSError) as caught:
            server._gguf_download.fetch('gemma4-e2b-instruct', threading.Event())
    assert caught.value is failure


def test_failed_speed_check_keeps_device_measurement_with_unviable_verdict(tmp_path):
    from whisperstream.gpu_benchmark import BenchmarkResult, VulkanDevice
    from whisperstream.server.benchmark_operation import BenchmarkOperation
    benchmark = BenchmarkOperation.__new__(BenchmarkOperation)
    benchmark._logger = MagicMock()
    benchmark.publish = MagicMock()
    async def measure(**kwargs):
        return BenchmarkResult(verdict='unviable', predicted_per_second=28.0, predicted_n=200)
    with patch('whisperstream.gpu_benchmark.run_benchmark', side_effect=measure):
        device, rate, verdict = benchmark.bench_one_rung(
            tmp_path/'llama-server', tmp_path/'synthetic.gguf',
            [VulkanDevice(index=0, name='Synthetic GPU', total_mib=8192)], 'gemma4-e2b-instruct',
        )
    assert device.index == 0
    assert verdict == 'unviable'
    assert rate == 28.0
