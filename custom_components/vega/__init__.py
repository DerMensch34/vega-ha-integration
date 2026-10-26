"""The Vega integration.

Runs a raw-TCP listener inside Home Assistant that the Vega MT X CAN tracker
dials into (after its Setting 3001 server slot is re-pointed to this host), and
exposes the decoded telemetry and direct commands as a separate HA device.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CARD_URL,
    CARD_VERSION,
    CONF_COMMANDS_ENABLED,
    CONF_DIRECT_CONTROL,
    CONF_USE_RUN_SCRIPT,
    CONF_IMEI,
    CONF_LEARNED_IMEI,
    CONF_LEARNED_PIN,
    CONF_PIN,
    CONF_PIN2,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_PROXY_MODE,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DEFAULT_DIRECT_CONTROL,
    DEFAULT_USE_RUN_SCRIPT,
    DEFAULT_IMEI,
    DEFAULT_PIN,
    DEFAULT_PIN2,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_PROXY_MODE,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
)
from .coordinator import VegaCoordinator
from .runtime import VegaRuntime

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
]


def _opt(entry: ConfigEntry, key: str, default):
    """Prefer an options override, fall back to the entry data, then default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


async def _setup_www_files(hass: HomeAssistant) -> None:
    """Install the bundled hold-button card into <config>/www/vega/."""
    try:
        source_dir = Path(__file__).parent / "www" / "vega"
        dest_dir = Path(hass.config.path("www")) / "vega"
        if not source_dir.exists():
            return

        def _copy() -> int:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.chmod(0o755)
            count = 0
            for file in source_dir.glob("*.js"):
                dest = dest_dir / file.name
                shutil.copy2(file, dest)
                dest.chmod(0o644)
                count += 1
            return count

        copied = await hass.async_add_executor_job(_copy)
        if copied:
            _LOGGER.info("Installed %d file(s) to www/vega/", copied)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Failed to set up www/vega/ files: %s", err)


