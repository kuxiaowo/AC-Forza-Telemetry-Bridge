import ctypes as C
import math
import os
import socket
import struct
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from acbridge.acc import ACCPhysics, ACCGraphics, ACCStatic, from_acc, player_position
from acbridge.runtime import Bridge, DEFAULTS, load_config, validate
from acbridge.shared_memory import ACReader, Graphics, MissingGame, running_games
from acbridge.telemetry import Frame, encode


def fixture():
    # Independent byte offsets from the ACC v1.8.12 tables, Windows wchar=2,
    # pack=4. In particular the player is not at slot zero.
    physics = bytearray(580)
    struct.pack_into("<ifffii ff", physics, 0, 7, .5, .25, 30, 4, 6000, .5, 180)
    struct.pack_into("<4f", physics, 136, 10, 20, 30, 40)  # tyreDirtyLevel, unsupported
    struct.pack_into("<4f", physics, 152, 80, 81, 82, 83)
    struct.pack_into("<4f", physics, 184, .02, .03, .04, .05)
    struct.pack_into("<f", physics, 364, .75)
    struct.pack_into("<3f", physics, 568, 1, 2, 3)
    graphics = bytearray(1324)
    struct.pack_into("<iii", graphics, 0, 8, 2, 0)
    struct.pack_into("<iiiii", graphics, 132, 3, 2, 12345, 90456, 89123)
    struct.pack_into("<i", graphics, 252, 2)
    struct.pack_into("<6f", graphics, 256, 100, 200, 300, 10, 20, 30)
    struct.pack_into("<2i", graphics, 976, 7, 42)
    struct.pack_into("<i", graphics, 1216, 42)
    struct.pack_into("<i", graphics, 1320, 5)
    static = bytearray(420)
    for offset, text in ((0, "1.8.12"), (30, "1.10.4"), (68, "bmw_m4_gt3"), (134, "monza")):
        raw = text.encode("utf-16-le")
        static[offset:offset+len(raw)] = raw
    struct.pack_into("<if", static, 412, 7000, 120)
    return (ACCPhysics.from_buffer_copy(physics), ACCGraphics.from_buffer_copy(graphics),
            ACCStatic.from_buffer_copy(static))


