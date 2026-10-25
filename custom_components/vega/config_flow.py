"""Config and options flow for the Vega integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_IMEI,
    CONF_PIN,
    CONF_PIN2,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DEFAULT_IMEI,
    DEFAULT_PIN,
    DEFAULT_PIN2,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
)

# IMEI and PIN are learned automatically in proxy/MITM mode, so the setup form
# only asks for the listen port and the upstream to relay to (both prefilled).
# Proxy Mode and Direct Control are runtime switches (switch.py), not form fields.
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_UPSTREAM_HOST, default=DEFAULT_UPSTREAM_HOST): str,
        vol.Optional(CONF_UPSTREAM_PORT, default=DEFAULT_UPSTREAM_PORT): vol.All(int, vol.Range(min=1, max=65535)),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vega."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step (single Vega instance; IMEI/PIN learned later)."""
        if user_input is not None:
            await self.async_set_unique_id(DEFAULT_IMEI)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Vega", data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "OptionsFlowHandler":
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Allow changing the port, PIN, poll interval and command gate after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Merge so the Proxy Mode / Direct Control switch states (also stored in
            # options) and learned data are preserved across an options save.
            return self.async_create_entry(title="", data={**self.config_entry.options, **user_input})

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): vol.All(int, vol.Range(min=1, max=65535)),
                vol.Required(CONF_PIN, default=current.get(CONF_PIN, DEFAULT_PIN)): str,
                vol.Optional(CONF_PIN2, default=current.get(CONF_PIN2, DEFAULT_PIN2)): str,
                vol.Optional(CONF_POLL_INTERVAL, default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)): vol.All(int, vol.Range(min=5, max=3600)),
                vol.Optional(CONF_UPSTREAM_HOST, default=current.get(CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST)): str,
                vol.Optional(CONF_UPSTREAM_PORT, default=current.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT)): vol.All(int, vol.Range(min=1, max=65535)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
