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
    CONF_CONTROLLED_ENTITY,
    CONF_DEADLINE,
    CONF_EARLIEST,
    CONF_MIN_SEPARATION,
    CONF_MIN_SERVICE,
    CONF_MODE,
    CONF_NAME,
    CONF_PRICE_CAP,
    CONF_RUNS_PER_DAY,
    CONF_TARGET_MINUTES,
    DEFAULT_MIN_SEPARATION,
    DEFAULT_MIN_SERVICE,
    DEFAULT_RUNS_PER_DAY,
    DEFAULT_TARGET_MINUTES,
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
    cap: float | None
    min_service_minutes: float
    controlled_entity: str | None

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
            cap=float(cap) if cap is not None else None,
            min_service_minutes=float(data.get(CONF_MIN_SERVICE, DEFAULT_MIN_SERVICE)),
            controlled_entity=data.get(CONF_CONTROLLED_ENTITY),
        )

    @property
    def is_informational(self) -> bool:
        return self.mode is ScheduleMode.INFORMATIONAL


def build_load_params(cfg: LoadConfig, now: datetime, target_minutes: float) -> LoadParams:
    """Combine static config + a (possibly runtime-overridden) target + ``now``.

    ``target_minutes`` is passed explicitly so the caller can substitute the
    live value from the load's ``number`` entity / an external source.
    """
    window = resolve_window(now, cfg.earliest, cfg.deadline)
    return LoadParams(
        mode=cfg.mode,
        target_minutes=target_minutes,
        window=window,
        min_service_minutes=cfg.min_service_minutes,
        cap=cfg.cap,
        runs_per_day=cfg.runs_per_day,
        min_separation_minutes=cfg.min_separation_minutes,
    )
