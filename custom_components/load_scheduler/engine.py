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

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    # Per-run block length when ``runs_per_day > 1``; ``target_minutes`` is then
    # the *total* still to deliver across the remaining runs. Delivered-today can
    # only be subtracted from that total — taken off each block, one finished run
    # shrank every block to nothing and the second run was never planned. None
    # keeps the plain form: ``runs_per_day`` blocks of ``target_minutes`` each.
    run_minutes: float | None = None
    min_separation_minutes: float = 0.0
    # Compressor protection (both modes):
    min_run_minutes: float = 0.0
    min_off_minutes: float = 0.0
    # Instant by which the min-service floor must be *delivered*. Delivered-today
    # is measured since local midnight, so guaranteed minutes placed after that
    # boundary never count towards the day they were meant to protect; the caller
    # passes the next local midnight. None leaves the floor free to float.
    min_service_by: datetime | None = None
    # How long the load's current on-run has lasted (0 when off), valid only when
    # the window starts *now*. A replan mid-run must not cut that run short of
    # ``min_run`` or move the rest of a sequential cycle elsewhere; see
    # ``_plan_runs`` / ``plan_sequential``.
    running_minutes: float = 0.0
    # Minutes since the load last switched off, measured to the window start
    # (None when on, or unknown). Replans are stateless, so without it a load
    # switched off a minute ago could be planned straight back on, breaking
    # ``min_off`` / ``min_separation``.
    stopped_minutes: float | None = None


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
    # UTC, like the slots: a clipped edge inherits the window's datetime, and
    # adding minutes to a *local* one is wall-clock arithmetic — across a DST
    # change a 120-minute plan then ran 60 or 180 real minutes.
    w_start, w_end = window[0].astimezone(UTC), window[1].astimezone(UTC)
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


def _mandatory_minutes(params: LoadParams) -> float:
    """How much longer a run in progress must go on to reach ``min_run``.

    Hardware protection, so it holds even with the target and floor both met,
    and ignores the cap. A divert or external run gets it too: the actuator's
    divert already refuses to shed before min_run, and a run the integration
    doesn't own is never switched off anyway. Fixed by the clock, never by
    delivered, so a replan can't stretch it.
    """
    if params.running_minutes <= _EPS or params.min_run_minutes <= 0:
        return 0.0
    return max(0.0, params.min_run_minutes - params.running_minutes)


def plan_non_sequential(slots: list[Slot], params: LoadParams) -> list[Period]:
    """Pick the cheapest (by effective cost) slots until the target is met.

    The first ``min_service_minutes`` are filled from the cheapest slots
    **regardless of price** (anti-starvation); the remaining discretionary
    minutes are only filled from slots at or below ``cap``. The final, most
    expensive selected slot is trimmed to land on the exact target minute.

    The guarantee prefers slots finishing before ``min_service_by`` so it lands
    inside the day it is accounted against, but falls back to the whole window
    rather than going unmet — anti-starvation outranks same-day placement.

    With ``min_run_minutes`` or ``min_off_minutes`` set the load cannot be
    scattered a slot at a time, so selection switches to whole runs (see
    ``_plan_runs``).
    """
    target = max(params.target_minutes, params.min_service_minutes)
    if target <= 0 and _mandatory_minutes(params) <= _EPS:
        return []
    guaranteed = params.min_service_minutes
    candidates = _window_slots(slots, params.window)
    if params.min_run_minutes > 0 or params.min_off_minutes > 0:
        if not candidates:
            return []
        # Without a min_run the run is stepped a native slot at a time; the
        # *unclipped* length, so a few leftover minutes of the slot in progress
        # don't shrink the step (and multiply the iterations).
        unit = params.min_run_minutes or min(
            s.minutes for s in slots if s.end > params.window[0] and s.start < params.window[1]
        )
        grid = _Grid(candidates, params)
        return _merge(_plan_runs(grid, params, target, guaranteed, unit))

    costs = [effective_cost(s, params.draw_kw, params.solar_enabled) for s in candidates]
    # (cost, start, end, source): pass 1 may split a slot, and the unused part
    # has to re-enter pass 2 as a candidate of its own.
    pool = [
        (costs[i], s.start, s.end, _slot_source(s, params.draw_kw, params.solar_enabled))
        for i, s in enumerate(candidates)
    ]
    # Cheapest first; ties broken by start time for determinism.
    pool.sort(key=lambda c: (c[0], c[1]))

    picks: list[_Pick] = []
    taken: set[int] = set()
    acc = 0.0

    # Pass 1: the cap-exempt guarantee, same-day slots first, then anywhere.
    # Only the guaranteed minutes are exempt: a slot that overshoots the floor
    # is split at it, and its remainder goes back into the pool so pass 2 can
    # cap-check it like any other discretionary minute. Taking the whole slot
    # let a 5-minute floor smuggle 55 above-cap minutes into the plan.
    rest: list[tuple[float, datetime, datetime, RunSource]] = []
    for same_day_only in (True, False):
        if params.min_service_by is None and same_day_only:
            continue
        for i, (cost, start, end, source) in enumerate(pool):
            need = guaranteed - acc
            if need <= _EPS:
                break
            if i in taken or (same_day_only and end > params.min_service_by):
                continue
            taken.add(i)
            minutes = (end - start).total_seconds() / 60.0
            if minutes > need + _EPS:
                cut = start + timedelta(minutes=need)
                rest.append((cost, cut, end, source))
                end, minutes = cut, need
            picks.append(_Pick(start=start, end=end, cost=cost, source=source))
            acc += minutes

    # Pass 2: discretionary minutes, subject to the cap.
    pass2 = [c for i, c in enumerate(pool) if i not in taken] + rest
    pass2.sort(key=lambda c: (c[0], c[1]))
    for cost, start, end, source in pass2:
        if acc >= target - _EPS:
            break
        if params.cap is not None and cost > params.cap:
            continue
        picks.append(_Pick(start=start, end=end, cost=cost, source=source))
        acc += (end - start).total_seconds() / 60.0

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


