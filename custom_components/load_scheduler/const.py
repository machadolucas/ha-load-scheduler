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
    Platform.BUTTON,
    Platform.CALENDAR,
]

# ── Hub config-entry keys ────────────────────────────────────────────────────
CONF_NAME = "name"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"
# Optional predictor-supplied price forecast for slots BEYOND the real horizon
# (e.g. a wind/temperature/solar-based estimate of day-after-tomorrow prices).
CONF_FORECAST_PRICE_ENTITY = "forecast_price_entity"
CONF_FORECAST_PRICE_MARGIN = "forecast_price_margin"  # €/kWh added to forecast buy
DEFAULT_FORECAST_PRICE_MARGIN = 0.0

DEFAULT_NAME = "Load Scheduler"

# ── Subentry (per-load) ──────────────────────────────────────────────────────
SUBENTRY_TYPE_LOAD = "load"

CONF_MODE = "mode"
CONF_TARGET_TYPE = "target_type"
CONF_TARGET_MINUTES = "target_minutes"
CONF_DELIVERED_ENTITY = "delivered_entity"

# Target types.
TARGET_TYPE_RUNTIME = "runtime"  # the target is a run time (minutes)
TARGET_TYPE_KWH = "kwh"  # the target is energy to deliver (kWh) at draw_kw
DEFAULT_TARGET_TYPE = TARGET_TYPE_RUNTIME
CONF_EARLIEST = "earliest"
CONF_DEADLINE = "deadline"
CONF_HORIZON_HOURS = "horizon_hours"  # multi-day: search the next N hours instead
CONF_RUNS_PER_DAY = "runs_per_day"
CONF_MIN_SEPARATION = "min_separation_minutes"
CONF_MIN_RUN = "min_run_minutes"
CONF_MIN_OFF = "min_off_minutes"
CONF_PRICE_CAP = "price_cap"
CONF_MIN_SERVICE = "min_service_minutes"
CONF_CONTROLLED_ENTITY = "controlled_entity"
CONF_FAILSAFE_START = "failsafe_start"
CONF_ALLOW_SOLAR = "allow_solar"
CONF_DRAW_KW = "draw_kw"
CONF_PRIORITY = "priority"
DEFAULT_PRIORITY = 0

# Per-load safety / feedback.
CONF_TEMP_ENTITY = "temp_entity"  # inside-temperature sensor for the safety floor
CONF_TEMP_MIN = "temp_min"  # force heat below this (°C)
CONF_FEEDBACK_ENTITY = "feedback_entity"  # actual-heating power/led signal
CONF_FEEDBACK_IDLE_W = "feedback_idle_w"  # below this W the element is idle/satisfied
DEFAULT_TEMP_MIN = 18.0
DEFAULT_FEEDBACK_IDLE_W = 50.0

# Hub solar settings.
CONF_CONSUMPTION_BASELINE_W = "consumption_baseline_w"
DEFAULT_BASELINE_W = 400  # flat fallback baseline (W)
CONF_BASELINE_ENTITY = "baseline_entity"  # consumption sensor → hour-of-day profile

# Hub real-time divert settings.
CONF_NET_ENERGY_ENTITY = "net_energy_entity"  # live net energy; negative = export
CONF_NET_EXPORT_THRESHOLD = "net_export_threshold"  # export beyond this triggers divert
CONF_LIVE_SELL_ENTITY = "live_sell_entity"  # live sell price (optional gate)
CONF_SELL_THRESHOLD = "sell_threshold"  # only divert when live sell is below this
DEFAULT_NET_EXPORT_THRESHOLD = 0.1
DEFAULT_SELL_THRESHOLD = 0.05

# Real-time control timing.
MANUAL_OVERRIDE_GRACE_S = 600  # back off this long after a foreign (manual) change
DIVERT_MIN_DWELL_S = 120  # min time before flipping a divert decision (anti-thrash)

# Schedule modes (string values match engine.ScheduleMode).
MODE_NON_SEQUENTIAL = "non_sequential"
MODE_SEQUENTIAL = "sequential"
MODE_INFORMATIONAL = "informational"

# Defaults for the per-load wizard.
DEFAULT_TARGET_MINUTES = 180  # 3 h
DEFAULT_RUNS_PER_DAY = 1
DEFAULT_MIN_SEPARATION = 0
DEFAULT_MIN_SERVICE = 0
DEFAULT_BOOST_MINUTES = 60  # used by the boost button when target is 0

# Bounds for the target `number` entity (minutes).
TARGET_MIN = 0
TARGET_MAX = 1440  # 24 h
TARGET_STEP = 15
# Bounds when the target is shown in kWh (EV charging etc.).
TARGET_MAX_KWH = 100
TARGET_STEP_KWH = 0.5

# How often the coordinator recomputes as a safety net (event-driven otherwise).
UPDATE_INTERVAL_MINUTES = 5

# Persistence: a Store under .storage/ (included in Home Assistant backups).
STORAGE_VERSION = 1
SAVE_DELAY = 10  # seconds — debounce runtime writes

# Events fired when a load's controlled entity is switched on/off.
EVENT_RUN_STARTED = "load_scheduler_run_started"
EVENT_RUN_ENDED = "load_scheduler_run_ended"

# Repair issues.
ISSUE_PRICE_UNAVAILABLE = "price_unavailable"
