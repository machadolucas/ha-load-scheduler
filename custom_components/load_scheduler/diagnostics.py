"""Diagnostics for the Load Scheduler hub.

The dump is also designed to be replayed as a test fixture (the
``nordpool_planner`` pattern): it captures the hub sources, each load's config +
runtime, and the currently computed plan.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import LoadSchedulerConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LoadSchedulerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the hub config entry."""
    coordinator = entry.runtime_data
    plans = coordinator.data or {}

    loads: dict[str, Any] = {}
    for subentry_id, subentry in entry.subentries.items():
        rt = coordinator.runtime.get(subentry_id)
        plan = plans.get(subentry_id)
        loads[subentry_id] = {
            "title": subentry.title,
            "config": dict(subentry.data),
            "runtime": None
            if rt is None
            else {
                "target_minutes": rt.target_minutes,
                "enabled": rt.enabled,
                "boost_until": rt.boost_until.isoformat() if rt.boost_until else None,
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
        }

    return {
        # Full hub config so the sources, forecast and divert wiring are all
        # verifiable from a diagnostics dump (entity IDs only — nothing secret).
        "hub": dict(entry.data),
        "loads": loads,
    }
