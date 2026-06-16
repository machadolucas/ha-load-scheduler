"""`number.<load>_target` — the adjustable run-time target (minutes)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SUBENTRY_TYPE_LOAD, TARGET_MAX, TARGET_MIN, TARGET_STEP
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity


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
    """The load's target runtime in minutes.

    The coordinator's runtime state (persisted to the Store) is the source of
    truth; this entity is a view + setter over it, so it is naturally restored
    across restarts without needing RestoreEntity.
    """

    _attr_native_min_value = TARGET_MIN
    _attr_native_max_value = TARGET_MAX
    _attr_native_step = TARGET_STEP
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "target")

    @property
    def native_value(self) -> float:
        return self.coordinator.runtime[self._subentry_id].target_minutes

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_target(self._subentry_id, value)
