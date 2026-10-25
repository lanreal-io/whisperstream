from __future__ import annotations

from pathlib import Path

import pytest

from whisperstream.stt_worker_process import (
    SttWorkerProcess,
    SttWorkerRemoteError,
    source_worker_command,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_qwen_worker_negotiates_capabilities_and_rejects_hotwords(
    tmp_path: Path,
) -> None:
    process = SttWorkerProcess(
        source_worker_command(REPO_ROOT),
        backend="qwen",
        diagnostic_dir=tmp_path,
        cwd=REPO_ROOT,
    )
    negotiated = process.start(timeout=20)
    worker_pid = process.pid
    try:
        assert worker_pid is not None
        assert negotiated["engine_id"] == "qwen"
        assert negotiated["capabilities"] == {
            "preview": {"metadata": "text", "nonblocking_admission": True},
            "language_hint": {"dynamic_updates": True},
        }
        assert process.request(
            "set_language_hint",
            body={"language": "Japanese"},
            timeout=5,
        ) == {"updated": True, "loaded": False}
        with pytest.raises(SttWorkerRemoteError) as captured:
            process.request(
                "update_hotwords",
                body={"hotwords": [], "score": 2.0},
                timeout=5,
            )
        assert captured.value.error["category"] == "capability"
        assert captured.value.error["code"] == "capability_unsupported"
    finally:
        report = process.shutdown(timeout=5)
    assert report is not None and report.expected is True
