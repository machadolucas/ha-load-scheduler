"""Config flow for Load Scheduler.

The hub flow selects the shared price (and optional solar) sources. Each load is
added as a ``ConfigSubentry`` via the per-load wizard below
(``async_get_supported_subentry_types``).

M2 scope: a single-step load wizard covering the core scheduling parameters.
Solar options, the pluggable parameter sources and reconfigure flows are layered
on in later milestones; price-source validation (the ``PriceAdaptor`` pattern)
lands in M3.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CONTROLLED_ENTITY,
    CONF_DEADLINE,
    CONF_EARLIEST,
    CONF_MIN_SEPARATION,
    CONF_MIN_SERVICE,
    CONF_MODE,
    CONF_NAME,
    CONF_PRICE_CAP,
    CONF_RUNS_PER_DAY,
    CONF_SELL_PRICE_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_TARGET_MINUTES,
    DEFAULT_NAME,
    DEFAULT_RUNS_PER_DAY,
    DEFAULT_TARGET_MINUTES,
    DOMAIN,
    MODE_INFORMATIONAL,
    MODE_NON_SEQUENTIAL,
    MODE_SEQUENTIAL,
    SUBENTRY_TYPE_LOAD,
    TARGET_MAX,
    TARGET_MIN,
    TARGET_STEP,
)

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _minutes_selector(maximum: int = TARGET_MAX) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=TARGET_MIN,
            max=maximum,
            step=TARGET_STEP,
            unit_of_measurement="min",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class LoadSchedulerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the hub config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the hub: pick the price (and optional solar) sources."""
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_BUY_PRICE_ENTITY])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME), data=user_input
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_BUY_PRICE_ENTITY): _SENSOR,
                vol.Optional(CONF_SELL_PRICE_ENTITY): _SENSOR,
                vol.Optional(CONF_SOLAR_FORECAST_ENTITY): _SENSOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Loads are added as subentries of the hub."""
        return {SUBENTRY_TYPE_LOAD: LoadSubentryFlowHandler}


class LoadSubentryFlowHandler(ConfigSubentryFlow):
    """Wizard for adding a load to the hub."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Collect the load's scheduling parameters."""
        if user_input is not None:
            # Drop unset optionals so model defaults apply cleanly.
            data = {k: v for k, v in user_input.items() if v not in (None, "")}
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_MODE, default=MODE_NON_SEQUENTIAL): (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                MODE_NON_SEQUENTIAL,
                                MODE_SEQUENTIAL,
                                MODE_INFORMATIONAL,
                            ],
                            translation_key="mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                ),
                vol.Required(
                    CONF_TARGET_MINUTES, default=DEFAULT_TARGET_MINUTES
                ): _minutes_selector(),
                vol.Optional(CONF_EARLIEST): selector.TimeSelector(),
                vol.Optional(CONF_DEADLINE): selector.TimeSelector(),
                vol.Optional(
                    CONF_RUNS_PER_DAY, default=DEFAULT_RUNS_PER_DAY
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_MIN_SEPARATION): _minutes_selector(maximum=720),
                vol.Optional(CONF_MIN_SERVICE): _minutes_selector(),
                vol.Optional(CONF_PRICE_CAP): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=5, step=0.001, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_CONTROLLED_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["switch", "input_boolean"])
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
