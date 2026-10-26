"""Exercise real HA service registration/dispatch, using fake runtimes only."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.vega import _register_services


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.runtimes = {
            entry: types.SimpleNamespace(
                async_write_can_script=AsyncMock(return_value={"verified": True, "slot": 5}),
                async_read_can_scripts=AsyncMock(return_value=[{}] * 8),
                async_write_commands=AsyncMock(return_value={"verified": True, "written": ["4001/3"]}),
                scenario_read_at="today",
            ) for entry in ("one", "two")
        }
        self.hass.data["vega"] = {entry: types.SimpleNamespace(runtime=runtime)
                                  for entry, runtime in self.runtimes.items()}
        _register_services(self.hass)

    async def test_multiple_trackers_require_target_without_broadcasting(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call("vega", "write_can_script", {"slot": 5, "script": {}},
                                                blocking=True, return_response=True)
        for runtime in self.runtimes.values():
            runtime.async_write_can_script.assert_not_called()

    async def test_explicit_target_and_revision_reach_only_selected_tracker(self):
        response = await self.hass.services.async_call(
            "vega", "write_can_script", {"entry_id": "two", "slot": 5, "script": {}, "expected_raw": "ab" * 82},
            blocking=True, return_response=True)
        self.assertEqual(response, {"verified": True, "slot": 5})
        self.runtimes["one"].async_write_can_script.assert_not_called()
        self.runtimes["two"].async_write_can_script.assert_awaited_once_with(5, {}, "ab" * 82)

    async def test_single_tracker_preserves_existing_service_calls(self):
        self.hass.data["vega"].pop("two")
        await self.hass.services.async_call("vega", "write_can_script", {"slot": 5, "script": {}}, blocking=True)
        self.runtimes["one"].async_write_can_script.assert_awaited_once_with(5, {}, None)

    async def test_unknown_target_does_not_fall_back_to_other_tracker(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call("vega", "read_can_scripts", {"entry_id": "missing"},
                                                blocking=True, return_response=True)
        for runtime in self.runtimes.values():
            runtime.async_read_can_scripts.assert_not_called()

    async def test_read_returns_scenario_availability(self):
        self.runtimes["one"].scenario_read_at = None
        response = await self.hass.services.async_call("vega", "read_can_scripts", {"entry_id": "one"},
                                                      blocking=True, return_response=True)
        self.assertEqual(response, {"slots": 8, "scenarios_read": False})
        self.runtimes["two"].async_read_can_scripts.assert_not_called()

    async def test_command_bundle_targets_one_tracker_and_returns_verification(self):
        document = {"scenarios": [], "scripts": []}
        expected = {"4001/3": "00" * 50}
        result = await self.hass.services.async_call("vega", "write_commands",
            {"entry_id": "two", "document": document, "expected": expected}, blocking=True, return_response=True)
        self.assertTrue(result["verified"])
        self.runtimes["two"].async_write_commands.assert_awaited_once_with(document, expected)
        self.runtimes["one"].async_write_commands.assert_not_called()

    async def test_commands_read_alias_is_registered(self):
        result = await self.hass.services.async_call("vega", "read_commands", {"entry_id": "one"}, blocking=True, return_response=True)
        self.assertEqual(result, {"slots": 8, "scenarios_read": True})


if __name__ == "__main__":
    unittest.main()
