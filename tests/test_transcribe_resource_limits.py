"""Small synthetic inputs exercise resource admission without real inference."""

import logging
import shutil
import subprocess
import threading
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from whisperstream.audio_limits import AudioLimitExceeded

pytestmark = pytest.mark.usefixtures("accepted_eula")


def test_wav_duration_rejected_before_reading_frames(tmp_path, monkeypatch):
    from whisperstream.main import _load_audio_file
    path = tmp_path / "long.wav"
    path.write_bytes(b"synthetic")
    reader = SimpleNamespace(getframerate=lambda: 16000, getnchannels=lambda: 1, getsampwidth=lambda: 2, getnframes=lambda: 16000 * 901)
    class Context:
        def __enter__(self): return reader
        def __exit__(self, *args): pass
    monkeypatch.setattr(wave, "open", lambda *args: Context())
    with pytest.raises(AudioLimitExceeded):
        _load_audio_file(str(path), logging.getLogger(__name__))


def test_encoded_size_rejected_before_decoder(tmp_path, monkeypatch):
    import whisperstream.audio_limits as limits
    from whisperstream.main import _load_audio_file
    path = tmp_path / "large.mp3"
    path.write_bytes(b"too large")
    monkeypatch.setattr(limits, "MAX_AUDIO_FILE_BYTES", 1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("decoder invoked"))
    with pytest.raises(AudioLimitExceeded):
        _load_audio_file(str(path), logging.getLogger(__name__))


def test_ffmpeg_has_time_output_and_diagnostic_limits(tmp_path, monkeypatch):
    from whisperstream.main import _load_audio_file
    path = tmp_path / "short.mp3"
    path.write_bytes(b"synthetic")
    def run(command, **kwargs):
        assert "-fs" in command and "-t" in command and "-nostdin" in command
        assert kwargs["timeout"] == 120
        assert kwargs["stderr"] == subprocess.DEVNULL
        kwargs["stdout"].write(b"\x00\x00" * 10)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    data, rate = _load_audio_file(str(path), logging.getLogger(__name__))
    assert data.size == 10 and rate == 16000


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
@pytest.mark.parametrize("seconds,accepted", [(0.05, True), (2, False)])
def test_real_compressed_decoder_enforces_duration(tmp_path, monkeypatch, seconds, accepted):
    import whisperstream.audio_limits as limits
    from whisperstream.main import _load_audio_file
    wav, flac = tmp_path / "synthetic.wav", tmp_path / "synthetic.flac"
    with wave.open(str(wav), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * int(seconds * 16000))
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), str(flac)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    monkeypatch.setattr(limits, "MAX_AUDIO_SECONDS", 1)
    if accepted:
        data, rate = _load_audio_file(str(flac), logging.getLogger(__name__))
        assert data.size == int(seconds * 16000) and rate == 16000
    else:
        with pytest.raises(AudioLimitExceeded):
            _load_audio_file(str(flac), logging.getLogger(__name__))


def test_upload_limit_before_multipart_parser(monkeypatch):
    from tests.test_server_endpoints import _make_client
    from whisperstream.server import middleware
    monkeypatch.setattr(middleware, "MAX_UPLOAD_REQUEST_BYTES", 8)
    client, server = _make_client()
    response = client.post("/api/transcribe", content=b"not multipart", headers={"content-type": "multipart/form-data; boundary=test"})
    assert response.status_code == 413
    server._app.stt_engine.transcribe.assert_not_called()
    client.close()


def test_chunked_upload_limit_uses_actual_bytes(monkeypatch):
    from tests.test_server_endpoints import _make_client
    from whisperstream.server import middleware
    monkeypatch.setattr(middleware, "MAX_UPLOAD_REQUEST_BYTES", 8)
    client, server = _make_client()
    response = client.post("/api/transcribe", content=iter([b"a" * 5, b"b" * 5]))
    assert response.status_code == 413
    server._app.stt_engine.transcribe.assert_not_called()
    client.close()


def test_inference_runs_off_serving_thread(monkeypatch):
    from tests.test_server_endpoints import _make_client
    import whisperstream.main as main
    client, server = _make_client()
    server._app.license_manager.can_start_recording.return_value = True
    server._app.config.enhancement_enabled = False
    server._app.text_injector.process.side_effect = lambda text: text
    monkeypatch.setattr(main, "_load_audio_file", lambda *_: (np.zeros(10, dtype=np.float32), 16000))
    threads = {}
    @server.fastapi.get("/test-thread")
    async def current():
        threads["serving"] = threading.get_ident()
        return {}
    def infer(*args, **kwargs):
        threads["inference"] = threading.get_ident()
        return "synthetic"
    server._app.stt_engine.transcribe.side_effect = infer
    with client:
        client.get("/test-thread")
        assert client.post("/api/transcribe", files={"file": ("test.wav", b"synthetic", "audio/wav")}).status_code == 200
    assert threads["serving"] != threads["inference"]


def test_busy_model_guard_prevents_even_decoding(monkeypatch):
    from tests.test_server_endpoints import _make_client
    import whisperstream.main as main
    client, server = _make_client()
    server._app.license_manager.can_start_recording.return_value = True
    monkeypatch.setattr(main, "_load_audio_file", lambda *_: pytest.fail("busy decoder invoked"))
    server._model_operation_lock.acquire()
    try:
        assert client.post("/api/transcribe", files={"file": ("test.wav", b"synthetic", "audio/wav")}).status_code == 409
    finally:
        server._model_operation_lock.release()
        client.close()