class _Grid:
    """The window's slots as a splittable timeline, in minutes from the window start.

    Offsets (not datetimes) and per-slot cost/source computed once per plan keep
    the block scan cheap — it runs once per run placed, over a few hundred slots.

    A slot can be split at any instant, so a partly consumed slot leaves its
    remainder buyable: marking the whole slot spent when a run (or a guard) ends
    inside it wasted the rest — a 30-min run in an hourly slot threw away the
    other half hour and pushed the load into a dearer slot. Fragments keep the
    slot's price: ``effective_cost`` is invariant when span and ``excess_kwh``
    scale together (see ``_window_slots``).
    """

    def __init__(self, win: list[Slot], params: LoadParams) -> None:
        # UTC so ``at()`` adds real elapsed minutes (see ``_window_slots``).
        self.origin = params.window[0].astimezone(UTC)
        self.start = [self.offset(s.start) for s in win]
        self.end = [self.offset(s.end) for s in win]
        self.cost = [effective_cost(s, params.draw_kw, params.solar_enabled) for s in win]
        self.src = [_slot_source(s, params.draw_kw, params.solar_enabled) for s in win]
        self.used = [False] * len(win)

    def offset(self, t: datetime) -> float:
        return (t - self.origin).total_seconds() / 60.0

    def at(self, minutes: float) -> datetime:
        return self.origin + timedelta(minutes=minutes)

    def split(self, t: float) -> None:
        """Cut the slot containing ``t`` (strictly inside it) in two."""
        j = bisect_right(self.start, t) - 1
        if j < 0 or not (self.start[j] + _EPS < t < self.end[j] - _EPS):
            return
        for col, value in (
            (self.start, t),
            (self.end, self.end[j]),
            (self.cost, self.cost[j]),
            (self.src, self.src[j]),
            (self.used, self.used[j]),
        ):
            col.insert(j + 1, value)
        self.end[j] = t

    def contiguous_from(self, t: float, limit: float) -> float:
        """Unbroken, unused minutes starting exactly at ``t`` (at most ``limit``)."""
        j = bisect_left(self.start, t - _EPS)
        acc, cursor = 0.0, t
        while j < len(self.start) and acc < limit - _EPS:
            if self.used[j] or abs(self.start[j] - cursor) > _EPS:
                break
            acc += min(self.end[j] - self.start[j], limit - acc)
            cursor = self.end[j]
            j += 1
        return acc

    def cost_between(self, a: float, b: float) -> float:
        """cost·minutes of ``[a, b)`` (uncovered minutes cost nothing)."""
        total = 0.0
        for j in range(bisect_left(self.end, a + _EPS), len(self.start)):
            if self.start[j] >= b - _EPS:
                break
            total += self.cost[j] * max(0.0, min(self.end[j], b) - max(self.start[j], a))
        return total

    def take(self, a: float, b: float, guard: float = 0.0) -> list[_Pick]:
        """Spend ``[a, b)`` (plus ``guard`` either side) and return its picks."""
        for t in (a, b, a - guard, b + guard):
            self.split(t)
        picks: list[_Pick] = []
        j = bisect_left(self.start, a - _EPS)
        while j < len(self.start) and self.start[j] < b - _EPS:
            picks.append(
                _Pick(
                    start=self.at(max(self.start[j], a)),
                    end=self.at(min(self.end[j], b)),
                    cost=self.cost[j],
                    source=self.src[j],
                )
            )
            j += 1
        lo, hi = a - guard, b + guard
        for j in range(bisect_left(self.start, lo - _EPS), len(self.start)):
            if self.start[j] >= hi - _EPS:
                break
            if self.end[j] > lo + _EPS:
                self.used[j] = True
        return picks


