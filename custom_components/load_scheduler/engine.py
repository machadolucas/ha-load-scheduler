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
    """Slots that *overlap* ``[window[0], window[1])``, time-ordered.

    Overlap (not just ``start`` inside) so the slot currently in progress — which
    began just before ``window[0]`` when that is clamped to ``now`` — is still
    eligible. Without this, a load that should be running *right now* would never
    be scheduled until the next slot boundary.
    """
    w_start, w_end = window
    inside = [s for s in slots if s.end > w_start and s.start < w_end]
    inside.sort(key=lambda s: s.start)
    return inside


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
    """
    target = max(params.target_minutes, params.min_service_minutes)
    if target <= 0:
        return []
    guaranteed = params.min_service_minutes
    candidates = _window_slots(slots, params.window)
    # Cheapest first; ties broken by start time for determinism.
    candidates.sort(
        key=lambda s: (effective_cost(s, params.draw_kw, params.solar_enabled), s.start)
    )

    picks: list[_Pick] = []
    acc = 0.0
    for slot in candidates:
        if acc >= target - _EPS:
            break
        cost = effective_cost(slot, params.draw_kw, params.solar_enabled)
        within_guarantee = acc < guaranteed - _EPS
        # Cheapest slots fill the guarantee first; beyond it the cap applies.
        if not within_guarantee and params.cap is not None and cost > params.cap:
            continue
        picks.append(
            _Pick(
                start=slot.start,
                end=slot.end,
                cost=cost,
                source=_slot_source(slot, params.draw_kw, params.solar_enabled),
            )
        )
        acc += slot.minutes

    # Trim the overshoot off the last-added (most expensive) pick.
    overshoot = acc - target
    if overshoot > _EPS and picks:
        last = picks[-1]
        last.end = last.end - timedelta(minutes=overshoot)

    return _merge(picks)


def _contiguous(block: list[Slot]) -> bool:
    """True if the slots form an unbroken chain (no gaps / DST holes)."""
    for a, b in zip(block, block[1:], strict=False):
        if abs((a.end - b.start).total_seconds()) > _EPS:
            return False
    return True


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
    slot_minutes = win[0].minutes
    if slot_minutes <= 0:
        return []
    needed = max(1, math.ceil(block_minutes / slot_minutes - _EPS))

    used = [False] * len(win)
    results: list[Period] = []

    for _ in range(max(1, params.runs_per_day)):
        best_i: int | None = None
        best_sum = math.inf
        for i in range(0, len(win) - needed + 1):
            block = win[i : i + needed]
            if any(used[i : i + needed]):
                continue
            if not _contiguous(block):
                continue
            total = sum(effective_cost(s, params.draw_kw, params.solar_enabled) for s in block)
            # `<` (strict) keeps the *earliest* cheapest block on ties.
            if total < best_sum - _EPS:
                best_sum = total
                best_i = i
        if best_i is None:
            break

        block = win[best_i : best_i + needed]
        start = block[0].start
        end = start + timedelta(minutes=block_minutes)  # trim to exact length
        picks = [
            _Pick(
                start=s.start,
                end=min(s.end, end),
                cost=effective_cost(s, params.draw_kw, params.solar_enabled),
                source=_slot_source(s, params.draw_kw, params.solar_enabled),
            )
            for s in block
            if s.start < end
        ]
        results.extend(_merge(picks))

        # Mark the block plus the separation guard as used.
        sep = timedelta(minutes=params.min_separation_minutes)
        guard_start = start - sep
        guard_end = end + sep
        for j, s in enumerate(win):
            if s.start < guard_end and s.end > guard_start:
                used[j] = True

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