class ACCProtocolTests(unittest.TestCase):
    def test_independent_abi_offsets(self):
        self.assertEqual((C.sizeof(ACCPhysics), C.sizeof(ACCGraphics), C.sizeof(ACCStatic)),
                         (580, 1324, 420))
        for name, offset in (("activeCars", 252), ("carCoordinates", 256),
                             ("carID", 976), ("playerCarID", 1216),
                             ("isSetupMenuVisible", 1256), ("sessionIndex", 1320)):
            self.assertEqual(getattr(ACCGraphics, name).offset, offset)
        _, g, _ = fixture()
        wrong = Graphics.from_buffer_copy(bytes(g)[:288])
        self.assertNotEqual(tuple(wrong.carCoordinates), player_position(g))

    def test_acc_core_fields_and_forza_offsets(self):
        p, g, s = fixture()
        p.wheelAngularSpeed[:] = (1, 2, 3, 4)
        frame = from_acc(p, g, s, DEFAULTS)
        data = encode(frame, 1234)
        self.assertEqual(len(data), 324)
        self.assertEqual(struct.unpack_from("<iI", data), (1, 1234))
        self.assertEqual(struct.unpack_from("<3f", data, 8), (7000, 0, 6000))
        self.assertEqual(struct.unpack_from("<f", data, 256)[0], 50)
        self.assertEqual(data[315:321], bytes((128, 64, 191, 0, 3, 64)))
        self.assertEqual(struct.unpack_from("<4f", data, 100), (1, 2, 3, 4))
        for actual, expected in zip(struct.unpack_from("<4f", data, 196), (.02, .03, .04, .05)):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(struct.unpack_from("<4f", data, 268), (176, 177.8, 179.6, 181.4)):
            self.assertAlmostEqual(actual, expected, places=4)
        self.assertAlmostEqual(struct.unpack_from("<f", data, 304)[0], 12.345, places=4)
        self.assertEqual(struct.unpack_from("<f", data, 288)[0], .25)
        self.assertEqual(frame.source_data["player_position"], (10, 20, 30))
        self.assertEqual((frame.fuel_quantity, frame.fuel_unit, frame.fuel_litres), (30, "原值", 0))

    def test_unsupported_fields_stay_zero_and_have_notes(self):
        p, g, s = fixture()
        p.accG[:] = (1, 2, 3)
        p.localAngularVelocity[:] = (4, 5, 6)
        p.turboBoost = 2
        s.maxPower, s.maxTorque = 999, 888  # documented as unused; do not trust
        frame = from_acc(p, g, s, DEFAULTS)
        data = encode(frame, 0)
        for start, end in ((20, 100), (244, 256), (260, 268), (284, 288)):
            self.assertEqual(data[start:end], bytes(end-start))
        self.assertEqual(frame.source_data["acc_g"], (1, 2, 3))
        self.assertEqual(frame.source_data["turbo_boost"], 2)
        self.assertTrue(any("归一化悬挂" in note for note in frame.notes))
        self.assertTrue(any("标定" in note for note in frame.notes))

    def test_clutch_settings_are_separate_from_ac1_and_steer_is_normalized(self):
        p, g, s = fixture()
        for ac_clutch in (False, True):
            config = {**DEFAULTS, "invert_clutch": ac_clutch}
            self.assertEqual(from_acc(p, g, s, config).clutch, .75)
            self.assertEqual(from_acc(p, g, s, config).steer, .5)
        frame = from_acc(p, g, s, {**DEFAULTS, "acc_invert_clutch": True, "invert_steer": True})
        self.assertEqual((frame.clutch, frame.steer), (.25, -.5))

    def test_player_selection_rejects_invalid_and_ambiguous_slots(self):
        _, g, _ = fixture()
        self.assertEqual(player_position(g), (10, 20, 30))
        for count in (-1, 0, 61, 100000):
            g.activeCars = count
            self.assertIsNone(player_position(g))
        g.activeCars = 2
        g.playerCarID = 999
        self.assertIsNone(player_position(g))
        g.playerCarID = 42
        g.carID[0] = 42
        self.assertIsNone(player_position(g))
        g.carID[0] = 7
        g.carCoordinates[1][0] = math.nan
        self.assertIsNone(player_position(g))

    def test_nonlive_setup_and_invalid_values(self):
        p, g, s = fixture()
        for status in (0, 1, 3, 99):
            g.status = status
            self.assertFalse(from_acc(p, g, s, DEFAULTS).active)
        g.status, g.isSetupMenuVisible = 2, 1
        self.assertFalse(from_acc(p, g, s, DEFAULTS).active)
        g.isSetupMenuVisible = 0
        s.maxRpm, s.maxFuel, p.speedKmh, p.gas = 0, math.nan, math.inf, math.nan
        frame = from_acc(p, g, s, DEFAULTS)
        self.assertEqual((frame.max_rpm, frame.fuel_fraction, frame.speed_kmh, frame.throttle), (9000, 0, 0, 0))

    def test_ac_acc_vehicle_identity_isolated_including_inactive(self):
        ac = encode(Frame(car="same_car"), 0)
        acc = encode(Frame(game="acc", car="same_car"), 0)
        self.assertNotEqual(ac[212:216], acc[212:216])
        frame = from_acc(*fixture(), DEFAULTS)
        inactive = Frame(game=frame.game, car=frame.car)
        self.assertEqual(encode(frame, 0)[212:216], encode(inactive, 0)[212:216])

    def test_config_migration_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"invert_clutch": false}', encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config["game"], "ac")
        self.assertFalse(config["invert_clutch"])
        self.assertFalse(config["acc_invert_clutch"])
        for changes in ({"game": "auto"}, {"game": None}, {"acc_invert_clutch": 1}):
            with self.assertRaises(ValueError):
                validate(changes)

    def test_wrong_or_ambiguous_producer_never_opens_shared_pages(self):
        for game, games in (("ac", {"acc"}), ("acc", {"ac"}),
                            ("ac", {"ac", "acc"}), ("acc", set())):
            with self.subTest(game=game, games=games), patch("acbridge.shared_memory.running_games", return_value=games), \
                    patch("acbridge.shared_memory.ReadPage") as page:
                with self.assertRaises(MissingGame):
                    ACReader(game=game).read()
                page.assert_not_called()

    def test_process_cache_is_bounded_and_reconnect_rechecks(self):
        reader = ACReader(game="acc")
        reader.pages = [Mock(), Mock(), Mock()]
        with patch("acbridge.shared_memory.running_games", return_value={"acc"}) as games, \
                patch("acbridge.shared_memory.time.monotonic", return_value=10.) as clock:
            reader.read()
            clock.return_value = 10.1
            reader.read()
            self.assertEqual(games.call_count, 1)
            clock.return_value = 10.26
            games.return_value = {"ac", "acc"}
            with self.assertRaises(MissingGame):
                reader.read()
            self.assertEqual(games.call_count, 2)
            reader.close()
            games.return_value = {"ac"}
            with self.assertRaises(MissingGame):
                reader.read()
            self.assertEqual(games.call_count, 3)

    @unittest.skipUnless(os.name == "nt", "Windows process snapshot")
    def test_actual_process_enumeration(self):
        self.assertLessEqual(running_games(), {"ac", "acc"})


