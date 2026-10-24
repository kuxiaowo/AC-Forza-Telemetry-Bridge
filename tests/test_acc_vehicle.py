"""Synthetic UE4 fixtures only; no proprietary game resources or keys."""
import hashlib
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from acbridge.acc import from_acc
from acbridge.acc_vehicle import Pak, Reader, aes_decrypt, asset_properties
from acbridge.runtime import DEFAULTS
from acbridge.telemetry import encode
from tests.test_acc import fixture


def synthetic_asset():
    names = ["None", "FloatProperty", "IntProperty", "StructProperty", "ArrayProperty",
             "Suspensions", "SuspData", "hubFront", "hubRear", "SuspHubData", "bumpStop",
             "SuspBumpStop", "up", "down", "SuspBumpStopDef", "Range", "EngineData",
             "Minimum", "Limiter", "Unused"]

    def fname(name):
        return struct.pack("<ii", names.index(name), 0)

    def prop(name, kind, payload, extra=b""):
        return fname(name) + fname(kind) + struct.pack("<ii", len(payload), 0) + extra + b"\0" + payload

    def scalar(name, value, kind="FloatProperty"):
        return prop(name, kind, struct.pack("<f" if kind == "FloatProperty" else "<i", value))

    def group(name, kind, contents):
        return prop(name, "StructProperty", contents + fname("None"), fname(kind) + bytes(16))

    def axle(name, up, down):
        return group(name, "SuspHubData", group("bumpStop", "SuspBumpStop",
                     group("up", "SuspBumpStopDef", scalar("Range", up)) +
                     group("down", "SuspBumpStopDef", scalar("Range", down))))

    export = group("Suspensions", "SuspData", axle("hubFront", .06, -.02) + axle("hubRear", .08, -.01))
    export += group("EngineData", "EngineData", scalar("Minimum", 1000, "IntProperty") +
                    scalar("Limiter", 8000, "IntProperty"))
    # Unknown array payload can be skipped by size, without guessing its schema.
    export += prop("Unused", "ArrayProperty", struct.pack("<i", 0), fname("StructProperty"))
    export += fname("None") + bytes(8)
    table = b"".join(struct.pack("<i", len(n) + 1) + n.encode() + b"\0" + bytes(4) for n in names)
    header = struct.pack("<Iiiiiii", 0x9e2a83c1, -7, 0, 0, 0, 0, 64 + len(table))
    header += struct.pack("<i", 5) + b"None\0" + struct.pack("<Iii", 0, len(names), 64)
    return header.ljust(64, b"\0") + table, export


