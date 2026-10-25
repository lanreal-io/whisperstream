"""Mac input-selection persistence reconciliation.

The capture adapter owns runtime fallback.  This module owns only the persisted
identity/label pair used by settings surfaces, so fallback can never be mistaken
for migration evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_MACOS_INPUT_ID_PREFIX = "macos-input-"


@dataclass(frozen=True, slots=True)
class ReconciledInputSelection:
    value: str
    label: str


def reconcile_macos_input_selection(
    value: str,
    label: str,
    devices: Sequence[Mapping[str, Any]],
) -> ReconciledInputSelection:
    """Return the stable Mac identity and presentation-only saved label.

    A legacy name becomes an opaque ID only when exactly one current device has
    that exact display name.  Missing and duplicate names stay legacy values so
    the capture adapter retains its established fallback behavior.
    """

    selected = value if isinstance(value, str) else ""
    saved_label = label if isinstance(label, str) else ""
    if not selected:
        return ReconciledInputSelection(value="", label="")

    current_by_id = [device for device in devices if device.get("id") == selected]
    if len(current_by_id) == 1:
        current_label = current_by_id[0].get("name")
        return ReconciledInputSelection(
            value=selected,
            label=current_label if isinstance(current_label, str) else saved_label,
        )

    # An unmatched opaque value is a stale saved identity, not a legacy name.
    # Preserve both values so the UI can truthfully show the saved label while
    # runtime capture retains selected-to-default fallback.
    if selected.startswith(_MACOS_INPUT_ID_PREFIX):
        return ReconciledInputSelection(value=selected, label=saved_label)

    exact_name_matches = [
        device
        for device in devices
        if isinstance(device.get("name"), str) and device["name"] == selected
    ]
    if len(exact_name_matches) == 1:
        logical_id = exact_name_matches[0].get("id")
        if isinstance(logical_id, str) and logical_id:
            return ReconciledInputSelection(value=logical_id, label=selected)

    # Keep the name as the runtime compatibility value.  Copy it into the
    # presentation field so no frontend ever needs to render an unknown value
    # that might instead be an opaque ID.
    return ReconciledInputSelection(value=selected, label=saved_label or selected)


def macos_input_selection_available(
    value: str,
    devices: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether the persisted selection names one exact current logical ID."""

    return not value or sum(device.get("id") == value for device in devices) == 1


__all__ = [
    "ReconciledInputSelection",
    "macos_input_selection_available",
    "reconcile_macos_input_selection",
]