class _Runs:
    """Runs already committed to a plan: merged, time-ordered, in grid minutes."""

    def __init__(self) -> None:
        self.starts: list[float] = []
        self.ends: list[float] = []

    def add(self, a: float, b: float) -> None:
        i = bisect_left(self.starts, a)
        if i > 0 and self.ends[i - 1] >= a - _EPS:
            i -= 1
            a, b = self.starts[i], max(b, self.ends[i])
            del self.starts[i], self.ends[i]
        while i < len(self.starts) and self.starts[i] <= b + _EPS:
            b = max(b, self.ends[i])
            del self.starts[i], self.ends[i]
        self.starts.insert(i, a)
        self.ends.insert(i, b)

    def fits(self, a: float, b: float, min_off: float, touch_only: bool) -> bool:
        """Whether ``[a, b)`` may join the plan beside the existing runs.

        Each neighbour must be either touching (the block extends that run, and
        a run extended stays a legal length) or at least ``min_off`` away. A
        guard that also forbids touching — marking the neighbourhood spent —
        split one cheap stretch into min_run pieces and pushed the second one
        somewhere dearer. ``touch_only`` admits extensions alone: how a tail
        shorter than ``min_run`` is kept legal.
        """
        i = bisect_right(self.starts, a)
        touches = False
        if i > 0:
            gap = a - self.ends[i - 1]
            if abs(gap) <= _EPS:
                touches = True
            elif gap < min_off - _EPS:
                return False
        if i < len(self.starts):
            gap = self.starts[i] - b
            if abs(gap) <= _EPS:
                touches = True
            elif gap < min_off - _EPS:
                return False
        return touches or not touch_only


