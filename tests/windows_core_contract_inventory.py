#!/usr/bin/env python3
"""Print a content-free native Windows capture/input-stack inventory."""

from __future__ import annotations

import importlib.metadata
import json
import platform


def main() -> int:
    if platform.system() != "Windows":
        print(json.dumps({"verdict": "FAIL", "reason": "native-windows-required"}))
        return 1

    import sounddevice as sd

    devices = list(sd.query_devices())
    host_apis = list(sd.query_hostapis())
    default_input = sd.default.device[0]
    portaudio_version, portaudio_text = sd.get_portaudio_version()
    payload = {
        "verdict": "PASS",
        "platform": platform.platform(),
        "packages": {
            "sounddevice": importlib.metadata.version("sounddevice"),
            "pynput": importlib.metadata.version("pynput"),
        },
        "portaudio": {
            "version": portaudio_version,
            "description": portaudio_text,
        },
        "device_count": len(devices),
        "input_device_count": sum(
            1 for item in devices if int(item["max_input_channels"]) > 0
        ),
        "host_api_count": len(host_apis),
        "host_api_types": sorted({str(item["name"]) for item in host_apis}),
        "default_input_present": (
            isinstance(default_input, int) and default_input >= 0
        ),
        "device_names_recorded": False,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
