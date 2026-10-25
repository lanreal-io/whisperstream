"""Content-free teardown proof for an isolated local-model browser run."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", required=True)
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise SystemExit("native Windows AMD64/x86_64 is required")
    ports = tuple(int(value) for value in args.ports.split(",") if value)
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise SystemExit("one or more valid ports are required")

    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.25)
            if client.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    temp_roots = tuple(
        Path(tempfile.gettempdir()).glob("whisperstream-candidate-transcript-e2e-*")
    )
    print(
        json.dumps(
            {
                "platform": platform.system(),
                "machine": platform.machine(),
                "checked_port_count": len(ports),
                "listening_ports": listening,
                "candidate_temp_roots_remaining": len(temp_roots),
                "private_content_emitted": False,
            },
            sort_keys=True,
        )
    )
    return 0 if not listening and not temp_roots else 1


if __name__ == "__main__":
    raise SystemExit(main())
