"""Load the optional official Windows payload-signing provider."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def require_provider():
    value = os.environ.get("WHISPERSTREAM_WINDOWS_SIGNING_PROVIDER")
    if not value:
        raise RuntimeError("Official Windows builds require a payload-signing provider")
    path = Path(value).resolve()
    spec = importlib.util.spec_from_file_location("windows_payload_signing_provider", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Windows signing provider: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
