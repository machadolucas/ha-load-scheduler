"""Build an hour-of-day household-consumption baseline from statistics samples.

The pure part — averaging watt samples by local hour into a kW profile — lives
here so it is unit-testable. The coordinator does the (defensive, best-effort)
recorder fetch and feeds the ``(hour, watts)`` samples in.

Using long-term *statistics* (hourly means, kept permanently) rather than raw
history is deliberate: it gives a usable baseline without retaining
high-frequency net-energy history.
"""

from __future__ import annotations


def build_hourly_profile(samples: list[tuple[int, float]]) -> dict[int, float]:
    """Average ``(hour_of_day, watts)`` samples into an hour → kW profile.

    Hours with no samples are simply absent (callers fall back to the flat
    baseline for those).
    """
    by_hour: dict[int, list[float]] = {}
    for hour, watts in samples:
        by_hour.setdefault(hour, []).append(watts)
    return {hour: (sum(values) / len(values)) / 1000.0 for hour, values in by_hour.items()}
