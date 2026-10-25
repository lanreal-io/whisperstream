"""Content-free native Windows probe for the Phase 4 contract inventory."""

from __future__ import annotations

import json
import platform

from whisperstream.audio_policy_contract import AUDIO_POLICY_SCHEMA_VERSION
from whisperstream.daily_use_capabilities import create_daily_use_capability_report
from whisperstream.enhancement_provider_contract import ENHANCEMENT_PROVIDER_SCHEMA_VERSION
from whisperstream.history_security_contract import HISTORY_SECURITY_SCHEMA_VERSION
from whisperstream.model_lifecycle_contract import MODEL_LIFECYCLE_SCHEMA_VERSION
from whisperstream.window_lifecycle_contract import WINDOW_LIFECYCLE_SCHEMA_VERSION


def main() -> int:
    if platform.system() != "Windows" or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise SystemExit("native Windows AMD64/x86_64 is required")

    report = create_daily_use_capability_report("Windows")
    payload = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "schemas": {
            "audio_policy": AUDIO_POLICY_SCHEMA_VERSION,
            "capabilities": report.schema_version,
            "enhancement_provider": ENHANCEMENT_PROVIDER_SCHEMA_VERSION,
            "history_security": HISTORY_SECURITY_SCHEMA_VERSION,
            "model_lifecycle": MODEL_LIFECYCLE_SCHEMA_VERSION,
            "window_lifecycle": WINDOW_LIFECYCLE_SCHEMA_VERSION,
        },
        "capability_states": {
            item.capability_id: item.state.value for item in report.capabilities
        },
        "content_accessed": False,
        "native_state_mutated": False,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
