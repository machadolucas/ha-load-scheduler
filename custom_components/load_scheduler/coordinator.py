"""Hub coordinator: read the price forecast, compute a plan per load.

One coordinator per hub config entry (stored in ``entry.runtime_data``). It
normalises the price entity into UTC slots once, then runs the pure scheduling
engine for every load subentry, keyed by ``subentry_id``. Recompute is
event-driven (price-entity change, a load's target/enable change, a periodic
safety tick) — there is no polling of an external API.

Solar excess is folded into each slot from the configured solar forecast(s)
minus a consumption baseline (an hour-of-day profile from statistics when
available, else a flat value), so the engine values solar slots at the sell
price. Excess is allocated across loads by priority, and a live divert
controller dispatches real-time surplus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import baseline as baseline_mod
from . import engine, price_source, solar_source
from .const import (
    CONF_BASELINE_ENTITY,
    CONF_BUY_PRICE_ENTITY,
    CONF_CONSUMPTION_BASELINE_W,
    CONF_FORECAST_PRICE_ENTITY,
    CONF_FORECAST_PRICE_MARGIN,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    DEFAULT_BASELINE_W,
    DEFAULT_FORECAST_PRICE_MARGIN,
    DOMAIN,
    ISSUE_PRICE_UNAVAILABLE,
    UPDATE_INTERVAL_MINUTES,
)
from .engine import Period, RunSource
from .models import LoadConfig, build_load_params
from .persistence import RuntimeStore
from .windows import next_time

# How often the recorder-backed "delivered today" measurement is recomputed.
DELIVERED_REFRESH_S = 120


def _state_on(value: str, threshold: float | None) -> bool:
    """Whether a recorded state counts as 'delivering'.

    With a power threshold (a numeric feedback sensor) the element is delivering
    at or above it; otherwise an on/off entity simply has to be ``on``.
    """
    if threshold is not None:
        try:
            return float(value) >= threshold
        except (TypeError, ValueError):
            return False
    return str(value).lower() == "on"


def _on_minutes(states, start: datetime, end: datetime, threshold: float | None) -> float:
    """Minutes a recorded entity spent 'delivering' within ``[start, end]``."""
    on_seconds = 0.0
    n = len(states)
    for i, st in enumerate(states):
        seg_start = max(st.last_changed, start)
        seg_end = states[i + 1].last_changed if i + 1 < n else end
        seg_end = min(seg_end, end)
        if seg_end <= seg_start:
            continue
        if _state_on(st.state, threshold):
            on_seconds += (seg_end - seg_start).total_seconds()
    return on_seconds / 60.0


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
    # Rationale the coordinator computes while planning (surfaced by the
    # schedule sensor for the diagnostic card; otherwise discarded).
    delivered_minutes: float = 0.0  # runtime already delivered today
    remaining_minutes: float = 0.0  # max(0, target - delivered): what was planned for
    min_service_remaining: float = 0.0  # max(0, min_service - delivered)
    boost_until: datetime | None = None  # active boost end (UTC), else None
    solar_enabled: bool = False  # competed for solar excess this tick
    scheduled_minutes: float = 0.0  # sum of the planned periods' minutes
    est_cost: float = 0.0  # rough run cost (€) when the load's draw is known

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
        self._baseline_entity: str | None = entry.data.get(CONF_BASELINE_ENTITY)
        # hour-of-day → kW, built from statistics; None until/unless available.
        self._baseline_profile: dict[int, float] | None = None
        # Auto-measured "delivered today" (minutes), keyed by subentry_id, from
        # the recorder; refreshed on a throttle. Used when a load has no explicit
        # delivered_entity but does have a feedback/controlled entity to measure.
        self._delivered_today: dict[str, float] = {}
        self._delivered_at: datetime | None = None
        # Predictor price forecast for slots beyond the real horizon.
        self._forecast_entity: str | None = entry.data.get(CONF_FORECAST_PRICE_ENTITY)
        self._forecast_margin: float = float(
            entry.data.get(CONF_FORECAST_PRICE_MARGIN, DEFAULT_FORECAST_PRICE_MARGIN)
        )
        # Per-load runtime state, keyed by subentry_id.
        self.runtime: dict[str, LoadRuntime] = {}
        self._store = RuntimeStore(hass, entry.entry_id)
        self._init_runtime()
        # Set by __init__.py once the actuator is built (for stop-backoff wiring).
        self.actuator = None

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
    def _update_price_issue(self, *, has_slots: bool) -> None:
        """Raise/clear a repair issue reflecting price-source usability."""
        if has_slots:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PRICE_UNAVAILABLE)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_PRICE_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PRICE_UNAVAILABLE,
                translation_placeholders={"entity": self._buy_entity},
            )

    @callback
    def async_setup_listeners(self) -> None:
        """Recompute whenever a watched source entity changes."""
        watched = [self._buy_entity]
        if self._sell_entity:
            watched.append(self._sell_entity)
        if self._forecast_entity:
            watched.append(self._forecast_entity)
        watched.extend(self._solar_entities)
        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, watched, self._handle_source_change)
        )
        # Rebuild the statistics baseline once a day (slow-changing).
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass, self._async_daily_baseline, hour=3, minute=30, second=0
            )
        )

    @callback
    def _async_daily_baseline(self, _now) -> None:
        self.config_entry.async_create_task(self.hass, self.async_refresh_baseline(), "ls_baseline")

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

    async def async_cancel_boost(self, subentry_id: str) -> None:
        """Cancel an active boost, persist, and recompute."""
        self.runtime[subentry_id].boost_until = None
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_request_refresh()

    def _price_slots(self) -> list[engine.Slot]:
        """Real price slots, optionally extended with the predictor's forecast.

        The optional forecast entity supplies slots *beyond* the real day-ahead
        horizon (e.g. a wind/temperature/solar-based estimate of the following
        day), with a confidence margin added to its buy price so the engine only
        defers to a forecast window when it is cheaper than the known prices by
        more than that margin. This is what lets a load bet "skip the next 24 h,
        the following 24 h will be cheaper" using 72 h weather forecasts.
        """
        buy_state = self.hass.states.get(self._buy_entity)
        real = price_source.slots_from_state(buy_state)
        if self._sell_entity:
            sell_state = self.hass.states.get(self._sell_entity)
            if sell_state is not None:
                real = price_source.merge_sell(real, price_source.slots_from_state(sell_state))
        combined = list(real) + self._forecast_slots(real)
        return [
            engine.Slot(start=fs.start, end=fs.end, buy=fs.buy, sell=fs.sell) for fs in combined
        ]

    def _forecast_slots(
        self, real: list[price_source.ForecastSlot]
    ) -> list[price_source.ForecastSlot]:
        """Predictor forecast slots beyond the real-price horizon (+ margin)."""
        if not self._forecast_entity:
            return []
        state = self.hass.states.get(self._forecast_entity)
        if state is None:
            return []
        try:
            forecast = price_source.slots_from_state(state)
        except price_source.PriceFormatError as err:
            _LOGGER.warning("Forecast price source unusable: %s", err)
            return []
        last_real = max((s.start for s in real), default=None)
        return [
            price_source.ForecastSlot(
                start=f.start,
                end=f.end,
                buy=f.buy + self._forecast_margin,
                sell=f.sell,
            )
            for f in forecast
            if last_real is None or f.start > last_real
        ]

    def _excess_by_slot(self, slots: list[engine.Slot]) -> dict[datetime, float]:
        """Predicted solar excess (kWh) per slot start = forecast PV − baseline.

        The baseline is the hour-of-day profile from statistics when available,
        else the flat fallback.
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
            s.start: max(0.0, kwh.get(s.start, 0.0) - self._baseline_kw_for(s) * (s.minutes / 60.0))
            for s in slots
        }

    def _baseline_kw_for(self, slot: engine.Slot) -> float:
        """Baseline consumption (kW) for a slot: hour profile, else the flat value."""
        if self._baseline_profile:
            hour = dt_util.as_local(slot.start).hour
            return self._baseline_profile.get(hour, self._baseline_kw)
        return self._baseline_kw

    async def async_refresh_baseline(self) -> None:
        """Rebuild the hour-of-day baseline from the consumption sensor's stats.

        Best-effort: silently keeps the flat baseline if the recorder isn't
        available or the sensor has no statistics yet.
        """
        if not self._baseline_entity:
            return
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except ImportError:
            return
        end = dt_util.utcnow()
        start = end - timedelta(days=7)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                {self._baseline_entity},
                "hour",
                None,
                {"mean"},
            )
        except Exception as err:  # noqa: BLE001 - recorder may be unavailable
            _LOGGER.debug("Baseline statistics unavailable: %s", err)
            return
        samples: list[tuple[int, float]] = []
        for row in stats.get(self._baseline_entity, []):
            mean = row.get("mean")
            if mean is None:
                continue
            ts = row["start"]
            when = (
                dt_util.utc_from_timestamp(ts)
                if isinstance(ts, int | float)
                else dt_util.as_utc(ts)
            )
            samples.append((dt_util.as_local(when).hour, float(mean)))
        if profile := baseline_mod.build_hourly_profile(samples):
            self._baseline_profile = profile

    async def _maybe_refresh_delivered(self, now_utc: datetime) -> None:
        """Refresh auto-measured delivered-today, throttled to ~2 min."""
        if (
            self._delivered_at is None
            or (now_utc - self._delivered_at).total_seconds() >= DELIVERED_REFRESH_S
        ):
            await self.async_refresh_delivered()

    async def async_refresh_delivered(self) -> None:
        """Measure today's on-time for loads without an explicit delivered sensor.

        For each such load, the on-time of its feedback element (or, lacking one,
        its controlled entity) since local midnight is read from the recorder.
        This makes dynamic-remaining work with no extra sensor, counts heating no
        matter who started it (manual / comfort automation / the scheduler), and
        resets at midnight because the query window restarts each day.
        Best-effort: silently no-ops if the recorder is unavailable.
        """
        targets: list[tuple[str, str, float | None]] = []
        for subentry_id, subentry in self.config_entry.subentries.items():
            cfg = LoadConfig.from_subentry(subentry.data)
            if cfg.delivered_entity or cfg.is_informational:
                continue
            if cfg.feedback_entity:
                targets.append((subentry_id, cfg.feedback_entity, cfg.feedback_idle_w))
            elif cfg.controlled_entity:
                targets.append((subentry_id, cfg.controlled_entity, None))
        if not targets:
            return
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                state_changes_during_period,
            )
        except ImportError:
            return
        start = dt_util.as_utc(dt_util.start_of_local_day())
        end = dt_util.utcnow()

        def _measure() -> dict[str, float]:
            out: dict[str, float] = {}
            for subentry_id, entity_id, threshold in targets:
                changes = state_changes_during_period(
                    self.hass, start, end, entity_id, include_start_time_state=True
                )
                out[subentry_id] = _on_minutes(changes.get(entity_id, []), start, end, threshold)
            return out

        try:
            self._delivered_today = await get_instance(self.hass).async_add_executor_job(_measure)
            self._delivered_at = end
        except Exception as err:  # noqa: BLE001 - recorder may be unavailable
            _LOGGER.debug("Delivered-today measurement unavailable: %s", err)

    def _solar_enabled(self, cfg: LoadConfig) -> bool:
        return cfg.allow_solar and bool(self._solar_entities)

    def _delivered_minutes(self, cfg: LoadConfig, subentry_id: str) -> float:
        """Runtime already delivered today (minutes).

        With an explicit ``delivered_entity`` the sensor's unit is interpreted
        (h/min/s, or kWh/Wh via ``draw_kw``). Otherwise the integration measures
        it itself — the on-time of the feedback element (or the controlled entity)
        since local midnight, from the recorder (see ``_measure_delivered``) — so
        no extra sensor is needed. Either way, a load that already ran enough this
        period shrinks/skips its planned run.
        """
        if not cfg.delivered_entity:
            return self._delivered_today.get(subentry_id, 0.0)
        state = self.hass.states.get(cfg.delivered_entity)
        if state is None:
            return 0.0
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return 0.0
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        if unit in ("h", "hr", "hrs", "hour", "hours"):
            return value * 60.0
        if unit in ("s", "sec", "secs", "second", "seconds"):
            return value / 60.0
        if unit in ("kwh", "wh"):
            kwh = value / 1000.0 if unit == "wh" else value
            return (kwh / cfg.draw_kw * 60.0) if cfg.draw_kw else 0.0
        return value  # minutes (explicit or assumed)

    def _failsafe_periods(self, cfg: LoadConfig, rt: LoadRuntime, now: datetime) -> list[Period]:
        """A fixed-time fallback run used when no price forecast is available."""
        if cfg.failsafe_start is None:
            return []
        minutes = max(rt.target_minutes, cfg.min_service_minutes)
        if minutes <= 0:
            return []
        start = next_time(now, cfg.failsafe_start)
        end = start + timedelta(minutes=minutes)
        return [Period(dt_util.as_utc(start), dt_util.as_utc(end), RunSource.GRID, 0.0)]

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

        self._update_price_issue(has_slots=bool(base_slots))
        residual = self._excess_by_slot(base_slots) if base_slots else {}
        now = dt_util.now()  # local: windows anchor to wall-clock
        now_utc = dt_util.utcnow()
        await self._maybe_refresh_delivered(now_utc)

        # Solar loads first, highest priority first: they claim excess before
        # lower-priority / non-solar loads, which then see only the residual.
        def order_key(item):
            cfg = LoadConfig.from_subentry(item[1].data)
            return (0 if self._solar_enabled(cfg) else 1, -cfg.priority)

        plans: dict[str, LoadPlan] = {}
        for subentry_id, subentry in sorted(self.config_entry.subentries.items(), key=order_key):
            cfg = LoadConfig.from_subentry(subentry.data)
            rt = self.runtime[subentry_id]
            solar = self._solar_enabled(cfg)
            # Measure delivered-today once and reuse it for both the plan math
            # and the rationale (it's what shrinks the target / min-service floor).
            delivered = self._delivered_minutes(cfg, subentry_id)
            plan = LoadPlan(
                target_minutes=rt.target_minutes,
                enabled=rt.enabled,
                delivered_minutes=delivered,
                remaining_minutes=max(0.0, rt.target_minutes - delivered),
                min_service_remaining=max(0.0, cfg.min_service_minutes - delivered),
                solar_enabled=solar,
            )
            periods: list[Period] = []
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
                        delivered_minutes=delivered,
                        solar_enabled=solar,
                        draw_kw=cfg.draw_kw,
                    )
                    periods = engine.compute_plan(slots, params)
                    if solar:
                        self._consume_excess(residual, base_slots, periods, cfg.draw_kw)
                else:
                    periods = self._failsafe_periods(cfg, rt, now)
                    if not periods:
                        plan.error = "no_price_data"
            # A manual boost overrides both the price plan and the enable switch.
            if rt.boost_until and now_utc < rt.boost_until:
                boost = Period(now_utc, rt.boost_until, RunSource.GRID, 0.0)
                periods = engine.merge_periods([*periods, boost])
                plan.error = None
                plan.boost_until = rt.boost_until
            plan.periods = periods
            plan.scheduled_minutes = sum(p.minutes for p in periods)
            if cfg.draw_kw:
                plan.est_cost = sum(p.minutes / 60.0 * cfg.draw_kw * p.avg_cost for p in periods)
            plans[subentry_id] = plan
        return plans


type LoadSchedulerConfigEntry = ConfigEntry[LoadSchedulerCoordinator]
