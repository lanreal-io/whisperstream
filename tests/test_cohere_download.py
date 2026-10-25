from pathlib import Path
from unittest.mock import patch

from whisperstream.cohere_download import download_cohere_model
from whisperstream.model_downloader import ModelId


def test_cohere_download_is_thin_r2_registry_wrapper(tmp_path: Path) -> None:
    progress = lambda _done, _total: None
    cancel = object()
    with patch("whisperstream.cohere_download.download") as download:
        download_cohere_model(
            tmp_path,
            progress_callback=progress,
            cancel_event=cancel,
        )

    download.assert_called_once_with(
        ModelId.COHERE_TRANSCRIBE_INT8,
        tmp_path,
        progress_callback=progress,
        cancel_event=cancel,
    )
