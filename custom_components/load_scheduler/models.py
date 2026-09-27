"""Typed view over a load subentry's config + mapping to engine parameters.

Pure (no Home Assistant import): turns the raw subentry ``data`` mapping into a
:class:`LoadConfig`, and combines it with a runtime target + ``now`` into an
:class:`engine.LoadParams`. Tested directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from .const import (
    CONF_ALLOW_SOLAR,
    CONF_COEXIST,
    CONF_CONTROLLED_ENTITY,
    CONF_DEADLINE,
    CONF_DELIVERED_ENTITY,
    CONF_DRAW_KW,
    CONF_EARLIEST,
    CONF_FAILSAFE_START,
    CONF_FEEDBACK_ENTITY,
    CONF_FEEDBACK_IDLE_W,
    CONF_HORIZON_HOURS,
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
    CONF_TARGET_TYPE,
    CONF_TEMP_ENTITY,
    CONF_TEMP_MIN,
    DEFAULT_COEXIST,
    DEFAULT_FEEDBACK_IDLE_W,
    DEFAULT_MIN_SEPARATION,
    DEFAULT_MIN_SERVICE,
    DEFAULT_PRIORITY,
    DEFAULT_RUNS_PER_DAY,
    DEFAULT_TARGET_MINUTES,
    DEFAULT_TARGET_TYPE,
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
    horizon_hours: float | None
    runs_per_day: int
    min_separation_minutes: float
    min_run_minutes: float
    min_off_minutes: float
    cap: float | None
    min_service_minutes: float
    controlled_entity: str | None
    allow_solar: bool
    coexist: bool
    draw_kw: float | None
    priority: int
    temp_entity: str | None
    temp_min: float
    feedback_entity: str | None
    feedback_idle_w: float
    failsafe_start: time | None
    target_type: str
    delivered_entity: str | None

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
            horizon_hours=(
                float(data[CONF_HORIZON_HOURS]) if data.get(CONF_HORIZON_HOURS) else None
            ),
            runs_per_day=int(data.get(CONF_RUNS_PER_DAY, DEFAULT_RUNS_PER_DAY)),
            min_separation_minutes=float(data.get(CONF_MIN_SEPARATION, DEFAULT_MIN_SEPARATION)),
            min_run_minutes=float(data.get(CONF_MIN_RUN, 0)),
            min_off_minutes=float(data.get(CONF_MIN_OFF, 0)),
            cap=float(cap) if cap is not None else None,
            min_service_minutes=float(data.get(CONF_MIN_SERVICE, DEFAULT_MIN_SERVICE)),
            controlled_entity=data.get(CONF_CONTROLLED_ENTITY),
            allow_solar=bool(data.get(CONF_ALLOW_SOLAR, True)),
            coexist=bool(data.get(CONF_COEXIST, DEFAULT_COEXIST)),
            draw_kw=(float(data[CONF_DRAW_KW]) if data.get(CONF_DRAW_KW) is not None else None),
            priority=int(data.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
            temp_entity=data.get(CONF_TEMP_ENTITY),
            temp_min=float(data.get(CONF_TEMP_MIN, DEFAULT_TEMP_MIN)),
            feedback_entity=data.get(CONF_FEEDBACK_ENTITY),
            feedback_idle_w=float(data.get(CONF_FEEDBACK_IDLE_W, DEFAULT_FEEDBACK_IDLE_W)),
            failsafe_start=_parse_time(data.get(CONF_FAILSAFE_START)),
            target_type=data.get(CONF_TARGET_TYPE, DEFAULT_TARGET_TYPE),
            delivered_entity=data.get(CONF_DELIVERED_ENTITY),
        )

    @property
    def is_informational(self) -> bool:
        return self.mode is ScheduleMode.INFORMATIONAL


def build_load_params(
    cfg: LoadConfig,
    now: datetime,
    target_minutes: float,
    *,
    delivered_minutes: float = 0.0,
    solar_enabled: bool = False,
    draw_kw: float | None = None,
    running_minutes: float = 0.0,
    stopped_minutes: float | None = None,
) -> LoadParams:
    """Combine static config + a (possibly runtime-overridden) target + ``now``.

    ``target_minutes`` is the live target in minutes (kWh-mode loads are converted
    at the ``number`` entity, so the engine always works in minutes).
    ``delivered_minutes`` — runtime already delivered today — is subtracted from
    both the target and the minimum-service floor (dynamic remaining), so a load
    that already ran enough (e.g. on solar) shrinks or skips its planned run.
    For a sequential load with ``runs_per_day > 1`` it comes off the day's total
    (runs × target) instead, which the engine re-splits into whole runs.

    ``running_minutes`` is how long the load's current on-run has lasted (0 when
    off); it only reaches the engine while the window is open, since the engine
    pins the continuation of that run at the window start. ``stopped_minutes``
    is how long ago it last switched off (None when on or unknown), so a replan
    keeps ``min_off`` / ``min_separation`` from that stop.
    """
    if cfg.horizon_hours:
        # Multi-day: search the next N hours so the engine can defer an expensive
        # today to a cheaper tomorrow (once tomorrow's real prices are known).
        # A configured earliest/deadline still applies — the wizard collects all
        # three, and silently dropping two of them made them look like no-ops.
        # Added in UTC: N hours of real time, not of wall clock across a DST change.
        horizon_end = (now.astimezone(UTC) + timedelta(hours=cfg.horizon_hours)).astimezone(
            now.tzinfo
        )
        window = (now, horizon_end)
        if cfg.earliest is not None or cfg.deadline is not None:
            daily = resolve_window(now, cfg.earliest, cfg.deadline)
            start, end = max(window[0], daily[0]), min(window[1], daily[1])
            window = (start, end) if start < end else (start, start)
    else:
        window = resolve_window(now, cfg.earliest, cfg.deadline)
    # Multi-run sequential: subtracting delivered from the per-run block shrank
    # *every* block, so once run one finished the rest were never planned. The
    # per-run length is passed for every sequential load: it fixes where a cycle
    # in progress ends, whatever delivered says.
    run_minutes: float | None = None
    total = target_minutes
    if cfg.mode is not ScheduleMode.NON_SEQUENTIAL:
        run_minutes = target_minutes
        total = target_minutes * max(1, cfg.runs_per_day)
    # Before the window opens (window[0] > now) the run in progress isn't one the
    # plan can continue — its first period can't start now.
    running = running_minutes if window[0] <= now < window[1] else 0.0
    # Measured to the window start: a window opening later is that much further
    # from the stop.
    stopped = None
    if stopped_minutes is not None and running <= 0:
        lead = (window[0].astimezone(UTC) - now.astimezone(UTC)).total_seconds() / 60.0
        stopped = max(0.0, stopped_minutes) + max(0.0, lead)
    return LoadParams(
        mode=cfg.mode,
        target_minutes=max(0.0, total - delivered_minutes),
        run_minutes=run_minutes,
        running_minutes=max(0.0, running),
        stopped_minutes=stopped,
        window=window,
        min_service_minutes=max(0.0, cfg.min_service_minutes - delivered_minutes),
        cap=cfg.cap,
        draw_kw=draw_kw,
        solar_enabled=solar_enabled,
        runs_per_day=cfg.runs_per_day,
        min_separation_minutes=cfg.min_separation_minutes,
        min_run_minutes=cfg.min_run_minutes,
        min_off_minutes=cfg.min_off_minutes,
        min_service_by=_min_service_by(cfg, now, window, delivered_minutes),
    )


def _min_service_by(
    cfg: LoadConfig,
    now: datetime,
    window: tuple[datetime, datetime],
    delivered_minutes: float,
) -> datetime | None:
    """When the min-service floor stops counting: the next local midnight.

    Delivered-today is measured from local midnight, so a guaranteed minute
    scheduled after it protects tomorrow, not today — and today's floor is then
    silently never met. Built from the *date* (never ``+ timedelta(hours=24)``)
    so a 23h/25h DST day still lands on the real boundary.
    """
    if cfg.min_service_minutes - delivered_minutes <= 0:
        return None
    day_end = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    return min(day_end, window[1])
