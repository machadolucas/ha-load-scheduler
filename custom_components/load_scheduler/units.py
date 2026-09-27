"""Pure unit helpers (no Home Assistant import) — tested directly.

A load's feedback sensor is compared against ``feedback_idle_w``, a threshold in
**watts**, but plenty of power sensors report kW (Shelly, many P1 meters). A
1.5 kW element read as "1.5" is far below a 50 W idle threshold, so a heating
load looked permanently idle. Every consumer of the feedback reading should go
through :func:`power_to_watts`.
"""

from __future__ import annotations

# Case matters: "mW" (milliwatt) and "MW" (megawatt) are nine orders apart.
_POWER_FACTORS: dict[str, float] = {
    "mW": 1e-3,
    "W": 1.0,
    "kW": 1e3,
    "MW": 1e6,
    "GW": 1e9,
}


def power_to_watts(value: float, unit: str | None) -> float:
    """Convert a power reading in ``unit`` to watts.

    An unknown or missing unit is assumed to be watts — the threshold's own
    unit, and what a unit-less template sensor most likely reports — so the
    behaviour for such sensors is unchanged.
    """
    if unit is None:
        return value
    return value * _POWER_FACTORS.get(unit.strip(), 1.0)
