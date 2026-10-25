"""Pinned public model delivery with compatible local cache reuse.

Model weights use immutable Hugging Face revisions and exact upstream GitHub
backups where supported. The cloud model-name catalog is a separate service.
Legacy private-only weights remain usable from existing complete local caches.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

import httpx


DOWNLOAD_CHUNK_SIZE = 65536

from .model_sources import MODEL_SOURCES

# Files required by sherpa-onnx for a Parakeet transducer bundle. Shared by
# both Parakeet specs and re-exported from setup.py (stt_factory + tests still
# import it from there).
REQUIRED_FILES: tuple[str, ...] = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)

# GitHub asr-models release — the manual fallback named in the Qwen spec's
# ``manual_fallback_hint`` when Hugging Face is unavailable.
_QWEN_GITHUB_TARBALL = (
    "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2"
    " (https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)"
)

class DownloadCancelled(Exception):
    """Raised by the streaming helpers when ``cancel_event`` is set.

    Callers in ``server.py`` catch this separately from generic
    ``Exception`` so the cancel flow can broadcast ``download.cancelled``
    instead of ``download.error``. Propagates up through
    ``download_model`` / ``download_llm_model`` without triggering another
    request.
    """


def _validate_hf_request(request: httpx.Request) -> None:
    host = request.url.host
    if request.url.scheme != "https" or not (
        host == "huggingface.co" or host.endswith(".huggingface.co")
        or host == "hf.co" or host.endswith(".hf.co")
        or host.endswith(".xethub.hf.co")
    ):
        raise RuntimeError("Model download redirected to an unsupported host")
    if "authorization" in request.headers:
        raise RuntimeError("Model downloads must be unauthenticated")


def _validate_github_request(request: httpx.Request, archive_url: str) -> None:
    """Only the shipped upstream asset URL and GitHub's asset CDN are allowed."""
    if (
        not archive_url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/")
        or request.url.scheme != "https"
        or request.url.port not in (None, 443)
        or request.url.userinfo
        or "authorization" in request.headers
        or not (
            str(request.url) == archive_url
            or request.url.host == "release-assets.githubusercontent.com"
        )
    ):
        raise RuntimeError("Model download redirected to an unsupported host")


def new_http_client(*, github_url: str | None = None) -> httpx.Client:
    """Construct the ``httpx.Client`` used for every model download.

    Whisperstream-defaults:

    - A 60-second idle read timeout lets stalled sources retry/fall back without
      imposing a total deadline on multi-GB downloads.
    - ``follow_redirects=True`` with a source-specific HTTPS allowlist enforced
      before each request. GitHub is never added to the HF client's allowlist.
    """
    timeout = httpx.Timeout(connect=30.0, read=60.0, write=None, pool=None)
    validate = (
        _validate_hf_request
        if github_url is None
        else (lambda request: _validate_github_request(request, github_url))
    )
    return httpx.Client(timeout=timeout, follow_redirects=True, event_hooks={"request": [validate]})


def probe_content_length(
    client: httpx.Client,
    url: str,
    *,
    logger: logging.Logger,
    label: str,
) -> int | None:
    """Issue a ``HEAD`` request and return ``Content-Length`` as ``int``.

    Returns ``None`` if the HEAD request fails or the server omits the
    header. Used by callers to drive a real "X of Y" progress bar from
    the first chunk onwards; a ``None`` return means "show indeterminate
    progress until the body stream finishes".

    Never raises. HEAD failures are downgraded to a DEBUG log line.
    """
    try:
        head = client.head(url)
        head.raise_for_status()
        cl = head.headers.get("Content-Length")
        return int(cl) if cl else None
    except Exception as exc:
        logger.debug(
            "download head failed file=%s reason=%s",
            label,
            type(exc).__name__,
        )
        return None


