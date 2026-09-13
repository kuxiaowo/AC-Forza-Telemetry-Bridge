import math
from pathlib import Path
import struct
import tempfile
import unittest

from acbridge.vehicle_curve import analyze, load_car, clean_name
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
        (self.car / 'data/power.lut').write_bytes(b'0|100\n3000|200\n6000|100\n')

    def test_curve_units_interpolation_metadata(self):
        p, info = analyze(self.car)
        sample = next(x for x in p['PowerCurvePoints'] if x['Rpm']==4500)
        self.assertEqual(sample['TorqueNm'],150)
        self.assertAlmostEqual(sample['PowerWatts'],150*4500*math.pi/30)
        self.assertEqual(p['RedlineRpm'],6000)
        self.assertEqual(p['CarId'],'test_car')
        self.assertTrue(info['computed_not_measured'])

    def test_acd_header_and_truncation(self):
        path = self.car/'data.acd'
        key = get_encryption_key(path)
        chunks=[]
        for name,raw in [('engine.ini',self.engine),('power.lut',b'0|100\n6000|200')]:
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

    def test_reject_unsafe_and_unsupported(self):
        for name in ('../power.lut','C:/power.lut','/power.lut','a//b'):
            with self.assertRaises(ValueError):clean_name(name)
        (self.car/'data/engine.ini').write_bytes(self.engine+b'[TURBO_0]\nMAX_BOOST=1\n')
        with self.assertRaisesRegex(ValueError,'涡轮'):analyze(self.car)
        (self.car/'data/engine.ini').write_bytes(self.engine)
        (self.car/'data/script.lua').write_text('')
        with self.assertRaisesRegex(ValueError,'脚本'):analyze(self.car)


if __name__=='__main__':unittest.main()