async def _register_card_resource(hass: HomeAssistant) -> None:
    """Register every bundled card (/local/vega/*.js) as a Lovelace resource."""
    try:
        source_dir = Path(__file__).parent / "www" / "vega"
        js_files = sorted(f.name for f in source_dir.glob("*.js")) if source_dir.exists() else []
    except Exception:  # noqa: BLE001
        js_files = []
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        if resources is None:
            return
        if not getattr(resources, "loaded", True):
            await resources.async_load()
            resources.loaded = True
        existing = {item.get("url", "").split("?")[0]: item for item in (resources.async_items() or [])}
        for name in js_files:
            base = f"/local/vega/{name}"
            versioned = f"{base}?v={CARD_VERSION}"
            item = existing.get(base)
            if item is None:
                await resources.async_create_item({"res_type": "module", "url": versioned})
                _LOGGER.info("Registered Lovelace resource %s", versioned)
            elif item.get("url") != versioned:
                await resources.async_update_item(item["id"], {"url": versioned})
    except Exception as err:  # noqa: BLE001
        _LOGGER.info("Could not auto-register Vega cards (%s); add /local/vega/*.js as JS modules manually", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vega from a config entry."""
    await _setup_www_files(hass)
    await _register_card_resource(hass)
    imei = (
        _opt(entry, CONF_IMEI, None)
        or _opt(entry, CONF_LEARNED_IMEI, None)
        or (entry.unique_id if entry.unique_id and entry.unique_id != DEFAULT_IMEI else None)
        or DEFAULT_IMEI
    )
    port = int(_opt(entry, CONF_PORT, DEFAULT_PORT))

    # direct_control supersedes the legacy commands_enabled flag.
    direct_control = bool(_opt(entry, CONF_DIRECT_CONTROL, _opt(entry, CONF_COMMANDS_ENABLED, DEFAULT_DIRECT_CONTROL)))

    def _persist(changed: dict) -> None:
        """Save learned PIN/IMEI back into the entry so they survive restarts."""
        cur = hass.config_entries.async_get_entry(entry.entry_id)
        if not cur:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                cur = entries[0]
        if not cur:
            return
        data = {**cur.data}
        if "learned_pin" in changed:
            data[CONF_LEARNED_PIN] = changed["learned_pin"]
        if "learned_imei" in changed:
            data[CONF_LEARNED_IMEI] = changed["learned_imei"]
            if not data.get(CONF_IMEI) or data.get(CONF_IMEI) == DEFAULT_IMEI:
                data[CONF_IMEI] = changed["learned_imei"]
        if "imei" in changed:
            data[CONF_IMEI] = changed["imei"]
        hass.config_entries.async_update_entry(cur, data=data)

    if not entry.data.get(CONF_IMEI) and imei != DEFAULT_IMEI:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_IMEI: imei})

    runtime = VegaRuntime(
        hass,
        imei=imei,
        port=port,
        pin=str(_opt(entry, CONF_PIN, DEFAULT_PIN)),
        pin2=str(_opt(entry, CONF_PIN2, DEFAULT_PIN2)),
        poll_interval=int(_opt(entry, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        proxy_mode=bool(_opt(entry, CONF_PROXY_MODE, DEFAULT_PROXY_MODE)),
        direct_control=direct_control,
        use_run_script=bool(_opt(entry, CONF_USE_RUN_SCRIPT, DEFAULT_USE_RUN_SCRIPT)),
        upstream=(
            str(_opt(entry, CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST)),
            int(_opt(entry, CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT)),
        ),
        learned_pin=_opt(entry, CONF_LEARNED_PIN, None),
        learned_imei=_opt(entry, CONF_LEARNED_IMEI, ""),
        persist_cb=_persist,
    )

    coordinator = VegaCoordinator(hass, runtime, imei, entry.title or f"Vega {imei}")
    runtime.set_update_callback(coordinator.push)

    try:
        await runtime.start()
    except OSError as err:
        raise ConfigEntryNotReady(f"Could not bind Vega listener on port {port}: {err}") from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register the CAN-script read/write services once."""
    if hass.services.has_service(DOMAIN, "read_can_scripts"):
        return

    def _runtimes():
        return [c.runtime for c in hass.data.get(DOMAIN, {}).values()]

    async def _read(call) -> None:
        for runtime in _runtimes():
            await runtime.async_read_can_scripts()

    async def _write(call) -> None:
        slot = int(call.data["slot"])
        script = call.data["script"]  # {name, repeat, frames:[{delay_ms,can_id,bus,data}]}
        for runtime in _runtimes():
            await runtime.async_write_can_script(slot, script)

    async def _run(call) -> None:
        slot = int(call.data["slot"])
        for runtime in _runtimes():
            await runtime.async_run_script(slot)

    hass.services.async_register(DOMAIN, "read_can_scripts", _read)
    hass.services.async_register(DOMAIN, "write_can_script", _write)
    hass.services.async_register(DOMAIN, "run_script", _run)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: stop the listener and tear down platforms."""
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.runtime.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options/data updates without reloading unless network settings change."""
    coordinator: VegaCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not coordinator:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    runtime = coordinator.runtime

    # Update runtime flags dynamically without dropping live socket
    runtime.direct_control = bool(_opt(entry, CONF_DIRECT_CONTROL, DEFAULT_DIRECT_CONTROL))
    runtime.use_run_script = bool(_opt(entry, CONF_USE_RUN_SCRIPT, DEFAULT_USE_RUN_SCRIPT))
    runtime.proxy_mode = bool(_opt(entry, CONF_PROXY_MODE, DEFAULT_PROXY_MODE))
    runtime.poll_interval = int(_opt(entry, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))

    # Port or upstream change requires restarting the TCP listener
    new_port = int(_opt(entry, CONF_PORT, DEFAULT_PORT))
    new_upstream = (
        str(_opt(entry, CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST)),
        int(_opt(entry, CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT)),
    )
    if new_port != runtime.port or new_upstream != runtime.upstream:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    runtime._publish()
    coordinator.async_update_listeners()