class CumulativeProgress:
    """Drive one monotonic progress bar across a multi-file download.

    A model download is several files (Parakeet ships 4 ONNX files, Qwen 6).
    The progress bar should fill 0->100 ONCE across the whole set, not reset
    to 0 at each file boundary. This helper owns that: it holds a single grand
    total and a running cumulative byte count, and hands out one ``on_chunk``
    that every file's ``stream_to_file`` call shares, so the reported fraction
    only ever climbs.

    Pinned model paths construct this directly from the manifest's known total.
    The generic :meth:`probe` constructor remains useful when a caller must
    discover totals with ``HEAD`` requests. Before this existed, downloaders
    reimplemented the cumulative counter inline, and the Qwen one reported
    ``(cumulative_bytes, current_file_size)``, so the bar shot to 100% on the
    small first file then snapped back down when the next, larger file started.

    If a file's size is unknown (the ``HEAD`` failed or returned no
    ``Content-Length``), its bytes are simply left out of the grand total; the
    total is only ``None`` (indeterminate) when EVERY probe failed. Letting one
    unknown size poison a multi-GB total used to leave the download bar stuck
    at 0% the whole time. A small unknown file can now make the fraction reach
    100% a hair early, which the frontend clamps; that is deliberately
    preferred over an indeterminate bar for an entire download.
    """

    def __init__(
        self,
        total_bytes: int | None,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> None:
        self.total_bytes = total_bytes
        self._progress_callback = progress_callback
        self._downloaded = 0

    @classmethod
    def probe(
        cls,
        client: httpx.Client,
        urls: list[str],
        *,
        logger: logging.Logger,
        labels: list[str] | None = None,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> "CumulativeProgress":
        """Probe every URL's ``Content-Length`` and sum into one grand total.

        The grand total is the sum of all sizes when every probe succeeds; if
        any one is unknown, the total is ``None`` (so callers show indeterminate
        progress instead of a bar that can exceed 100%). ``labels`` (defaulting
        to the URLs themselves) only feed :func:`probe_content_length`'s debug
        log line.
        """
        sizes = [
            probe_content_length(
                client,
                url,
                logger=logger,
                label=(labels[i] if labels else url),
            )
            for i, url in enumerate(urls)
        ]
        # Sum the KNOWN sizes; fall back to indeterminate (None) only when
        # EVERY probe failed. A single small file with no Content-Length must
        # NOT zero out the whole total (see the class docstring) -- that left
        # the bar stuck at 0% for the entire multi-GB download. Unknown files
        # still accumulate via on_chunk, so `downloaded` can edge slightly past
        # this total at the end; the frontend clamps the fraction to 100%.
        known = [s for s in sizes if s is not None]
        total = sum(known) if known else None
        return cls(total, progress_callback)

    def on_chunk(self, n: int) -> None:
        """Advance the cumulative counter and report ``(downloaded, total)``.

        Pass this as the ``on_chunk`` for every file's :func:`stream_to_file`
        call so the byte count accumulates across the whole download.
        """
        self._downloaded += n
        if self._progress_callback is not None:
            self._progress_callback(self._downloaded, self.total_bytes)


def stream_to_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    on_chunk: Callable[[int], None],
    cancel_event: threading.Event | None = None,
    cancel_message: str = "download cancelled",
) -> None:
    """Stream *url* into *dest* atomically.

    Writes to ``dest.with_suffix(dest.suffix + ".tmp")`` chunk-by-chunk,
    calls ``on_chunk(len(chunk))`` after every successful write so
    callers can advance a progress bar, then ``os.replace(tmp, dest)``
    for cross-platform atomic rename (``Path.rename`` fails on Windows
    when the destination already exists).

    If ``cancel_event`` is supplied and set between chunks,
    :class:`DownloadCancelled` is raised with ``cancel_message`` as the
    payload. The ``except BaseException`` cleanup unlinks the partial
    ``.tmp`` file before re-raising.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled(cancel_message)
                    if not chunk:
                        continue
                    fh.write(chunk)
                    on_chunk(len(chunk))
        os.replace(tmp, dest)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def copy_tree_with_progress(
    src: Path,
    dst: Path,
    *,
    on_bytes: Callable[[int], None],
    cancel_event: threading.Event | None = None,
    cancel_message: str = "relocation cancelled",
) -> None:
    """Recursively copy the *src* directory tree into *dst*, chunk by chunk.

    Mirrors the streaming + cleanup contract of :func:`stream_to_file` for the
    local-to-local copy used by the model-relocation worker (Task 2.3):

    - Files are read and written in ``DOWNLOAD_CHUNK_SIZE`` chunks; ``on_bytes``
      is invoked with each chunk length so the worker can broadcast aggregate
      byte progress across BOTH model trees (the Parakeet ``models/`` tree and
      the ``local-llm/`` GGUF tree).
    - ``cancel_event`` is honoured before each file and between chunks; when set,
      :class:`DownloadCancelled` is raised with ``cancel_message``.
    - On ANY ``BaseException`` (cancel, OS error, or an ``on_bytes`` callback
      that raises) the partially written *dst* tree is removed before
      re-raising, so a failed copy never leaves a half-populated staging
      directory behind.

    Unlike :func:`stream_to_file` there is no atomic rename here: the worker
    copies into a ``.tmp`` staging directory and performs the atomic
    ``os.replace`` of the whole directory itself once the copy is verified
    (cross-volume ``os.rename`` would raise ``EXDEV``, so a copy + verify +
    same-drive rename is the only safe move across drives).
    """
    src = Path(src)
    dst = Path(dst)
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for root, _dirs, files in os.walk(src):
            root_path = Path(root)
            rel = root_path.relative_to(src)
            target_dir = dst / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            for fname in files:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled(cancel_message)
                src_file = root_path / fname
                dst_file = target_dir / fname
                with open(src_file, "rb") as rfh, open(dst_file, "wb") as wfh:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled(cancel_message)
                        chunk = rfh.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        wfh.write(chunk)
                        on_bytes(len(chunk))
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def download_file_with_sha256(
    client: httpx.Client,
    url: str,
    dest: Path,
    expected_sha256: str,
    on_chunk: Callable[[int], None],
    cancel_event: threading.Event | None = None,
    *,
    expected_size: int | None = None,
) -> None:
    """Stream *url* into *dest* atomically, verifying SHA256 along the way.

    Same atomicity and cancellation contract as :func:`stream_to_file`
    plus an integrity check: SHA256 is computed incrementally during the
    stream and compared to *expected_sha256* (case-insensitive hex)
    before the atomic rename. Hash mismatch raises ``RuntimeError`` and
    unlinks the ``.tmp`` file.

    Used by every pinned file and archive model path, where Whisperstream
    carries an explicit reviewed hash.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    hasher = hashlib.sha256()
    received = 0
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("download cancelled")
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled(
                            f"download cancelled during {url}"
                        )
                    if not chunk:
                        continue
                    received += len(chunk)
                    if expected_size is not None and received > expected_size:
                        raise RuntimeError("Model download exceeds its pinned size")
                    fh.write(chunk)
                    hasher.update(chunk)
                    on_chunk(len(chunk))
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("download cancelled")
        if expected_size is not None and received != expected_size:
            raise RuntimeError("Model download does not match its pinned size")
        computed = hasher.hexdigest().lower()
        expected = expected_sha256.lower()
        if computed != expected:
            raise RuntimeError(
                f"SHA256 mismatch for {url}: "
                f"expected {expected}, got {computed}"
            )
        os.replace(tmp, dest)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Retired manifest compatibility helpers. No runtime manifest fetch exists.
# ---------------------------------------------------------------------------


def fetch_manifest(client: httpx.Client) -> dict:
    """Retired compatibility entry point; never contact company infrastructure."""
    raise RuntimeError("Remote model manifests are unavailable. Use the bundled model catalog.")


def get_manifest_files(
    manifest: dict, model_name: str, quantization: str | None
) -> list[dict]:
    """Drill into ``manifest["models"][model_name][quant]["files"]``.

    ``quantization`` defaults to ``"int8"`` when ``None``. Raises
    ``RuntimeError`` with a clear message if any nesting level is missing or
    ``files`` is not a list.
    """
    q = (quantization or "int8").lower()
    try:
        models = manifest["models"]
        model_entry = models[model_name]
        quant_entry = model_entry[q]
        files = quant_entry["files"]
    except KeyError as exc:
        raise RuntimeError(
            f"Manifest missing entry for model '{model_name}' quantization '{q}': {exc}"
        ) from exc
    if not isinstance(files, list):
        raise RuntimeError(
            f"Manifest 'files' for model '{model_name}' quantization '{q}' is not a list"
        )
    return files


def generate_bpe_vocab(model_dir: Path) -> None:
    """Generate ``bpe.vocab`` from ``tokens.txt`` for hotword support.

    The bpe.vocab file maps token strings to scores. When the real
    SentencePiece model isn't available we use a score of 0 for every token --
    sufficient for sherpa-onnx hotword matching, which only needs the
    token-to-id mapping for BPE segmentation. Used as the Parakeet specs'
    ``post_download`` hook; a no-op when ``tokens.txt`` is absent or the vocab
    already exists.
    """
    tokens_path = model_dir / "tokens.txt"
    vocab_path = model_dir / "bpe.vocab"
    if vocab_path.exists():
        return
    if not tokens_path.exists():
        return
    lines: list[str] = []
    for raw in tokens_path.read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split()
        if len(parts) >= 2:
            token = parts[0]
            lines.append(f"{token}\t0")
    vocab_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Registry: one ModelSpec per STT model, one download() over them all.
# ---------------------------------------------------------------------------


class ModelId(str, Enum):
    """Stable identifier for every downloadable model (STT + local-LLM GGUF).

    For Parakeet, the value also names the
    on-disk directory name produced by ``config_manager.model_dir`` /
    ``config.parakeet_model``. For the GGUF entries the value also equals the
    ``enhancement_models.LOCAL_BUNDLED_MODELS`` catalog ``id`` -- the two
    catalogs are meant to line up 1:1.
    """

    PARAKEET_V3_INT8 = "nemo-parakeet-tdt-0.6b-v3"
    PARAKEET_V2_INT8 = "nemo-parakeet-tdt-0.6b-v2"
    QWEN3_ASR_06B_INT8 = "qwen3-asr-0.6b-int8"
    COHERE_TRANSCRIBE_INT8 = "cohere-transcribe-03-2026-int8"
    # Local-LLM GGUFs (kind="gguf" in DOWNLOAD_REGISTRY below). Values match
    # enhancement_models.LOCAL_BUNDLED_MODELS ids exactly.
    STREAM_1_MINI_Q4 = "stream-1-mini"
    GEMMA_4_E2B_Q4 = "gemma4-e2b-instruct"
    GEMMA_4_E2B_GOOGLE_QAT = "gemma4-e2b-google-qat"
    # Agent-speech TTS voices use pinned Hugging Face files or archives.
    SUPERTONIC_3_INT8 = "supertonic-3-int8-2026-05-11"
    KOKORO_82M_FP32 = "kokoro-82m-fp32-en-us-sherpa-v1_0"


@dataclass(frozen=True)
class ModelSpec:
    """Download-only config for one model. Every per-model variance is a field.

    Attributes:
        id: Stable model-source and catalog id.
        files: Relative file paths (flat OR with subdirs, e.g. Qwen's
            ``tokenizer/``) that must all be present for the model to load.
        dir_name: On-disk subfolder under ``config_manager.get_models_root()``
            where this model's files live. The single source of truth for
            destination; :func:`model_dir` joins it onto the models root.
            Parakeet keeps its historical ``models/<variant>-int8`` names so no
            re-download is forced; Qwen moves under ``models/`` from its old
            top-level ``qwen3-asr/`` location. GGUF specs set this for
            consistency only -- ``download()`` is always called with an
            explicit ``dest_dir`` for GGUFs (the shared ``local-llm/``
            directory), so :func:`model_dir`/:func:`is_present`/
            :func:`delete_model` are never used for them.
        post_download: Optional hook run with ``dest_dir`` after a successful
            download (Parakeet -> :func:`generate_bpe_vocab`).
        manual_fallback_hint: Optional human-readable archive description;
            when a download fails, :func:`download` raises a ``RuntimeError`` naming it
            (Qwen's GitHub tarball).
        kind: Discriminates STT models (``"stt"``, the default) from local-LLM
            GGUFs (``"gguf"``) and the agent-speech TTS voice (``"tts"``).
            :func:`delete_unused_stt_models` and ``setup.py``'s non-Parakeet
            dir-name guard filter on ``"stt"`` so user-initiated STT cleanup
            can never delete a downloaded GGUF or TTS voice. Adding a model to
            this registry without setting ``kind`` correctly could expose the
            wrong artifact to cleanup.
        backend: User-facing STT backend family (``"parakeet"`` or ``"qwen"``).
            Required for ``kind == "stt"`` rows and ``None`` for other model
            families. Exact artifact status reads this field instead of
            inferring a backend from model-id spelling.
        archive_url: Historical compatibility field, unused by runtime delivery.
        archive_sha256: Historical archive digest; MODEL_SOURCES owns delivery pins.
        download_bytes: Exact archive bytes when known. Used for truthful UI
            estimates without a network probe.
        installed_bytes: Exact extracted payload bytes when known. This is
            disk usage, not runtime memory.
    """

    id: str
    files: tuple[str, ...]
    dir_name: str
    post_download: Callable[[Path], None] | None = None
    manual_fallback_hint: str | None = None
    kind: str = "stt"
    backend: str | None = None
    archive_url: str | None = None
    archive_sha256: str | None = None
    download_bytes: int | None = None
    installed_bytes: int | None = None


def _qwen_spec() -> ModelSpec:
    """Build the Qwen ModelSpec from the qwen_models catalog (one source of truth)."""
    from .qwen_models import get_qwen_model

    entry = get_qwen_model(ModelId.QWEN3_ASR_06B_INT8.value)
    if entry is None:
        raise RuntimeError(
            "qwen_models.QWEN_MODELS is missing the "
            f"{ModelId.QWEN3_ASR_06B_INT8.value!r} entry"
        )
    return ModelSpec(
        id=ModelId.QWEN3_ASR_06B_INT8.value,
        files=tuple(entry["files"]),
        # NEW location: moved under models/ for symmetry with Parakeet. Was the
        # top-level qwen3-asr/ sibling; existing installs re-download once.
        dir_name="qwen3-asr-0.6b-int8",
        post_download=None,
        manual_fallback_hint=_QWEN_GITHUB_TARBALL,
        backend="qwen",
    )


COHERE_TRANSCRIBE_FILES = (
    "encoder.int8.onnx",
    "encoder.int8.onnx.data",
    "decoder.int8.onnx",
    "tokens.txt",
)


DOWNLOAD_REGISTRY: dict[ModelId, ModelSpec] = {
    ModelId.PARAKEET_V3_INT8: ModelSpec(
        id=ModelId.PARAKEET_V3_INT8.value,
        files=REQUIRED_FILES,
        # UNCHANGED — must equal config_manager.model_dir()'s historical output
        # (slug + "-int8") so installed Parakeet bundles are NOT re-downloaded.
        dir_name="nemo-parakeet-tdt-0.6b-v3-int8",
        post_download=generate_bpe_vocab,
        backend="parakeet",
    ),
    ModelId.PARAKEET_V2_INT8: ModelSpec(
        id=ModelId.PARAKEET_V2_INT8.value,
        files=REQUIRED_FILES,
        # UNCHANGED (see v3 note).
        dir_name="nemo-parakeet-tdt-0.6b-v2-int8",
        post_download=generate_bpe_vocab,
        backend="parakeet",
    ),
    ModelId.QWEN3_ASR_06B_INT8: _qwen_spec(),
    ModelId.COHERE_TRANSCRIBE_INT8: ModelSpec(
        id=ModelId.COHERE_TRANSCRIBE_INT8.value,
        files=COHERE_TRANSCRIBE_FILES,
        dir_name="cohere-transcribe-03-2026-int8",
        backend="cohere",
    ),
    # -------------------------------------------------------------------
    # Local-LLM GGUFs (kind="gguf"). Excluded from every STT-only sweep
    # (delete_unused_stt_models below; setup.py's non-Parakeet dir-name
    # guard). dir_name is set for consistency only -- these are always
    # downloaded with an explicit dest_dir (config_manager.get_local_llm_dir()),
    # never resolved via model_dir()/is_present()/delete_model().
    # -------------------------------------------------------------------
    ModelId.STREAM_1_MINI_Q4: ModelSpec(
        id=ModelId.STREAM_1_MINI_Q4.value,
        files=("stream-1-mini-vocabulary-q4_k_m.gguf",),
        dir_name="local-llm",
        kind="gguf",
        download_bytes=484215648,
        installed_bytes=484215648,
    ),
    ModelId.GEMMA_4_E2B_Q4: ModelSpec(
        id=ModelId.GEMMA_4_E2B_Q4.value,
        files=("gemma-4-E2B-it-Q4_K_M.gguf",),
        dir_name="local-llm",
        kind="gguf",
    ),
    ModelId.GEMMA_4_E2B_GOOGLE_QAT: ModelSpec(
        id=ModelId.GEMMA_4_E2B_GOOGLE_QAT.value,
        files=("gemma-4-E2B_q4_0-it.gguf",),
        dir_name="local-llm",
        kind="gguf",
        download_bytes=3349516256,
        installed_bytes=3349516256,
    ),
    # -------------------------------------------------------------------
    # Agent-speech TTS voices (kind="tts"). Supertonic and Kokoro install
    # their pinned Hugging Face sources. Legacy manifest rows are
    # INFORMATIONAL only -- download_archive() reads these concrete pins and
    # never resolves a quantization row.
    # Excluded from the STT-only sweep just like GGUF entries.
    # -------------------------------------------------------------------
    ModelId.SUPERTONIC_3_INT8: ModelSpec(
        id=ModelId.SUPERTONIC_3_INT8.value,
        files=(
            "duration_predictor.int8.onnx",
            "text_encoder.int8.onnx",
            "vector_estimator.int8.onnx",
            "vocoder.int8.onnx",
            "tts.json",
            "unicode_indexer.bin",
            "voice.bin",
        ),
        dir_name="supertonic-3-int8-2026-05-11",
        kind="tts",
        download_bytes=128_774_318,
        # Sum of every file extracted from the pinned archive, including its
        # LICENSE and README in addition to the seven runtime files.
        installed_bytes=145_316_356,
    ),
    ModelId.KOKORO_82M_FP32: ModelSpec(
        id=ModelId.KOKORO_82M_FP32.value,
        files=(
            'LICENSE',
            'README.md',
            'model.onnx',
            'voices.bin',
            'tokens.txt',
            'lexicon-us-en.txt',
            'espeak-ng-data/en_dict',
            'espeak-ng-data/phondata',
            'espeak-ng-data/phonindex',
            'espeak-ng-data/phontab',
            'espeak-ng-data/intonations',
            'espeak-ng-data/lang/gmw/en',
            'espeak-ng-data/lang/gmw/en-US',
        ),
        dir_name="kokoro-82m-fp32-en-us-sherpa-v1_0",
        kind="tts",
        download_bytes=360545851,
        installed_bytes=360545851,
    ),
}


# ---------------------------------------------------------------------------
# Local-LLM download AVAILABILITY (hide-until-published gate)
# ---------------------------------------------------------------------------
#
# The local model picker must only SURFACE a GGUF the user can actually
# download today, determined locally from the packaged source pins.


def local_gguf_available(model_id: str, *, installed: bool = False) -> bool:
    if installed:
        return True
    try:
        spec = DOWNLOAD_REGISTRY[ModelId(model_id)]
    except (ValueError, KeyError):
        return False
    if spec.kind != "gguf":
        return False
    return model_id in MODEL_SOURCES


def model_dir(model: ModelId) -> Path:
    """Return the on-disk directory where *model*'s files live.

    The single destination resolver for every STT model:
    ``get_models_root() / DOWNLOAD_REGISTRY[model].dir_name``. Absorbs the old
    per-family resolvers (``ConfigManager.model_dir`` for Parakeet,
    ``get_qwen_model_dir`` for Qwen) so destination, presence, download, and
    delete all read one spec. Parakeet dir_names match the historical paths
    (no re-download); Qwen now lives under ``models/`` alongside them.

    ``get_models_root`` is imported lazily so importing this module never pulls
    in ``config_manager`` at module top (avoids an import cycle).
    """
    from .config_manager import get_models_root

    return get_models_root() / DOWNLOAD_REGISTRY[model].dir_name


def is_present(model: ModelId) -> bool:
    """Return ``True`` when every file *model* needs is on disk and non-empty.

    The single presence predicate for every STT model. Delegates to
    :func:`_compute_pending` — the only zero-byte-aware check — so presence and
    the download gate agree by construction: a file this reports present is a
    file :func:`download` would skip, and a truncated (zero-byte) file reads as
    NOT present (it would fail to load and must be re-fetched). Collapses the
    old forked presence helpers (``setup._model_ready``,
    ``stt_factory.qwen_model_present``) which used bare ``.exists()`` and so
    could call a zero-byte file "ready".
    """
    spec = DOWNLOAD_REGISTRY[model]
    return not _compute_pending(model_dir(model), spec.files)


def model_size_bytes(model: ModelId, *, root: Path | None = None) -> int:
    """Return the logical bytes currently stored under *model*'s directory.

    This deliberately measures every regular file, not only required files, so
    interrupted downloads and other incomplete bundles still contribute to the
    reclaimable-storage total. Directory entries can disappear during a status
    read; those races and unreadable entries are skipped so this read-only UI
    probe never prevents the rest of the inventory from being reported.

    The root may itself be a Windows junction used by development fixtures, so
    opening the root follows it. Nested symlinks/reparse points are not followed
    and therefore cannot make the scan escape or loop.
    """
    root = root if root is not None else model_dir(model)
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                except OSError:
                    continue
    return total


def _coerce_stt_model_id(value: ModelId | str | None) -> ModelId | None:
    """Normalize one external exact id, rejecting non-STT registry rows."""
    if value is None:
        return None
    try:
        model = value if isinstance(value, ModelId) else ModelId(value)
    except (TypeError, ValueError):
        return None
    spec = DOWNLOAD_REGISTRY.get(model)
    return model if spec is not None and spec.kind == "stt" else None


def stt_artifact_inventory(
    *,
    configured_artifact_id: ModelId | str,
    loaded_artifact_id: ModelId | str | None,
) -> dict[str, object]:
    """Report authoritative exact-artifact readiness and storage usage.

    Only stable registry IDs cross this boundary. ``active`` means the exact
    artifact currently owned by the live recognizer, while
    ``configured_artifact_id`` can differ during a prepare/activation
    transition. Reclaimable bytes exclude both identities so storage UI and
    later deletion APIs never offer either side of an in-flight swap.
    """
    configured = _coerce_stt_model_id(configured_artifact_id)
    if configured is None:
        raise ValueError("configured_artifact_id must identify a registered STT model")
    loaded = _coerce_stt_model_id(loaded_artifact_id)
    if loaded_artifact_id is not None and loaded is None:
        raise ValueError("loaded_artifact_id must identify a registered STT model")

    protected = {configured}
    if loaded is not None:
        protected.add(loaded)

    artifacts: list[dict[str, object]] = []
    unused_size_bytes = 0
    for model, spec in DOWNLOAD_REGISTRY.items():
        if spec.kind != "stt":
            continue
        if spec.backend not in {"parakeet", "qwen", "cohere"}:
            raise RuntimeError(f"STT model {model.value!r} has no registered backend")
        size_bytes = model_size_bytes(model)
        artifacts.append(
            {
                "id": model.value,
                "backend": spec.backend,
                "downloaded": is_present(model),
                "active": model == loaded,
                "size_bytes": size_bytes,
            }
        )
        if model not in protected:
            unused_size_bytes += size_bytes

    return {
        "configured_artifact_id": configured.value,
        "loaded_artifact_id": loaded.value if loaded is not None else None,
        "artifacts": artifacts,
        "unused_size_bytes": unused_size_bytes,
    }


def delete_model(model: ModelId) -> None:
    """Delete *model*'s on-disk directory (``model_dir(model)``).

    The single STT delete path: ``shutil.rmtree`` of the registry-resolved
    destination. **Idempotent** — a missing directory is a no-op (return,
    don't raise), matching the ``/api/stt/delete`` precedent of deleting only
    when the dir exists. ``ignore_errors=False`` is deliberate: a real failure
    (e.g. a locked file under the dir) still raises ``OSError`` so the endpoint
    can surface it as a 500 instead of silently swallowing a half-deleted
    bundle. STT only — GGUF files are unlinked by their own delete path.
    """
    exact = _coerce_stt_model_id(model)
    if exact is None:
        raise ValueError("model must identify a registered STT model")
    target = model_dir(exact)
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=False)


