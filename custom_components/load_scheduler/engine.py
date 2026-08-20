"""Pure scheduling engine for the Load Scheduler integration.

This module is deliberately **free of any Home Assistant imports** so it can be
unit-tested in isolation and reasoned about as pure functions: given a list of
price ``Slot``s and a ``LoadParams``, it returns the ``Period``s to run.

Design rules that keep it testable and DST-correct:

* Every datetime is timezone-aware. The engine **never calls ``now()``** — the
  caller passes an explicit ``now`` so behaviour is deterministic.
* Time arithmetic is done by adding/subtracting from the *actual slot
  boundaries* coming from the price source (which already carry the correct
  UTC offset), never by synthesising ``naive + timedelta(hours=n)``. This is
  what makes 23h/25h DST days work.
* Durations are tracked in **minutes** (floats) so sub-hour targets are exact;
  the final run is trimmed to the exact minute, mirroring the legacy LVV
  template behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# Small tolerance (minutes) for floating-point time comparisons.
_EPS = 1e-6


class ScheduleMode(StrEnum):
    """How a load's run periods are chosen."""

    NON_SEQUENTIAL = "non_sequential"  # cheapest slots, possibly scattered
    SEQUENTIAL = "sequential"  # one (or more) contiguous block(s)
    INFORMATIONAL = "informational"  # compute + display only, never actuated


class RunSource(StrEnum):
    """Where the energy for a period is expected to come from."""

    GRID = "grid"  # imported at buy price
    SOLAR = "solar"  # self-consumed excess (opportunity cost = sell price)
    MIXED = "mixed"  # a merged period spanning both


@dataclass(frozen=True)
class Slot:
    """A single price slot from the (normalised) forecast.

    ``buy``/``sell`` are €/kWh. ``excess_kwh`` is the predicted *solar excess*
    available during the slot (kWh that would otherwise be exported); it is 0
    when there is no surplus or solar is not configured.
    """

    start: datetime
    end: datetime
    buy: float
    sell: float | None = None
    excess_kwh: float = 0.0

    @property
    def minutes(self) -> float:
        """Slot length in minutes."""
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class Period:
    """A scheduled run period (the engine's output)."""

    start: datetime
    end: datetime
    source: RunSource = RunSource.GRID
    # Average effective €/kWh across the period, energy-weighted by minutes.
    avg_cost: float = 0.0

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class LoadParams:
    """Everything the engine needs to plan one load.

    ``window`` is the search window ``[start, end)`` (already resolved to
    concrete tz-aware datetimes by the caller, midnight-spanning allowed).
    ``target_minutes`` is the desired run time; for kWh-mode loads the caller
    converts kWh→minutes via the charge power before calling.
    """

    mode: ScheduleMode
    target_minutes: float
    window: tuple[datetime, datetime]
    # Anti-starvation floor: guaranteed minutes that ignore the price cap.
    min_service_minutes: float = 0.0
    # Absolute €/kWh cap: discretionary runtime above the min-service floor is
    # only scheduled in slots whose effective cost is <= cap. None disables it.
    cap: float | None = None
    # Load draw in kW (used to value solar excess and, in kWh mode, to size the
    # target). None => solar excess is treated as binary (any excess = solar).
    draw_kw: float | None = None
    solar_enabled: bool = False
    # Sequential only:
    runs_per_day: int = 1
    min_separation_minutes: float = 0.0
    # Compressor protection (both modes):
    min_run_minutes: float = 0.0
    min_off_minutes: float = 0.0
    # Instant by which the min-service floor must be *delivered*. Delivered-today
    # is measured since local midnight, so guaranteed minutes placed after that
    # boundary never count towards the day they were meant to protect; the caller
    # passes the next local midnight. None leaves the floor free to float.
    min_service_by: datetime | None = None


