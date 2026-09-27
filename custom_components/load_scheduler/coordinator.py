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
from . import competing, engine, price_source, rationale, solar_source
from .const import (
    CONF_BASELINE_ENTITY,
    CONF_BUY_PRICE_ENTITY,
    CONF_CONSUMPTION_BASELINE_W,
    CONF_DELIVERED_ENTITY,
    CONF_FORECAST_PRICE_ENTITY,
    CONF_FORECAST_PRICE_MARGIN,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    DEFAULT_BASELINE_W,
    DEFAULT_FORECAST_PRICE_MARGIN,
    DOMAIN,
    ISSUE_COMPETING_CONTROLLER,
    ISSUE_PRICE_GAP,
    ISSUE_PRICE_UNAVAILABLE,
    ISSUE_UNOWNED_RUN,
    UNOWNED_RUN_HOURS,
    UPDATE_INTERVAL_MINUTES,
)
from .engine import Period, RunSource
from .models import LoadConfig, build_load_params
from .persistence import RuntimeStore
from .rationale import PlanRationale
from .units import power_to_watts
from .windows import next_time

# How often the recorder-backed "delivered today" measurement is recomputed.
DELIVERED_REFRESH_S = 120


def _state_on(value: str, threshold: float | None) -> bool:
    """Whether a recorded state counts as 'delivering'.

    With a power threshold (a numeric feedback sensor) the element is delivering
    at or above it; otherwise an on/off entity simply has to be ``on``. A
    feedback entity can also be a binary_sensor (config allows either), so a
    non-numeric state with a threshold configured falls back to the same on/off
    check the live dot uses (``sensor.py:_actual_state``) instead of silently
    counting as not-delivering — "unavailable"/"unknown" still don't count.
    """
    if threshold is not None:
        try:
            return float(value) >= threshold
        except (TypeError, ValueError):
            return str(value).lower() in ("on", "heating")
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
    # True while the integration itself holds this load's run ON. It lives here,
    # in the persisted runtime, rather than in the actuator's memory because a
    # coexist load is only ever switched *off* by the integration if it was the
    # one that switched it *on*: an in-memory-only flag makes every restart or
    # reload disown a run in progress, and the load then stays on forever
    # (observed on the author's floor heating for three days). One source of
    # truth, mutated through `note_driven` so the debounced save can't drift.
    driven: bool = False


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
    rationale: PlanRationale | None = None  # narration-ready decision facts

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
        # Local date the measurement above belongs to: it must not survive
        # midnight, or the first refreshes of a new day plan against yesterday.
        self._delivered_day = None
        # Last good reading of each explicit delivered sensor, with its local
        # date, so a brief dropout doesn't re-plan the whole target.
        self._delivered_last: dict[str, tuple[object, float]] = {}
        # Source-parse failures already warned about (logged on transition only,
        # like the price gap), keyed by a short source label.
        self._source_errors: set[str] = set()
        self._config_cache: dict[str, tuple[object, LoadConfig]] = {}
        # True while the price forecast has slots but none covering *now*; kept
        # so the warning is logged on transition, not on every 5-minute tick.
        self._price_gap: bool = False
        # Predictor price forecast for slots beyond the real horizon.
        self._forecast_entity: str | None = entry.data.get(CONF_FORECAST_PRICE_ENTITY)
        self._forecast_margin: float = float(
            entry.data.get(CONF_FORECAST_PRICE_MARGIN, DEFAULT_FORECAST_PRICE_MARGIN)
        )
        # Per-load runtime state, keyed by subentry_id.
        self.runtime: dict[str, LoadRuntime] = {}
        # Foreign changes seen on each load's controlled entity (see
        # `competing.py`). Persisted alongside the runtime: the pattern only
        # emerges over days, and the user chasing it will restart HA meanwhile.
        self.foreign_log: dict[str, list[competing.ForeignEvent]] = {}
        self._store = RuntimeStore(hass, entry.entry_id)
        self._init_runtime()
        # Set by __init__.py once the actuator is built (for stop-backoff wiring).
        self.actuator = None

    def _init_runtime(self) -> None:
        """Seed runtime state from each load subentry's stored config."""
        for subentry_id in self.config_entry.subentries:
            self.runtime_for(subentry_id)

    def runtime_for(self, subentry_id: str) -> LoadRuntime:
        """The load's runtime state, seeded from its config on first sight.

        ``config_entry.subentries`` is live, so a load can appear after the
        refresh seeded runtime but before its per-load loop reaches it: two
        subentries added back to back, the second landing while the reload the
        first triggered is awaiting the recorder. Indexing ``self.runtime``
        directly then raised ``KeyError`` and failed the whole refresh, which put
        the hub (every load) into setup-retry. Seeding a default keeps every load
        scheduled; ``async_setup_entry`` reloads if the load set changed under it.
        """
        rt = self.runtime.get(subentry_id)
        if rt is None:
            cfg = LoadConfig.from_subentry(self.config_entry.subentries[subentry_id].data)
            rt = self.runtime[subentry_id] = LoadRuntime(target_minutes=cfg.target_minutes)
        return rt

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
                driven=saved.get("driven", False),
            )
            self.foreign_log[subentry_id] = [
                ev
                for raw in saved.get("foreign_events", [])
                if (ev := competing.ForeignEvent.from_dict(raw)) is not None
            ]

    def _runtime_snapshot(self) -> dict:
        return {
            sid: {
                "target_minutes": rt.target_minutes,
                "enabled": rt.enabled,
                "boost_until": rt.boost_until.isoformat() if rt.boost_until else None,
                "driven": rt.driven,
                "foreign_events": [ev.as_dict() for ev in self.foreign_log.get(sid, [])],
            }
            for sid, rt in self.runtime.items()
        }

    def load_config(self, subentry_id: str) -> LoadConfig:
        """The static config for a load subentry.

        Cached per subentry: the actuator asks for it several times per load on
        every watched state change (net-energy samples arrive every few seconds),
        and re-parsing the subentry each time is pure waste. Keyed on the data
        mapping's identity — a reconfigure replaces the subentry (and its data)
        rather than mutating it, so a stale entry can't be served.
        """
        data = self.config_entry.subentries[subentry_id].data
        cached = self._config_cache.get(subentry_id)
        if cached is not None and cached[0] is data:
            return cached[1]
        cfg = LoadConfig.from_subentry(data)
        self._config_cache[subentry_id] = (data, cfg)
        return cfg

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
    def _update_price_gap_issue(self, slots: list[engine.Slot], now_utc: datetime) -> None:
        """Flag a forecast that has slots but none covering *now*.

        A price entity anchored to a market day (or one that simply lags) can
        return a full, healthy-looking list whose first slot is still in the
        future. Every load then plans around a hole it cannot see, silently — the
        exact failure that hid a nightly 00:00-01:00 blind spot for weeks. Kept
        separate from ISSUE_PRICE_UNAVAILABLE so a dead sensor raises one issue,
        not two, and logged only on transition so a 5-minute tick can't spam.
        """
        if not slots:  # already covered by ISSUE_PRICE_UNAVAILABLE
            covered = True
        else:
            covered = any(s.start <= now_utc < s.end for s in slots)
        if covered:
            if self._price_gap:
                _LOGGER.info("Price forecast covers the current time again")
            self._price_gap = False
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PRICE_GAP)
            return
        first = min(s.start for s in slots)
        if not self._price_gap:
            _LOGGER.warning(
                "Price forecast %s has no slot covering now; earliest is %s",
                self._buy_entity,
                first.isoformat(),
            )
        self._price_gap = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_PRICE_GAP,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PRICE_GAP,
            translation_placeholders={
                "entity": self._buy_entity,
                "first_slot": dt_util.as_local(first).isoformat(timespec="minutes"),
            },
        )

    @callback
    def note_driven(self, subentry_id: str, driven: bool) -> None:
        """Record whether the integration currently holds this load's run ON.

        Called by the actuator for every ownership change. Persisted through the
        same debounced save as the rest of the runtime — writes happen only on a
        real transition (a command it actually sent, or a foreign change), never
        on a tick, so this cannot turn into a write per reconcile.
        """
        rt = self.runtime.get(subentry_id)
        if rt is None or rt.driven == driven:
            return
        rt.driven = driven
        self._store.async_schedule_save(self._runtime_snapshot)

    @callback
    def note_foreign_change(self, subentry_id: str, ev: competing.ForeignEvent) -> None:
        """Record a foreign change to a load's controlled entity and re-assess.

        Called by the actuator for every change it did not make itself. The log
        is pruned on the way in so it can never outgrow its window or its cap.
        """
        self.foreign_log[subentry_id] = competing.prune(
            [*self.foreign_log.get(subentry_id, []), ev], ev.when
        )
        self._store.async_schedule_save(self._runtime_snapshot)
        self._update_competing_issue(subentry_id)

    @callback
    def _update_competing_issue(self, subentry_id: str) -> None:
        """Raise/clear "something else is driving this load" for one load."""
        subentry = self.config_entry.subentries.get(subentry_id)
        if subentry is None:
            return
        issue_id = f"{ISSUE_COMPETING_CONTROLLER}_{subentry_id}"
        verdict = competing.assess(
            self.foreign_log.get(subentry_id, []),
            dt_util.utcnow(),
            dt_util.get_default_time_zone(),
        )
        if not verdict.competing:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        cfg = LoadConfig.from_subentry(subentry.data)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_COMPETING_CONTROLLER,
            translation_placeholders={
                "name": subentry.title,
                "entity": cfg.controlled_entity or "",
                "count": str(verdict.count),
                "scripted": str(verdict.scripted_count),
                "in_period": str(verdict.in_period_count),
                "last_change": dt_util.as_local(verdict.last).isoformat(timespec="minutes"),
            },
        )

    @callback
    def _update_unowned_run_issue(
        self, subentry_id: str, cfg: LoadConfig, plan: LoadPlan, now_utc: datetime
    ) -> None:
        """Flag a coexist load that is on with nobody left to switch it off.

        A coexist load is deliberately never switched off by the integration
        unless it started the run — so an unowned run outside every scheduled
        period ends only when whoever started it says so. If nobody does, the
        load simply stays on: no error, no wrong plan, just a heater burning for
        days (exactly how the lost-ownership bug hid). The actuator owns the
        on-since/ownership facts; this only decides when the silence is long
        enough to be worth breaking.
        """
        issue_id = f"{ISSUE_UNOWNED_RUN}_{subentry_id}"
        since = self.actuator.unowned_on_since(subentry_id) if self.actuator is not None else None
        stuck = (
            cfg.coexist
            and since is not None
            and plan.active_period(now_utc) is None
            and (now_utc - since) >= timedelta(hours=UNOWNED_RUN_HOURS)
        )
        if not stuck:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNOWNED_RUN,
            translation_placeholders={
                "name": self.config_entry.subentries[subentry_id].title,
                "entity": cfg.controlled_entity or "",
                "hours": str(UNOWNED_RUN_HOURS),
                "since": dt_util.as_local(since).isoformat(timespec="minutes"),
            },
        )

    @callback
    def _refresh_competing_issues(self) -> None:
        """Re-assess every load's foreign-change log on the periodic tick.

        Evidence decays with *time*, not with new events, so without this an
        issue raised by an automation that has since been disabled would sit
        there forever — the very silence the detector exists to break.
        """
        for subentry_id in list(self.foreign_log):
            if subentry_id not in self.config_entry.subentries:
                # Load removed: forget its history and the issue it left behind.
                del self.foreign_log[subentry_id]
                ir.async_delete_issue(
                    self.hass, DOMAIN, f"{ISSUE_COMPETING_CONTROLLER}_{subentry_id}"
                )
                ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_UNOWNED_RUN}_{subentry_id}")
                continue
            self.foreign_log[subentry_id] = competing.prune(
                self.foreign_log[subentry_id], dt_util.utcnow()
            )
            self._update_competing_issue(subentry_id)

    @callback
    def async_setup_listeners(self) -> None:
        """Recompute whenever a watched source entity changes."""
        watched = [self._buy_entity]
        if self._sell_entity:
            watched.append(self._sell_entity)
        if self._forecast_entity:
            watched.append(self._forecast_entity)
        watched.extend(self._solar_entities)
        # An explicit delivered sensor reaching the target should stop the run
        # now, not at the next 5-minute tick (the refresh is debounced).
        for subentry in self.config_entry.subentries.values():
            if delivered := subentry.data.get(CONF_DELIVERED_ENTITY):
                watched.append(delivered)
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

    # User actions refresh immediately rather than through the 10 s debouncer:
    # a boost pressed (or a load disabled) just after any other refresh would
    # otherwise take up to 10 s to reach the relay. They're rare and cheap.
    async def async_set_target(self, subentry_id: str, minutes: float) -> None:
        """Update a load's target, persist it, and recompute."""
        self.runtime_for(subentry_id).target_minutes = minutes
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_refresh()

    async def async_set_enabled(self, subentry_id: str, enabled: bool) -> None:
        """Enable/disable a load, persist it, and recompute."""
        self.runtime_for(subentry_id).enabled = enabled
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_refresh()

    async def async_boost(self, subentry_id: str, minutes: float) -> None:
        """Force a load to run now for ``minutes`` (overrides price + enable)."""
        self.runtime_for(subentry_id).boost_until = dt_util.utcnow() + timedelta(minutes=minutes)
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_refresh()

    async def async_cancel_boost(self, subentry_id: str) -> None:
        """Cancel an active boost, persist, and recompute."""
        self.runtime_for(subentry_id).boost_until = None
        self._store.async_schedule_save(self._runtime_snapshot)
        await self.async_refresh()

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
                # An optional sell feed going dead must not throw away a healthy
                # buy forecast (and with it every load's plan): keep buy-only
                # slots, whose embedded sell (if any) still stands.
                try:
                    sell = price_source.slots_from_state(sell_state)
                except price_source.PriceFormatError as err:
                    self._note_source_error("sell", err)
                else:
                    self._clear_source_error("sell")
                    real = price_source.merge_sell(real, sell)
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
            self._note_source_error("forecast", err)
            return []
        self._clear_source_error("forecast")
        # Extend from the real feed's *end*, not its last start: with an hourly
        # real tail and a quarter-hourly forecast, the forecast's :15/:30/:45
        # slots would otherwise overlap the last real hour and count the same
        # wall time twice. A forecast slot straddling the seam is clipped.
        real_end = max((s.end for s in real), default=None)
        out: list[price_source.ForecastSlot] = []
        for f in forecast:
            start = f.start if real_end is None else max(f.start, real_end)
            if f.end <= start:
                continue
            out.append(
                price_source.ForecastSlot(
                    start=start, end=f.end, buy=f.buy + self._forecast_margin, sell=f.sell
                )
            )
        return out

    @callback
    def _note_source_error(self, source: str, err: Exception) -> None:
        """Warn once when an optional source becomes unusable (then debug)."""
        if source in self._source_errors:
            _LOGGER.debug("%s source still unusable: %s", source, err)
            return
        self._source_errors.add(source)
        _LOGGER.warning("%s source unusable: %s", source, err)

    @callback
    def _clear_source_error(self, source: str) -> None:
        if source in self._source_errors:
            self._source_errors.discard(source)
            _LOGGER.info("%s source usable again", source)

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
                self._note_source_error(f"Solar {entity_id}", err)
            else:
                self._clear_source_error(f"Solar {entity_id}")
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
        today = dt_util.as_local(now_utc).date()
        if self._delivered_day is not None and self._delivered_day != today:
            # A new local day: yesterday's on-time must not be planned against,
            # even for the one refresh until the query below lands (or if the
            # recorder is failing and it never does).
            self._delivered_today = {}
            self._delivered_day = today
            self._delivered_at = None
        if (
            self._delivered_at is None
            or (now_utc - self._delivered_at).total_seconds() >= DELIVERED_REFRESH_S
        ):
            await self.async_refresh_delivered()

    def _delivered_targets(self) -> list[tuple[str, str, float | None]]:
        """Which entity to measure on-time from, per load, and its threshold.

        Prefers the feedback entity (it isolates the element's own draw from
        e.g. a shared circulation pump on the same switch). But a feedback
        entity can go dead — orphaned after a device re-add, battery-out, zwave
        drop — and its *current* state is the only cheap liveness check available
        before paying for a recorder history query; a feedback sensor stuck on
        "unavailable"/"unknown" would otherwise measure 0 delivered all day even
        though the load ran fine. In that case fall back to the controlled
        entity (switch on/off, no threshold) for this refresh.
        """
        targets: list[tuple[str, str, float | None]] = []
        for subentry_id, subentry in self.config_entry.subentries.items():
            cfg = LoadConfig.from_subentry(subentry.data)
            if cfg.delivered_entity or cfg.is_informational:
                continue
            if cfg.feedback_entity:
                fb_state = self.hass.states.get(cfg.feedback_entity)
                if fb_state is None or fb_state.state in ("unknown", "unavailable"):
                    if cfg.controlled_entity:
                        targets.append((subentry_id, cfg.controlled_entity, None))
                    continue
                # The recorder rows are read without attributes, so express the
                # W threshold in the sensor's *current* unit once here: a kW
                # feedback sensor compared against a W threshold read idle all day.
                threshold = cfg.feedback_idle_w
                if threshold is not None:
                    per_unit = power_to_watts(1.0, fb_state.attributes.get("unit_of_measurement"))
                    threshold = threshold / per_unit if per_unit else threshold
                targets.append((subentry_id, cfg.feedback_entity, threshold))
            elif cfg.controlled_entity:
                targets.append((subentry_id, cfg.controlled_entity, None))
        return targets

    async def async_refresh_delivered(self) -> None:
        """Measure today's on-time for loads without an explicit delivered sensor.

        For each such load, the on-time of its feedback element (or, lacking one,
        its controlled entity) since local midnight is read from the recorder.
        This makes dynamic-remaining work with no extra sensor, counts heating no
        matter who started it (manual / comfort automation / the scheduler), and
        resets at midnight because the query window restarts each day. A feedback
        entity that's currently unavailable/unknown falls back to the controlled
        entity for this refresh (see ``_delivered_targets``).
        Best-effort: silently no-ops if the recorder is unavailable.
        """
        targets = self._delivered_targets()
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
                # Only state + last_changed are read, so skip the attribute join:
                # this runs every couple of minutes over the whole day per load,
                # and a power feedback sensor has thousands of rows by evening.
                changes = state_changes_during_period(
                    self.hass,
                    start,
                    end,
                    entity_id,
                    no_attributes=True,
                    include_start_time_state=True,
                )
                out[subentry_id] = _on_minutes(changes.get(entity_id, []), start, end, threshold)
            return out

        try:
            self._delivered_today = await get_instance(self.hass).async_add_executor_job(_measure)
            self._delivered_at = end
            self._delivered_day = dt_util.as_local(end).date()
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
        today = dt_util.as_local(dt_util.utcnow()).date()
        try:
            value = float(state.state) if state is not None else None
        except (TypeError, ValueError):
            value = None
        if value is None:
            # A dropout (unavailable/unknown) must not read as "nothing delivered
            # yet" and re-plan the whole target: hold today's last good value.
            last = self._delivered_last.get(subentry_id)
            return last[1] if last is not None and last[0] == today else 0.0
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        if unit in ("h", "hr", "hrs", "hour", "hours"):
            minutes = value * 60.0
        elif unit in ("s", "sec", "secs", "second", "seconds"):
            minutes = value / 60.0
        elif unit in ("kwh", "wh"):
            kwh = value / 1000.0 if unit == "wh" else value
            minutes = (kwh / cfg.draw_kw * 60.0) if cfg.draw_kw else 0.0
        else:
            minutes = value  # minutes (explicit or assumed)
        self._delivered_last[subentry_id] = (today, minutes)
        return minutes

    def _failsafe_periods(self, cfg: LoadConfig, rt: LoadRuntime, now: datetime) -> list[Period]:
        """A fixed-time fallback run used when no price forecast is available."""
        if cfg.failsafe_start is None:
            return []
        minutes = max(rt.target_minutes, cfg.min_service_minutes)
        if minutes <= 0:
            return []
        # Keep today's occurrence while it is still running: `next_time` only
        # looks forward, so once the start passed the next refresh would move
        # the run to tomorrow and the actuator would switch it off minutes in.
        # (Duration is added in UTC so a DST night keeps its real length.)
        today = dt_util.as_utc(
            datetime.combine(dt_util.as_local(now).date(), cfg.failsafe_start, now.tzinfo)
        )
        if today <= dt_util.as_utc(now) < today + timedelta(minutes=minutes):
            start = today
        else:
            start = dt_util.as_utc(next_time(now, cfg.failsafe_start))
        end = start + timedelta(minutes=minutes)
        return [Period(start, end, RunSource.GRID, 0.0)]

    @staticmethod
    def _avg_buy(slots: list[engine.Slot], start: datetime, end: datetime) -> float:
        """Minutes-weighted buy price over ``[start, end)`` (0 when uncovered).

        A boost runs at grid price whatever the plan thought; costing it at 0
        made the schedule's est_cost understate exactly the runs the user forced.
        """
        total = weighted = 0.0
        for s in slots:
            overlap = (min(s.end, end) - max(s.start, start)).total_seconds()
            if overlap > 0:
                total += overlap
                weighted += overlap * s.buy
        return weighted / total if total else 0.0

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

        Matched by interval *overlap*, not by the period starting on the slot
        boundary: the engine clips the in-progress slot to ``now``, so a run that
        is already under way starts mid-slot and a start-equality test would let
        its solar be handed out twice. Only the overlapping minutes are charged.
        """
        for s in base_slots:
            if residual.get(s.start, 0.0) <= 0:
                continue
            overlap = min(
                s.minutes,
                sum(
                    max(0.0, (min(p.end, s.end) - max(p.start, s.start)).total_seconds())
                    for p in periods
                )
                / 60.0,
            )
            if overlap <= 0:
                continue
            if draw_kw is None:
                used = residual[s.start]
            else:
                used = min(residual[s.start], draw_kw * (overlap / 60.0))
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
        self._update_price_gap_issue(base_slots, now_utc)
        self._refresh_competing_issues()
        await self._maybe_refresh_delivered(now_utc)

        # Solar loads first, highest priority first: they claim excess before
        # lower-priority / non-solar loads, which then see only the residual.
        def order_key(item):
            cfg = LoadConfig.from_subentry(item[1].data)
            return (0 if self._solar_enabled(cfg) else 1, -cfg.priority)

        plans: dict[str, LoadPlan] = {}
        for subentry_id, subentry in sorted(self.config_entry.subentries.items(), key=order_key):
            cfg = LoadConfig.from_subentry(subentry.data)
            rt = self.runtime_for(subentry_id)
            solar = self._solar_enabled(cfg)
            # Measure delivered-today once and reuse it for both the plan math
            # and the rationale (it's what shrinks the target / min-service floor).
            delivered = self._delivered_minutes(cfg, subentry_id)
            multi_run = cfg.mode is not engine.ScheduleMode.NON_SEQUENTIAL and cfg.runs_per_day > 1
            plan = LoadPlan(
                target_minutes=rt.target_minutes,
                enabled=rt.enabled,
                delivered_minutes=delivered,
                # Multi-run sequential loads plan against the day's total
                # (runs × target), so report what's left of that, not of one run.
                remaining_minutes=max(
                    0.0,
                    rt.target_minutes * (cfg.runs_per_day if multi_run else 1) - delivered,
                ),
                min_service_remaining=max(0.0, cfg.min_service_minutes - delivered),
                solar_enabled=solar,
            )
            periods: list[Period] = []
            rat: PlanRationale | None = None
            if not rt.enabled:
                rat = rationale.state_only(cfg.mode, rationale.SKIP_DISABLED, solar_enabled=solar)
            elif base_slots:
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
                # How long the current on-run has lasted, so a replan mid-run
                # continues it instead of cutting it short of min_run (the engine
                # pins it). Informational loads are never driven: an always-on
                # plug would otherwise pin their display block to "now".
                # And how long ago it last stopped, so the stateless replan keeps
                # min_off / min_separation from that stop.
                on_since = off_since = None
                if cfg.controlled_entity and not cfg.is_informational:
                    st = self.hass.states.get(cfg.controlled_entity)
                    run_on_since = getattr(self.actuator, "_run_on_since", None)
                    if run_on_since is not None:
                        on_since = run_on_since(subentry_id, cfg.controlled_entity)
                    elif st is not None and st.state == "on":
                        on_since = st.last_changed
                    # Only a stop the actuator actually *observed* counts: HA
                    # re-stamps `last_changed` on restart, so a load that had been
                    # off for hours would look freshly stopped and its first run
                    # would be held back by min_off/separation for no reason.
                    if st is not None and st.state == "off" and self.actuator is not None:
                        off_since = getattr(self.actuator, "_off_since", {}).get(subentry_id)
                params = build_load_params(
                    cfg,
                    now,
                    rt.target_minutes,
                    delivered_minutes=delivered,
                    solar_enabled=solar,
                    draw_kw=cfg.draw_kw,
                    running_minutes=(
                        max(0.0, (now_utc - on_since).total_seconds() / 60.0) if on_since else 0.0
                    ),
                    stopped_minutes=(
                        max(0.0, (now_utc - off_since).total_seconds() / 60.0)
                        if off_since
                        else None
                    ),
                )
                periods = engine.compute_plan(slots, params)
                rat = rationale.explain(slots, params, periods, now=now)
            else:
                periods = self._failsafe_periods(cfg, rt, now)
                if not periods:
                    plan.error = "no_price_data"
                rat = rationale.state_only(
                    cfg.mode, rationale.SKIP_NO_PRICE_DATA, solar_enabled=solar
                )
            # A manual boost overrides both the price plan and the enable switch.
            if rt.boost_until and now_utc < rt.boost_until:
                boost = Period(
                    now_utc,
                    rt.boost_until,
                    RunSource.GRID,
                    self._avg_buy(base_slots, now_utc, rt.boost_until),
                )
                periods = engine.merge_periods([*periods, boost])
                plan.error = None
                plan.boost_until = rt.boost_until
                if rat is not None:
                    rat.boost = True
            # Claim solar only once the boost is folded in: a boosted run through
            # a solar slot uses that excess too, and deducting before the merge
            # let a lower-priority load plan on the same forecast surplus.
            if solar and base_slots and periods:
                self._consume_excess(residual, base_slots, periods, cfg.draw_kw)
            plan.periods = periods
            plan.scheduled_minutes = sum(p.minutes for p in periods)
            if cfg.draw_kw:
                plan.est_cost = sum(p.minutes / 60.0 * cfg.draw_kw * p.avg_cost for p in periods)
            plan.rationale = rat
            self._update_unowned_run_issue(subentry_id, cfg, plan, now_utc)
            plans[subentry_id] = plan
        return plans


type LoadSchedulerConfigEntry = ConfigEntry[LoadSchedulerCoordinator]
