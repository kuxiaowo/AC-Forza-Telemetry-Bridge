import ctypes as C
import json
import math
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from acbridge.shared_memory import Physics, Graphics, Static, ACReader, MissingGame, utf16
from acbridge.telemetry import Frame, demo_frame, encode, from_ac, gear_byte, pedal
from acbridge.runtime import DEFAULTS, Bridge, PortGuard, load_config, save_config, validate


def text_field(obj, name, text):
    array = getattr(obj, name)
    raw = text.encode("utf-16-le")
    C.memmove(C.addressof(array), raw, len(raw))


def fixture():
    # Construct bytes at independently recorded SDK offsets, not via encoder fields.
    p = bytearray(580)
    struct.pack_into("<ifffii ff", p, 0, 7, .5, .25, 30., 5, 4321, -.5, 123.4)
    struct.pack_into("<3f", p, 44, 1, 0, -.5)
    struct.pack_into("<4f", p, 152, 70, 80, 90, 100)
    struct.pack_into("<f", p, 364, .75)
    struct.pack_into("<3f", p, 568, 0, 0, 123.4/3.6)
    g = bytearray(288)
    struct.pack_into("<iii", g, 0, 8, 2, 0)
    struct.pack_into("<iiiii", g, 132, 3, 2, 12345, 90456, 89123)
    struct.pack_into("<3f", g, 252, 1, 2, 3)
    s = bytearray(556)
    s[30:42] = "1.16.4".encode("utf-16-le")
    s[68:76] = "test".encode("utf-16-le")
    struct.pack_into("<if", s, 412, 8000, 60)
    return Physics.from_buffer_copy(p), Graphics.from_buffer_copy(g), Static.from_buffer_copy(s)


class ProtocolTests(unittest.TestCase):
    def test_sdk_offsets_and_utf16_padding(self):
        self.assertEqual((C.sizeof(Physics), C.sizeof(Graphics), C.sizeof(Static)), (580, 288, 556))
        self.assertEqual(Physics.clutch.offset, 364)
        self.assertEqual(Physics.localVelocity.offset, 568)
        self.assertEqual(Graphics.carCoordinates.offset, 252)
        self.assertEqual(Static.maxRpm.offset, 412)
        self.assertEqual(Static.trackConfiguration.offset, 524)
        _, _, s = fixture()
        self.assertEqual(utf16(s.acVersion), "1.16.4")
        text_field(s, "carModel", "赛车测试")
        self.assertEqual(utf16(s.carModel), "赛车测试")

    def test_known_packet_offsets_and_units(self):
        f = from_ac(*fixture(), DEFAULTS)
        data = encode(f, 1000)
        self.assertEqual(len(data), 324)
        self.assertEqual(struct.unpack_from("<iI", data, 0), (1, 1000))
        self.assertEqual(struct.unpack_from("<f", data, 16)[0], 4321)
        self.assertAlmostEqual(struct.unpack_from("<f", data, 256)[0], 123.4/3.6, places=4)
        self.assertEqual(struct.unpack_from("<4f", data, 268), (158, 176, 194, 212))
        self.assertEqual(struct.unpack_from("<f", data, 288)[0], .5)
        self.assertEqual(data[315:321], bytes([128, 64, 64, 0, 4, 192]))
        self.assertAlmostEqual(struct.unpack_from("<f", data, 304)[0], 12.345, places=4)
        self.assertEqual(struct.unpack_from("<H", data, 312)[0], 3)
        self.assertAlmostEqual(struct.unpack_from("<f", data, 20)[0], 9.80665, places=5)
        self.assertEqual(struct.unpack_from("<3f", data, 244), (1, 2, 3))

    def test_extended_format_and_counter_wrap(self):
        d = encode(demo_frame(), 2**32+7, "fh5")
        self.assertEqual(len(d), 331)
        self.assertEqual(struct.unpack_from("<I", d, 4)[0], 7)
        self.assertEqual(d[324:], bytes(7))
        with self.assertRaises(ValueError):
            encode(Frame(), 0, "unknown")

    def test_gears(self):
        self.assertEqual([gear_byte(x) for x in (0, 1, 2, 5, 7, -1, 99)], [0, 11, 1, 4, 6, 11, 11])

    def test_pedal_limits_nonfinite_and_calibration(self):
        self.assertEqual([pedal(x) for x in (-1, 0, .5, 1, 2, math.nan, math.inf)], [0, 0, 128, 255, 255, 0, 0])
        p, g, s = fixture()
        f = from_ac(p, g, s, {**DEFAULTS, "invert_clutch": False, "invert_steer": True})
        self.assertEqual(f.clutch, .75)
        self.assertEqual(f.steer, .5)
        s.maxFuel = 0
        s.maxRpm = 0
        p.speedKmh = math.nan
        f = from_ac(p, g, s, DEFAULTS)
        self.assertEqual((f.fuel_fraction, f.max_rpm, f.speed_kmh), (0, 9000, 0))
        self.assertTrue(any("最高转速" in note for note in f.notes))
        self.assertTrue(any("油箱容量" in note for note in f.notes))

    def test_nonlive_states(self):
        p, g, s = fixture()
        for status in (0, 1, 3, 99):
            g.status = status
            self.assertFalse(from_ac(p, g, s, DEFAULTS).active)

    def test_identity_and_unknown_fields(self):
        a = encode(demo_frame(), 0)
        f = demo_frame()
        f.car = "another_car"
        b = encode(f, 0)
        self.assertNotEqual(a[212:216], b[212:216])
        self.assertEqual(a[260:268], bytes(8))
        self.assertEqual(struct.unpack_from("<i", a, 220)[0], 1)
        self.assertEqual(a[216:220], bytes(4))
        self.assertEqual(a[224:232], bytes(8))
        self.assertEqual(a[84:100], bytes(16))

    def test_config_validation_and_roundtrip(self):
        for key, value in (("port", 0), ("port", 65536), ("hz", 0), ("hz", 60.1), ("host", "example.com"),
                           ("invert_clutch", "false"), ("stale_seconds", math.nan)):
            with self.assertRaises((ValueError, TypeError)):
                validate({key: value})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            save_config(path, DEFAULTS)
            self.assertEqual(load_config(path), DEFAULTS)


