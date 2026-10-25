"""Shared entity helpers for the Vega integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def build_device_info(imei: str, name: str) -> DeviceInfo:
    """Device registry entry shared by every Vega entity.

    The identifier is (DOMAIN="vega", imei) — a different namespace from the
    evolute integration's ("evolute", car_id) — so Home Assistant registers this
    as a separate device and never merges the two cars' entities.
    """
    return {
        "identifiers": {(DOMAIN, imei)},
        "name": name,
        "manufacturer": "Vega",
        "model": "MT X CAN",
        "suggested_area": "Garage",
    }
