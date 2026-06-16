"""Hub coordinator: read the price forecast, compute a plan per load.

One coordinator per hub config entry (stored in ``entry.runtime_data``). It
normalises the price entity into UTC slots once, then runs the pure scheduling
engine for every load subentry, keyed by ``subentry_id``. Recompute is
event-driven (price-entity change, a load's target/enable change, a periodic
safety tick) — there is no polling of an external API.

Solar excess is left at zero here (M2); the solar coordinator / allocator add
it in M4+. Actuation, persistence and restart catch-up are layered on in M2b.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import engine, price_source
from .const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_SELL_PRICE_ENTITY,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .engine import Period
from .models import LoadConfig, build_load_params

_LOGGER = logging.getLogger(__name__)


@dataclass
class LoadRuntime:
    """Mutable, user-adjustable state for one load (source of truth in memory).

    Persisted to ``Store`` in M2b; for now initialised from the subentry config
    and updated by the load's ``number`` / ``switch`` entities.
    """

    target_minutes: float
    enabled: bool = True


@dataclass
class LoadPlan:
    """The computed schedule for one load (the coordinator's per-load output)."""

    periods: list[Period] = field(default_factory=list)
    target_minutes: float = 0.0
    enabled: bool = True
    error: str | None = None

    def active_period(self, when: datetime) -> Period | None:
        """The period containing ``when`` (UTC), if any."""
        return next((p for p in self.periods if p.start <= when < p.end), None)

    def next_period(self, when: datetime) -> Period | None:
        """The earliest period starting at/after ``when`` (UTC), if any."""
        upcoming = [p for p in self.periods if p.end > when]
        return min(upcoming, key=lambda p: p.start) if upcoming else None


class LoadSchedulerCoordinator(DataUpdateCoordinator[dict[str, LoadPlan]]):
    """Compute and hold every load's plan for one hub."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
            config_entry=entry,
        )
        self._buy_entity: str = entry.data[CONF_BUY_PRICE_ENTITY]
        self._sell_entity: str | None = entry.data.get(CONF_SELL_PRICE_ENTITY)
        # Per-load runtime state, keyed by subentry_id.
        self.runtime: dict[str, LoadRuntime] = {}
        self._init_runtime()

    def _init_runtime(self) -> None:
        """Seed runtime state from each load subentry's stored config."""
        for subentry_id, subentry in self.config_entry.subentries.items():
            if subentry_id not in self.runtime:
                cfg = LoadConfig.from_subentry(subentry.data)
                self.runtime[subentry_id] = LoadRuntime(target_minutes=cfg.target_minutes)

    @callback
    def async_setup_listeners(self) -> None:
        """Recompute whenever a watched source entity changes."""
        watched = [self._buy_entity]
        if self._sell_entity:
            watched.append(self._sell_entity)
        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, watched, self._handle_source_change)
        )

    @callback
    def _handle_source_change(self, _event) -> None:
        self.config_entry.async_create_task(
            self.hass, self.async_request_refresh(), "ls_source_change"
        )

    async def async_set_target(self, subentry_id: str, minutes: float) -> None:
        """Update a load's target and recompute."""
        self.runtime[subentry_id].target_minutes = minutes
        await self.async_request_refresh()

    async def async_set_enabled(self, subentry_id: str, enabled: bool) -> None:
        """Enable/disable a load and recompute."""
        self.runtime[subentry_id].enabled = enabled
        await self.async_request_refresh()

    def _build_slots(self) -> list[engine.Slot]:
        """Normalise the price entity (+ optional sell entity) into UTC slots."""
        buy_state = self.hass.states.get(self._buy_entity)
        forecast = price_source.slots_from_state(buy_state)
        if self._sell_entity:
            sell_state = self.hass.states.get(self._sell_entity)
            if sell_state is not None:
                forecast = price_source.merge_sell(
                    forecast, price_source.slots_from_state(sell_state)
                )
        return [
            engine.Slot(start=fs.start, end=fs.end, buy=fs.buy, sell=fs.sell) for fs in forecast
        ]

    async def _async_update_data(self) -> dict[str, LoadPlan]:
        """Recompute every load's plan from the current forecast."""
        self._init_runtime()  # pick up newly-added subentries

        try:
            slots = self._build_slots()
        except price_source.PriceFormatError as err:
            _LOGGER.warning("Price source unusable: %s", err)
            slots = []

        now = dt_util.now()  # local: windows anchor to wall-clock
        plans: dict[str, LoadPlan] = {}
        for subentry_id, subentry in self.config_entry.subentries.items():
            cfg = LoadConfig.from_subentry(subentry.data)
            rt = self.runtime[subentry_id]
            plan = LoadPlan(target_minutes=rt.target_minutes, enabled=rt.enabled)
            if not rt.enabled:
                plans[subentry_id] = plan
                continue
            if not slots:
                plan.error = "no_price_data"
                plans[subentry_id] = plan
                continue
            params = build_load_params(cfg, now, rt.target_minutes)
            plan.periods = engine.compute_plan(slots, params)
            plans[subentry_id] = plan
        return plans


type LoadSchedulerConfigEntry = ConfigEntry[LoadSchedulerCoordinator]