class FakeReader:
    def __init__(self):
        self.mode = "live"
        self.count = 0
        self.closed = 0

    def read(self):
        if self.mode == "missing":
            raise MissingGame("not running")
        p, g, s = fixture()
        if self.mode != "stale":
            self.count += 1
        p.packetId = g.packetId = self.count
        if self.mode == "paused":
            g.status = 3
        if self.mode == "othercar":
            text_field(s, "carModel", "other")
        return p, g, s

    def close(self):
        self.closed += 1


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2)
        self.reader = FakeReader()
        self.bridge = Bridge({**DEFAULTS, "port": self.sock.getsockname()[1], "stale_seconds": .5}, reader=self.reader)

    def tearDown(self):
        self.bridge.stop()
        if self.bridge.thread:
            self.bridge.thread.join(2)
        self.sock.close()

    def receive_until(self, predicate, seconds=3):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            data = self.sock.recv(4096)
            if predicate(data):
                return data
        self.fail("Expected UDP packet not received")

    def test_actual_udp_pause_resume_disconnect_and_stop(self):
        self.bridge.start()
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.reader.mode = "paused"
        data = self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        self.assertEqual(struct.unpack_from("<f", data, 256)[0], 0)
        self.reader.mode = "live"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.reader.mode = "missing"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        self.reader.mode = "live"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.bridge.stop()
        self.bridge.thread.join(2)
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        self.assertFalse(self.bridge.thread.is_alive())

    def test_stale_page_does_not_reactivate_on_reopen(self):
        self.bridge.start()
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.reader.mode = "stale"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        deadline = time.monotonic()+1.2
        while time.monotonic()<deadline:
            self.assertEqual(struct.unpack_from("<i", self.sock.recv(4096))[0], 0)
        self.reader.mode = "live"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)

    def test_vehicle_switch_inserts_boundary(self):
        self.bridge.start()
        old = self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.reader.mode = "othercar"
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        new = self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.assertNotEqual(old[212:216], new[212:216])

    def test_restart_preserves_monotonic_protocol_clock(self):
        self.bridge.start()
        old = self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        self.bridge.stop()
        self.bridge.thread.join(2)
        self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 0)
        time.sleep(.03)
        self.bridge = Bridge({**DEFAULTS, "port": self.sock.getsockname()[1]}, simulated=True)
        self.bridge.start()
        new = self.receive_until(lambda d: struct.unpack_from("<i", d)[0] == 1)
        before = struct.unpack_from("<I", old, 4)[0]
        after = struct.unpack_from("<I", new, 4)[0]
        self.assertGreater((after-before) & 0xffffffff, 0)
        self.assertLess((after-before) & 0xffffffff, 1000)

    @unittest.skipUnless(os.name == "nt", "Windows mutex")
    def test_duplicate_sender_blocked(self):
        first = PortGuard("127.0.0.1", self.sock.getsockname()[1])
        try:
            with self.assertRaises(OSError):
                PortGuard("127.0.0.1", self.sock.getsockname()[1])
        finally:
            first.close()

    @unittest.skipUnless(os.name == "nt", "Windows mappings")
    def test_real_windows_mappings_readonly_and_missing(self):
        # Unique test names cannot overwrite or touch the running game's real pages.
        import mmap
        prefix = "ACBridgeTest_"+uuid.uuid4().hex+"_"
        reader = ACReader(prefix)
        with self.assertRaises(MissingGame):
            reader.read()
        pages = []
        try:
            for name, value in zip(("physics", "graphics", "static"), fixture()):
                page = mmap.mmap(-1, C.sizeof(value), tagname=prefix+name)
                page.write(bytes(value))
                pages.append(page)
            p, g, s = reader.read()
            self.assertEqual((p.rpms, g.iCurrentTime, utf16(s.carModel)), (4321, 12345, "test"))
            self.assertEqual(pages[0][:], bytes(fixture()[0]))
        finally:
            reader.close()
            for page in pages:
                page.close()
        with self.assertRaises(MissingGame):
            reader.read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
