import unittest,struct,tempfile,json
from pathlib import Path
from acbridge.vehicles import VehicleStore
from acbridge.telemetry import from_ac,encode
from acbridge.runtime import DEFAULTS
from test_bridge import fixture,text_field

class VehiclesTests(unittest.TestCase):
 def test_fallback_and_packet(self):
  with tempfile.TemporaryDirectory() as d:
   Path(d,'test.json').write_text(json.dumps({'schema_version':1,'car_id':'test_car','suspension':{'fallback_max_travel_m':[.125,.125,.14,.14]}}))
   store=VehicleStore(d)
   p,g,s=fixture();text_field(s,'carModel','test_car')
   p.suspensionTravel[:]=(.025,.0625,.07,.14);s.suspensionMaxTravel[:]=(0,0,.14,.14)
   f=from_ac(p,g,s,DEFAULTS,store)
   for a,b in zip(struct.unpack_from('<4f',encode(f,1),68),(.2,.5,.5,1)):self.assertAlmostEqual(a,b,places=6)
   self.assertEqual(f.suspension_sources,('vehicle_config_estimate',)*2+('shared_memory',)*2)
   s.suspensionMaxTravel[0]=.1
   self.assertAlmostEqual(from_ac(p,g,s,DEFAULTS,store).normalized_suspension[0],.25)
 def test_missing_and_boundaries(self):
  p,g,s=fixture();s.suspensionMaxTravel[:]=(0,.1,.1,float('nan'));p.suspensionTravel[:]=(.1,-.1,.2,float('inf'))
  self.assertEqual(from_ac(p,g,s,DEFAULTS,VehicleStore()).normalized_suspension,(0,0,1,0))
 def test_invalid_catalog(self):
  with tempfile.TemporaryDirectory() as d:
   Path(d,'bad.json').write_text('{')
   self.assertEqual(VehicleStore(d).ranges('unknown'),(0,0,0,0))