class ACCAssetTests(unittest.TestCase):
    def test_named_structures_and_scalar_types(self):
        values = asset_properties(*synthetic_asset())
        self.assertEqual(values["EngineData.Limiter"], 8000)
        self.assertEqual(values["EngineData.Minimum"], 1000)
        self.assertAlmostEqual(values["Suspensions.hubRear.bumpStop.down.Range"], -.01)
        self.assertNotIn("Unused", values)

    def test_unknown_version_truncated_names_and_export_fail_closed(self):
        asset, export = synthetic_asset()
        for bad_asset, bad_export in ((b"wrong", export), (asset[:-1], export),
                                      (asset, export[:20]),
                                      (asset[:4] + struct.pack("<i", -8) + asset[8:], export)):
            with self.subTest(lengths=(len(bad_asset), len(bad_export))), self.assertRaises(ValueError):
                asset_properties(bad_asset, bad_export)

    def test_exact_car_match_reference_span_and_provenance(self):
        pak = Pak.__new__(Pak)
        pak.entries = {"AC2/Content/cars/test_car/data/CarData.uasset": 0}
        pak.index_digest = "synthetic-index"
        pak.zero_range_defaults_verified = False
        asset, export = synthetic_asset()
        with patch.object(pak, "get", side_effect=(asset, export)):
            fields = pak.car("test_car")
        for value, expected in zip(fields["suspension_reference_m"], (.08, .08, .09, .09)):
            self.assertAlmostEqual(value, expected)
        self.assertEqual(fields["engine_idle_rpm"], 1000)
        self.assertEqual(fields["engine_limiter_rpm"], 8000)
        self.assertEqual(fields["asset_sha256"], hashlib.sha256(asset + export).hexdigest())
        for car in ("../test_car", "test", "test_car/data"):
            with self.assertRaises(ValueError):
                pak.car(car)
        pak.entries["AC2/Content/cars/backup/test_car/data/CarData.uasset"] = 12
        with self.assertRaises(ValueError):
            pak.car("test_car")

    def test_invalid_ranges_never_become_normalization_denominators(self):
        pak = Pak.__new__(Pak)
        pak.entries = {"AC2/Content/cars/test/data/CarData.uasset": 0}
        pak.index_digest = "test"
        pak.zero_range_defaults_verified = False
        for up, down in ((0., 0.), (.1, .1), (math.nan, -.1), (math.inf, -.1), (2., -.1)):
            values = {"Suspensions.hubFront.bumpStop.up.Range": up,
                      "Suspensions.hubFront.bumpStop.down.Range": down}
            with patch.object(pak, "get", return_value=b""), \
                    patch("acbridge.acc_vehicle.asset_properties", return_value=values):
                fields = pak.car("test")
            self.assertEqual(fields["suspension_reference_m"], (0.,) * 4)

    def test_reference_interval_does_not_require_zero_between_endpoints(self):
        pak = Pak.__new__(Pak)
        pak.entries = {"AC2/Content/cars/test/data/CarData.uasset": 0}
        pak.index_digest = "test"
        pak.zero_range_defaults_verified = False
        values = {"Suspensions.hubFront.bumpStop.up.Range": .09,
                  "Suspensions.hubFront.bumpStop.down.Range": .005,
                  "Suspensions.hubRear.bumpStop.up.Range": -.03,
                  "Suspensions.hubRear.bumpStop.down.Range": -.005}
        with patch.object(pak, "get", return_value=b""), \
                patch("acbridge.acc_vehicle.asset_properties", return_value=values):
            fields = pak.car("test")
        self.assertAlmostEqual(fields["suspension_reference_m"][0], .085)
        self.assertAlmostEqual(fields["suspension_reference_m"][2], .025)

    def test_omitted_fields_require_verified_native_defaults(self):
        pak = Pak.__new__(Pak)
        pak.entries = {"AC2/Content/cars/test/data/CarData.uasset": 0}
        pak.index_digest = "test"
        values = {"Suspensions.hubFront.bumpStop.up.Range": .06,
                  "Suspensions.hubRear.bumpStop.up.Range": .07}
        with patch.object(pak, "get", return_value=b""), \
                patch("acbridge.acc_vehicle.asset_properties", return_value=values):
            pak.zero_range_defaults_verified = False
            self.assertEqual(pak.car("test")["suspension_reference_m"], (0.,) * 4)
            pak.zero_range_defaults_verified = True
            fields = pak.car("test")
            self.assertEqual(fields["suspension_reference_m"], (.06, .06, .07, .07))
            self.assertEqual(len(fields["verified_default_fields"]), 2)

    def test_bounded_reader_rejects_bad_strings_and_reads(self):
        for data in (struct.pack("<i", 5000), struct.pack("<i", -5000), struct.pack("<i", 10)):
            with self.assertRaises(ValueError):
                Reader(data).string()
        with self.assertRaises(ValueError):
            Reader(b"abc").read(-1)

    def test_compressed_pak_payload_hash_and_length(self):
        # Standalone v11 file entry, deliberately independent of PAK index writer.
        payload = b"synthetic tagged car resource" * 20
        compressed = zlib.compress(payload)
        header = struct.pack("<QQQI", 0, len(compressed), len(payload), 1)
        header += hashlib.sha1(compressed).digest() + struct.pack("<IQQBI", 1, 73, 73 + len(compressed), 0, len(payload))
        self.assertEqual(len(header), 73)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pak"
            path.write_bytes(bytes(100) + header + compressed)
            pak = Pak.__new__(Pak)
            pak.path, pak.key, pak.compression = path, None, b"Zlib"
            pak.entries = {"car": 0}
            pak.encoded = struct.pack("<II", (1 << 31) | (1 << 30) | (1 << 23), 100)
            self.assertEqual(pak.get("car"), payload)
            damaged = bytearray(path.read_bytes())
            damaged[100 + 28] ^= 1  # hash, while payload remains decompressible
            path.write_bytes(damaged)
            with self.assertRaises(ValueError):
                pak.get("car")

    @unittest.skipUnless(os.name == "nt", "Windows CNG")
    def test_windows_aes256_known_vector(self):
        # Public NIST AES-256 ECB example; unrelated to any game's encryption.
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        cipher = bytes.fromhex("f3eed1bdb5d2a03c064b5a7e3db181f8")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        self.assertEqual(aes_decrypt(cipher, key), plain)


class ACCLearningMappingTests(unittest.TestCase):
    def test_reference_normalization_reaches_udp_and_retains_original_metres(self):
        p, g, s = fixture()
        p.clutch = 1.  # live driving with drivetrain engaged; Forza pedal released
        fields = dict(suspension_reference_m=(.08, .08, .1, .1), engine_idle_rpm=900)
        p.suspensionTravel[:] = (.02, .04, .025, .05)
        frame = from_acc(p, g, s, DEFAULTS, fields)
        packet = encode(frame, 0)
        self.assertEqual(struct.unpack_from("<4f", packet, 68), (.25, .5, .25, .5))
        self.assertEqual(packet[317], 0)
        self.assertEqual(struct.unpack_from("<f", packet, 12)[0], 900)
        self.assertEqual(frame.suspension_sources, ("acc_vehicle_bumpstop_reference",) * 4)
        self.assertEqual(frame.source_data["vehicle_data"], fields)
        self.assertGreater(struct.unpack_from("<f", packet, 196)[0], .019)
        self.assertEqual(struct.unpack_from("<2f", packet, 260), (0, 0))

    def test_low_or_invalid_wheel_not_replaced_by_a_fixed_valid_value(self):
        p, g, s = fixture()
        fields = dict(suspension_reference_m=(.1,) * 4)
        p.suspensionTravel[:] = (.02, .02, .02, 0.)
        frame = from_acc(p, g, s, DEFAULTS, fields)
        self.assertEqual(frame.normalized_suspension[-1], 0)
        self.assertTrue(any("学习暂停" in n for n in frame.notes))
        p.suspensionTravel[3] = math.nan
        self.assertEqual(from_acc(p, g, s, DEFAULTS, fields).normalized_suspension[-1], 0)
        self.assertEqual(from_acc(p, g, s, DEFAULTS).normalized_suspension, (0,) * 4)

    def test_actual_file_limiter_only_fills_missing_shared_memory_maximum(self):
        p, g, s = fixture()
        fields = dict(engine_limiter_rpm=8000)
        self.assertEqual(from_acc(p, g, s, DEFAULTS, fields).max_rpm, 7000)
        s.maxRpm = 0
        self.assertEqual(from_acc(p, g, s, DEFAULTS, fields).max_rpm, 8000)
        for value in (0, -1, math.nan, math.inf, 999999):
            self.assertEqual(from_acc(p, g, s, DEFAULTS, dict(engine_limiter_rpm=value)).max_rpm, 9000)


if __name__ == "__main__":
    unittest.main()
