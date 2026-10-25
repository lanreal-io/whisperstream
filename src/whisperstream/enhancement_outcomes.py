"""Immutable enhancement outcomes and pure transcript provenance construction.

These values carry transcript text/profile identity, never diagnostic details.
They do not select profiles, execute providers, grant permission or write history.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

EnhancementOutcome = Literal["success", "not_attempted", "timeout", "error"]


@dataclass(frozen=True, slots=True)
class EnhancementResult:
    """Transcript text plus one bounded terminal enhancement outcome.

    The closed outcome deliberately carries no provider response, exception,
    endpoint, model, or other diagnostic detail. Callers that persist history
    can therefore record truthful provenance without risking raw error text.
    """

    text: str
    outcome: EnhancementOutcome


@dataclass(frozen=True, slots=True)
class EnhancementProfileSnapshot:
    """Effective enhancement profile identity frozen before provider work."""

    profile_id: str
    profile_name: str


@dataclass(frozen=True, slots=True)
class TranscriptEnhancementProvenance:
    """Bounded immutable outcome, independent of the encrypted store schema."""

    profile_id: str
    profile_name: str
    outcome: Literal["success", "timeout", "error"]


def aggregate_enhancement_outcomes(
    outcomes: Sequence[EnhancementOutcome],
) -> EnhancementOutcome:
    """Reduce chunk outcomes with generic errors taking precedence."""

    if "error" in outcomes:
        return "error"
    if "timeout" in outcomes:
        return "timeout"
    if "success" in outcomes:
        return "success"
    return "not_attempted"


def enhancement_provenance(
    profile: EnhancementProfileSnapshot | None,
    outcome: EnhancementOutcome,
) -> TranscriptEnhancementProvenance | None:
    """Build provenance only for a real success or attempted fallback."""

    if profile is None or outcome == "not_attempted":
        return None
    return TranscriptEnhancementProvenance(
        profile_id=profile.profile_id,
        profile_name=profile.profile_name,
        outcome=outcome,
    )
