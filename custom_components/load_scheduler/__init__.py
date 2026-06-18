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


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add/refresh the card in the Lovelace resource registry (storage mode).

    Preferred over ``add_extra_js_url``: the resource registry is fetched by the
    frontend at runtime over WebSocket, so the card survives a stale app shell —
    a CDN edge or the service worker serving cached index HTML that omits an
    injected ``<script>`` (the failure mode where the browser never even requests
    the card). This is how HACS registers its cards. Returns ``True`` when
    handled, ``False`` when the registry isn't usable (YAML resource mode, or
    Lovelace not ready) so the caller can fall back to ``add_extra_js_url``.

    Idempotent across restarts: it matches on the URL path and updates the version
    in place, so the resource list never accumulates duplicates.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import ResourceStorageCollection
    except ImportError:
        return False
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return False
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        return False  # YAML resource mode — can't be edited programmatically
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    base = url.split("?", 1)[0]
    existing = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?", 1)[0] == base
    ]
    if existing:
        keep, *dupes = existing
        if keep.get("url") != url:
            await resources.async_update_item(keep["id"], {"url": url})
        for dupe in dupes:  # collapse duplicates from older registrations
            await resources.async_delete_item(dupe["id"])
    else:
        await resources.async_create_item({"res_type": "module", "url": url})
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card (best-effort).

    The card is added to the Lovelace **resource registry** (like HACS) rather
    than injected into the index HTML via ``add_extra_js_url``: an injected
    ``<script>`` is dropped whenever a cached app shell — a CDN edge or the
    frontend service worker serving stale index HTML — is used, which makes the
    card vanish ("Custom element doesn't exist") until a hard refresh. The
    resource registry is fetched by the frontend at runtime, so it is immune;
    ``add_extra_js_url`` remains a fallback for YAML resource mode.

    The file is served with long-lived cache headers (``cache_headers=True``) and
    the URL carries a ``?v=<content-hash>`` query so a changed card refetches
    while unchanged files stay cached. The static route matches on path only, so
    the query is ignored server-side; the bare path is what's registered.

    Skipped silently when the frontend/http components aren't available (e.g. in
    the test harness); the integration works without the card.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    try:
        from homeassistant.components.http import StaticPathConfig

        path = pathlib.Path(__file__).parent / "frontend" / _CARD_FILE
        # Register the static route *first*: serving the card must never depend on
        # the cache-buster below. Computing the hash reads the file, and any
        # failure there must not leave the card unregistered (a silent 404).
        await hass.http.async_register_static_paths([StaticPathConfig(_CARD_URL, str(path), True)])
        url = _CARD_URL
        try:
            version = await hass.async_add_executor_job(_card_version, path)
            url = f"{_CARD_URL}?v={version}"
        except Exception as err:  # noqa: BLE001 - cache-bust is best-effort
            _LOGGER.debug("Load Scheduler card cache-buster skipped: %s", err)
        # Prefer the resource registry; fall back to extra-JS for YAML mode.
        if not await _async_register_resource(hass, url):
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, url)
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
