"""Config flow for Load Scheduler.

The hub flow selects the shared price (and optional solar) sources and supports
reconfigure. Each load is added/edited as a ``ConfigSubentry`` via the per-load
wizard (``async_get_supported_subentry_types``), which shares one ``init`` step
between add and reconfigure.

Solar options and the pluggable parameter sources are layered on in later
milestones.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import price_source
from .const import (
    CONF_ALLOW_SOLAR,
    CONF_BUY_PRICE_ENTITY,
    CONF_CONSUMPTION_BASELINE_W,
    CONF_CONTROLLED_ENTITY,
    CONF_DEADLINE,
    CONF_DRAW_KW,
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
    DEFAULT_BASELINE_W,
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


def _validate_price(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key if the price entity exists but isn't parseable.

    A not-yet-available entity is allowed (the coordinator + a repair issue
    handle that), so setup isn't blocked when the price integration loads later.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        price_source.detect_format(dict(state.attributes))
    except price_source.PriceFormatError:
        return "invalid_price_entity"
    return None


def _hub_schema(defaults: dict) -> vol.Schema:
    def suggest(key):
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(
                CONF_BUY_PRICE_ENTITY, description=suggest(CONF_BUY_PRICE_ENTITY)
            ): _SENSOR,
            vol.Optional(
                CONF_SELL_PRICE_ENTITY, description=suggest(CONF_SELL_PRICE_ENTITY)
            ): _SENSOR,
            vol.Optional(
                CONF_SOLAR_FORECAST_ENTITY,
                description=suggest(CONF_SOLAR_FORECAST_ENTITY),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Optional(
                CONF_CONSUMPTION_BASELINE_W,
                default=defaults.get(CONF_CONSUMPTION_BASELINE_W, DEFAULT_BASELINE_W),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    step=50,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _load_schema(defaults: dict) -> vol.Schema:
    def suggest(key):
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Required(CONF_NAME, description=suggest(CONF_NAME)): str,
            vol.Required(
                CONF_MODE, default=defaults.get(CONF_MODE, MODE_NON_SEQUENTIAL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        MODE_NON_SEQUENTIAL,
                        MODE_SEQUENTIAL,
                        MODE_INFORMATIONAL,
                    ],
                    translation_key="mode",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_TARGET_MINUTES,
                default=defaults.get(CONF_TARGET_MINUTES, DEFAULT_TARGET_MINUTES),
            ): _minutes_selector(),
            vol.Optional(
                CONF_EARLIEST, description=suggest(CONF_EARLIEST)
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_DEADLINE, description=suggest(CONF_DEADLINE)
            ): selector.TimeSelector(),
            vol.Required(
                CONF_RUNS_PER_DAY,
                default=defaults.get(CONF_RUNS_PER_DAY, DEFAULT_RUNS_PER_DAY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_MIN_SEPARATION, description=suggest(CONF_MIN_SEPARATION)
            ): _minutes_selector(maximum=720),
            vol.Optional(
                CONF_MIN_SERVICE, description=suggest(CONF_MIN_SERVICE)
            ): _minutes_selector(),
            vol.Optional(
                CONF_PRICE_CAP, description=suggest(CONF_PRICE_CAP)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=5, step=0.001, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_CONTROLLED_ENTITY, description=suggest(CONF_CONTROLLED_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "input_boolean"])
            ),
            vol.Optional(
                CONF_ALLOW_SOLAR, default=defaults.get(CONF_ALLOW_SOLAR, True)
            ): selector.BooleanSelector(),
            vol.Optional(CONF_DRAW_KW, description=suggest(CONF_DRAW_KW)): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=50,
                    step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _clean(user_input: dict) -> dict:
    """Drop unset optionals so model defaults apply cleanly."""
    return {k: v for k, v in user_input.items() if v not in (None, "")}


class LoadSchedulerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the hub config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the hub: pick the price (and optional solar) sources."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_price(self.hass, user_input[CONF_BUY_PRICE_ENTITY])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_BUY_PRICE_ENTITY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, DEFAULT_NAME), data=user_input
                )
        return self.async_show_form(
            step_id="user", data_schema=_hub_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the hub's price/solar sources."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_price(self.hass, user_input[CONF_BUY_PRICE_ENTITY])
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        defaults = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure", data_schema=_hub_schema(defaults), errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Loads are added as subentries of the hub."""
        return {SUBENTRY_TYPE_LOAD: LoadSubentryFlowHandler}


class LoadSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure a load (one shared ``init`` step)."""

    _defaults: dict

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        self._defaults = {}
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._defaults = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            data = _clean(user_input)
            if self._is_new:
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=data[CONF_NAME],
                data=data,
            )
        return self.async_show_form(step_id="init", data_schema=_load_schema(self._defaults))
