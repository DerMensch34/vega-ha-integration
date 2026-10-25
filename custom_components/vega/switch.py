"""Switch platform for Vega: Proxy Mode (learn) and Direct Control."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DIRECT_CONTROL,
    CONF_PROXY_MODE,
    DEFAULT_DIRECT_CONTROL,
    DEFAULT_PROXY_MODE,
    DOMAIN,
)
from .coordinator import VegaCoordinator
from .entity import build_device_info

# key -> (name, icon, option key, default)
SWITCHES = {
    "proxy_mode": ("Proxy Mode (learn)", "mdi:lan-connect", CONF_PROXY_MODE, DEFAULT_PROXY_MODE),
    "direct_control": ("Direct Control", "mdi:car-cog", CONF_DIRECT_CONTROL, DEFAULT_DIRECT_CONTROL),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        VegaSwitch(coordinator, entry, key, *spec) for key, spec in SWITCHES.items()
    )


class VegaSwitch(SwitchEntity):
    """A config toggle stored in the entry options; a change reloads the entry."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key, name, icon, opt_key, default):
        self._entry = entry
        self._opt_key = opt_key
        self._default = default
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"vega_{coordinator.imei}_{key}"
        self.entity_id = f"switch.vega_{coordinator.imei}_{key}"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name)

    @property
    def is_on(self) -> bool:
        # options win, then entry data, then the mode default (proxy on, direct off)
        if self._opt_key in self._entry.options:
            return bool(self._entry.options[self._opt_key])
        return bool(self._entry.data.get(self._opt_key, self._default))

    async def _set(self, value: bool) -> None:
        options = {**self._entry.options, self._opt_key: value}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        # the entry's update listener reloads the integration with the new mode

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
