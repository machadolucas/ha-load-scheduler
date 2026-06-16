"""Drive each load's controlled entity to match its computed plan.

Reconciliation model: at any instant the desired state of a load is simply
"is *now* inside one of its scheduled periods?". On every plan change and at
each period boundary we make the controlled entity match that.

This gives **restart catch-up** for free: ``async_start`` reconciles once on
setup, so a load whose run ended while Home Assistant was down is switched off
(the 02:59→03:01 case), and one that should be running is switched on — without
ever having seen the boundary "events".

Safety: if a load's plan carries an error (e.g. the price entity was briefly
unavailable) the actuator leaves the controlled entity untouched rather than
forcing it off on stale data.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import EVENT_RUN_ENDED, EVENT_RUN_STARTED
from .coordinator import LoadSchedulerCoordinator

_LOGGER = logging.getLogger(__name__)


class LoadActuator:
    """Reconciles controlled entities with the coordinator's plans."""

    def __init__(self, hass: HomeAssistant, coordinator: LoadSchedulerCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._unsub_boundary = None

    async def async_start(self) -> None:
        """Initial reconcile (restart catch-up) + schedule the next boundary."""
        await self._reconcile()
        self._schedule_next_boundary()

    @callback
    def async_handle_update(self) -> None:
        """Coordinator-listener callback: a plan may have changed."""
        self._schedule_next_boundary()
        self._coordinator.config_entry.async_create_task(
            self._hass, self._reconcile(), "ls_reconcile"
        )

    @callback
    def async_shutdown(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None

    def _boundaries_after(self, now: datetime) -> list[datetime]:
        return [
            t
            for plan in (self._coordinator.data or {}).values()
            for p in plan.periods
            for t in (p.start, p.end)
            if t > now
        ]

    @callback
    def _schedule_next_boundary(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        bounds = self._boundaries_after(dt_util.utcnow())
        if bounds:
            self._unsub_boundary = async_track_point_in_time(
                self._hass, self._boundary_fired, min(bounds)
            )

    @callback
    def _boundary_fired(self, _now) -> None:
        self._unsub_boundary = None
        self._coordinator.config_entry.async_create_task(
            self._hass, self._reconcile(), "ls_reconcile_boundary"
        )
        self._schedule_next_boundary()

    async def _reconcile(self) -> None:
        """Make every actuated load's controlled entity match its plan."""
        now = dt_util.utcnow()
        for subentry_id, plan in (self._coordinator.data or {}).items():
            if plan.error:
                continue  # don't actuate on stale / missing data
            cfg = self._coordinator.load_config(subentry_id)
            if cfg.is_informational or not cfg.controlled_entity:
                continue
            desired_on = plan.active_period(now) is not None
            await self._apply(subentry_id, cfg.name, cfg.controlled_entity, desired_on)

    async def _apply(self, subentry_id: str, name: str, entity_id: str, desired_on: bool) -> None:
        state = self._hass.states.get(entity_id)
        is_on = state is not None and state.state == "on"
        if desired_on == is_on:
            return
        await self._hass.services.async_call(
            "homeassistant",
            SERVICE_TURN_ON if desired_on else SERVICE_TURN_OFF,
            {"entity_id": entity_id},
            blocking=False,
        )
        self._hass.bus.async_fire(
            EVENT_RUN_STARTED if desired_on else EVENT_RUN_ENDED,
            {"subentry_id": subentry_id, "name": name, "entity_id": entity_id},
        )
