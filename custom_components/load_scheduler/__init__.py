"""The Load Scheduler integration.

A hub config entry holds the shared price/solar sources, the coordinator and the
actuator; one config *subentry* per load (water heater, dishwasher, EV, floor
heating, …) carries that load's schedule and owns its device + entities.
"""

from __future__ import annotations

import logging
import pathlib

from homeassistant.core import HomeAssistant

from .actuation import LoadActuator
from .const import DOMAIN, PLATFORMS
from .coordinator import LoadSchedulerConfigEntry, LoadSchedulerCoordinator

_LOGGER = logging.getLogger(__name__)
_CARD_URL = f"/{DOMAIN}/load-scheduler-card.js"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card as a resource (best-effort).

    Skipped silently when the frontend/http components aren't available (e.g. in
    the test harness); the integration works without the card.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        path = str(pathlib.Path(__file__).parent / "frontend" / "load-scheduler-card.js")
        await hass.http.async_register_static_paths([StaticPathConfig(_CARD_URL, path, True)])
        add_extra_js_url(hass, _CARD_URL)
        hass.data[f"{DOMAIN}_card"] = True
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Load Scheduler card not registered: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> bool:
    """Set up Load Scheduler from the hub config entry."""
    coordinator = LoadSchedulerCoordinator(hass, entry)
    await coordinator.async_load_runtime()
    await coordinator.async_refresh_baseline()
    coordinator.async_setup_listeners()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await _async_register_frontend(hass)

    # Actuation: drive controlled entities + reconcile on startup (catch-up).
    actuator = LoadActuator(hass, coordinator)
    coordinator.actuator = actuator  # let entities signal an explicit stop
    await actuator.async_start()
    entry.async_on_unload(actuator.async_shutdown)
    entry.async_on_unload(coordinator.async_add_listener(actuator.async_handle_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> bool:
    """Unload the hub config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> None:
    """Reload on options/subentry changes (picks up added/removed loads)."""
    await hass.config_entries.async_reload(entry.entry_id)
