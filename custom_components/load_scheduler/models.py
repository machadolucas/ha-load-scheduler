"""Typed view over a load subentry's config + mapping to engine parameters.

Pure (no Home Assistant import): turns the raw subentry ``data`` mapping into a
:class:`LoadConfig`, and combines it with a runtime target + ``now`` into an
:class:`engine.LoadParams`. Tested directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time

from .const import (
    CONF_ALLOW_SOLAR,
    CONF_CONTROLLED_ENTITY,
    CONF_DEADLINE,
    CONF_DRAW_KW,
    CONF_EARLIEST,
    CONF_FAILSAFE_START,
    CONF_FEEDBACK_ENTITY,
    CONF_FEEDBACK_IDLE_W,
    CONF_MIN_OFF,
    CONF_MIN_RUN,
    CONF_MIN_SEPARATION,
    CONF_MIN_SERVICE,
    CONF_MODE,
    CONF_NAME,
    CONF_PRICE_CAP,
    CONF_PRIORITY,
    CONF_RUNS_PER_DAY,
    CONF_TARGET_MINUTES,
    CONF_TEMP_ENTITY,
    CONF_TEMP_MIN,
    DEFAULT_FEEDBACK_IDLE_W,
    DEFAULT_MIN_SEPARATION,
    DEFAULT_MIN_SERVICE,
    DEFAULT_PRIORITY,
    DEFAULT_RUNS_PER_DAY,
    DEFAULT_TARGET_MINUTES,
    DEFAULT_TEMP_MIN,
    MODE_NON_SEQUENTIAL,
)
from .engine import LoadParams, ScheduleMode
from .windows import resolve_window


def _parse_time(value: str | time | None) -> time | None:
    """Parse a TimeSelector value ('HH:MM:SS') into a ``time`` (or pass through)."""
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    return time.fromisoformat(value)


@dataclass(frozen=True)
class LoadConfig:
    """A load subentry's static configuration."""

    name: str
    mode: ScheduleMode
    target_minutes: float
    earliest: time | None
    deadline: time | None
    runs_per_day: int
    min_separation_minutes: float
    min_run_minutes: float
    min_off_minutes: float
    cap: float | None
    min_service_minutes: float
    controlled_entity: str | None
    allow_solar: bool
    draw_kw: float | None
    priority: int
    temp_entity: str | None
    temp_min: float
    feedback_entity: str | None
    feedback_idle_w: float
    failsafe_start: time | None

    @classmethod
    def from_subentry(cls, data: Mapping) -> LoadConfig:
        """Build from a subentry ``data`` mapping (the wizard's output)."""
        cap = data.get(CONF_PRICE_CAP)
        return cls(
            name=data[CONF_NAME],
            mode=ScheduleMode(data.get(CONF_MODE, MODE_NON_SEQUENTIAL)),
            target_minutes=float(data.get(CONF_TARGET_MINUTES, DEFAULT_TARGET_MINUTES)),
            earliest=_parse_time(data.get(CONF_EARLIEST)),
            deadline=_parse_time(data.get(CONF_DEADLINE)),
            runs_per_day=int(data.get(CONF_RUNS_PER_DAY, DEFAULT_RUNS_PER_DAY)),
            min_separation_minutes=float(data.get(CONF_MIN_SEPARATION, DEFAULT_MIN_SEPARATION)),
            min_run_minutes=float(data.get(CONF_MIN_RUN, 0)),
            min_off_minutes=float(data.get(CONF_MIN_OFF, 0)),
            cap=float(cap) if cap is not None else None,
            min_service_minutes=float(data.get(CONF_MIN_SERVICE, DEFAULT_MIN_SERVICE)),
            controlled_entity=data.get(CONF_CONTROLLED_ENTITY),
            allow_solar=bool(data.get(CONF_ALLOW_SOLAR, True)),
            draw_kw=(float(data[CONF_DRAW_KW]) if data.get(CONF_DRAW_KW) is not None else None),
            priority=int(data.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
            temp_entity=data.get(CONF_TEMP_ENTITY),
            temp_min=float(data.get(CONF_TEMP_MIN, DEFAULT_TEMP_MIN)),
            feedback_entity=data.get(CONF_FEEDBACK_ENTITY),
            feedback_idle_w=float(data.get(CONF_FEEDBACK_IDLE_W, DEFAULT_FEEDBACK_IDLE_W)),
            failsafe_start=_parse_time(data.get(CONF_FAILSAFE_START)),
        )

    @property
    def is_informational(self) -> bool:
        return self.mode is ScheduleMode.INFORMATIONAL


def build_load_params(
    cfg: LoadConfig,
    now: datetime,
    target_minutes: float,
    *,
    solar_enabled: bool = False,
    draw_kw: float | None = None,
) -> LoadParams:
    """Combine static config + a (possibly runtime-overridden) target + ``now``.

    ``target_minutes`` is passed explicitly so the caller can substitute the
    live value from the load's ``number`` entity / an external source.
    ``solar_enabled``/``draw_kw`` are resolved by the coordinator from the hub's
    solar configuration and the load's own settings.
    """
    window = resolve_window(now, cfg.earliest, cfg.deadline)
    return LoadParams(
        mode=cfg.mode,
        target_minutes=target_minutes,
        window=window,
        min_service_minutes=cfg.min_service_minutes,
        cap=cfg.cap,
        draw_kw=draw_kw,
        solar_enabled=solar_enabled,
        runs_per_day=cfg.runs_per_day,
        min_separation_minutes=cfg.min_separation_minutes,
        min_run_minutes=cfg.min_run_minutes,
        min_off_minutes=cfg.min_off_minutes,
    )
