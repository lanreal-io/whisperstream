from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import whisperstream.stt_contracts as contracts_module
from whisperstream.parakeet_manager import ParakeetManager
from whisperstream.qwen_manager import QwenManager
from whisperstream.stt_contracts import (
    DetailedTranscriptionResult,
    InvalidSttAudio,
    SttCapabilities,
    SttEngine,
    SttHealth,
    SttHotwordSupport,
    SttHotwordUpdateMode,
    SttLanguageHintSupport,
    SttLifecycleState,
    SttPreviewMetadata,
    SttPreviewSupport,
    normalize_stt_audio,
)


class _FakePreview:
    def try_transcribe_preview(
        self, audio: np.ndarray, *, sample_rate: int = 16_000
    ) -> DetailedTranscriptionResult | None:
        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        return DetailedTranscriptionResult(text="ready" if waveform.size else "")


class _FakeHotwords:
    def __init__(self) -> None:
        self.update_count = 0

    def update_hotwords(self, hotwords: list[str], score: float) -> None:
        self.update_count += 1


class _FakeLanguageHint:
    def __init__(self) -> None:
        self.name: str | None = None

    def set_language_hint(self, name: str | None) -> None:
        self.name = name


class FakeSttEngine:
    """Platform-neutral fake usable for either a Windows or macOS adapter."""

    engine_id = "fake"

    def __init__(self, capabilities: SttCapabilities) -> None:
        self.capabilities = capabilities
        self.preview_capability = (
            _FakePreview() if capabilities.preview is not None else None
        )
        self.hotword_capability = (
            _FakeHotwords() if capabilities.hotwords is not None else None
        )
        self.language_hint_capability = (
            _FakeLanguageHint() if capabilities.language_hint is not None else None
        )
        self._loaded = False
        self._shutdown = False

    def load(self) -> None:
        if self._shutdown:
            raise RuntimeError("STT engine has been shut down")
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
    ) -> str:
        waveform = normalize_stt_audio(audio, sample_rate=sample_rate)
        return "ready" if waveform.size else ""

    def transcribe_chunk(
        self, audio: np.ndarray, *, sample_rate: int = 16_000
    ) -> str:
        return self.transcribe(audio, sample_rate=sample_rate)

    def transcribe_chunk_detailed(
        self, audio: np.ndarray, *, sample_rate: int = 16_000
    ) -> DetailedTranscriptionResult:
        return DetailedTranscriptionResult(
            text=self.transcribe_chunk(audio, sample_rate=sample_rate)
        )

    def health(self) -> SttHealth:
        state = (
            SttLifecycleState.SHUTDOWN
            if self._shutdown
            else SttLifecycleState.READY
            if self._loaded
            else SttLifecycleState.IDLE
        )
        return SttHealth(
            engine_id=self.engine_id,
            state=state,
            loaded=self._loaded,
        )

    def shutdown(self) -> None:
        self._loaded = False
        self._shutdown = True


def test_portable_contract_module_has_no_native_or_platform_imports() -> None:
    source = Path(contracts_module.__file__).read_text(encoding="utf-8").lower()

    for forbidden in (
        "import sherpa",
        "import onnxruntime",
        "import ctypes",
        "from pathlib",
        "namedpipe",
        "import tauri",
        "import fluidaudio",
    ):
        assert forbidden not in source


def test_fake_conforms_to_authoritative_lifecycle_and_transcription() -> None:
    engine = FakeSttEngine(SttCapabilities())

    assert isinstance(engine, SttEngine)
    assert engine.health() == SttHealth(
        engine_id="fake",
        state=SttLifecycleState.IDLE,
        loaded=False,
    )

    engine.load()
    assert engine.health().state is SttLifecycleState.READY
    assert engine.transcribe(np.zeros(8, dtype=np.float32)) == "ready"
    assert engine.transcribe_chunk(np.zeros(0, dtype=np.float32)) == ""
    assert not engine.capabilities.final_alignment
    assert engine.transcribe_chunk_detailed(np.zeros(8, dtype=np.float32)) == (
        DetailedTranscriptionResult(text="ready"))
    assert engine.transcribe_chunk_detailed(np.zeros(0, dtype=np.float32)) == (
        DetailedTranscriptionResult(text=""))

    engine.unload()
    assert engine.health().state is SttLifecycleState.IDLE
    engine.shutdown()
    engine.shutdown()
    assert engine.health().state is SttLifecycleState.SHUTDOWN


