"""`number.<load>_target` — the adjustable target.

Internally the target is always **minutes** (the engine works in minutes). For a
kWh-mode load with a known power draw this entity simply *presents* and accepts
the target in **kWh** (EV charging etc.), converting at the boundary.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    SUBENTRY_TYPE_LOAD,
    TARGET_MAX,
    TARGET_MAX_KWH,
    TARGET_MIN,
    TARGET_STEP,
    TARGET_STEP_KWH,
    TARGET_TYPE_KWH,
)
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity
from .models import LoadConfig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the target number for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadTargetNumber(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadTargetNumber(LoadSchedulerEntity, NumberEntity):
    """The load's target; minutes, or kWh for an energy-mode load with draw.

    The coordinator's runtime state (in the Store) is the source of truth, so the
    value is restored across restarts without RestoreEntity.
    """

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "target")
        cfg = LoadConfig.from_subentry(subentry.data)
        # kWh display only makes sense with a known power draw.
        self._kwh = cfg.target_type == TARGET_TYPE_KWH and bool(cfg.draw_kw)
        self._draw = cfg.draw_kw or 0.0
        self._attr_native_min_value = TARGET_MIN
        if self._kwh:
            self._attr_native_unit_of_measurement = "kWh"
            self._attr_native_max_value = TARGET_MAX_KWH
            self._attr_native_step = TARGET_STEP_KWH
        else:
            self._attr_native_unit_of_measurement = "min"
            self._attr_native_max_value = TARGET_MAX
            self._attr_native_step = TARGET_STEP

    @property
    def native_value(self) -> float:
        minutes = self.coordinator.runtime[self._subentry_id].target_minutes
        if self._kwh:
            return round(minutes / 60.0 * self._draw, 2)
        return minutes

    async def async_set_native_value(self, value: float) -> None:
        minutes = value / self._draw * 60.0 if self._kwh else value
        await self.coordinator.async_set_target(self._subentry_id, minutes)
