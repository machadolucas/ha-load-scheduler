"""Constants for the Load Scheduler integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "load_scheduler"

# Entity platforms forwarded by the hub config entry.
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
]

# ── Hub config-entry keys ────────────────────────────────────────────────────
CONF_NAME = "name"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"

DEFAULT_NAME = "Load Scheduler"

# ── Subentry (per-load) ──────────────────────────────────────────────────────
SUBENTRY_TYPE_LOAD = "load"

CONF_MODE = "mode"
CONF_TARGET_MINUTES = "target_minutes"
CONF_EARLIEST = "earliest"
CONF_DEADLINE = "deadline"
CONF_RUNS_PER_DAY = "runs_per_day"
CONF_MIN_SEPARATION = "min_separation_minutes"
CONF_PRICE_CAP = "price_cap"
CONF_MIN_SERVICE = "min_service_minutes"
CONF_CONTROLLED_ENTITY = "controlled_entity"

# Schedule modes (string values match engine.ScheduleMode).
MODE_NON_SEQUENTIAL = "non_sequential"
MODE_SEQUENTIAL = "sequential"
MODE_INFORMATIONAL = "informational"

# Defaults for the per-load wizard.
DEFAULT_TARGET_MINUTES = 180  # 3 h
DEFAULT_RUNS_PER_DAY = 1
DEFAULT_MIN_SEPARATION = 0
DEFAULT_MIN_SERVICE = 0

# Bounds for the target `number` entity (minutes).
TARGET_MIN = 0
TARGET_MAX = 1440  # 24 h
TARGET_STEP = 15

# How often the coordinator recomputes as a safety net (event-driven otherwise).
UPDATE_INTERVAL_MINUTES = 5
