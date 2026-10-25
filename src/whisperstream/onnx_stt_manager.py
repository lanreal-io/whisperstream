"""Native-free compatibility exports from the retired in-process STT base.

Production Parakeet and Qwen managers now share ``SupervisedSttManager`` and
all sherpa/ONNX work lives in child-only adapters. The Windows short-path
helper remains temporarily import-compatible for focused path tests; worker
adapters own the calls that cross into sherpa.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from .stt_contracts import DetailedTranscriptionResult
from .stt_errors import ModelNotPreparedError


def _windows_short_path(path: Path) -> Path:
    """Return Windows' 8.3 alias for an existing path."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path_name = kernel32.GetShortPathNameW
    get_short_path_name.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_short_path_name.restype = ctypes.c_uint32

    size = 260
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        result = get_short_path_name(str(path), buffer, size)
        if result == 0:
            error = ctypes.get_last_error()
            raise OSError(
                error,
                ctypes.FormatError(error) if error else "GetShortPathNameW failed",
                str(path),
            )
        if result < size:
            return Path(buffer.value)
        size = result + 1


def model_dir_for_sherpa(model_dir: Path) -> Path:
    """Return the Windows short-path form formerly used by the parent base."""

    if sys.platform != "win32" or str(model_dir).isascii():
        return model_dir
    if not model_dir.exists():
        raise ModelNotPreparedError(f"Speech model directory not found: {model_dir}")
    try:
        short_path = _windows_short_path(model_dir)
    except OSError as exc:
        raise ModelNotPreparedError(
            "Whisperstream cannot load this speech model because its Windows "
            "path contains special characters and Windows could not create a "
            f"compatible short path: {model_dir}. Move model storage on the "
            "Home page to an ASCII-only folder such as C:\\WhisperstreamModels, "
            "then try again."
        ) from exc
    if not str(short_path).isascii():
        raise ModelNotPreparedError(
            "Whisperstream cannot load this speech model because its Windows "
            "path contains special characters and Windows did not return an "
            f"ASCII-compatible short path: {model_dir}. Move model storage on "
            "the Home page to an ASCII-only folder such as "
            "C:\\WhisperstreamModels, then try again."
        )
    return short_path


__all__ = [
    "DetailedTranscriptionResult",
    "ModelNotPreparedError",
    "model_dir_for_sherpa",
]
