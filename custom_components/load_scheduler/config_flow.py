"""Config flow for the Load Scheduler hub.

M0 scope: a single hub step that selects the buy-price forecast sensor (and,
optionally, the sell-price and solar-forecast sensors). The per-load wizard is
added as a ``ConfigSubentryFlow`` in M2/M3 via
``async_get_supported_subentry_types``; the price-source auto-detection /
validation (the ``PriceAdaptor`` pattern) lands in M3.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_NAME,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    DEFAULT_NAME,
    DOMAIN,
)

_SENSOR_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_OPTIONAL_SENSOR_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


class LoadSchedulerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the hub config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the hub: pick the price (and optional solar) sources."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Only one hub is expected; allow more but keep them distinct.
            await self.async_set_unique_id(user_input[CONF_BUY_PRICE_ENTITY])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_BUY_PRICE_ENTITY): _SENSOR_SELECTOR,
                vol.Optional(CONF_SELL_PRICE_ENTITY): _OPTIONAL_SENSOR_SELECTOR,
                vol.Optional(CONF_SOLAR_FORECAST_ENTITY): _OPTIONAL_SENSOR_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
