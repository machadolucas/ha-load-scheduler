"""Hub coordinator: read the price forecast, compute a plan per load.

One coordinator per hub config entry (stored in ``entry.runtime_data``). It
normalises the price entity into UTC slots once, then runs the pure scheduling
engine for every load subentry, keyed by ``subentry_id``. Recompute is
event-driven (price-entity change, a load's target/enable change, a periodic
safety tick) — there is no polling of an external API.

Solar excess (M4) is folded into each slot from the configured solar forecast(s)
minus a flat consumption baseline, so the engine values solar slots at the sell
price. Cross-load allocation (M5) and real-time divert (M6) build on this.
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

from . import engine, price_source, solar_source
from .const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CONSUMPTION_BASELINE_W,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    DEFAULT_BASELINE_W,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .engine import Period, RunSource
from .models import LoadConfig, build_load_params
from .persistence import RuntimeStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class LoadRuntime:
    """Mutable, user-adjustable state for one load (source of truth in memory).

    Persisted to the Store and restored at setup; updated by the load's number /
    switch / boost-button entities.
    """

    target_minutes: float
    enabled: bool = True
    boost_until: datetime | None = None


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
        solar = entry.data.get(CONF_SOLAR_FORECAST_ENTITY) or []
        self._solar_entities: list[str] = [solar] if isinstance(solar, str) else list(solar)
        self._baseline_kw: float = (
            float(entry.data.get(CONF_CONSUMPTION_BASELINE_W, DEFAULT_BASELINE_W)) / 1000.0
        )
        # Per-load runtime state, keyed by subentry_id.
        self.runtime: dict[str, LoadRuntime] = {}
        self._store = RuntimeStore(hass, entry.entry_id)
        self._init_runtime()

    def _init_runtime(self) -> None:
        """Seed runtime state from each load subentry's stored config."""
        for subentry_id, subentry in self.config_entry.subentries.items():
            if subentry_id not in self.runtime:
                cfg = LoadConfig.from_subentry(subentry.data)
                self.runtime[subentry_id] = LoadRuntime(target_minutes=cfg.target_minutes)

    async def async_load_runtime(self) -> None:
        """Restore per-load runtime (target/enabled) from the Store at setup."""
        data = await self._store.async_load()
        for subentry_id, subentry in self.config_entry.subentries.items():
            cfg = LoadConfig.from_subentry(subentry.data)
            saved = data.get(subentry_id, {})
            boost_raw = saved.get("boost_until")
            self.runtime[subentry_id] = LoadRuntime(
                target_minutes=saved.get("target_minutes", cfg.target_minutes),
                enabled=saved.get("enabled", True),
                boost_until=dt_util.parse_datetime(boost_raw) if boost_raw else None,
            )

    def _runtime_snapshot(self) -> dict:
        return {
            sid: {
                "target_minutes": rt.target_minutes,
                "enabled": rt.enabled,
                "boost_until": rt.boost_until.isoformat() if rt.boost_until else None,
            }
            for sid, rt in self.runtime.items()
        }

    def load_config(self, subentry_id: str) -> LoadConfig:
        """The static config for a load subentry."""
        return LoadConfig.from_subentry(self.config_entry.subentries[subentry_id].data)

    @callback
    def async_setup_listeners(self) -> None:
        """Recompute whenever a watched source entity changes."""
        watched = [self._buy_entity]
        if self._sell_entity:
            watched.append(self._sell_entity)
        watched.extend(self._solar_entities)
        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, watched, self._handle_source_change)
        )

    @callback
    def _handle_source_change(self, _event) -> None:
        self.config_entry.async_create_task(
            self.hass, self.async_request_refresh(), "ls_source_change"
        )

    async def async_set_target(self, subentry_id: str, minutes: float) -> None:
        """Update a load's target, persist it, and recompute."""
        self.runtime[subentry_id].target_minutes = minutes
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_request_refresh()

    async def async_set_enabled(self, subentry_id: str, enabled: bool) -> None:
        """Enable/disable a load, persist it, and recompute."""
        self.runtime[subentry_id].enabled = enabled
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_request_refresh()

    async def async_boost(self, subentry_id: str, minutes: float) -> None:
        """Force a load to run now for ``minutes`` (overrides price + enable)."""
        self.runtime[subentry_id].boost_until = dt_util.utcnow() + timedelta(minutes=minutes)
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_request_refresh()

    def _price_slots(self) -> list[engine.Slot]:
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

    def _excess_by_slot(self, slots: list[engine.Slot]) -> dict[datetime, float]:
        """Predicted solar excess (kWh) per slot start = forecast PV − baseline.

        The flat baseline is a placeholder; a statistics-derived hour-of-day
        profile is a planned enhancement.
        """
        forecasts: list[list[solar_source.SolarPeriod]] = []
        for entity_id in self._solar_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            try:
                forecasts.append(solar_source.parse_solar(dict(state.attributes)))
            except solar_source.SolarFormatError as err:
                _LOGGER.warning("Solar source %s unusable: %s", entity_id, err)
        if not forecasts:
            return {}
        kwh = solar_source.available_kwh_by_slot(solar_source.merge_solar(*forecasts), slots)
        return {
            s.start: max(0.0, kwh.get(s.start, 0.0) - self._baseline_kw * (s.minutes / 60.0))
            for s in slots
        }

    def _solar_enabled(self, cfg: LoadConfig) -> bool:
        return cfg.allow_solar and bool(self._solar_entities)

    @staticmethod
    def _consume_excess(
        residual: dict[datetime, float],
        base_slots: list[engine.Slot],
        periods: list[Period],
        draw_kw: float | None,
    ) -> None:
        """Deduct the solar a load uses in its scheduled slots from ``residual``.

        Ensures a lower-priority load can't claim the same kWh a higher-priority
        one already took. With no known draw, the slot's excess is fully claimed.
        """
        for s in base_slots:
            if residual.get(s.start, 0.0) <= 0:
                continue
            if not any(p.start <= s.start < p.end for p in periods):
                continue
            if draw_kw is None:
                used = residual[s.start]
            else:
                used = min(residual[s.start], draw_kw * (s.minutes / 60.0))
            residual[s.start] = max(0.0, residual[s.start] - used)

    async def _async_update_data(self) -> dict[str, LoadPlan]:
        """Recompute every load's plan, allocating solar excess by priority."""
        self._init_runtime()  # pick up newly-added subentries

        try:
            base_slots = self._price_slots()
        except price_source.PriceFormatError as err:
            _LOGGER.warning("Price source unusable: %s", err)
            base_slots = []

        residual = self._excess_by_slot(base_slots) if base_slots else {}
        now = dt_util.now()  # local: windows anchor to wall-clock
        now_utc = dt_util.utcnow()

        # Solar loads first, highest priority first: they claim excess before
        # lower-priority / non-solar loads, which then see only the residual.
        def order_key(item):
            cfg = LoadConfig.from_subentry(item[1].data)
            return (0 if self._solar_enabled(cfg) else 1, -cfg.priority)

        plans: dict[str, LoadPlan] = {}
        for subentry_id, subentry in sorted(self.config_entry.subentries.items(), key=order_key):
            cfg = LoadConfig.from_subentry(subentry.data)
            rt = self.runtime[subentry_id]
            plan = LoadPlan(target_minutes=rt.target_minutes, enabled=rt.enabled)
            periods: list[Period] = []
            solar = self._solar_enabled(cfg)
            if rt.enabled:
                if base_slots:
                    slots = [
                        engine.Slot(
                            start=s.start,
                            end=s.end,
                            buy=s.buy,
                            sell=s.sell,
                            excess_kwh=residual.get(s.start, 0.0) if solar else 0.0,
                        )
                        for s in base_slots
                    ]
                    params = build_load_params(
                        cfg,
                        now,
                        rt.target_minutes,
                        solar_enabled=solar,
                        draw_kw=cfg.draw_kw,
                    )
                    periods = engine.compute_plan(slots, params)
                    if solar:
                        self._consume_excess(residual, base_slots, periods, cfg.draw_kw)
                else:
                    plan.error = "no_price_data"
            # A manual boost overrides both the price plan and the enable switch.
            if rt.boost_until and now_utc < rt.boost_until:
                boost = Period(now_utc, rt.boost_until, RunSource.GRID, 0.0)
                periods = engine.merge_periods([*periods, boost])
                plan.error = None
            plan.periods = periods
            plans[subentry_id] = plan
        return plans


type LoadSchedulerConfigEntry = ConfigEntry[LoadSchedulerCoordinator]
