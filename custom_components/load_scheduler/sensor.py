"""`sensor.<load>_schedule` — next run time + the upcoming periods.

A single merged sensor (per the "avoid entity bloat" goal): its state is the
next run's start (a timestamp), and its attributes carry the full upcoming
schedule, the active period, cost and status — everything a dashboard/card
needs in one entity.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import SUBENTRY_TYPE_LOAD
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity
from .rationale import PlanRationale
from .units import power_to_watts


def _rationale_attr(r: PlanRationale | None) -> dict | None:
    """Serialise the plan rationale into JSON-friendly attribute values."""
    if r is None:
        return None
    return {
        "mode": r.mode,
        "skip_reason": r.skip_reason,
        "scheduled_minutes": round(r.scheduled_minutes, 1),
        "window_start": (dt_util.as_local(r.window_start).isoformat() if r.window_start else None),
        "window_end": dt_util.as_local(r.window_end).isoformat() if r.window_end else None,
        "candidate_count": r.candidate_count,
        "cap": r.cap,
        "cap_qualifying_count": r.cap_qualifying_count,
        "cheapest_cost": round(r.cheapest_cost, 5) if r.cheapest_cost is not None else None,
        "costliest_selected_cost": (
            round(r.costliest_selected_cost, 5) if r.costliest_selected_cost is not None else None
        ),
        "solar_enabled": r.solar_enabled,
        "solar_excess_kwh": round(r.solar_excess_kwh, 3),
        "solar_minutes": round(r.solar_minutes, 1),
        "min_service_by": (
            dt_util.as_local(r.min_service_by).isoformat() if r.min_service_by else None
        ),
        "boost": r.boost,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the schedule sensor for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadScheduleSensor(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadScheduleSensor(LoadSchedulerEntity, SensorEntity):
    """State = next run start; attributes = the full upcoming plan."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # The bulky, slow-to-change attributes don't belong in the recorder: the
    # state (next-run timestamp) keeps its history, but the upcoming-periods
    # list and the static config summary would bloat every recorded row.
    _unrecorded_attributes = frozenset({"periods", "config", "rationale"})

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "schedule")
        self._unsub_state = None
        self._unsub_boundary = None
        # The bulky attributes only change with the plan, but the state is also
        # written on every controlled/feedback change and period edge; serialize
        # them once per coordinator update, not on every write.
        self._static_attrs: dict | None = None
        # The (controlled_on, heating) last published, so a feedback power sensor
        # sampling every few seconds only writes when the dot would change.
        self._published_actual: tuple[bool | None, bool | None] | None = None

    @property
    def native_value(self) -> datetime | None:
        plan = self._plan
        if not plan:
            return None
        nxt = plan.next_period(dt_util.utcnow())
        return nxt.start if nxt else None

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._plan
        if not plan:
            return {}
        now = dt_util.utcnow()
        active = plan.active_period(now)
        controlled_on, heating = self._actual_state()
        self._published_actual = (controlled_on, heating)
        if self._static_attrs is None:
            self._static_attrs = self._serialize_static(plan)
        static = self._static_attrs
        return {
            "periods": static["periods"],
            "running": active is not None,
            # Actual controlled-entity / element state, for the card's dot:
            # active = the switch is on; heating = the element is actually drawing
            # (power ≥ idle threshold). None when there's no such entity to read.
            "active": controlled_on,
            "heating": heating,
            "current_period_end": dt_util.as_local(active.end).isoformat() if active else None,
            "target_minutes": plan.target_minutes,
            "enabled": plan.enabled,
            "status": plan.error or ("disabled" if not plan.enabled else "ok"),
            # Rationale the diagnostic card renders: how the target was reduced by
            # what already ran today, what got scheduled, and the rough cost.
            "delivered_minutes": round(plan.delivered_minutes, 1),
            "remaining_minutes": round(plan.remaining_minutes, 1),
            "min_service_remaining": round(plan.min_service_remaining, 1),
            "scheduled_minutes": round(plan.scheduled_minutes, 1),
            "est_cost": round(plan.est_cost, 4),
            "solar_enabled": plan.solar_enabled,
            "boost_until": (
                dt_util.as_local(plan.boost_until).isoformat() if plan.boost_until else None
            ),
            # Structured decision facts the diagnostic card narrates in prose.
            "rationale": static["rationale"],
            # Static configuration summary (the load's "type" + its wiring), so
            # the card can explain the rules without a second data source. Flat
            # and low-churn; excluded from the recorder via _unrecorded_attributes.
            "config": static["config"],
        }

    def _serialize_static(self, plan) -> dict:
        """The plan-/config-derived attributes, which only change with the plan."""
        cfg = self.coordinator.load_config(self._subentry_id)
        return {
            "periods": [
                {
                    "start": dt_util.as_local(p.start).isoformat(),
                    "end": dt_util.as_local(p.end).isoformat(),
                    "source": str(p.source),
                    "avg_cost": round(p.avg_cost, 5),
                }
                for p in plan.periods
            ],
            "rationale": _rationale_attr(plan.rationale),
            "config": {
                "mode": str(cfg.mode),
                "target_type": cfg.target_type,
                "priority": cfg.priority,
                "cap": cfg.cap,
                "min_service_minutes": cfg.min_service_minutes,
                "min_run_minutes": cfg.min_run_minutes,
                "min_off_minutes": cfg.min_off_minutes,
                "runs_per_day": cfg.runs_per_day,
                "horizon_hours": cfg.horizon_hours,
                "earliest": cfg.earliest.isoformat() if cfg.earliest else None,
                "deadline": cfg.deadline.isoformat() if cfg.deadline else None,
                "allow_solar": cfg.allow_solar,
                "coexist": cfg.coexist,
                "draw_kw": cfg.draw_kw,
                "temp_min": cfg.temp_min if cfg.temp_entity else None,
                "controlled_entity": cfg.controlled_entity,
                "feedback_entity": cfg.feedback_entity,
                # The card thresholds the feedback power history to split
                # heating vs idle on its activity timeline.
                "feedback_idle_w": cfg.feedback_idle_w if cfg.feedback_entity else None,
                "temp_entity": cfg.temp_entity,
                "delivered_entity": cfg.delivered_entity,
            },
        }

    def _actual_state(self) -> tuple[bool | None, bool | None]:
        """(controlled_on, heating) read live from the load's entities."""
        cfg = self.coordinator.load_config(self._subentry_id)
        controlled_on: bool | None = None
        heating: bool | None = None
        if cfg.controlled_entity:
            st = self.coordinator.hass.states.get(cfg.controlled_entity)
            controlled_on = st is not None and st.state == "on"
        if cfg.feedback_entity:
            fb = self.coordinator.hass.states.get(cfg.feedback_entity)
            if fb is not None and fb.state not in ("unknown", "unavailable"):
                try:
                    # The threshold is in W; a kW-reporting sensor read raw
                    # would look permanently idle.
                    watts = power_to_watts(
                        float(fb.state), fb.attributes.get("unit_of_measurement")
                    )
                    heating = watts >= cfg.feedback_idle_w
                except (TypeError, ValueError):
                    heating = fb.state in ("on", "heating")
        return controlled_on, heating

    @callback
    def _state_changed(self, _event) -> None:
        # Only the dot (controlled_on / heating) depends on these entities.
        if self._actual_state() != self._published_actual:
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._static_attrs = None  # new plan: re-serialize once
        self._schedule_next_boundary()
        super()._handle_coordinator_update()

    # ── boundary-accurate updates ────────────────────────────────────────────
    # `running`, the next start (state) and `current_period_end` flip at each
    # period edge, not on a refresh — wake up exactly then (as the running
    # binary_sensor does) instead of lagging up to a refresh interval.

    def _next_boundary(self, now: datetime) -> datetime | None:
        plan = self._plan
        if not plan:
            return None
        bounds = [t for p in plan.periods for t in (p.start, p.end) if t > now]
        return min(bounds) if bounds else None

    @callback
    def _schedule_next_boundary(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        when = self._next_boundary(dt_util.utcnow())
        if when is not None:
            self._unsub_boundary = async_track_point_in_time(self.hass, self._boundary_fired, when)

    @callback
    def _boundary_fired(self, _now) -> None:
        self._unsub_boundary = None
        self.async_write_ha_state()
        self._schedule_next_boundary()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_next_boundary()
        cfg = self.coordinator.load_config(self._subentry_id)
        watch = [e for e in (cfg.controlled_entity, cfg.feedback_entity) if e]
        if watch:
            self._unsub_state = async_track_state_change_event(
                self.hass, watch, self._state_changed
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
