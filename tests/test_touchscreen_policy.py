from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import unittest

from t300_mainline.touchscreen_policy import (
    error_response,
    review_request,
    success_response,
    translate_response,
)
from t300_mainline.touchscreen_gateway import (
    build_parser,
    decode_bridge_message,
    local_noop_log_level,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "mainline/touchscreen/button-contract.json"


def rpc(script: str, request_id: int = 1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "printer.gcode.script",
        "params": {"script": script},
    }


class TouchscreenContractTests(unittest.TestCase):
    def test_contract_is_complete_and_machine_readable(self):
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        controls = data["controls"]
        ids = [item["id"] for item in controls]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 60)
        dispositions = set(data["dispositions"])
        for item in controls:
            self.assertEqual(
                set(item),
                {"id", "page", "label", "stock_action", "candidate_action", "disposition"},
            )
            self.assertIn(item["disposition"], dispositions)

    def test_contract_discloses_irreducible_expectation_differences(self):
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        controls = {item["id"]: item for item in data["controls"]}
        for control_id in (
            "files.delete",
            "files.timelapse_delete",
            "files.timelapse_export",
            "top.led",
            "control.led",
            "print.led",
            "level.z_tilt",
            "level.z_offset",
            "level.mesh",
            "system.factory_reset",
            "system.vendor_update",
        ):
            self.assertEqual(controls[control_id]["disposition"], "explicitly_refused")
        self.assertIn("0.05", controls["tune.z.step.01"]["candidate_action"])
        self.assertIn("500%", controls["tune.speed"]["candidate_action"])
        for control_id in ("move.home.z", "move.home.all"):
            action = controls[control_id]["candidate_action"]
            self.assertIn("cleaned-and-rearmed", action)
            self.assertNotIn("one-use", action)
        macro_action = controls["bottom.macros"]["candidate_action"]
        self.assertIn("Printer Status", macro_action)
        for label in ("Pause", "Resume", "Stop", "Change Filament"):
            self.assertIn(label, macro_action)
        self.assertIn("dedicated stock controls", macro_action)


