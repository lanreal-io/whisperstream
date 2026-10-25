"""Independent encoded and decoded limits for file transcription."""

MAX_AUDIO_FILE_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_AUDIO_FILE_BYTES + 64 * 1024
MAX_AUDIO_SECONDS = 15 * 60
MAX_DECODED_SAMPLES = 32 * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 120


class AudioLimitExceeded(ValueError):
    """The input exceeds file transcription's resource budget."""


class TranscriptionBusy(RuntimeError):
    """Another operation owns the recognizer."""
