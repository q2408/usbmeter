"""纹波统计单元测试：python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usbmeter.protocol import Sample
from usbmeter.session import Session


class RippleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(Path(self.tmp.name))

    def tearDown(self):
        self.session.close()
        self.tmp.cleanup()

    def feed(self, values, current=1.0):
        t = 1000.0
        for v in values:
            self.session.add_packet([Sample(t=t, v=v, i=current, dp=0, dn=0, temp=25.0)])
            t += 0.01

    def test_flat_dc_has_no_ripple(self):
        self.feed([5.0] * 100)
        rp = self.session.snapshot()["ripple"]
        self.assertAlmostEqual(rp["vpp"], 0.0, places=6)
        self.assertAlmostEqual(rp["vrms"], 0.0, places=6)
        self.assertAlmostEqual(rp["dc"], 5.0, places=6)

    def test_square_ripple(self):
        # 直流 5V 上叠一个 ±50mV 的方波
        values = [5.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(100)]
        self.feed(values)
        rp = self.session.snapshot()["ripple"]
        self.assertAlmostEqual(rp["vpp"], 0.1, places=6)
        self.assertAlmostEqual(rp["vrms"], 0.05, places=6)
        self.assertAlmostEqual(rp["dc"], 5.0, places=6)

    def test_ripple_current(self):
        self.feed([5.0] * 100, current=2.0)
        self.session.add_packet([Sample(t=1001.0, v=5.0, i=2.5, dp=0, dn=0, temp=25.0)])
        rp = self.session.snapshot()["ripple"]
        self.assertAlmostEqual(rp["ipp"], 0.5, places=6)

    def test_window_is_capped(self):
        self.feed([5.0] * 400)
        rp = self.session.snapshot()["ripple"]
        self.assertEqual(rp["n"], 200)


if __name__ == "__main__":
    unittest.main()