def _plan_runs(
    grid: _Grid, params: LoadParams, target: float, guaranteed: float, unit: float
) -> list[_Pick]:
    """Select whole runs of at least ``min_run_minutes`` until the target is met.

    ``min_run`` is a hardware constraint, so it has to shape the *selection*, not
    trim it afterwards: picking the cheapest scattered slots and then deleting
    the fragments shorter than ``min_run`` throws those minutes away entirely,
    leaving the load short even when a cheap contiguous run existed elsewhere.
    ``min_off`` likewise: bridging gaps after the fact ran the load through
    expensive slots the plan never priced. Every block placed is kept at least
    ``min_off`` from the other runs, or glued onto one of them.

    Runs are taken ``unit`` (``min_run``, else one native slot) at a time,
    except that the last one absorbs the remainder so the target is still hit
    exactly. A tail smaller than ``min_run`` is only legal glued onto an
    existing run — the combined run is long enough — and is otherwise skipped
    rather than overshot, unless it is the anti-starvation floor, which is
    allowed to overshoot to stay a legal run length.

    A run already in progress (``running_minutes``) is part of the plan: it
    first gets pinned out to ``min_run`` from the window start (cap-exempt,
    overshooting the target by at most ``min_run - running``, and even with
    nothing left to deliver — see ``_mandatory_minutes``), and later blocks may
    extend it as ordinary cap-checked picks. Without this a replan two minutes
    into a run re-optimised the remainder as a fresh block elsewhere and
    switched the load off.
    """
    hard = params.min_run_minutes
    min_off = params.min_off_minutes
    runs = _Runs()
    picks: list[_Pick] = []
    acc = 0.0

    def add_run(a: float, b: float) -> None:
        runs.add(a, b)
        # Blocks start on slot boundaries, so without a cut where the off-time
        # expires the first legal restart inside a slot was never tried (an
        # hourly slot with min_off 10 offered nothing at all).
        if min_off > 0:
            grid.split(b + min_off)

    def commit(block: tuple[float, float, float]) -> None:
        nonlocal acc
        a, b, _ = block
        picks.extend(grid.take(a, b))
        add_run(a, b)
        acc += b - a

    running = params.running_minutes
    if running > _EPS:
        add_run(-running, 0.0)
        need = _mandatory_minutes(params)
        if need > _EPS:
            span = grid.contiguous_from(0.0, need)
            if span > _EPS:
                commit((0.0, span, 0.0))
    elif params.stopped_minutes is not None and min_off > 0:
        # The run that just ended, so the next one keeps min_off from it. Ends
        # strictly before the window start: "touching" it would mean restarting
        # at once, the exact short-cycle min_off exists to prevent. None (no
        # observed stop, e.g. after a restart) means no guard.
        end = -max(params.stopped_minutes, 1e-3)
        add_run(end - 1.0, end)

    def find(
        length: float, cap: float | None, in_guarantee: bool, touch_only: bool = False
    ) -> tuple[float, float, float] | None:
        limits: tuple[datetime | None, ...] = (None,)
        if in_guarantee and params.min_service_by is not None:
            limits = (params.min_service_by, None)
        for limit in limits:
            block = _best_block(
                grid,
                length,
                cap=cap,
                not_after=limit,
                runs=runs,
                min_off=min_off,
                touch_only=touch_only,
            )
            if block is not None:
                return block
        return None

    while True:
        remaining = target - acc
        if remaining <= _EPS:
            break
        in_guarantee = acc < guaranteed - _EPS
        cap = None if in_guarantee else params.cap
        if remaining >= 2 * unit - _EPS:
            length: float | None = unit
        elif remaining >= hard - _EPS:
            length = remaining  # last run absorbs the remainder exactly
        else:
            length = None  # a tail below min_run
        if length is not None:
            if in_guarantee:
                # Only the floor (rounded up to a legal run) is cap-exempt; the
                # discretionary rest is cap-checked in later steps.
                length = min(length, max(guaranteed - acc, hard))
            block = find(length, cap, in_guarantee)
            if block is None and length > unit + _EPS:
                # No single block of the remainder: take one unit and let the
                # rest glue on or go elsewhere, rather than stopping short.
                block = find(unit, cap, in_guarantee)
        else:
            glued = find(remaining, cap, in_guarantee, touch_only=True)
            alone = find(hard, None, True) if in_guarantee else None
            options = [b for b in (glued, alone) if b is not None]
            block = min(options, key=lambda b: b[2]) if options else None
        if block is None:
            break
        commit(block)
    return picks


def _best_block(
    grid: _Grid,
    block_minutes: float,
    *,
    cap: float | None = None,
    not_after: datetime | None = None,
    runs: _Runs | None = None,
    min_off: float = 0.0,
    touch_only: bool = False,
) -> tuple[float, float, float] | None:
    """Cheapest unbroken run of exactly ``block_minutes``, starting on a boundary.

    Scans by **real minutes**, not slot counts, and weights each slot's cost by
    the minutes actually taken from it. Both matter because the forecast mixes
    resolutions — the day-ahead feed is quarter-hourly while the predictor slots
    appended beyond its horizon are hourly — so "n slots per block" sizes the run
    wrong on one side of the seam, and an unweighted cost sum compares an hour
    against a quarter of an hour as if they were the same purchase.

    ``cap`` rejects a block whose minutes-weighted average exceeds it;
    ``not_after`` requires the run to finish by then. With ``runs`` the block
    must also sit legally beside them (``_Runs.fits``), and blocks ending exactly
    where a run begins are tried too, so a run can grow backwards. Returns
    ``(start, end, cost·minutes)`` in grid minutes, or ``None`` if nothing fits.
    """
    start, end, cost, used = grid.start, grid.end, grid.cost, grid.used
    n = len(start)
    limit = None if not_after is None else grid.offset(not_after)
    best: tuple[float, float, float] | None = None

    def consider(a: float, b: float, total: float) -> None:
        nonlocal best
        if limit is not None and b > limit + _EPS:
            return
        if cap is not None and total / block_minutes > cap + _EPS:
            return
        if runs is not None and not runs.fits(a, b, min_off, touch_only):
            return
        # `<` (strict) keeps the *earliest* cheapest block on ties.
        if best is None or total < best[2] - _EPS:
            best = (a, b, total)

    for i in range(n):
        if limit is not None and start[i] + block_minutes > limit + _EPS:
            break  # time-ordered: every later start ends later still
        if used[i]:
            continue
        acc = total = 0.0
        j = i
        while j < n and acc < block_minutes - _EPS:
            if used[j] or (j > i and abs(end[j - 1] - start[j]) > _EPS):
                break  # spent, or a gap / DST hole: the run would not be contiguous
            take = min(end[j] - start[j], block_minutes - acc)
            acc += take
            total += cost[j] * take
            j += 1
        if acc >= block_minutes - _EPS:
            consider(start[i], start[i] + block_minutes, total)

    for run_start in runs.starts if runs is not None else ():
        j = bisect_left(end, run_start - _EPS)
        if j >= n or abs(end[j] - run_start) > _EPS:
            continue
        acc = total = 0.0
        k = j
        while k >= 0 and acc < block_minutes - _EPS:
            if used[k] or (k < j and abs(end[k] - start[k + 1]) > _EPS):
                break
            take = min(end[k] - start[k], block_minutes - acc)
            acc += take
            total += cost[k] * take
            k -= 1
        if acc >= block_minutes - _EPS:
            consider(run_start - block_minutes, run_start, total)
    return best


