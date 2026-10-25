# Strings are LOCKED copy. Do not paraphrase. Backend-only — the frontend reads these from the WebSocket payload.

COPY = {
    "no_input_device": {
        "title": "No microphone found",
        "message": "Plug in a microphone to start recording.",
    },
    "backend_disconnect": {
        "title": "Whisperstream stopped responding",
        "message": "Restart Whisperstream to recover. Antivirus software can sometimes interrupt it.",
    },
    "model_load_failed": {
        "title": "Couldn't load the speech model",
        "message": "Restart Whisperstream to try again. You can also re-download the model from settings.",
    },
    "audio_capture_failed": {
        "title": "Couldn't start recording",
        "message": "Check that your microphone is connected, then try again.",
    },
}
