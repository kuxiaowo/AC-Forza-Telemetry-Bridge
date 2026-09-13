import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch
from acbridge.runtime import Bridge,DEFAULTS
from acbridge.telemetry import Frame,encode
from acbridge.learning import curve_matches,sweep_frames
from test_bridge import FakeReader


class LearningTests(unittest.TestCase):
    def test_sweep_fields_and_end_signal(self):
        points=[dict(Rpm=r,TorqueNm=100,PowerWatts=100*r*3.141592653589793/30) for r in range(1000,2001,50)]
        frames=list(sweep_frames(dict(PowerCurvePoints=points,EngineMaxRpm=2000),'test'))
        samples=[f for f in frames if f.active and f.throttle==1]
        self.assertEqual(samples[0].rpm,1000)
        self.assertEqual(max(f.rpm for f in samples),2000)
        self.assertFalse(frames[-1].active)
        for f in samples:
            data=encode(f,123)
            self.assertEqual(len(data),324)
            self.assertAlmostEqual(struct.unpack_from('<f',data,264)[0],100)
            self.assertEqual(data[315],255)
            self.assertEqual(data[319],3)
        self.assertTrue(curve_matches(points,points))
        self.assertFalse(curve_matches(points[:-1],points))
        self.assertFalse(curve_matches([dict(p,PowerWatts=p['PowerWatts']*1.2) for p in points],points))

    def test_learning_excludes_live_and_resumes_after_failure(self):
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1',0));sock.settimeout(2)
        b=Bridge({**DEFAULTS,'port':sock.getsockname()[1]},reader=FakeReader())
        entered=threading.Event();release=threading.Event()
        def learn(bridge,sender,target,frame):
            sender.sendto(encode(Frame(active=True,car=frame.car,rpm=1777,power_watts=10),123456),target)
            entered.set();release.wait(2)
            raise ValueError('intentional test failure')
        try:
            with patch('acbridge.learning.run_learning',learn):
                b.start();sock.recv(4096)
                # Wait until initial frame has been published.
                end=time.monotonic()+1
                while not b.get_snapshot()['frame']['car'] and time.monotonic()<end:time.sleep(.01)
                b.learn();self.assertTrue(entered.wait(1))
                while True:
                    data=sock.recv(4096)
                    if struct.unpack_from('<f',data,260)[0]==10:break
                sock.settimeout(.08)
                with self.assertRaises(socket.timeout):sock.recv(4096)
                release.set();sock.settimeout(2)
                data=sock.recv(4096)
                self.assertEqual(struct.unpack_from('<f',data,260)[0],0)
                self.assertEqual(struct.unpack_from('<i',data)[0],1)
                self.assertIn('未完成',b.get_snapshot()['learning_result'])
        finally:
            release.set();b.stop();b.thread.join(2);sock.close()


if __name__=='__main__':unittest.main()
