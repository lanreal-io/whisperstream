"""Bundled and development local enhancement model catalog."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


# ---------------------------------------------------------------------------
# Local bundled model catalog (llama.cpp / GGUF, downloaded on first use)
# ---------------------------------------------------------------------------

# The production lineup: Stream-1 Mini (default) and Customizable (Gemma).
LOCAL_BUNDLED_MODELS: list[dict] = [
    {
        # First-party default (Balanced seed 23). Fixed Chat/Email prompts; the
        # auto speed check measures only this model.
        "id": "stream-1-mini",
        "label": "Stream-1 Mini",
        "disk_mb": 462,
        "ram_mb": 1024,
        "min_system_ram_mb": 4096,
        "recommended": True,
        "auto_recommend": True,
        "gguf_repo": "whisperstream/stream-1-mini",
        "gguf_filename": "stream-1-mini-vocabulary-q4_k_m.gguf",
        "prompt_key": "stream-mini-vocabulary",
        "serving_contract": "wrap-v2",
        "trained_modes": ["chat", "email"],
    },
    {
        "id": "gemma4-e2b-instruct",
        "label": "Gemma 4 E2B Instruct",
        "disk_mb": 3195,
        "ram_mb": 2200,
        # LowRamDialog threshold: warn when the user's total system RAM is
        # below this. Decoupled from ``ram_mb`` (the model's runtime cost)
        # so the dialog fires on small machines even though the model itself
        # uses a conservative resident estimate for either artifact. 8 GB is the practical floor for a
        # comfortable cleanup experience alongside the user's other apps.
        "min_system_ram_mb": 8192,
        "recommended": False,
        # Customizable/opt-in general model: never the benchmark ladder's
        # auto-recommendation (Stream-1 Mini is).
        "auto_recommend": False,
        "gguf_repo": "unsloth/gemma-4-E2B-it-GGUF",
        "gguf_filename": "gemma-4-E2B-it-Q4_K_M.gguf",
        "prompt_key": "gemma",
    },
]


def get_local_bundled_model(model_id: str) -> dict | None:
    """Return the ``LOCAL_BUNDLED_MODELS`` entry for *model_id*, or ``None`` if unknown."""
    for entry in LOCAL_BUNDLED_MODELS:
        if entry["id"] == model_id:
            return entry
    return None


def get_local_model_catalog(*, active_id: str | None = None) -> list[dict]:
    """Return the production catalog merged with the enabled dev overlay.

    ``LOCAL_BUNDLED_MODELS`` remains the immutable production/download
    contract. This accessor returns fresh row dictionaries so runtime metadata
    can be attached without mutating that contract. Candidate loading is lazy,
    compile-time development gated, and fail-closed in the optional
    development candidate module, so a missing module or a missing or invalid
    overlay simply yields the original production rows.

    The private ``_artifact_path`` value on candidate rows is an internal seam
    for :func:`resolve_local_model_path`; API callers must not serialize it.
    """
    active_key = active_id.casefold() if isinstance(active_id, str) else None
    production_ids = tuple(entry["id"] for entry in LOCAL_BUNDLED_MODELS)

    catalog: list[dict] = []
    for production_entry in LOCAL_BUNDLED_MODELS:
        row = dict(production_entry)
        row.update(
            {
                "source": "production",
                "experiment_id": None,
                "parameter_size": None,
                "role": None,
                "status": None,
                "serving_contract": production_entry.get("serving_contract"),
                "trained_modes": production_entry.get("trained_modes"),
                "downloadable": True,
            }
        )
        catalog.append(row)

    for candidate, record in _load_dev_candidates(production_ids):
        catalog.append(
            {
                "id": candidate.id,
                "label": candidate.label,
                # Keep the shared local-model row shape backward compatible.
                # Candidate artifacts are developer supplied, so managed-cache
                # size requirements do not apply.
                "disk_mb": 0,
                "ram_mb": 0,
                "min_system_ram_mb": 0,
                "gguf_filename": candidate.artifact_filename,
                "prompt_key": candidate.prompt_key,
                "source": "candidate",
                "experiment_id": candidate.id,
                "parameter_size": candidate.parameter_size,
                "role": candidate.role,
                "status": candidate.status,
                "serving_contract": record.serving_contract.value,
                "trained_modes": [mode.value for mode in record.trained_modes],
                "downloadable": False,
                "_artifact_path": candidate.artifact_path,
            }
        )

    for row in catalog:
        artifact_path = _resolve_local_model_entry_path(row)
        artifact_available = _nonempty_file(artifact_path)
        row["artifact_available"] = artifact_available
        row["selectable"] = (
            artifact_available if row["source"] == "candidate" else True
        )
        row["active"] = (
            active_key is not None and str(row["id"]).casefold() == active_key
        )

    return catalog


def _load_dev_candidates(production_ids: tuple[str, ...]) -> list[tuple]:
    """Return enabled development candidates with their serving records.

    :mod:`dev_candidate_models` is development tooling that source releases do
    not ship, so its absence means there are no candidates.
    """
    try:
        from .dev_candidate_models import candidate_record, load_dev_candidate_models
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.dev_candidate_models":
            raise
        return []
    return [
        (candidate, candidate_record(candidate))
        for candidate in load_dev_candidate_models(production_ids=production_ids)
    ]


def get_local_model(model_id: str) -> dict | None:
    """Return a production or enabled candidate row by ID.

    Lookup is case-insensitive because registry validation reserves IDs using
    the same comparison and persisted config historically normalizes model IDs.
    The returned row retains the registry's canonical exact ID.
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    model_key = model_id.casefold()
    return next(
        (
            entry
            for entry in get_local_model_catalog()
            if str(entry["id"]).casefold() == model_key
        ),
        None,
    )


def resolve_local_model_path(
    model: str | Mapping[str, object],
) -> Path | None:
    """Resolve one merged-catalog model to its canonical GGUF path.

    Production rows live in Whisperstream's managed local-LLM cache. Candidate
    rows retain the validated artifact path resolved by the development
    registry parser. Unknown IDs and incomplete row mappings return ``None``.
    """
    entry = get_local_model(model) if isinstance(model, str) else model
    if entry is None:
        return None
    return _resolve_local_model_entry_path(entry)


def _resolve_local_model_entry_path(entry: Mapping[str, object]) -> Path | None:
    source = entry.get("source")
    if source == "candidate":
        candidate_path = entry.get("_artifact_path")
        return candidate_path if isinstance(candidate_path, Path) else None
    # Raw LOCAL_BUNDLED_MODELS entries predate source metadata. Treat an
    # absent source as production so production-only downloader call sites can
    # use this resolver without changing their catalog-membership boundary.
    if source in (None, "production"):
        filename = entry.get("gguf_filename")
        if not isinstance(filename, str) or not filename:
            return None
        from .config_manager import get_local_llm_dir

        if entry.get("id") == "gemma4-e2b-instruct":
            from .gemma_artifacts import resolve
            return resolve(get_local_llm_dir())[1]
        return get_local_llm_dir() / filename
    return None


def _nonempty_file(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