def effective_cost(slot: Slot, draw_kw: float | None, solar_enabled: bool) -> float:
    """€/kWh the load effectively pays in this slot.

    Importing from the grid costs ``buy``. Running on predicted solar excess
    costs the *foregone* ``sell`` price (opportunity cost), which is lower. A
    partially-covered slot is blended by the covered fraction.
    """
    base = slot.buy
    if not solar_enabled or slot.sell is None or slot.excess_kwh <= 0:
        return base
    if draw_kw is None:
        # Binary model: any excess means the slot runs "on solar".
        return slot.sell
    load_kwh = draw_kw * (slot.minutes / 60.0)
    if load_kwh <= 0:
        return base
    covered = min(slot.excess_kwh, load_kwh)
    frac = covered / load_kwh
    return frac * slot.sell + (1.0 - frac) * slot.buy


def _slot_source(slot: Slot, draw_kw: float | None, solar_enabled: bool) -> RunSource:
    """Classify a slot as solar- or grid-sourced for display."""
    if solar_enabled and slot.sell is not None and slot.excess_kwh > 0:
        return RunSource.SOLAR
    return RunSource.GRID


@dataclass
class _Pick:
    """An internal selected interval (a slot, possibly trimmed)."""

    start: datetime
    end: datetime
    cost: float
    source: RunSource

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def _window_slots(slots: list[Slot], window: tuple[datetime, datetime]) -> list[Slot]:
    """Slots overlapping ``[window[0], window[1])``, **clipped** to it, time-ordered.

    Overlap (not just ``start`` inside) so the slot currently in progress — which
    began just before ``window[0]`` when that is clamped to ``now`` — is still
    eligible. Without this, a load that should be running *right now* would never
    be scheduled until the next slot boundary.

    Clipping matters because the planner budgets in minutes: an unclipped
    half-elapsed slot would spend a full slot of the target on runtime that has
    already gone by, and an unclipped tail slot would plan past the deadline.
    ``excess_kwh`` is scaled by the retained fraction so the solar valuation is
    unchanged — ``effective_cost`` divides it by a ``load_kwh`` that scales the
    same way, so the covered fraction (and the blended price) is invariant.
    """
    w_start, w_end = window
    clipped: list[Slot] = []
    for s in slots:
        if s.end <= w_start or s.start >= w_end:
            continue
        start, end = max(s.start, w_start), min(s.end, w_end)
        if start == s.start and end == s.end:
            clipped.append(s)
            continue
        span = (end - start).total_seconds()
        if span <= 0:
            continue
        full = (s.end - s.start).total_seconds()
        clipped.append(
            Slot(
                start=start,
                end=end,
                buy=s.buy,
                sell=s.sell,
                excess_kwh=s.excess_kwh * (span / full) if full > 0 else s.excess_kwh,
            )
        )
    clipped.sort(key=lambda s: s.start)
    return clipped


def _merge(picks: list[_Pick]) -> list[Period]:
    """Merge contiguous picks (by time) into periods, weighting avg cost."""
    if not picks:
        return []
    picks = sorted(picks, key=lambda p: p.start)
    periods: list[Period] = []
    # Track weighted cost accumulation per open period.
    cur_cost_min = 0.0
    sources: set[RunSource] = set()
    for pick in picks:
        if periods and abs((periods[-1].end - pick.start).total_seconds()) < _EPS:
            # Contiguous with the open period: extend it.
            periods[-1].end = pick.end
        else:
            # Close out previous weighted average, start a new period.
            if periods:
                periods[-1].avg_cost = (
                    cur_cost_min / periods[-1].minutes if periods[-1].minutes else 0.0
                )
                periods[-1].source = _combine_sources(sources)
            periods.append(Period(start=pick.start, end=pick.end))
            cur_cost_min = 0.0
            sources = set()
        cur_cost_min += pick.cost * pick.minutes
        sources.add(pick.source)
    # Finalise the last open period.
    periods[-1].avg_cost = cur_cost_min / periods[-1].minutes if periods[-1].minutes else 0.0
    periods[-1].source = _combine_sources(sources)
    return periods


