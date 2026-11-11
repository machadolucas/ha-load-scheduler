"""Pure calculations for adjusting an active boost."""

from __future__ import annotations

import math
from datetime import datetime, timedelta


def adjusted_end(now: datetime, current_end: datetime | None, minutes: float) -> datetime:
    """Return the proposed UTC end, enforcing adjustment service bounds.

    Positive adjustments may extend the boost to at most 24 hours from now.
    Reductions intentionally have no such upper bound so legacy long boosts can
    be shortened, including to a short remaining duration.
    """
    if not math.isfinite(minutes) or minutes == 0:
        raise ValueError("minutes must be a finite, nonzero number")
    if current_end is None or current_end <= now:
        raise ValueError("there is no active boost")
    try:
        result = current_end + timedelta(minutes=minutes)
    except (OverflowError, ValueError) as err:
        raise ValueError("adjustment is outside the supported time range") from err
    if result <= now:
        raise ValueError("adjustment must leave the boost active")
    if minutes > 0 and result > now + timedelta(minutes=1440):
        raise ValueError("positive adjustment would exceed 24 hours from now")
    return result
