"""The Load Scheduler integration.

A hub config entry holds the shared price/solar sources, the coordinator and the
actuator; one config *subentry* per load (water heater, dishwasher, EV, floor
heating, …) carries that load's schedule and owns its device + entities.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

from homeassistant.core import HomeAssistant

from .actuation import LoadActuator
from .const import DOMAIN, PLATFORMS
from .coordinator import LoadSchedulerConfigEntry, LoadSchedulerCoordinator

_LOGGER = logging.getLogger(__name__)
_CARD_FILE = "load-scheduler-card.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILE}"


def _card_version(path: pathlib.Path) -> str:
    """A short content hash of the card file, for cache-busting its URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card as a resource (best-effort).

    The file is served with long-lived cache headers (``cache_headers=True``), so
    the injected URL carries a ``?v=<content-hash>`` query: a fixed URL would let
    browsers, the PWA service worker and the companion-app WebView keep serving a
    stale card for weeks after an update, whereas a hash that changes with the
    file forces every client to refetch. The static handler ignores the query, so
    the bare path is what's registered.

    Skipped silently when the frontend/http components aren't available (e.g. in
    the test harness); the integration works without the card.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        path = pathlib.Path(__file__).parent / "frontend" / _CARD_FILE
        version = await hass.async_add_executor_job(_card_version, path)
        await hass.http.async_register_static_paths([StaticPathConfig(_CARD_URL, str(path), True)])
        add_extra_js_url(hass, f"{_CARD_URL}?v={version}")
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
