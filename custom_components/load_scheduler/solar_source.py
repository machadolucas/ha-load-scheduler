"""Parse solar-production forecast entities into per-slot available energy.

Supports Solcast (``detailedForecast`` / ``detailedHourly``: a list of
``{period_start, pv_estimate}`` where ``pv_estimate`` is the **average power in
kW** over the period) and a generic ``forecast`` list. Pure (operates on
attribute dicts), normalised to UTC. A forecast period's energy is integrated
onto the scheduling slots it overlaps, so a 30-min Solcast period feeds the two
15-min price slots beneath it.

Confirmed against live Solcast data: summing ``pv_estimate × period_hours`` over
a day equals the sensor's daily-total kWh, i.e. ``pv_estimate`` is power, not
energy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .engine import Slot

_FORECAST_ATTRS = ["detailedForecast", "detailedHourly", "forecast", "watts"]
_START_KEYS = ["period_start", "start", "time", "datetime", "from"]
# Power keys, with the divisor to convert the value to kW.
_POWER_KEYS: dict[str, float] = {
    "pv_estimate": 1.0,
    "power_kw": 1.0,
    "power": 1.0,
    "value": 1.0,
    "watts": 1000.0,
    "power_w": 1000.0,
}


class SolarFormatError(ValueError):
    """Raised when a solar entity's attributes can't be understood."""


@dataclass(frozen=True)
class SolarPeriod:
    """A forecast period with its average power in kW."""

    start: datetime
    end: datetime
    power_kw: float


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise SolarFormatError(f"unparseable period start: {value!r}")
    if dt.tzinfo is None:
        raise SolarFormatError(f"period start is not timezone-aware: {value!r}")
    return dt.astimezone(UTC)


def parse_solar(attributes: dict) -> list[SolarPeriod]:
    """Normalise a solar entity's attributes into UTC ``SolarPeriod``s."""
    attr = next((a for a in _FORECAST_ATTRS if a in attributes), None)
    if attr is None:
        raise SolarFormatError("no recognised solar forecast attribute")
    items = attributes.get(attr) or []
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise SolarFormatError(f"attribute {attr!r} is not a list of dicts")

    sample = items[0]
    start_key = next((k for k in _START_KEYS if k in sample), None)
    power_key = next((k for k in _POWER_KEYS if k in sample), None)
    if start_key is None or power_key is None:
        raise SolarFormatError("could not find start/power keys in forecast items")
    divisor = _POWER_KEYS[power_key]

    starts = [_parse_dt(it[start_key]) for it in items]
    # Most common gap → assumed period length for the final item.
    gaps: dict[float, int] = {}
    for a, b in zip(starts, starts[1:], strict=False):
        secs = (b - a).total_seconds()
        if secs > 0:
            gaps[secs] = gaps.get(secs, 0) + 1
    default = timedelta(seconds=max(gaps, key=gaps.get)) if gaps else timedelta(minutes=30)

    periods: list[SolarPeriod] = []
    for i, it in enumerate(items):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else start + default
        periods.append(SolarPeriod(start=start, end=end, power_kw=float(it[power_key]) / divisor))
    return periods


def merge_solar(*period_lists: list[SolarPeriod]) -> list[SolarPeriod]:
    """Concatenate several forecasts (e.g. today + tomorrow), dropping dup starts."""
    seen: set[datetime] = set()
    out: list[SolarPeriod] = []
    for periods in period_lists:
        for p in periods:
            if p.start in seen:
                continue
            seen.add(p.start)
            out.append(p)
    out.sort(key=lambda p: p.start)
    return out


def available_kwh_by_slot(periods: list[SolarPeriod], slots: list[Slot]) -> dict[datetime, float]:
    """Energy (kWh) forecast to be produced during each slot, by slot start.

    Integrates each overlapping forecast period's power across the slot.
    """
    result: dict[datetime, float] = {}
    for slot in slots:
        energy = 0.0
        for p in periods:
            overlap = (min(slot.end, p.end) - max(slot.start, p.start)).total_seconds()
            if overlap > 0:
                energy += p.power_kw * (overlap / 3600.0)
        result[slot.start] = energy
    return result