def _combine_sources(sources: set[RunSource]) -> RunSource:
    if not sources or sources == {RunSource.GRID}:
        return RunSource.GRID
    if sources == {RunSource.SOLAR}:
        return RunSource.SOLAR
    return RunSource.MIXED


def plan_non_sequential(slots: list[Slot], params: LoadParams) -> list[Period]:
    """Pick the cheapest (by effective cost) slots until the target is met.

    The first ``min_service_minutes`` are filled from the cheapest slots
    **regardless of price** (anti-starvation); the remaining discretionary
    minutes are only filled from slots at or below ``cap``. The final, most
    expensive selected slot is trimmed to land on the exact target minute.

    The guarantee prefers slots finishing before ``min_service_by`` so it lands
    inside the day it is accounted against, but falls back to the whole window
    rather than going unmet — anti-starvation outranks same-day placement.

    With ``min_run_minutes`` set the load cannot be scattered a slot at a time,
    so selection switches to whole runs (see ``_plan_runs``).
    """
    target = max(params.target_minutes, params.min_service_minutes)
    if target <= 0:
        return []
    guaranteed = params.min_service_minutes
    candidates = _window_slots(slots, params.window)
    if params.min_run_minutes > 0:
        return _merge(_plan_runs(candidates, params, target, guaranteed))

    # Cheapest first; ties broken by start time for determinism.
    candidates.sort(
        key=lambda s: (effective_cost(s, params.draw_kw, params.solar_enabled), s.start)
    )

    picks: list[_Pick] = []
    taken: set[int] = set()
    acc = 0.0

    def take(index: int, slot: Slot, cost: float) -> None:
        nonlocal acc
        taken.add(index)
        picks.append(
            _Pick(
                start=slot.start,
                end=slot.end,
                cost=cost,
                source=_slot_source(slot, params.draw_kw, params.solar_enabled),
            )
        )
        acc += slot.minutes

    costs = [effective_cost(s, params.draw_kw, params.solar_enabled) for s in candidates]

    # Pass 1: the cap-exempt guarantee, same-day slots first, then anywhere.
    for same_day_only in (True, False):
        if params.min_service_by is None and same_day_only:
            continue
        for i, slot in enumerate(candidates):
            if acc >= min(guaranteed, target) - _EPS:
                break
            if i in taken:
                continue
            if same_day_only and slot.end > params.min_service_by:
                continue
            take(i, slot, costs[i])

    # Pass 2: discretionary minutes, subject to the cap.
    for i, slot in enumerate(candidates):
        if acc >= target - _EPS:
            break
        if i in taken:
            continue
        if params.cap is not None and costs[i] > params.cap:
            continue
        take(i, slot, costs[i])

    periods = _merge(picks)
    # Land on the exact target by trimming the overshoot off the *tail* (latest
    # period). Trimming the most-expensive pick instead can shorten a slot that
    # sits mid-run, leaving a sub-minute gap that splits one contiguous run into
    # two periods on the card; the cost difference for a sub-slot trim is
    # negligible.
    overshoot = acc - target
    return _trim_tail(periods, overshoot) if overshoot > _EPS else periods


def _trim_tail(periods: list[Period], overshoot: float) -> list[Period]:
    """Remove ``overshoot`` minutes from the end of the (time-ordered) periods."""
    trimmed = list(periods)
    while overshoot > _EPS and trimmed:
        last = trimmed[-1]
        if last.minutes <= overshoot + _EPS:
            overshoot -= last.minutes
            trimmed.pop()
        else:
            last.end = last.end - timedelta(minutes=overshoot)
            overshoot = 0.0
    return trimmed