def _sequential_blocks(params: LoadParams) -> list[float]:
    """The contiguous block lengths still to place (any partial run first)."""
    total = max(params.target_minutes, params.min_service_minutes)
    if total <= _EPS:
        return []
    runs = max(1, params.runs_per_day)
    per_run = params.run_minutes
    if per_run is None:
        return [total] * runs
    if per_run <= _EPS:
        return [total]
    # Whole runs still to do, the first absorbing the partial remainder of a run
    # that was interrupted (or is in progress) — the delivered minutes came off
    # the day's total, not off every block.
    # Never more blocks than runs: a floor above runs × run_minutes grows the
    # first block rather than adding a cycle nobody configured.
    count = min(max(1, math.ceil(total / per_run - _EPS)), runs)
    return [total - (count - 1) * per_run] + [per_run] * (count - 1)


def plan_sequential(slots: list[Slot], params: LoadParams) -> list[Period]:
    """Find the cheapest contiguous block(s) of ``target_minutes``.

    Supports ``runs_per_day > 1`` (e.g. run the washing machine twice): the
    best block is chosen, then its slots plus a ``min_separation_minutes``
    guard are excluded and the next best non-overlapping block is found.

    A block must respect the price ``cap`` (minutes-weighted). Only an
    outstanding min-service floor is cap-exempt: when no cap-compliant block
    fits, a block of just the floor's length (at least ``min_run``) is placed
    uncapped, preferring to finish by ``min_service_by``. A cycle already
    running (``running_minutes``) is continued in place from the window start
    to its fixed end instead of re-optimised, and the next cycle — or, after a
    stop (``stopped_minutes``), any cycle — keeps the separation guard.
    """
    blocks = _sequential_blocks(params)
    mandatory = _mandatory_minutes(params)
    if not blocks and mandatory <= _EPS:
        return []
    win = _window_slots(slots, params.window)
    if not win:
        return []

    grid = _Grid(win, params)
    floor_left = params.min_service_minutes
    results: list[Period] = []

    # Runs closer than min_off would only get bridged by the safety net into one
    # long run through unpriced slots, so the guard honours it too.
    guard = max(params.min_separation_minutes, params.min_off_minutes)

    def commit(a: float, b: float) -> None:
        nonlocal floor_left
        results.extend(_merge(grid.take(a, b, guard=guard)))
        floor_left -= b - a

    running = params.running_minutes
    if running > _EPS:
        # The cycle in progress continues in place, to ends fixed by the clock
        # so no replan can stretch it (sizing it from delivered let an idle
        # feedback re-pin a full run every minute, and a lagging delivered
        # glued the next cycle onto this one). Two parts:
        #  * mandatory — out to min_run, cap-exempt, even with nothing left
        #    (a plan that rounded a small target or floor up to min_run must
        #    not cut its own run short at the next replan);
        #  * discretionary — on to the run length, only while this cycle still
        #    has minutes to deliver, and cap-checked like any block (the floor
        #    part excepted): it's a choice, not hardware protection.
        # The in-progress cycle is blocks[0] (the one that absorbed delivered);
        # it is spent either way, and the guard after it keeps the next cycle
        # separated. Direct callers without run_minutes: the block stands in
        # for the run length.
        cycle = blocks.pop(0) if blocks else 0.0
        run_len = params.run_minutes if params.run_minutes is not None else cycle
        wanted = min(max(0.0, run_len - running), cycle)
        exempt = max(mandatory, min(wanted, max(0.0, floor_left)))
        end = grid.contiguous_from(0.0, max(mandatory, wanted))
        exempt = min(exempt, end)
        if (
            params.cap is not None
            and end > exempt + _EPS
            and grid.cost_between(exempt, end) / (end - exempt) > params.cap + _EPS
        ):
            end = exempt
        commit(0.0, end)
    elif params.stopped_minutes is not None and params.stopped_minutes < guard:
        # ``take`` cuts the grid at the guard's end, so a restart exactly when
        # the separation expires — mid-slot — is on offer. None: no guard.
        grid.take(0.0, 0.0, guard=guard - params.stopped_minutes)

    min_run = params.min_run_minutes
    # Largest first: a short partial block placed first could split the cheap
    # stretch a full run needs.
    for length in sorted(blocks, reverse=True):
        # A block shorter than min_run would only be dropped by the safety net,
        # so a small target would never run at all; round it up instead.
        length = max(length, min_run)
        block = _best_block(grid, length, cap=params.cap)
        if block is None and floor_left > _EPS:
            # Rounded up to a legal run, as in ``_plan_runs``: a floor below
            # min_run would only be dropped by the safety net.
            floor = max(min(floor_left, length), min_run)
            for limit in dict.fromkeys((params.min_service_by, None)):
                block = _best_block(grid, floor, not_after=limit)
                if block is not None:
                    break
        if block is not None:
            commit(block[0], block[1])

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
        win = _window_slots(slots, params.window)
        costs = [effective_cost(s, params.draw_kw, params.solar_enabled) for s in win]

        def gap_cost(a: datetime, b: datetime) -> tuple[float, float]:
            return _interval_cost(win, costs, a, b)

        periods = enforce_min_run_off(
            periods,
            params.min_run_minutes,
            params.min_off_minutes,
            running_minutes=params.running_minutes,
            window_start=params.window[0],
            gap_cost=gap_cost,
        )
    return periods


