"""Resolve a load's daily search window into concrete tz-aware datetimes.

A load runs within a recurring daily window described by two wall-clock times:
an ``earliest`` start and a ``deadline``. This module turns that into the
concrete ``(start, end)`` for the *upcoming* window, given ``now``.

Pure and **DST-safe**: windows are built by combining a calendar *date* with a
wall-clock *time* in ``now``'s timezone (``datetime.combine(date, time, tz)``),
and day rollovers add to the **date**, never ``timedelta(hours=24)`` to a
datetime. So on a 23h/25h DST day a "21:00 → 07:00" window correctly spans 9h
or 11h of real time, which is exactly the case the old Jinja templates got
wrong.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta


def resolve_window(
    now: datetime,
    earliest: time | None,
    deadline: time | None,
    *,
    clamp_to_now: bool = True,
) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` for the window ending at the next ``deadline``.

    * ``earliest`` / ``deadline`` are wall-clock ``datetime.time`` in ``now``'s
      timezone. ``earliest=None`` means "from now"; ``deadline=None`` means a
      rolling 24h horizon.
    * Daytime window (``deadline > earliest``) resolves within one calendar day;
      an overnight window (``deadline <= earliest``) puts ``earliest`` on the
      evening *before* the deadline day.
    * The deadline chosen is the next future occurrence of ``deadline``.
    * With ``clamp_to_now`` (default) the start is pulled forward to ``now`` so
      the engine never considers slots in the past.

    ``now`` must be timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo

    def at(d, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=tz)

    # End of the window = the next future occurrence of `deadline`.
    if deadline is None:
        end = now + timedelta(days=1)
    else:
        end = at(now.date(), deadline)
        if end <= now:
            end = at(now.date() + timedelta(days=1), deadline)

    # Start of the window.
    if earliest is None:
        start = now
    elif deadline is not None and deadline > earliest:
        # Daytime window: earliest is on the deadline's own day.
        start = at(end.date(), earliest)
    else:
        # Overnight window (or no deadline): earliest is the evening before.
        start = at(end.date() - timedelta(days=1), earliest)

    if clamp_to_now and start < now:
        start = now

    return (start, end)


def next_time(now: datetime, at: time) -> datetime:
    """Next future occurrence of wall-clock ``at`` (today, else tomorrow)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidate = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = datetime.combine(now.date() + timedelta(days=1), at, tzinfo=now.tzinfo)
    return candidate
