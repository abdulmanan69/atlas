"""Simulated calls for dry runs.

No phone call is placed. A synthetic, clearly labelled result is produced so the
whole pipeline (campaign → call record → outcome → CRM timeline → analytics) can
be exercised safely. Used whenever live calls are disabled or a campaign runs in
dry-run mode.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

SIMULATED_MARKER = "simulated"

# Plausible distribution for a lead-qualification campaign.
_OUTCOME_WEIGHTS: list[tuple[str, int]] = [
    ("qualified", 25),
    ("callback_requested", 15),
    ("not_interested", 25),
    ("voicemail_detected", 20),
    ("wrong_number", 5),
    ("no_answer", 10),
]


@dataclass(slots=True)
class SimulatedResult:
    answered: bool
    outcome: str | None
    duration_seconds: int
    captured: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    opted_out: bool = False


def simulate(*, seed: str, first_name: str, available_outcomes: list[str]) -> SimulatedResult:
    """Deterministic for a given seed so repeated dry runs are reproducible."""
    rng = random.Random(seed)  # noqa: S311 - not security related
    choices = [(o, w) for o, w in _OUTCOME_WEIGHTS if o == "no_answer" or o in available_outcomes]
    if not choices:
        choices = [("no_answer", 1)]
    outcome = rng.choices([c for c, _ in choices], weights=[w for _, w in choices])[0]
    if outcome == "no_answer":
        return SimulatedResult(answered=False, outcome=None, duration_seconds=0)
    duration = rng.randint(35, 240)
    transcript = (
        f"[{SIMULATED_MARKER.upper()} CALL — no audio was exchanged]\n"
        f"Agent: Hi {first_name}, this is a simulated conversation.\n"
        f"{first_name}: (synthetic reply)\n"
        f"Agent: Thanks, recording outcome '{outcome}'."
    )
    captured: dict[str, Any] = {"note": "synthetic values from a dry run"}
    return SimulatedResult(
        answered=True,
        outcome=outcome,
        duration_seconds=duration,
        captured=captured,
        transcript=transcript,
        opted_out=outcome == "do_not_call",
    )