def delete_unused_stt_models(
    protected: Iterable[ModelId | str],
) -> list[ModelId]:
    """Delete every exact STT artifact except the protected identities.

    Both the configured target and the exact artifact owned by the live
    recognizer belong in ``protected`` during a transition. Values must be
    stable registry IDs; paths and non-STT registry rows are rejected. Missing
    directories are naturally idempotent, while incomplete directories are
    removed because they still consume storage.

    The registry ``kind`` filter is the boundary that keeps local enhancement
    GGUFs and Agent Speech TTS voices out of this operation.
    """
    protected_ids: set[ModelId] = set()
    for value in protected:
        model = _coerce_stt_model_id(value)
        if model is None:
            raise ValueError("protected values must identify registered STT models")
        protected_ids.add(model)

    removed: list[ModelId] = []
    for model, spec in DOWNLOAD_REGISTRY.items():
        if spec.kind != "stt" or model in protected_ids:
            continue
        if model_dir(model).exists():
            delete_model(model)
            removed.append(model)
    return removed


def model_id_for_parakeet(model_name: str) -> ModelId:
    """Map a ``config.parakeet_model`` name to its ``ModelId`` (v2 vs v3).

    ``"nemo-parakeet-tdt-0.6b-v2"`` -> ``PARAKEET_V2_INT8``; anything else
    (the default ``...-v3`` and unknown names) -> ``PARAKEET_V3_INT8``.
    """
    return (
        ModelId.PARAKEET_V2_INT8
        if "v2" in model_name.lower()
        else ModelId.PARAKEET_V3_INT8
    )


