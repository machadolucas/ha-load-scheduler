"""Pure detection of a *competing controller* (no Home Assistant imports).

A load's controlled entity is rarely the scheduler's alone: this integration
replaces a pile of legacy automations, and one left enabled keeps flipping the
same switch. The actuator reads every such foreign change as a *manual override*
— a foreign off suppresses the rest of the active period, a foreign on on a
coexist load is credited and never cut — which is exactly right for a human at a
wall switch and exactly wrong for an automation that will do it again tomorrow.
The result is a schedule that silently degrades while every entity still looks
healthy. This module turns that invisible failure into a repair issue.

The signal is the *shape* of the foreign changes, not their count alone, because
a household legitimately flips its own switches:

* **Burst** — several flips inside a day. No one touches a water heater four
  times before dinner; something is fighting the plan right now.
* **Recurrence** — flips landing at the same wall-clock minute on several
  distinct days. That is a cron/automation fingerprint, and the comparison is
  done in *local* time so a DST shift doesn't scatter the cluster (the same
  22:00 run is 20:00 UTC in winter and 19:00 UTC in summer).

Two guards keep it honest. Changes carrying a ``user_id`` are excluded outright
— a person clicking in the UI is not a competing controller — and an
unattributable flip needs more days of recurrence than a scripted one, because a
punctual human on a physical switch can look scripted for three days but rarely
for five. Events older than the rolling window decay out, so the issue clears
itself about a week after the competing automation is disabled, with nothing to
acknowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

# Events older than this are forgotten, which is what makes the issue
# self-clearing rather than something the user has to dismiss.
ROLLING_WINDOW_H = 7 * 24
# Per-load cap. Bounds both the Store payload and the O(n²) same-time scan; the
# verdict never needs more evidence than this.
MAX_EVENTS = 50

# Burst: this many non-user flips inside one day is already a fight.
BURST_WINDOW_H = 24
BURST_COUNT = 4

# Recurrence: "same time" is ± this many minutes of the local minute-of-day.
RECUR_TOLERANCE_MIN = 45
RECUR_DAYS_SCRIPTED = 3
RECUR_DAYS_UNKNOWN = 5

# Who made the change, as far as the event context can tell.
SOURCE_USER = "user"  # a person acting in the UI (never counted)
SOURCE_SCRIPTED = "scripted"  # spawned by an automation/script
SOURCE_UNKNOWN = "unknown"  # no attribution (physical switch, integration, …)

_SOURCES = (SOURCE_USER, SOURCE_SCRIPTED, SOURCE_UNKNOWN)
_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class ForeignEvent:
    """One observed change of a controlled entity that the scheduler didn't make."""

    when: datetime  # UTC, tz-aware
    turned_on: bool
    in_active_period: bool  # it landed inside a scheduled period (i.e. it cost us)
    source: str  # one of SOURCE_*

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "turned_on": self.turned_on,
            "in_active_period": self.in_active_period,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ForeignEvent | None:
        """Rebuild from a persisted dict, or None if it isn't usable.

        Tolerant by design: this log is diagnostic, so a row mangled by a
        hand-edited ``.storage`` file or an older format is dropped rather than
        allowed to break setup.
        """
        if not isinstance(data, dict):
            return None
        raw = data.get("when")
        if not isinstance(raw, str):
            return None
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if when.tzinfo is None:
            return None  # naive timestamps can't be compared against `now`
        source = data.get("source")
        if source not in _SOURCES:
            return None
        return cls(
            when=when,
            turned_on=bool(data.get("turned_on")),
            in_active_period=bool(data.get("in_active_period")),
            source=source,
        )


@dataclass(frozen=True)
class CompetingVerdict:
    """The assessment of one load's foreign-change history."""

    competing: bool
    reason: str | None  # "burst" | "recurring" | None
    count: int  # non-user events in the rolling window
    scripted_count: int
    in_period_count: int  # how many landed inside a scheduled period
    recurring_days: int  # distinct days in the largest same-time cluster
    last: datetime | None


def prune(events: list[ForeignEvent], now: datetime) -> list[ForeignEvent]:
    """Drop decayed events and keep only the most recent ``MAX_EVENTS``."""
    cutoff = now - timedelta(hours=ROLLING_WINDOW_H)
    kept = sorted((e for e in events if e.when >= cutoff), key=lambda e: e.when)
    return kept[-MAX_EVENTS:]


def _circular_minutes_apart(a: int, b: int) -> int:
    """Distance between two minutes-of-day, wrapping at midnight.

    Without the wrap a 23:50 and a 00:10 automation would look like opposite
    ends of the day instead of 20 minutes apart.
    """
    diff = abs(a - b) % _MINUTES_PER_DAY
    return min(diff, _MINUTES_PER_DAY - diff)


def _max_in_burst_window(events: list[ForeignEvent]) -> int:
    """The most events falling inside any ``BURST_WINDOW_H`` window."""
    span = timedelta(hours=BURST_WINDOW_H)
    return max(
        (
            sum(
                1
                for other in events
                if 0 <= (other.when - e.when).total_seconds() <= span.total_seconds()
            )
            for e in events
        ),
        default=0,
    )


def _max_same_time_days(events: list[ForeignEvent], tz: tzinfo) -> int:
    """Distinct local dates in the largest "same wall-clock minute" cluster.

    Anchored on each event in turn rather than on fixed buckets, so a cluster
    straddling a bucket edge still counts. Dates are counted, not events, so an
    automation that retries three times in one evening is one day, not three.
    """
    local = [
        (dt.date(), dt.hour * 60 + dt.minute) for dt in (e.when.astimezone(tz) for e in events)
    ]
    return max(
        (
            len({d for d, m in local if _circular_minutes_apart(m, anchor) <= RECUR_TOLERANCE_MIN})
            for _, anchor in local
        ),
        default=0,
    )


def assess(events: list[ForeignEvent], now: datetime, tz: tzinfo) -> CompetingVerdict:
    """Decide whether something other than the scheduler is driving this load.

    ``tz`` is the local timezone the recurrence clustering is done in (see the
    module docstring). User-sourced changes are excluded from every count.
    """
    flips = [e for e in prune(events, now) if e.source != SOURCE_USER]
    if not flips:
        return CompetingVerdict(False, None, 0, 0, 0, 0, None)

    scripted = [e for e in flips if e.source == SOURCE_SCRIPTED]
    # The reported cluster is the largest over all non-user events; the two
    # thresholds below are applied to their own populations.
    recurring_days = _max_same_time_days(flips, tz)
    recurring = (
        _max_same_time_days(scripted, tz) >= RECUR_DAYS_SCRIPTED
        or recurring_days >= RECUR_DAYS_UNKNOWN
    )
    burst = _max_in_burst_window(flips) >= BURST_COUNT

    return CompetingVerdict(
        competing=burst or recurring,
        # Burst wins the label: it describes what is happening *now*, which is
        # the more actionable half when both patterns are present.
        reason="burst" if burst else ("recurring" if recurring else None),
        count=len(flips),
        scripted_count=len(scripted),
        in_period_count=sum(1 for e in flips if e.in_active_period),
        recurring_days=recurring_days,
        last=max(e.when for e in flips),
    )
