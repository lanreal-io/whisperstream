"""Synthetic import admission regression; no model, audio file or provider access."""
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from whisperstream import byok_consent, main
from whisperstream.config_manager import WhisperstreamConfig
from whisperstream.enhancement_security import consent_target
from whisperstream.llm_enhancer import EnhancementResult
from whisperstream.server import WhisperstreamServer
from whisperstream.transcript_store import TranscriptStore

pytestmark = pytest.mark.usefixtures("accepted_eula")


@pytest.mark.parametrize("state", ["accepted", "missing", "revoked", "changed_endpoint", "changed_model", "disabled"])
def test_import_retains_raw_and_only_enhances_with_current_permission(tmp_path, monkeypatch, state):
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(tmp_path))
    cfg = WhisperstreamConfig(
        enhancement_enabled=True, enhancement_provider="ollama", save_audio=False,
        history_enabled=True, default_profile_id="chat",
        profiles=[{"id": "chat", "name": "Chat", "mode": "chat", "prompt": "synthetic instructions", "signature": ""}],
    )
    if state != "missing":
        target = consent_target(cfg)
        byok_consent.record_consent("ollama", "Synthetic disclosure", target=target)
    if state == "revoked":
        byok_consent.remove_provider("ollama")
    elif state == "changed_endpoint":
        cfg.enhancement_ollama_endpoint = "https://other.invalid"
    elif state == "changed_model":
        cfg.enhancement_ollama_model = "other-model"
    elif state == "disabled":
        cfg.enhancement_enabled = False
    store = TranscriptStore()
    app = MagicMock()
    app.config, app.transcript_store = cfg, store
    app.stt_engine = MagicMock()
    app.stt_engine.transcribe.return_value = "synthetic raw imported text"
    app.text_injector.process.side_effect = lambda text: text
    for name in ("_snapshot_enhancement_profile", "_enhance_with_bounded_outcome", "_apply_email_signature"):
        setattr(app, name, getattr(main.WhisperstreamApp, name).__get__(app, main.WhisperstreamApp))
    app._enhancement_provenance = main.WhisperstreamApp._enhancement_provenance
    app.llm_enhancer.enhance_with_outcome = AsyncMock(return_value=EnhancementResult("synthetic enhanced text", "success"))
    monkeypatch.setattr(main, "_load_audio_file", lambda *_: (np.zeros(16000, dtype=np.float32), 16000))
    server = WhisperstreamServer(app, port=0, register_hotkeys=False)
    server.broadcast = MagicMock()
    try:
        server._imports.run(["synthetic.wav"])
        records = store.list()["items"]
        assert len(records) == 1
        record = store.get(records[0]["id"])
        assert record["raw_text"] == "synthetic raw imported text"
        if state == "accepted":
            app.llm_enhancer.enhance_with_outcome.assert_awaited_once()
            assert record["enhanced_text"] == "synthetic enhanced text"
        else:
            app.llm_enhancer.enhance_with_outcome.assert_not_awaited()
            assert record["enhanced_text"] is None
            assert record["enhancement_provenance"] is None
    finally:
        store.close()
