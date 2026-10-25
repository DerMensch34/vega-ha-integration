"""Binary sensor platform for Vega."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BINARY_SENSOR_TYPES, DOMAIN
from .coordinator import VegaCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Vega binary sensors."""
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        VegaBinarySensor(coordinator, key, name, device_class, state_key)
        for key, (name, device_class, state_key) in BINARY_SENSOR_TYPES.items()
    )


class VegaBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """A single boolean telemetry flag from the Vega channel."""

    def __init__(
        self,
        coordinator: VegaCoordinator,
        key: str,
        name: str,
        device_class: str | None,
        state_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._state_key = state_key
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_unique_id = f"vega_{coordinator.imei}_{key}"
        self.entity_id = f"binary_sensor.vega_{coordinator.imei}_{key}"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name)

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get(self._state_key))

    @property
    def available(self) -> bool:
        # Unavailable until we actually have a reading for this flag, so a missing
        # value shows as unavailable instead of a misleading fake "off".
        return self._state_key in (self.coordinator.data or {})
