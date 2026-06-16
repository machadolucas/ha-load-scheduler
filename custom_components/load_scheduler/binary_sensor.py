"""`binary_sensor.<load>_running` — on while the load is scheduled to run."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import SUBENTRY_TYPE_LOAD
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the running binary_sensor for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadRunningBinarySensor(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadRunningBinarySensor(LoadSchedulerEntity, BinarySensorEntity):
    """True while the load is actually running.

    For an actuating load this reflects the **controlled entity's real state**,
    so a manual override (or any out-of-band change) shows the truth rather than
    the intended schedule. For an informational load (no controlled entity) it
    falls back to "is *now* inside a scheduled period".
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "running")
        self._unsub_boundary = None
        self._unsub_controlled = None

    @property
    def is_on(self) -> bool:
        cfg = self.coordinator.load_config(self._subentry_id)
        if cfg.controlled_entity:
            state = self.coordinator.hass.states.get(cfg.controlled_entity)
            return state is not None and state.state == "on"
        plan = self._plan
        return bool(plan and plan.active_period(dt_util.utcnow()))

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._plan
        if not plan:
            return {}
        now = dt_util.utcnow()
        active = plan.active_period(now)
        nxt = plan.next_period(now)
        return {
            "current_period_end": dt_util.as_local(active.end).isoformat() if active else None,
            "next_start": dt_util.as_local(nxt.start).isoformat() if nxt and not active else None,
        }

    # ── boundary-accurate updates ────────────────────────────────────────────
    # The plan only changes on a coordinator refresh, but the *state* flips at
    # each period start/end. Schedule a precise wake-up at the next boundary
    # instead of relying on the periodic refresh.

    def _next_boundary(self, now: datetime) -> datetime | None:
        plan = self._plan
        if not plan:
            return None
        bounds = [t for p in plan.periods for t in (p.start, p.end) if t > now]
        return min(bounds) if bounds else None

    @callback
    def _schedule_next_boundary(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        when = self._next_boundary(dt_util.utcnow())
        if when is not None:
            self._unsub_boundary = async_track_point_in_time(self.hass, self._boundary_fired, when)

    @callback
    def _boundary_fired(self, _now) -> None:
        self._unsub_boundary = None
        self.async_write_ha_state()
        self._schedule_next_boundary()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_next_boundary()
        super()._handle_coordinator_update()

    @callback
    def _controlled_changed(self, _event) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_next_boundary()
        cfg = self.coordinator.load_config(self._subentry_id)
        if cfg.controlled_entity:
            self._unsub_controlled = async_track_state_change_event(
                self.hass, [cfg.controlled_entity], self._controlled_changed
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        if self._unsub_controlled is not None:
            self._unsub_controlled()
            self._unsub_controlled = None
