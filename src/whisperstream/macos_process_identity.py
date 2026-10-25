"""Private Darwin process-instance identity for delivery and pin validation."""

from __future__ import annotations

import math

import psutil


def process_launch_token(pid: int) -> int | None:
    """Read a fresh kernel start time without collecting names or paths.

    AppKit's launchDate can be nil for a running foreground application. The
    kernel creation time still distinguishes PID reuse. Never cache Process
    objects here: every delivery validation must observe the current instance.
    """
    if pid <= 0:
        return None
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        if not math.isfinite(created) or created <= 0 or not process.is_running():
            return None
        return round(created * 1_000_000)
    except (psutil.Error, OSError, ValueError):
        return None
