import math
from pathlib import Path
import struct
import tempfile
import unittest

from acbridge.vehicle_curve import load_car, clean_name, protocol_fields, curve_torque
from acbridge.vendor.acd import get_encryption_key, _encrypt_bytes


class VehicleCurveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.car = self.base / 'test_car'
        (self.car / 'data').mkdir(parents=True)
        self.engine = b'[HEADER]\nPOWER_CURVE=power.lut\n[ENGINE_DATA]\nLIMITER=6000\nMINIMUM=1000\n'
        (self.car / 'data/engine.ini').write_bytes(self.engine)
        (self.car / 'data/car.ini').write_bytes(b'[CONTROLS]\nSTEER_LOCK=450\n')
        (self.car / 'data/drivetrain.ini').write_bytes(b'[TRACTION]\nTYPE=RWD\n')
        (self.car / 'data/suspensions.ini').write_bytes(
            b'[FRONT]\nBUMPSTOP_UP=.08\nBUMPSTOP_DN=.04\n'
            b'[REAR]\nBUMPSTOP_UP=.06\nBUMPSTOP_DN=.08\n')
        (self.car / 'data/power.lut').write_bytes(b'-3000|50\n0|100\n3000|200\n6000|100\n')

    def test_protocol_fields(self):
        fields, notes = protocol_fields(self.car)
        self.assertEqual(fields['engine_idle_rpm'], 1000)
        self.assertEqual(fields['drivetrain_type'], 1)
        self.assertEqual(fields['drivetrain_source'], 'RWD')
        self.assertEqual(fields['steer_lock_degrees'], 450)
        self.assertAlmostEqual(fields['steer_normalization'], 1 / math.radians(450))
        self.assertEqual(fields['suspension_max_travel_m'], (.12, .12, .14, .14))
        self.assertEqual(fields['power_curve'],
                         ((-3000.0, 50.0), (0.0, 100.0), (3000.0, 200.0), (6000.0, 100.0)))
        self.assertEqual(curve_torque(fields['power_curve'], 4500), 150)
        self.assertEqual(notes, [])
        (self.car / 'data/drivetrain.ini').write_bytes(b'[TRACTION]\nTYPE=AWD2\n')
        self.assertEqual(protocol_fields(self.car)[0]['drivetrain_type'], 2)
        (self.car / 'data/drivetrain.ini').write_bytes(b'[TRACTION]\nTYPE=UNKNOWN\n')
        fields, notes = protocol_fields(self.car)
        self.assertEqual(fields['engine_idle_rpm'], 1000)
        self.assertNotIn('drivetrain_type', fields)
        self.assertTrue(any('驱动形式' in note for note in notes))

    def test_acd_header_and_truncation(self):
        path = self.car/'data.acd'
        key = get_encryption_key(path)
        chunks=[]
        for name,raw in [('engine.ini',self.engine),('drivetrain.ini',b'[TRACTION]\nTYPE=FWD\n'),
                         ('power.lut',b'0|100\n6000|200')]:
            name=name.encode()
            chunks.append(struct.pack('<I',len(name))+name+struct.pack('<I',len(raw))+bytes(_encrypt_bytes(raw,key)))
        body=b''.join(chunks)
        for prefix in (b'',struct.pack('<ii',-1111,591346)):
            path.write_bytes(prefix+body)
            files,_=load_car(self.car,'acd')
            self.assertEqual(files['engine.ini'],self.engine)
        path.write_bytes(body[:-1])
        with self.assertRaises(ValueError):load_car(self.car,'acd')
        with self.assertRaises(ValueError):load_car(self.car)

    def test_reject_unsafe_names(self):
        for name in ('../power.lut','C:/power.lut','/power.lut','a//b'):
            with self.assertRaises(ValueError):clean_name(name)


if __name__=='__main__':unittest.main()