def _plan_runs(
    win: list[Slot], params: LoadParams, target: float, guaranteed: float
) -> list[_Pick]:
    """Select whole runs of at least ``min_run_minutes`` until the target is met.

    ``min_run`` is a hardware constraint, so it has to shape the *selection*, not
    trim it afterwards: picking the cheapest scattered slots and then deleting
    the fragments shorter than ``min_run`` throws those minutes away entirely,
    leaving the load short even when a cheap contiguous run existed elsewhere.

    Runs are taken one ``min_run`` at a time, except that the last one absorbs
    the remainder so the target is still hit exactly. A tail smaller than
    ``min_run`` is skipped rather than overshot — unless it is the anti-starvation
    floor, which is allowed to overshoot to stay a legal run length.
    """
    min_run = params.min_run_minutes
    used = [False] * len(win)
    picks: list[_Pick] = []
    acc = 0.0
    while True:
        remaining = target - acc
        in_guarantee = acc < guaranteed - _EPS
        if remaining >= 2 * min_run - _EPS:
            length = min_run
        elif remaining >= min_run - _EPS:
            length = remaining  # last run absorbs the remainder exactly
        elif in_guarantee:
            length = min_run
        else:
            break
        cap = None if in_guarantee else params.cap
        block = None
        if in_guarantee and params.min_service_by is not None:
            block = _best_block(win, used, length, params, cap=cap, not_after=params.min_service_by)
        if block is None:
            block = _best_block(win, used, length, params, cap=cap)
        if block is None:
            break
        picks.extend(block)
        # The guard keeps the next run at least ``min_off`` away, so the plan
        # comes out already compliant and nothing has to be bridged afterwards.
        _mark_used(win, used, block, params.min_off_minutes)
        acc += length
    return picks


def _best_block(
    win: list[Slot],
    used: list[bool],
    block_minutes: float,
    params: LoadParams,
    *,
    cap: float | None = None,
    not_after: datetime | None = None,
) -> list[_Pick] | None:
    """Cheapest unbroken run of exactly ``block_minutes``, starting on a boundary.

    Scans by **real minutes**, not slot counts, and weights each slot's cost by
    the minutes actually taken from it. Both matter because the forecast mixes
    resolutions — the day-ahead feed is quarter-hourly while the predictor slots
    appended beyond its horizon are hourly — so "n slots per block" sizes the run
    wrong on one side of the seam, and an unweighted cost sum compares an hour
    against a quarter of an hour as if they were the same purchase.

    ``cap`` rejects a block whose minutes-weighted average exceeds it;
    ``not_after`` requires the run to finish by then. Returns the picks (the
    trailing one already trimmed to length) or ``None`` if no run fits.
    """
    best: tuple[float, list[_Pick]] | None = None
    for i in range(len(win)):
        if used[i]:
            continue
        picks: list[_Pick] = []
        acc = 0.0
        total = 0.0
        j = i
        while j < len(win) and acc < block_minutes - _EPS:
            if used[j]:
                break
            if j > i and abs((win[j - 1].end - win[j].start).total_seconds()) > _EPS:
                break  # gap or DST hole: the run would not be contiguous
            slot = win[j]
            take = min(slot.minutes, block_minutes - acc)
            cost = effective_cost(slot, params.draw_kw, params.solar_enabled)
            picks.append(
                _Pick(
                    start=slot.start,
                    end=(
                        slot.end
                        if take >= slot.minutes - _EPS
                        else slot.start + timedelta(minutes=take)
                    ),
                    cost=cost,
                    source=_slot_source(slot, params.draw_kw, params.solar_enabled),
                )
            )
            acc += take
            total += cost * take
            j += 1
        if acc < block_minutes - _EPS:
            continue
        if not_after is not None and picks[-1].end > not_after:
            continue
        if cap is not None and total / block_minutes > cap + _EPS:
            continue
        # `<` (strict) keeps the *earliest* cheapest block on ties.
        if best is None or total < best[0] - _EPS:
            best = (total, picks)
    return None if best is None else best[1]


def _mark_used(win: list[Slot], used: list[bool], picks: list[_Pick], guard_minutes: float) -> None:
    """Mark the picked run — plus a guard either side — as spent."""
    guard = timedelta(minutes=guard_minutes)
    guard_start = picks[0].start - guard
    guard_end = picks[-1].end + guard
    for j, s in enumerate(win):
        if s.start < guard_end and s.end > guard_start:
            used[j] = True


