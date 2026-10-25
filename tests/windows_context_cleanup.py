"""Content-free post-check for Unit 08 owned state and privacy boundaries."""

from __future__ import annotations

import json
from pathlib import Path
import platform
import tempfile

from whisperstream.app_context_contract import (
    ApplicationRef,
    BrowserPageRef,
    ContextRequirement,
    ContextSnapshot,
    ContextStatus,
)


def main() -> int:
    machine = platform.machine().lower()
    if platform.system() != "Windows" or machine not in {"amd64", "x86_64"}:
        raise RuntimeError("native Windows x64 is required")

    temp_root = Path(tempfile.gettempdir())
    temporary_roots = [
        path
        for path in temp_root.glob("whisperstream-unit08-*")
        if path.is_dir()
    ]
    if temporary_roots:
        raise AssertionError("owned Unit 08 temporary roots remain")

    application = ApplicationRef("synthetic.exe", "opaque-instance")
    context = ContextSnapshot(
        1,
        ContextRequirement.APPLICATION_AND_BROWSER_PAGE,
        ContextStatus.RESOLVED,
        application,
        BrowserPageRef(application, "synthetic.example"),
    )
    representations = repr(context)
    for forbidden in ("synthetic.exe", "synthetic.example", "opaque-instance"):
        if forbidden in representations:
            raise AssertionError("opaque contract identity leaked through repr")

    print(
        json.dumps(
            {
                "platform": "Windows-x64",
                "temporary_roots_remaining": 0,
                "opaque_repr_leaks": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