def _compute_pending(dest_dir: Path, files: tuple[str, ...]) -> list[str]:
    """Return the files that still need downloading (missing or zero-byte).

    Per-file idempotency: a present, non-empty file is skipped so a partial
    download resumes against whichever source we try next.
    """
    logger = logging.getLogger(__name__)
    pending: list[str] = []
    for rel_path in files:
        dest = dest_dir / rel_path
        if dest.is_file() and dest.stat().st_size > 0:
            logger.debug("model download skip reason=already_present file=%s", rel_path)
            continue
        pending.append(rel_path)
    return pending


def _check_model_available(model: ModelId) -> None:
    if model == ModelId.KOKORO_82M_FP32:
        from .speech.availability import require_engine_available
        require_engine_available("kokoro")
    if model not in DOWNLOAD_REGISTRY:
        raise ValueError("This model is not available in the supported catalog")


def _download_from_hf(spec, dest_dir, pending, progress_callback, cancel_event, logger):
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("download cancelled")
    source = MODEL_SOURCES[spec.id]
    files = source["files"]
    total = sum(files[name]["size"] for name in pending)
    completed = 0
    high_water = 0
    with new_http_client() as client:
        for name in pending:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            target = dest_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://huggingface.co/{source['repo']}/resolve/{source['revision']}/{name}"
            for attempt in range(2):
                transferred = 0
                def on_chunk(size):
                    nonlocal transferred, high_water
                    transferred += size
                    high_water = max(high_water, completed + transferred)
                    if progress_callback:
                        progress_callback(high_water, total)
                try:
                    download_file_with_sha256(
                        client, url, target, files[name]["sha256"], on_chunk,
                        cancel_event=cancel_event, expected_size=files[name]["size"],
                    )
                    break
                except DownloadCancelled:
                    raise
                except httpx.HTTPError as exc:
                    if attempt or not _source_unavailable(exc):
                        raise
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("download cancelled")
            completed += files[name]["size"]