def _interval_cost(
    win: list[Slot], costs: list[float], a: datetime, b: datetime
) -> tuple[float, float]:
    """``(cost·minutes, covered minutes)`` of running through ``[a, b)``."""
    total = covered = 0.0
    for slot, cost in zip(win, costs, strict=True):
        lo, hi = max(slot.start, a), min(slot.end, b)
        if hi > lo:
            minutes = (hi - lo).total_seconds() / 60.0
            total += cost * minutes
            covered += minutes
    return total, covered


def enforce_min_run_off(
    periods: list[Period],
    min_run: float,
    min_off: float,
    *,
    running_minutes: float = 0.0,
    window_start: datetime | None = None,
    gap_cost: Callable[[datetime, datetime], tuple[float, float]] | None = None,
) -> list[Period]:
    """Bridge too-short off-gaps and drop too-short runs (compressor protection).

    A safety net: the planners already shape their selection around both
    limits, so on their output this is normally a no-op.

    Off-gaps shorter than ``min_off`` are filled (the load keeps running through
    them rather than short-cycling); any remaining period shorter than
    ``min_run`` is then dropped. A bridged gap is priced with ``gap_cost`` (the
    real slots it runs through) when given; without it the gap is assumed to
    cost the neighbours' blend, which understates it when it bridges dear slots.

    A period starting at ``window_start`` while the load is already running is
    the continuation of that run and is never dropped: its true length includes
    ``running_minutes``, and dropping it could only cut the run shorter still.
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
            cost_min, w_gap = gap_cost(last.end, p.start) if gap_cost and gap > 0 else (0.0, 0.0)
            total = w_last + w_p + w_gap
            last.avg_cost = (
                (last.avg_cost * w_last + p.avg_cost * w_p + cost_min) / total if total else 0.0
            )
            last.source = last.source if last.source == p.source else RunSource.MIXED
            last.end = max(last.end, p.end)
        else:
            merged.append(Period(p.start, p.end, p.source, p.avg_cost))

    def continues_run(p: Period) -> bool:
        return (
            running_minutes > _EPS
            and window_start is not None
            and abs((p.start - window_start).total_seconds()) < 1.0
        )

    return [p for p in merged if p.minutes >= min_run - _EPS or continues_run(p)]


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