def plan_sequential(slots: list[Slot], params: LoadParams) -> list[Period]:
    """Find the cheapest contiguous block(s) of ``target_minutes``.

    Supports ``runs_per_day > 1`` (e.g. run the washing machine twice): the
    best block is chosen, then its slots plus a ``min_separation_minutes``
    guard are excluded and the next best non-overlapping block is found.
    """
    block_minutes = max(params.target_minutes, params.min_service_minutes)
    if block_minutes <= 0:
        return []
    win = _window_slots(slots, params.window)
    if not win:
        return []

    used = [False] * len(win)
    results: list[Period] = []
    for _ in range(max(1, params.runs_per_day)):
        picks = _best_block(win, used, block_minutes, params)
        if picks is None:
            break
        results.extend(_merge(picks))
        _mark_used(win, used, picks, params.min_separation_minutes)

    results.sort(key=lambda p: p.start)
    return results


def compute_plan(slots: list[Slot], params: LoadParams) -> list[Period]:
    """Dispatch to the right algorithm for the load's mode.

    ``INFORMATIONAL`` loads are scheduled exactly like ``SEQUENTIAL`` ones (the
    dishwasher case: find the cheapest contiguous block to *show*); the caller
    is responsible for not actuating them.
    """
    if params.mode is ScheduleMode.NON_SEQUENTIAL:
        periods = plan_non_sequential(slots, params)
    else:
        periods = plan_sequential(slots, params)
    if params.min_run_minutes or params.min_off_minutes:
        periods = enforce_min_run_off(periods, params.min_run_minutes, params.min_off_minutes)
    return periods


def enforce_min_run_off(periods: list[Period], min_run: float, min_off: float) -> list[Period]:
    """Bridge too-short off-gaps and drop too-short runs (compressor protection).

    Off-gaps shorter than ``min_off`` are filled (the load keeps running through
    them rather than short-cycling); any remaining period shorter than
    ``min_run`` is then dropped.
    """
    if not periods:
        return []
    ordered = sorted(periods, key=lambda p: p.start)
    merged = [Period(ordered[0].start, ordered[0].end, ordered[0].source, ordered[0].avg_cost)]
    for p in ordered[1:]:
        last = merged[-1]
        gap = (p.start - last.end).total_seconds() / 60.0
        if gap < min_off:
            w_last, w_p = last.minutes, p.minutes
            total = w_last + w_p
            last.avg_cost = (last.avg_cost * w_last + p.avg_cost * w_p) / total if total else 0.0
            last.source = last.source if last.source == p.source else RunSource.MIXED
            last.end = max(last.end, p.end)
        else:
            merged.append(Period(p.start, p.end, p.source, p.avg_cost))
    return [p for p in merged if p.minutes >= min_run - _EPS]


def merge_periods(periods: list[Period]) -> list[Period]:
    """Merge overlapping/adjacent periods (by time) into a minimal set.

    Used to fold a manual boost interval into the computed plan. ``avg_cost`` is
    a minutes-weighted blend of the merged inputs (good enough for display).
    """
    if not periods:
        return []
    ordered = sorted(periods, key=lambda p: p.start)
    merged = [Period(ordered[0].start, ordered[0].end, ordered[0].source, ordered[0].avg_cost)]
    for p in ordered[1:]:
        last = merged[-1]
        if p.start <= last.end:
            w_last, w_p = last.minutes, p.minutes
            total = w_last + w_p
            last.avg_cost = (last.avg_cost * w_last + p.avg_cost * w_p) / total if total else 0.0
            last.source = last.source if last.source == p.source else RunSource.MIXED
            last.end = max(last.end, p.end)
        else:
            merged.append(Period(p.start, p.end, p.source, p.avg_cost))
    return merged
