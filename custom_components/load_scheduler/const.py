"""Constants for the Load Scheduler integration."""

from __future__ import annotations

DOMAIN = "load_scheduler"

# Entity platforms forwarded by the hub config entry. Empty at M0 — entities
# (binary_sensor / sensor / number / switch / button / calendar) arrive in M2+.
PLATFORMS: list[str] = []

# Hub config-entry keys.
CONF_NAME = "name"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"
CONF_SOLAR_FORECAST_ENTITY = "solar_forecast_entity"

DEFAULT_NAME = "Load Scheduler"