def _source_unavailable(exc: Exception) -> bool:
    """Only network/source availability failures may change download source."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {401, 403, 404, 408, 410, 429} or (
            500 <= exc.response.status_code < 600
        )
    return isinstance(exc, httpx.TransportError)


def _extract_verified_stt_archive(archive_path, stage, source, cancel_event):
    """Verify every required member before allowing installation of any file.

    The caller has already checked the compressed archive's size and SHA256.
    No general-purpose extraction is used; only exact regular-file paths from
    the shipped file manifest are written into an isolated temporary directory.
    """
    archive = source["github"]
    files = source["files"]
    seen = set()
    verified = set()
    expanded = 0
    with tarfile.open(archive_path, "r|bz2") as tf:
        for count, member in enumerate(tf, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (
                count > archive["member_count"]
                or not name or "\\" in name or ":" in name
                or any(part in ("", ".", "..") for part in parts)
                or parts[0] != archive["directory"]
                or not (member.isfile() or member.isdir())
                or member.issparse()
                or name.casefold() in seen
            ):
                raise RuntimeError("Unsupported model archive entry")
            seen.add(name.casefold())
            expanded += member.size
            if member.size < 0 or expanded > archive["unpacked_size"]:
                raise RuntimeError("Model archive exceeds its pinned size")
            relative = "/".join(parts[1:])
            if relative not in files:
                continue
            pin = files[relative]
            if not member.isfile() or member.size != pin["size"]:
                raise RuntimeError("Model archive file does not match its pinned size")
            target = stage.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = tf.extractfile(member)
            if stream is None:
                raise RuntimeError("Model archive file is missing")
            digest = hashlib.sha256()
            with stream, target.open("wb") as output:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("download cancelled")
                    chunk = stream.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != pin["sha256"]:
                raise RuntimeError("Model archive file checksum mismatch")
            verified.add(relative)
    if verified != set(files):
        raise RuntimeError("Model archive is missing required files")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("download cancelled")


def _download_from_github(spec, dest_dir, pending, progress_callback, cancel_event):
    source = MODEL_SOURCES[spec.id]
    archive = source["github"]
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("download cancelled")
    # TemporaryDirectory owns only fresh scratch data, never the model cache.
    with tempfile.TemporaryDirectory(prefix=".model-download-", dir=dest_dir) as temp:
        scratch = Path(temp)
        downloaded = scratch / "model.tar.bz2"
        transferred = 0

        def on_chunk(size):
            nonlocal transferred
            transferred += size
            if progress_callback:
                progress_callback(transferred, archive["size"])

        with new_http_client(github_url=archive["url"]) as client:
            download_file_with_sha256(
                client, archive["url"], downloaded, archive["sha256"], on_chunk,
                cancel_event, expected_size=archive["size"],
            )
        stage = scratch / "files"
        _extract_verified_stt_archive(downloaded, stage, source, cancel_event)
        for name in pending:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            target = dest_dir / name
            # Preserve files completed by HF or supplied while the archive ran.
            if target.is_file() and target.stat().st_size > 0:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / name, target)


def _download_gemma(dest_dir, progress_callback, cancel_event):
    from . import gemma_artifacts as artifacts
    dest_dir.mkdir(parents=True, exist_ok=True)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("download cancelled")
    existing_id, existing_path = artifacts.resolve(dest_dir)
    if artifacts.present(existing_path):
        if existing_id == artifacts.GOOGLE_ID:
            pin = MODEL_SOURCES[existing_id]["files"][existing_path.name]
            digest = hashlib.sha256()
            with existing_path.open("rb") as stream:
                while chunk := stream.read(DOWNLOAD_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("download cancelled")
                    digest.update(chunk)
            if existing_path.stat().st_size != pin["size"] or digest.hexdigest() != pin["sha256"]:
                raise RuntimeError("Installed Gemma fallback failed integrity verification")
        return
    logger = logging.getLogger(__name__)
    for artifact_id in artifacts.ARTIFACT_IDS:
        spec = DOWNLOAD_REGISTRY[ModelId(artifact_id)]
        try:
            _download_from_hf(spec, dest_dir, list(spec.files), progress_callback, cancel_event, logger)
        except httpx.HTTPError as exc:
            if artifact_id != artifacts.PUBLIC_ID or not _source_unavailable(exc):
                raise
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            logger.info("model download fallback model=%s source=google reason=%s", artifacts.PUBLIC_ID, type(exc).__name__)
            continue
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("download cancelled")
        artifacts.select(dest_dir, artifact_id)
        return


def download(
    model: ModelId,
    dest_dir: Path,
    *,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download missing model files from their pinned Hugging Face source.

    Preserve nonempty existing files. New files are verified before replacement;
    cancellation is terminal. STT source failures may try a reviewed GitHub
    archive with the same file pins; integrity, policy and disk
    errors cannot.
    """
    if model == ModelId.GEMMA_4_E2B_Q4:
        try:
            _download_gemma(dest_dir, progress_callback, cancel_event)
        except DownloadCancelled:
            raise
        except Exception as exc:
            raise RuntimeError("Model download failed. Check your connection and retry, or install the verified model files locally.") from exc
        return
    _check_model_available(model)
    spec = DOWNLOAD_REGISTRY[model]
    if spec.kind == "tts":
        raise ValueError("Use download_archive for voice models")
    logger = logging.getLogger(__name__)

    dest_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create subdirectories (e.g. tokenizer/) so writes never hit a missing parent.
    for rel_path in spec.files:
        parent = (dest_dir / rel_path).parent
        if parent != dest_dir:
            parent.mkdir(parents=True, exist_ok=True)

    pending = _compute_pending(dest_dir, spec.files)
    if not pending:
        logger.debug("model download skip reason=all_present model=%s", spec.id)
        return

    if spec.id not in MODEL_SOURCES:
        raise ValueError(
            "This model has no public download. Restore its existing model file "
            "locally, or select an available model in AI Enhancement."
        )

    start_time = time.monotonic()

    # Wrap progress_callback with ~25%-interval DEBUG milestone logs (uniform
    # for every model).
    _last_pct_milestone = [0]
    last_downloaded = 0

    def _progress_with_milestones(downloaded: int, total: int | None) -> None:
        nonlocal last_downloaded
        last_downloaded = downloaded
        if total and total > 0:
            pct = int(downloaded * 100 / total)
            milestone = (pct // 25) * 25
            if milestone > _last_pct_milestone[0] and milestone <= 100:
                logger.debug(
                    "model download progress pct=%d bytes=%d", milestone, downloaded
                )
                _last_pct_milestone[0] = milestone
        if progress_callback is not None:
            progress_callback(downloaded, total)

    actual_source = "huggingface"
    logger.info("model download start model=%s source=%s", spec.id, actual_source)
    try:
        try:
            _download_from_hf(
                spec, dest_dir, pending, _progress_with_milestones, cancel_event, logger
            )
        except httpx.HTTPError as exc:
            if not _source_unavailable(exc) or not MODEL_SOURCES[spec.id].get("github"):
                raise
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("download cancelled")
            actual_source = "github"
            logger.info("model download fallback model=%s source=github reason=%s", spec.id, type(exc).__name__)
            offset = last_downloaded

            def fallback_progress(downloaded, total):
                _progress_with_milestones(offset + downloaded, offset + total)

            _download_from_github(
                spec, dest_dir, _compute_pending(dest_dir, spec.files),
                fallback_progress, cancel_event,
            )
    except DownloadCancelled:
        raise
    except Exception as exc:
        logger.error("model download failed model=%s reason=%s", spec.id, type(exc).__name__)
        raise RuntimeError("Model download failed. Check your connection and retry, or install the verified model files locally.") from exc

    duration = time.monotonic() - start_time
    try:
        total_bytes = sum(
            f.stat().st_size for f in dest_dir.rglob("*") if f.is_file()
        )
    except OSError:
        total_bytes = 0
    logger.info(
        "model download complete model=%s source=%s duration=%.2fs bytes=%d",
        spec.id, actual_source, duration, total_bytes,
    )

    if spec.post_download is not None:
        spec.post_download(dest_dir)


# ---------------------------------------------------------------------------
# Archive-model delivery (kind="tts" — the agent-speech voices). This remains a
# separate entry point from :func:`download`: one verified archive rather than
# a per-file source entry.
# ---------------------------------------------------------------------------


def _extract_archive_flattened(archive_path: Path, dest_dir: Path) -> None:
    """Extract *archive_path* (a ``.tar.bz2``) into *dest_dir*, flattening the
    archive's single top-level directory (mirrors ``tts_bench.py``'s
    ``_safe_extract`` plus flattening).

    Every member's resolved path is verified to stay inside the resolved
    *dest_dir* before it is written -- a member using ``..`` to escape the
    top-level dir (or any other path-traversal attempt) raises ``RuntimeError``
    instead of writing outside *dest_dir*. Only regular files and directories
    are extracted; symlinks/devices/etc. are skipped (none are expected in the
    voice bundle, and extracting one could itself escape *dest_dir*).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with tarfile.open(archive_path, "r:bz2") as tf:
        members = tf.getmembers()
        roots = {m.name.split("/", 1)[0] for m in members if m.name}
        if len(roots) != 1:
            raise RuntimeError(
                f"expected a single top-level dir in {archive_path.name}, "
                f"found {sorted(roots)}"
            )
        root = next(iter(roots))
        prefix = root + "/"

        for member in members:
            name = member.name
            if name == root:
                continue  # the top-level dir entry itself -- nothing to write
            if not name.startswith(prefix):
                raise RuntimeError(f"unexpected top-level entry in archive: {name}")
            rel = name[len(prefix):]
            if not rel:
                continue
            target = (resolved_dest / rel).resolve()
            if target != resolved_dest and not str(target).startswith(
                str(resolved_dest) + os.sep
            ):
                raise RuntimeError(f"unsafe path in archive: {name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted, open(target, "wb") as out:
                    shutil.copyfileobj(extracted, out)
            # Symlinks and other special member types are silently skipped --
            # none are expected in this bundle.


def download_archive(
    model: ModelId,
    *,
    progress_callback: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Stage a complete verified voice bundle, then install it locally.

    Both current voices use pinned upstream files.
    Download, extraction and cancellation failures preserve the existing bundle.
    """
    from .config_manager import get_models_root

    _check_model_available(model)
    spec = DOWNLOAD_REGISTRY[model]
    source = MODEL_SOURCES[spec.id]
    if spec.kind != "tts":
        raise ValueError("Voice download requires a speech model")

    logger = logging.getLogger(__name__)

    if is_present(model):
        logger.debug("model download skip reason=all_present model=%s", spec.id)
        return

    models_root = get_models_root()
    models_root.mkdir(parents=True, exist_ok=True)
    dest_dir = model_dir(model)
    archive_path = models_root / f"{spec.dir_name}.tar.bz2"
    extract_tmp = models_root / f"{spec.dir_name}.extract.tmp"
    # Clear any scratch dir left behind by a prior crash before we start.
    shutil.rmtree(extract_tmp, ignore_errors=True)

    try:
        actual_source = "huggingface"
        logger.info("model download start model=%s source=%s", spec.id, actual_source)
        extract_tmp.mkdir(parents=True, exist_ok=True)
        if source.get("archive"):
            archive_name = source["archive"]
            # The same verified file primitive downloads the pinned archive.
            _download_from_hf(spec, models_root, [archive_name], progress_callback, cancel_event, logger)
            downloaded_archive = models_root / archive_name
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("download cancelled before extraction")
                _extract_archive_flattened(downloaded_archive, extract_tmp)
            finally:
                downloaded_archive.unlink(missing_ok=True)
        else:
            _download_from_hf(spec, extract_tmp, list(source["files"]), progress_callback, cancel_event, logger)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("download cancelled before installation")
        if _compute_pending(extract_tmp, spec.files):
            raise RuntimeError("Voice model is incomplete")

        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        os.replace(extract_tmp, dest_dir)
    except BaseException:
        shutil.rmtree(extract_tmp, ignore_errors=True)
        raise
    finally:
        if archive_path.exists():
            archive_path.unlink()

    logger.info("model download complete model=%s source=%s", spec.id, actual_source)
