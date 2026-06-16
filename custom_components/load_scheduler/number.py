"""`number.<load>_target` — the adjustable run-time target (minutes)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
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


class LoadTargetNumber(LoadSchedulerEntity, RestoreNumber):
    """The load's target runtime in minutes; restored across restarts.

    The coordinator's runtime state is the source of truth; this entity is a
    view + setter over it. (M2b also persists it to ``Store`` so a backup
    captures it even if RestoreState is lost.)
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.coordinator.runtime[self._subentry_id].target_minutes = last.native_value
            await self.coordinator.async_request_refresh()