@unittest.skipUnless(os.name == "nt", "Windows shared memory and UDP")
class ACCRuntimeTests(unittest.TestCase):
    def test_real_mappings_udp_pause_session_switch_and_stale_reconnect(self):
        import mmap
        prefix = "ACCBridgeTest_" + uuid.uuid4().hex + "_"
        pages = []
        reader = ACReader(prefix, game="acc")
        p, g, s = fixture()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(2)
            bridge = Bridge({**DEFAULTS, "game": "acc", "port": receiver.getsockname()[1],
                             "stale_seconds": .5}, reader=reader, base=Path("unused_acc_path"))

            def receive(predicate):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    data = receiver.recv(4096)
                    if predicate(data):
                        return data
                self.fail("Expected ACC UDP packet not received")

            def update():
                g.packetId += 1
                pages[1].seek(0)
                pages[1].write(bytes(g))

            try:
                with self.assertRaises(MissingGame):
                    reader.read()
                for suffix, value in zip(("physics", "graphics", "static"), (p, g, s)):
                    page = mmap.mmap(-1, C.sizeof(value), tagname=prefix+suffix)
                    page.write(bytes(value))
                    pages.append(page)
                before = [page[:] for page in pages]
                with patch("acbridge.vehicle_curve.find_car", side_effect=AssertionError("ACC must not scan AC1 files")), \
                        patch("acbridge.acc_vehicle.ACCVehicleData.read", return_value=dict(
                            suspension_reference_m=(.08, .08, .1, .1), engine_idle_rpm=900)):
                    bridge.start()
                    live = receive(lambda d: struct.unpack_from("<i", d)[0] == 1)
                    self.assertEqual(struct.unpack_from("<f", live, 256)[0], 50)
                    self.assertGreater(min(struct.unpack_from("<4f", live, 68)), .1)
                    self.assertEqual(struct.unpack_from("<f", live, 12)[0], 900)
                    self.assertEqual([page[:] for page in pages], before)
                    self.assertEqual(bridge.get_snapshot()["frame"]["game"], "acc")
                    g.status = 3
                    update()
                    paused = receive(lambda d: struct.unpack_from("<i", d)[0] == 0)
                    self.assertEqual(paused[212:216], live[212:216])
                    self.assertEqual(struct.unpack_from("<f", paused, 256)[0], 0)
                    g.status = 2
                    update()
                    receive(lambda d: struct.unpack_from("<i", d)[0] == 1)
                    g.sessionIndex += 1
                    update()
                    receive(lambda d: struct.unpack_from("<i", d)[0] == 0)
                    receive(lambda d: struct.unpack_from("<i", d)[0] == 1)
                    # Producer now stops updating. Reopening the same pages must
                    # not reactivate stale data until packet counters change.
                    receive(lambda d: struct.unpack_from("<i", d)[0] == 0)
                    deadline = time.monotonic() + .7
                    while time.monotonic() < deadline:
                        self.assertEqual(struct.unpack_from("<i", receiver.recv(4096))[0], 0)
                    update()
                    receive(lambda d: struct.unpack_from("<i", d)[0] == 1)
            finally:
                bridge.stop()
                if bridge.thread:
                    bridge.thread.join(3)
                reader.close()
                for page in pages:
                    page.close()
            self.assertFalse(bridge.thread.is_alive())
            self.assertEqual(bridge.get_snapshot()["frame"]["game"], "acc")


if __name__ == "__main__":
    unittest.main()
