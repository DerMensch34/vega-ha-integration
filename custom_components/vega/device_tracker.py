"""Device tracker platform for Vega (GPS position from the Wialon channel)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_IMEI, DOMAIN
from .coordinator import VegaCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Vega device tracker."""
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    added = False

    @callback
    def _sync_tracker() -> None:
        nonlocal added
        if added:
            return
        if not coordinator.imei or coordinator.imei == DEFAULT_IMEI:
            return
        added = True
        async_add_entities([VegaDeviceTracker(coordinator)])

    _sync_tracker()
    entry.async_on_unload(coordinator.async_add_listener(_sync_tracker))


class VegaDeviceTracker(CoordinatorEntity, TrackerEntity):
    """GPS tracker fed by the Wialon IPS stream."""

    def __init__(self, coordinator: VegaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = coordinator.device_name
        self._attr_unique_id = f"vega_{coordinator.imei}_tracker"
        self.entity_id = f"device_tracker.vega_{coordinator.imei}"
        self._attr_icon = "mdi:car"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name)

    def _coord(self, key: str) -> float | None:
        value = (self.coordinator.data or {}).get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def latitude(self) -> float | None:
        lat = self._coord("latitude")
        return lat if lat is not None and -90 <= lat <= 90 else None

    @property
    def longitude(self) -> float | None:
        lon = self._coord("longitude")
        return lon if lon is not None and -180 <= lon <= 180 else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def available(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {}
        for key in ("course", "altitude", "satellites", "hdop", "speed", "signal_level"):
            if data.get(key) is not None:
                attrs[key] = data[key]
        return attrs
