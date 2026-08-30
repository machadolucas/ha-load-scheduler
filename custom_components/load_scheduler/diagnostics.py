"""Diagnostics for the Load Scheduler hub.

The dump is also designed to be replayed as a test fixture (the
``nordpool_planner`` pattern): it captures the hub sources, each load's config +
runtime, and the currently computed plan.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import competing
from .coordinator import LoadSchedulerConfigEntry

# Enough recent foreign changes to see the pattern the verdict is claiming,
# without turning the dump into a log file.
_FOREIGN_EVENTS_SHOWN = 10


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LoadSchedulerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the hub config entry."""
    coordinator = entry.runtime_data
    plans = coordinator.data or {}

    now = dt_util.utcnow()
    tz = dt_util.get_default_time_zone()

    loads: dict[str, Any] = {}
    for subentry_id, subentry in entry.subentries.items():
        rt = coordinator.runtime.get(subentry_id)
        plan = plans.get(subentry_id)
        foreign = coordinator.foreign_log.get(subentry_id, [])
        verdict = competing.assess(foreign, now, tz)
        loads[subentry_id] = {
            "title": subentry.title,
            "config": dict(subentry.data),
            "runtime": None
            if rt is None
            else {
                "target_minutes": rt.target_minutes,
                "enabled": rt.enabled,
                "boost_until": rt.boost_until.isoformat() if rt.boost_until else None,
                # Whether the integration holds this run — the fact that decides
                # if a coexist load will ever be switched off again.
                "driven": rt.driven,
            },
            "plan": None
            if plan is None
            else {
                "error": plan.error,
                "periods": [
                    {
                        "start": p.start.isoformat(),
                        "end": p.end.isoformat(),
                        "source": str(p.source),
                        "avg_cost": round(p.avg_cost, 5),
                    }
                    for p in plan.periods
                ],
            },
            # Who else has been driving this load's switch, and the verdict the
            # repair issue is (or isn't) based on — the first thing to check
            # when a plan looks right but the load doesn't follow it.
            "foreign_changes": {
                "verdict": {
                    "competing": verdict.competing,
                    "reason": verdict.reason,
                    "count": verdict.count,
                    "scripted_count": verdict.scripted_count,
                    "in_period_count": verdict.in_period_count,
                    "recurring_days": verdict.recurring_days,
                    "last": verdict.last.isoformat() if verdict.last else None,
                },
                "events": [ev.as_dict() for ev in foreign[-_FOREIGN_EVENTS_SHOWN:]],
            },
        }

    return {
        # Full hub config so the sources, forecast and divert wiring are all
        # verifiable from a diagnostics dump (entity IDs only — nothing secret).
        "hub": dict(entry.data),
        "loads": loads,
    }
