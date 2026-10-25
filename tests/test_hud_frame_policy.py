"""Guard the native-frame invariant from HUD rendering Incident 21.

This is a source tripwire, not compositor proof. A shaped panel and ordinary
recording reuse one HWND: no owner may disable DWM frame rendering and leave
Tao's restored caption styles to fall back to a classic Windows frame.
"""

from pathlib import Path
import re


def test_native_hud_never_disables_dwm_frame_rendering() -> None:
    native_root = Path(__file__).resolve().parents[1] / "src-tauri" / "src"
    for path in native_root.rglob("*.rs"):
        source = path.read_text(encoding="utf-8")
        # Keep forensic explanations in comments, but reject a runtime import
        # or use of the policy that produced the blue-bar/black-window flash.
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)
        assert not re.search(r"\bDWMNCRP_DISABLED\b", source), (
            f"{path.name} disables DWM frame rendering; see HUD Incident 21"
        )
