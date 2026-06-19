"""Plain-language rationale for a load's computed plan (pure, HA-free).

Given the **same inputs the engine used** — the per-load price ``Slot``s (with
their priority-allocated solar excess), the ``LoadParams``, and the resulting
``Period``s — this re-derives *why* the plan looks the way it does: which price
slots qualified under the cap, how cheap they were, how much predicted solar the
load could use, and, when nothing was scheduled, the reason. The diagnostic card
turns these structured facts into prose (wording lives in the card, facts here).

Kept free of Home Assistant imports (like ``engine``/``models``) so it can be
unit-tested in isolation. The few facts the engine never sees — the load is
disabled, there's no price forecast, a manual boost is running — are filled in
by the coordinator via :func:`state_only` / the ``boost`` flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .engine import (
    LoadParams,
    Period,
    RunSource,
    ScheduleMode,
    Slot,
    _window_slots,
    effective_cost,
)

# ``skip_reason`` values — also the card's lookup keys. ``None`` means something
# was scheduled. The first group is derived purely from the engine inputs; the
# last two are set by the coordinator (states the engine never evaluated).
SKIP_ALREADY_SATISFIED = "already_satisfied"  # remaining target/floor is 0
SKIP_NO_SLOTS = "no_slots_in_window"  # no price slots overlap the window
SKIP_ALL_ABOVE_CAP = "all_above_cap"  # cheapest-first, nothing at/under the cap
SKIP_NO_CONTIGUOUS_BLOCK = "no_contiguous_block"  # sequential: no block fits
SKIP_DISABLED = "disabled"  # the enable switch is off
SKIP_NO_PRICE_DATA = "no_price_data"  # no usable price forecast

_EPS = 1e-6


@dataclass
class PlanRationale:
    """Structured, narration-ready facts about one load's plan this tick."""

    mode: str  # str(ScheduleMode): non_sequential | sequential | informational
    skip_reason: str | None  # None when periods were scheduled, else a SKIP_* code
    scheduled_minutes: float
    # Price analysis over the search window:
    window_start: datetime | None
    window_end: datetime | None
    candidate_count: int  # price slots overlapping the window
    cap: float | None
    cap_qualifying_count: int  # in-window slots with effective_cost <= cap (all if no cap)
    cheapest_cost: float | None  # min effective_cost in the window
    costliest_selected_cost: float | None  # worst avg_cost among the chosen periods
    # Solar analysis (what THIS load saw, after higher-priority loads took theirs):
    solar_enabled: bool
    solar_excess_kwh: float  # sum of excess_kwh across in-window slots
    solar_minutes: float  # scheduled minutes whose source is SOLAR/MIXED
    # Coordinator-only: a manual boost is forcing the load on right now.
    boost: bool = False


def explain(
    slots: list[Slot],
    params: LoadParams,
    periods: list[Period],
    *,
    now: datetime,
) -> PlanRationale:
    """Re-derive the rationale for an engine-computed plan (the normal path)."""
    w_start, w_end = params.window
    candidates = _window_slots(slots, params.window)
    draw, solar = params.draw_kw, params.solar_enabled

    costs = [effective_cost(s, draw, solar) for s in candidates]
    cheapest = min(costs) if costs else None
    if params.cap is None:
        qualifying = len(candidates)
    else:
        qualifying = sum(1 for c in costs if c <= params.cap + _EPS)

    scheduled_minutes = sum(p.minutes for p in periods)
    solar_minutes = sum(
        p.minutes for p in periods if p.source in (RunSource.SOLAR, RunSource.MIXED)
    )
    solar_excess_kwh = sum(s.excess_kwh for s in candidates)
    costliest_selected = max((p.avg_cost for p in periods), default=None)

    return PlanRationale(
        mode=str(params.mode),
        skip_reason=_skip_reason(params, periods, candidates),
        scheduled_minutes=scheduled_minutes,
        window_start=w_start,
        window_end=w_end,
        candidate_count=len(candidates),
        cap=params.cap,
        cap_qualifying_count=qualifying,
        cheapest_cost=cheapest,
        costliest_selected_cost=costliest_selected,
        solar_enabled=solar,
        solar_excess_kwh=solar_excess_kwh,
        solar_minutes=solar_minutes,
    )


def _skip_reason(params: LoadParams, periods: list[Period], candidates: list[Slot]) -> str | None:
    """Classify *why* nothing was scheduled (None when something was)."""
    if periods:
        return None
    # ``target_minutes``/``min_service_minutes`` reaching the engine are already
    # the *remaining* amounts (delivered-today subtracted in build_load_params),
    # so a non-positive target means the day's work is done. A load with no
    # target at all (pure solar/temp comfort-shed) also lands here; the card
    # distinguishes the two from its config.
    if max(params.target_minutes, params.min_service_minutes) <= _EPS:
        return SKIP_ALREADY_SATISFIED
    if not candidates:
        return SKIP_NO_SLOTS
    if params.mode is ScheduleMode.NON_SEQUENTIAL:
        return SKIP_ALL_ABOVE_CAP
    return SKIP_NO_CONTIGUOUS_BLOCK


def state_only(
    mode: ScheduleMode | str,
    skip_reason: str | None,
    *,
    solar_enabled: bool = False,
    boost: bool = False,
) -> PlanRationale:
    """Rationale for a state the engine never evaluated (disabled / no price)."""
    return PlanRationale(
        mode=str(mode),
        skip_reason=skip_reason,
        scheduled_minutes=0.0,
        window_start=None,
        window_end=None,
        candidate_count=0,
        cap=None,
        cap_qualifying_count=0,
        cheapest_cost=None,
        costliest_selected_cost=None,
        solar_enabled=solar_enabled,
        solar_excess_kwh=0.0,
        solar_minutes=0.0,
        boost=boost,
    )
