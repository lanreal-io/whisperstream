"""Lazy keyed constructors for concrete speech engines.

Importing this module remains cheap. Each engine implementation is imported
only after its validated key is requested.
"""

from __future__ import annotations

import logging
from typing import Optional

from .engine_specs import DEFAULT_TTS_THREADS, get_engine_spec

__all__ = ["make_engine"]

def make_engine(
    engine_key: str = "supertonic",
    *,
    logger: Optional[logging.Logger] = None,
    num_threads: int = DEFAULT_TTS_THREADS,
):
    """Construct one unloaded engine without loading model files.

    Omitting *engine_key* preserves the original Supertonic-only factory
    contract for existing controller callers.
    """

    spec = get_engine_spec(engine_key)
    if spec.key == "supertonic":
        from .supertonic_engine import SupertonicEngine

        return SupertonicEngine(logger=logger, num_threads=num_threads)
    if spec.key == "kokoro":
        from .kokoro_engine import KokoroEngine

        return KokoroEngine(logger=logger, num_threads=num_threads)
    raise AssertionError(f"unhandled registered speech engine: {spec.key}")