def test_optional_capabilities_are_explicitly_present_or_absent() -> None:
    capabilities = SttCapabilities(
        preview=SttPreviewSupport(
            metadata=SttPreviewMetadata.TEXT,
            nonblocking_admission=True,
        ),
        hotwords=SttHotwordSupport(update_mode=SttHotwordUpdateMode.RELOAD),
        language_hint=SttLanguageHintSupport(dynamic_updates=True),
    )
    supported = FakeSttEngine(capabilities)
    unsupported = FakeSttEngine(SttCapabilities())

    assert supported.preview_capability is not None
    assert supported.hotword_capability is not None
    assert supported.language_hint_capability is not None
    assert unsupported.preview_capability is None
    assert unsupported.hotword_capability is None
    assert unsupported.language_hint_capability is None


def test_windows_manager_capability_matrix_preserves_backend_differences() -> None:
    parakeet = object.__new__(ParakeetManager)
    qwen = object.__new__(QwenManager)

    assert isinstance(parakeet, SttEngine)
    assert parakeet.capabilities.preview == SttPreviewSupport(
        metadata=SttPreviewMetadata.TIMED_TOKENS,
        nonblocking_admission=True,
    )
    assert parakeet.preview_capability is parakeet
    assert parakeet.capabilities.final_alignment
    assert parakeet.hotword_capability is parakeet
    assert parakeet.capabilities.hotwords == SttHotwordSupport(
        update_mode=SttHotwordUpdateMode.RELOAD
    )
    assert parakeet.language_hint_capability is None

    assert isinstance(qwen, SttEngine)
    assert qwen.capabilities.preview == SttPreviewSupport(
        metadata=SttPreviewMetadata.TEXT,
        nonblocking_admission=True,
    )
    assert qwen.preview_capability is qwen
    assert not qwen.capabilities.final_alignment
    assert qwen.hotword_capability is None
    assert qwen.capabilities.hotwords is None
    assert qwen.language_hint_capability is qwen
    assert qwen.capabilities.language_hint == SttLanguageHintSupport(
        dynamic_updates=True
    )


def test_windows_lifecycle_methods_wrap_existing_lazy_manager_behavior() -> None:
    from tests._platform_test_support import _FakeSupervisor

    manager = ParakeetManager(
        model_name="test",
        quantization="int8",
        threads=1,
        logger=MagicMock(),
        model_dir=Path("/tmp/model"),
        timeout=0,
        _supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
    )

    assert manager.health().state is SttLifecycleState.READY
    manager.unload()
    assert manager.health().state is SttLifecycleState.IDLE
    manager.load()
    assert manager.health().state is SttLifecycleState.READY
    manager.shutdown()
    manager.shutdown()
    assert manager.health().state is SttLifecycleState.SHUTDOWN
    with pytest.raises(RuntimeError, match="shut down"):
        manager.load()


def test_audio_normalization_preserves_flattening_and_float32_contract() -> None:
    stereo_shaped = np.arange(8, dtype=np.int16).reshape(4, 2)

    normalized = normalize_stt_audio(stereo_shaped, sample_rate=np.int64(16_000))

    assert normalized.shape == (8,)
    assert normalized.dtype == np.float32
    np.testing.assert_array_equal(normalized, np.arange(8, dtype=np.float32))


@pytest.mark.parametrize(
    "audio",
    [
        np.array(1.0, dtype=np.float32),
        np.zeros((1, 1, 1), dtype=np.float32),
        np.array(["not-a-sample"]),
        np.array([1 + 2j]),
    ],
)
def test_audio_normalization_rejects_invalid_shape_or_dtype(audio: np.ndarray) -> None:
    with pytest.raises(InvalidSttAudio):
        normalize_stt_audio(audio, sample_rate=16_000)


@pytest.mark.parametrize("sample_rate", [0, -1, 16_000.5, True])
def test_audio_normalization_rejects_invalid_sample_rate(sample_rate: object) -> None:
    with pytest.raises(InvalidSttAudio, match="sample_rate"):
        normalize_stt_audio(
            np.zeros(4, dtype=np.float32),
            sample_rate=sample_rate,  # type: ignore[arg-type]
        )