class TouchscreenPolicyTests(unittest.TestCase):
    def test_only_known_vendor_polling_noops_are_quiet(self):
        self.assertEqual(
            local_noop_log_level(
                "The legacy bridge may not disable runout protection."
            ),
            logging.DEBUG,
        )
        self.assertEqual(
            local_noop_log_level(
                "The stock bridge startup restart is intentionally suppressed."
            ),
            logging.INFO,
        )

    def test_gateway_defaults_are_split_loopback_endpoints(self):
        args = build_parser().parse_args([])
        self.assertEqual((args.listen_host, args.listen_port), ("127.0.0.1", 7125))
        self.assertEqual((args.upstream_host, args.upstream_port), ("127.0.0.1", 7126))

    def test_suppresses_unsolicited_startup_restart(self):
        decision = review_request(rpc("FIRMWARE_RESTART"))
        self.assertEqual(decision.outcome, "emulate_success")
        self.assertEqual(success_response(rpc("FIRMWARE_RESTART"))["result"], "ok")
        explicit = review_request(rpc("FIRMWARE_RESTART"), allow_explicit_restart=True)
        self.assertEqual(explicit.outcome, "forward")

    def test_rejects_malformed_jsonrpc_envelopes_and_duplicate_keys(self):
        malformed = (
            {"id": 1, "method": "printer.info"},
            {"jsonrpc": "1.0", "id": 1, "method": "printer.info"},
            {"jsonrpc": "2.0", "id": True, "method": "printer.info"},
            {"jsonrpc": "2.0", "id": -1, "method": "printer.info"},
            {"jsonrpc": "2.0", "id": 1, "method": "printer.info", "extra": 1},
        )
        for request in malformed:
            with self.subTest(request=request):
                self.assertEqual(review_request(request).outcome, "reject")
        with self.assertRaises(ValueError):
            decode_bridge_message(
                '{"jsonrpc":"2.0","id":1,"id":2,"method":"printer.info"}'
            )
        with self.assertRaises(ValueError):
            decode_bridge_message("[]")

    def test_lifecycle_apis_refuse_hidden_parameters(self):
        for method in (
            "printer.info",
            "printer.emergency_stop",
            "printer.firmware_restart",
            "printer.print.pause",
            "printer.print.resume",
            "printer.print.cancel",
            "printer.restart",
            "server.files.roots",
            "server.info",
        ):
            with self.subTest(method=method):
                clean = {"jsonrpc": "2.0", "id": 2, "method": method}
                self.assertEqual(review_request(clean).outcome, "forward")
                dirty = {**clean, "params": {"surprise": True}}
                self.assertEqual(review_request(dirty).outcome, "reject")

    def test_file_apis_are_confined_to_canonical_gcode_names(self):
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "printer.print.start",
            "params": {"filename": "sda1/models/part.gcode"},
        }
        decision = review_request(request)
        self.assertEqual(decision.outcome, "forward")
        self.assertEqual(decision.request["params"], {"filename": "models/part.gcode"})
        for filename in (
            "/absolute.gcode",
            "../escape.gcode",
            "models/../escape.gcode",
            "sda1",
            "model.stl",
            "model.gcode\x00extra",
        ):
            with self.subTest(filename=filename):
                unsafe = {**request, "params": {"filename": filename}}
                self.assertEqual(review_request(unsafe).outcome, "reject")
        listing = {"jsonrpc": "2.0", "id": 4, "method": "server.files.list"}
        self.assertEqual(review_request(listing).outcome, "forward")
        self.assertEqual(
            review_request({**listing, "params": {"root": "config"}}).outcome,
            "reject",
        )
        self.assertEqual(
            review_request({**listing, "params": None}).outcome,
            "reject",
        )

    def test_stop_sequence_cancels_once_without_vendor_cleanup(self):
        sequence = (
            "SET_HEATER_TEMPERATURE heater=extruder target=0",
            "SET_HEATER_TEMPERATURE heater=heater_bed target=0",
            "SDCARD_RESET_FILE",
            "CANCEL_PRINT",
            "clear_last_file",
            "RUN_SHELL_COMMAND CMD=clear_plr",
        )
        decisions = [review_request(rpc(script)) for script in sequence]
        self.assertEqual(
            [item.outcome for item in decisions],
            ["forward", "forward", "emulate_success", "forward", "emulate_success", "emulate_success"],
        )
        self.assertEqual(
            [item.request["params"]["script"] for item in decisions if item.outcome == "forward"].count("CANCEL_PRINT"),
            1,
        )

    def test_translates_jogs_without_leaking_coordinate_mode(self):
        decision = review_request(rpc("G91\nG1 X-50.000000 F7800\nG90"))
        self.assertEqual(decision.outcome, "forward")
        self.assertEqual(
            decision.request["params"]["script"],
            "T_SCREEN_JOG AXIS=X DISTANCE=-50 SPEED=130",
        )
        for script in ("G90", "G91"):
            self.assertEqual(review_request(rpc(script)).outcome, "emulate_success")

    def test_rejects_untraced_jog_distance_and_feed(self):
        for script in (
            "G91\nG1 X100 F7800\nG90",
            "G91\nG1 X1 F9000\nG90",
            "G91\nG1 A1 F7800\nG90",
        ):
            with self.subTest(script=script):
                self.assertEqual(review_request(rpc(script)).outcome, "reject")

    def test_translates_bounded_hot_filament_buttons(self):
        for distance in (-5, 5, 10, -3, 3):
            with self.subTest(distance=distance):
                decision = review_request(rpc("G1 E%s F300" % distance))
                self.assertEqual(decision.outcome, "forward")
                self.assertEqual(
                    decision.request["params"]["script"],
                    "T_SCREEN_FILAMENT DISTANCE=%s SPEED=5" % distance,
                )
        self.assertEqual(review_request(rpc("G1 E11 F300")).outcome, "reject")
        self.assertEqual(review_request(rpc("G1 E5 F301")).outcome, "reject")

    def test_preserves_stock_temperature_presets_inside_hard_limits(self):
        for script in (
            "SET_HEATER_TEMPERATURE heater=extruder target=210",
            "SET_HEATER_TEMPERATURE heater=extruder target=260",
            "SET_HEATER_TEMPERATURE heater=heater_bed target=60",
            "SET_HEATER_TEMPERATURE heater=heater_bed target=80",
            "SET_HEATER_TEMPERATURE heater=extruder target=0",
            "SET_HEATER_TEMPERATURE heater=heater_bed target=0",
        ):
            with self.subTest(script=script):
                self.assertEqual(review_request(rpc(script)).outcome, "forward")
        self.assertEqual(
            review_request(rpc("SET_HEATER_TEMPERATURE heater=extruder target=301")).outcome,
            "reject",
        )
        self.assertEqual(
            review_request(rpc("SET_HEATER_TEMPERATURE heater=heater_bed target=101")).outcome,
            "reject",
        )

    def test_preserves_stock_speed_and_flow_ranges(self):
        for factor in (10, 75, 100, 125, 500):
            self.assertEqual(review_request(rpc("M220 S%s" % factor)).outcome, "forward")
        for factor in (80, 100, 120):
            self.assertEqual(review_request(rpc("M221 S%s" % factor)).outcome, "forward")
        self.assertEqual(review_request(rpc("M220 S501")).outcome, "reject")
        self.assertEqual(review_request(rpc("M221 S121")).outcome, "reject")

    def test_live_z_refuses_large_stock_steps_before_klipper(self):
        self.assertEqual(
            review_request(rpc("SET_GCODE_OFFSET Z_ADJUST=+0.050000 MOVE=1")).outcome,
            "forward",
        )
        for step in ("+0.100000", "-0.100000", "+0.500000", "-0.500000"):
            decision = review_request(rpc("SET_GCODE_OFFSET Z_ADJUST=%s MOVE=1" % step))
            self.assertEqual(decision.outcome, "reject")
            self.assertIn("0.05", decision.reason)

    def test_home_and_emergency_paths_remain_available(self):
        for script in ("G28", "G28 X", "G28 Y", "G28 X Y", "G28 Z", "M84", "PAUSE", "RESUME", "CANCEL_PRINT", "M600", "PRINTER_STATUS"):
            with self.subTest(script=script):
                self.assertEqual(review_request(rpc(script)).outcome, "forward")
        decision = review_request(
            {"jsonrpc": "2.0", "id": 2, "method": "printer.emergency_stop"}
        )
        self.assertEqual(decision.outcome, "forward")

    def test_production_refuses_maintenance_controls(self):
        for script in (
            "Z_TILT_CALIBRATION",
            "PROBE_CALIBRATE",
            "heated_bed",
            "TESTZ z=+0.05",
            "ACCEPT",
            "ABORT",
            "SAVE_CONFIG",
        ):
            with self.subTest(script=script):
                decision = review_request(rpc(script))
                self.assertEqual(decision.outcome, "reject")
                self.assertIn("maintenance", decision.reason)

    def test_legacy_bridge_cannot_disable_runout_protection(self):
        decision = review_request(
            rpc("SET_FILAMENT_SENSOR SENSOR=my_sensor ENABLE=0")
        )
        self.assertEqual(decision.outcome, "emulate_success")
        self.assertIn("may not disable", decision.reason)
        enabled = review_request(
            rpc("SET_FILAMENT_SENSOR SENSOR=my_sensor ENABLE=1")
        )
        self.assertEqual(enabled.outcome, "emulate_success")
        self.assertIn("already forced on", enabled.reason)

    def test_vendor_power_loss_resume_is_explicitly_refused(self):
        decision = review_request(rpc("RESUME_INTERRUPTED"))
        self.assertEqual(decision.outcome, "reject")
        self.assertIn("power-loss", decision.reason)

    def test_object_requests_translate_aliases_and_strip_fake_outputs(self):
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "printer.objects.subscribe",
            "params": {
                "objects": {
                    "filament_switch_sensor my_sensor": ["filament_detected", "enabled"],
                    "heater_fan fan1": ["speed"],
                    "heater_fan my_nozzle_fan1": None,
                    "output_pin caselight": None,
                    "toolhead": None,
                }
            },
        }
        decision = review_request(request)
        self.assertEqual(decision.outcome, "forward")
        objects = decision.request["params"]["objects"]
        self.assertEqual(
            objects["filament_switch_sensor filament_runout"],
            ["filament_detected", "enabled"],
        )
        self.assertIsNone(objects["heater_fan hotend_fan"])
        self.assertNotIn("output_pin caselight", objects)

    def test_object_requests_reject_unreviewed_names_and_fields(self):
        base = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "printer.objects.query",
            "params": {"objects": {"toolhead": ["position"]}},
        }
        self.assertEqual(review_request(base).outcome, "forward")
        unknown = deepcopy(base)
        unknown["params"]["objects"] = {"manual_stepper unsafe": None}
        self.assertEqual(review_request(unknown).outcome, "reject")
        bad_fields = deepcopy(base)
        bad_fields["params"]["objects"] = {"toolhead": "position"}
        self.assertEqual(review_request(bad_fields).outcome, "reject")

    def test_status_translation_restores_legacy_names_and_fields(self):
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "eventtime": 1.0,
                "status": {
                    "filament_switch_sensor filament_runout": {
                        "enabled": True,
                        "filament_detected": True,
                    },
                    "heater_fan hotend_fan": {"speed": 1.0},
                    "toolhead": {"max_accel": 12000.0, "minimum_cruise_ratio": 0.75},
                    "configfile": {
                        "settings": {
                            "printer": {"max_accel": 12000.0, "minimum_cruise_ratio": 0.75},
                            "gcode_macro PRINTER_STATUS": {},
                            "gcode_macro PAUSE": {"variable": 1},
                            "gcode_macro M600": {},
                            "gcode_macro START_PRINT": {},
                            "gcode_macro _T_SAFE_PARK": {},
                        }
                    },
                },
            },
        }
        translated = translate_response(message)
        status = translated["result"]["status"]
        self.assertTrue(status["filament_switch_sensor my_sensor"]["filament_detected"])
        self.assertEqual(status["heater_fan fan1"]["speed"], 1.0)
        self.assertEqual(status["heater_fan my_nozzle_fan1"]["speed"], 1.0)
        self.assertEqual(status["toolhead"]["max_accel_to_decel"], 3000.0)
        self.assertEqual(
            status["configfile"]["settings"]["printer"]["max_accel_to_decel"],
            3000.0,
        )
        settings = status["configfile"]["settings"]
        self.assertIn("gcode_macro PRINTER_STATUS", settings)
        self.assertNotIn("gcode_macro PAUSE", settings)
        self.assertNotIn("gcode_macro M600", settings)
        self.assertNotIn("gcode_macro START_PRINT", settings)
        self.assertNotIn("gcode_macro _T_SAFE_PARK", settings)
        self.assertEqual(status["output_pin sound"]["value"], 0.0)

    def test_status_notifications_are_translated(self):
        translated = translate_response(
            {
                "jsonrpc": "2.0",
                "method": "notify_status_update",
                "params": [
                    {"filament_switch_sensor filament_runout": {"enabled": False}},
                    10.0,
                ],
            }
        )
        self.assertFalse(
            translated["params"][0]["filament_switch_sensor my_sensor"]["enabled"]
        )

    def test_blocks_system_mutation_apis_and_unknown_gcode(self):
        for method in (
            "machine.update.full",
            "machine.reboot",
            "machine.shutdown",
            "machine.services.restart",
            "server.files.delete_file",
        ):
            with self.subTest(method=method):
                decision = review_request({"jsonrpc": "2.0", "id": 3, "method": method})
                self.assertEqual(decision.outcome, "reject")
                self.assertIn("laptop", decision.reason)
        decision = review_request(rpc("FORCE_MOVE STEPPER=stepper_x DISTANCE=1"))
        self.assertEqual(decision.outcome, "reject")
        response = error_response(rpc("FORCE_MOVE STEPPER=stepper_x DISTANCE=1"), decision.reason)
        self.assertEqual(response["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
