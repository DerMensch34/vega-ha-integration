"""Push coordinator for the Vega integration (with command block/pending tracking).

The tracker pushes to us, so there is no polling interval: the runtime calls
``push`` whenever a frame changes the snapshot. The coordinator also tracks
command safety (block reasons) and pending state, mirroring the evolute UX.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    BLOCK_IGNITION,
    BLOCK_OFFLINE,
    COMMAND_PENDING_TIMEOUT_SECONDS,
    COMMAND_REFRESH_DELAYS,
    DOMAIN,
    IGNITION_BLOCKED_COMMANDS,
)
from .runtime import VegaRuntime

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    confirm_key: str | None
    baseline: Any
    expires_at: float


class VegaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Holds the latest snapshot and the command block/pending state."""

    def __init__(self, hass: HomeAssistant, runtime: VegaRuntime, imei: str, name: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.runtime = runtime
        self.imei = imei
        self.device_name = name
        self._pending: dict[str, PendingCommand] = {}
        self._burst_cancels: list[Any] = []

    async def _async_update_data(self) -> dict[str, Any]:
        return self.runtime.snapshot()

    @callback
    def push(self, data: dict[str, Any]) -> None:
        """Runtime update callback: resolve pending commands, then publish."""
        self._resolve_pending(data)
        self.async_set_updated_data(data)

    # --- command blocking (checks) -------------------------------------------

    def command_block_reason(self, action: str) -> str | None:
        data = self.data or {}
        if not data.get("is_online"):
            return BLOCK_OFFLINE
        if action in IGNITION_BLOCKED_COMMANDS and data.get("ignition"):
            return BLOCK_IGNITION
        return None

    def is_command_blocked(self, action: str) -> bool:
        return self.command_block_reason(action) is not None

    # --- pending tracking ----------------------------------------------------

    def is_command_pending(self, action: str) -> bool:
        return action in self._pending

    async def async_send_command(self, action: str, confirm_key: str | None) -> None:
        """Send via the runtime (raises CommandError on failure) and track pending."""
        await self.runtime.async_send_command(action)
        if confirm_key:
            baseline = (self.data or {}).get(confirm_key)
            self._pending[action] = PendingCommand(
                confirm_key, baseline, monotonic() + COMMAND_PENDING_TIMEOUT_SECONDS
            )
            self.async_update_listeners()
            self._schedule_refreshes()

    @callback
    def _schedule_refreshes(self) -> None:
        self._cancel_refreshes()
        for delay in COMMAND_REFRESH_DELAYS:
            self._burst_cancels.append(async_call_later(self.hass, delay, self._burst_refresh))

    @callback
    def _cancel_refreshes(self) -> None:
        for cancel in self._burst_cancels:
            cancel()
        self._burst_cancels.clear()

    async def _burst_refresh(self, _now: Any) -> None:
        await self.runtime.async_poll_once()

    def _resolve_pending(self, data: dict[str, Any]) -> None:
        now = monotonic()
        for action, pending in list(self._pending.items()):
            confirmed = pending.confirm_key is not None and data.get(pending.confirm_key) != pending.baseline
            if confirmed or now >= pending.expires_at:
                del self._pending[action]
        if not self._pending:
            self._cancel_refreshes()
