"""Scenario codec and multi-record command saves, without a live tracker."""
import asyncio
import copy
import json
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_can_config import protocol, runtime_module, VegaRuntime, CommandError, FakeSession, SCENARIOS, SLOTS, document


def edit_document():
    return {"scenarios": [{"index": 3, **protocol.scenario_document(SCENARIOS[3])}],
            "scripts": [{"slot": 4, **document(4)}]}


def versions():
    return {**{f"4001/{i}": raw.hex() for i, raw in SCENARIOS.items()},
            **{f"5502/{i}": raw.hex() for i, raw in SLOTS.items()}}


class ScenarioCodecTests(unittest.TestCase):
    def test_all_25_records_roundtrip_including_disabled_condition_values(self):
        for index, raw in SCENARIOS.items():
            with self.subTest(index=index):
                self.assertEqual(protocol.build_scenario_from_dict(protocol.scenario_document(raw), raw), raw)

    def test_heating_and_cooling_include_output_one_and_clear_own_trigger(self):
        for index, output, slot in [(3, 7, 4), (5, 6, 2)]:
            actions = protocol.scenario_document(SCENARIOS[index])["actions"]
            self.assertEqual(actions, {"external_output_on": 1, "external_output_on_seconds": 0,
                                      "external_output_off": output, "external_output_off_seconds": 0, "can_script": slot})

    def test_unedited_actions_and_shared_selector_bits_are_preserved(self):
        raw = bytearray(SCENARIOS[3])
        raw[15:42] = bytes(range(1, 28))  # phone/SMS/physical outputs/photo
        raw[46:49] = b"\x55\xaa\xf0"
        raw[49] |= 3
        doc = protocol.scenario_document(raw)
        doc["actions"]["can_script"] = 0
        doc["actions"]["external_output_on"] = 2
        result = protocol.build_scenario_from_dict(doc, bytes(raw))
        self.assertEqual([i for i in range(50) if raw[i] != result[i]], [42, 49])
        self.assertEqual(result[49], 7)  # slot 0 plus original low two bits
        self.assertTrue(protocol.decode_scenario(result, 3)["has_other_actions"])

    def test_second_condition_keeps_its_position_and_disabling_clears_sensor(self):
        raw = bytearray(SCENARIOS[3])
        raw[:7] = bytes(7)
        struct.pack_into("<HfB", raw, 8, 1347, 0.0, 1)
        doc = protocol.scenario_document(raw)
        self.assertIsNone(doc["conditions"][0])
        self.assertEqual(protocol.build_scenario_from_dict(doc, bytes(raw)), raw)
        doc["conditions"][1] = None
        result = protocol.build_scenario_from_dict(doc, bytes(raw))
        self.assertEqual(result[8:15], bytes(7))

    def test_validation_rejects_bad_conditions_outputs_and_unknown_fields(self):
        baseline = protocol.scenario_document(SCENARIOS[3])
        cases = []
        for field, bad in [("external_output_on", 16), ("external_output_off", -1),
                           ("external_output_on_seconds", 256), ("external_output_off_seconds", True),
                           ("can_script", 8), ("can_script", True)]:
            d = copy.deepcopy(baseline); d["actions"][field] = bad; cases.append(d)
        for field, bad in [("sensor", 0), ("sensor", "1347"), ("comparison", "less"),
                           ("value", float("nan")), ("value", float("inf")), ("value", 1e100), ("value", True)]:
            d = copy.deepcopy(baseline); d["conditions"][0][field] = bad; cases.append(d)
        cases += [{**baseline, "match": "AND"}, {**baseline, "conditions": []}, {**baseline, "raw": "00"}]
        for d in cases:
            with self.subTest(doc=d), self.assertRaises(ValueError):
                protocol.build_scenario_from_dict(d, SCENARIOS[3])

    def test_remote_command_includes_scenarios_without_can_action(self):
        scenarios = [protocol.decode_scenario(raw, i) for i, raw in SCENARIOS.items()]
        bindings = protocol.command_script_bindings(scenarios)
        self.assertEqual([r["scenario"] for r in bindings["remote_on"]["scenario_routes"]], [1, 10, 11])
        self.assertEqual(bindings["remote_on"]["slots"], [])


class CommandSaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        async def executor(fn, *args): return fn(*args)
        hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
                                     async_add_executor_job=executor)
        self.runtime = VegaRuntime(hass, "test", 21443, "0000", "0000", 30, direct_control=True)
        self.session = FakeSession()
        self.runtime._inject_session = self.session
        self.doc = edit_document()
        self.doc["scenarios"][0]["actions"]["external_output_on"] = 2
        self.doc["scripts"][0]["name"] = "Edited preheat"
        self.expected = versions()
        self.runtime._scenarios = [protocol.decode_scenario(raw, i) for i, raw in SCENARIOS.items()]
        self.runtime._scenario_read_at = "today"

    async def test_whole_command_is_backed_up_written_in_dependency_order_and_verified(self):
        response = await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(response["written"], ["5502/4", "4001/3"])
        self.assertEqual([(sid, sub) for sid, sub, _ in self.session.writes], [(5502, 4), (4001, 3)])
        self.assertEqual(self.session.settings[4001, 3][42], 2)
        backup = json.loads(Path(self.temp.name, "vega", "command_backups_test.jsonl").read_text())
        self.assertEqual(backup["records"]["4001/3"]["before"], SCENARIOS[3].hex())
        self.assertEqual(backup["records"]["5502/4"]["before"], SLOTS[4].hex())
        cache = json.loads(Path(self.temp.name, "vega", "scenarios_test.json").read_text())
        self.assertEqual(next(s for s in cache if s["index"] == 3)["document"]["actions"]["external_output_on"], 2)

    async def test_unchanged_document_only_reads(self):
        response = await self.runtime.async_write_commands(edit_document(), self.expected)
        self.assertTrue(response["unchanged"])
        self.assertEqual(self.session.writes, [])

    async def test_changed_snapshot_aborts_every_write(self):
        self.session.settings[4001, 9] = bytes(50)
        with self.assertRaisesRegex(CommandError, "настройки изменились"):
            await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.writes, [])
        self.assertIsNone(self.runtime.command_slot("preheat"))

    async def test_invalid_last_record_never_sends_first_record(self):
        self.doc["scripts"][0]["frames"][0]["data"] = "ff"
        with self.assertRaises(CommandError):
            await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.reads, [])
        self.assertEqual(self.session.writes, [])

    async def test_missing_dependency_duplicate_record_and_missing_snapshot_rejected(self):
        for mutate in [lambda: self.expected.pop("5502/4"),
                       lambda: self.doc["scenarios"].append(self.doc["scenarios"][0]),
                       lambda: self.expected.pop("4001/3")]:
            self.doc = edit_document(); self.expected = versions(); mutate()
            with self.assertRaises(CommandError):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.reads, [])

    async def test_failed_first_write_stops_scenarios(self):
        self.session.mode = "mismatch"
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CommandError, "Подтверждённые записи: нет"):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(len(self.session.writes), 1)
        self.assertEqual(self.session.settings[4001, 3], SCENARIOS[3])

    async def test_partial_failure_reports_verified_script_and_preserves_actual_scenario(self):
        set_setting = self.session.set_setting
        async def fail_scenario(sid, sub, data):
            self.session.mode = "mismatch" if sid == 4001 else "normal"
            return await set_setting(sid, sub, data)
        self.session.set_setting = fail_scenario
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CommandError, "Подтверждённые записи: 5502/4"):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(len(self.session.writes), 2)
        self.assertEqual(self.session.settings[4001, 3], SCENARIOS[3])
        self.assertEqual(self.runtime.slot_name(4), "Edited preheat")
        self.assertEqual(self.runtime.command_bindings["preheat"]["status"], "unavailable")

    async def test_lost_acks_with_matching_readback_are_accepted_without_resend(self):
        self.session.mode = "lost_ack"
        response = await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertTrue(response["verified"])
        self.assertEqual(len(self.session.writes), 2)

    async def test_readback_timeout_clears_slot_and_stops_further_writes(self):
        self.session.mode = "read_timeout"
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CommandError, "нет ответа на чтение"):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertIsNone(self.runtime.slot_name(4))
        self.assertEqual(len(self.session.writes), 1)

    async def test_control_off_and_backup_failure_never_write(self):
        self.runtime.direct_control = False
        with self.assertRaises(CommandError):
            await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.reads, [])
        self.runtime.direct_control = True
        with patch.object(self.runtime, "_backup_command_edit", side_effect=CommandError("backup failure")):
            with self.assertRaises(CommandError):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.writes, [])

    async def test_disable_control_between_records_stops_remaining_write(self):
        set_setting = self.session.set_setting
        async def disable(sid, sub, data):
            result = await set_setting(sid, sub, data)
            self.runtime.direct_control = False
            return result
        self.session.set_setting = disable
        with self.assertRaisesRegex(CommandError, "Подтверждённые записи: 5502/4"):
            await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(len(self.session.writes), 1)

    async def test_change_between_preflight_and_write_is_not_overwritten(self):
        original_backup = self.runtime._backup_command_edit
        async def change(*args):
            await original_backup(*args)
            self.session.settings[5502, 4] = SLOTS[5]
        with patch.object(self.runtime, "_backup_command_edit", side_effect=change):
            with self.assertRaisesRegex(CommandError, "изменились перед записью"):
                await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertEqual(self.session.writes, [])

    async def test_cancelled_write_invalidates_uncertain_record(self):
        async def cancel(*args): raise asyncio.CancelledError
        self.session.set_setting = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.async_write_commands(self.doc, self.expected)
        self.assertIsNone(self.runtime.slot_name(4))


if __name__ == "__main__":
    unittest.main()
