"""Nuitka entry point with a private supervised STT worker dispatch."""
from __future__ import annotations

import io
import sys


def _run() -> int | None:
    # Dispatch before importing the long-lived backend. The frozen onefile
    # engine therefore enters exactly the same worker module as source mode,
    # and the private byte protocol retains exclusive ownership of stdout.
    if sys.argv[1:2] == ["--macos-child-guardian"]:
        from whisperstream.macos_child_process import guardian_main

        return guardian_main(sys.argv[2:])

    if sys.argv[1:] == ["--kokoro-worker"]:
        from whisperstream.speech.kokoro_worker import main as kokoro_worker_main

        return kokoro_worker_main()

    if sys.argv[1:] == ["--stt-worker"]:
        from whisperstream.stt_worker import main as worker_main

        return worker_main()

    if sys.argv[1:2] == ["--macos-keychain-probe"]:
        from whisperstream.macos_keychain_probe import main as keychain_probe_main

        return keychain_probe_main(sys.argv[2:])

    if sys.argv[1:2] == ["--macos-protected-storage-probe"]:
        from whisperstream.macos_protected_storage_probe import (
            main as protected_storage_probe_main,
        )

        return protected_storage_probe_main(sys.argv[2:])

    if sys.argv[1:2] == ["--macos-phase4-probe"]:
        # Internal packaged-verification probe. Source checkouts do not ship
        # it, so its absence is reported instead of raising a traceback.
        try:
            from whisperstream.macos_phase4_probe import main as phase4_probe_main
        except ModuleNotFoundError as exc:
            if exc.name != "whisperstream.macos_phase4_probe":
                raise
            print("This build does not include the macOS verification probe.", file=sys.stderr)
            return 2

        return phase4_probe_main(sys.argv[2:])

    if sys.argv[1:2] == ["--windows-protected-storage-probe"]:
        from whisperstream.windows_protected_storage_probe import (
            main as protected_storage_probe_main,
        )

        return protected_storage_probe_main(sys.argv[2:])

    # Force UTF-8 on stdout/stderr so Rich's Unicode spinners don't crash on
    # Windows cp1252 when launched as a Tauri sidecar (no proper console).
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

    from whisperstream.main import main

    main()
    return None


if __name__ == "__main__":
    raise SystemExit(_run())
