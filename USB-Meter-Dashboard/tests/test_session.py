"""会话行为单元测试：默认不写日志、平滑、记录开关。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usbmeter.protocol import Sample
from usbmeter.session import Session


def sample(v=5.0, i=1.0, t=1000.0):
    return Sample(t=t, v=v, i=i, dp=0.0, dn=0.0, temp=25.0)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.session = Session(self.dir / "logs")

    def tearDown(self):
        self.session.close()
        self.tmp.cleanup()

    def test_no_file_by_default(self):
        for k in range(20):
            self.session.add_packet([sample(t=1000 + k * 0.01)])
        snap = self.session.snapshot()
        self.assertFalse(snap["recording"])
        self.assertIsNone(snap["csv"])
        self.assertEqual(list((self.dir / "logs").glob("*.csv")), [])

    def test_recording_creates_file_on_first_packet(self):
        self.assertTrue(self.session.set_recording(True))
        self.session.add_packet([sample()])
        snap = self.session.snapshot()
        self.assertTrue(snap["recording"])
        self.assertIsNotNone(snap["csv"])
        files = list((self.dir / "logs").glob("*.csv"))
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)

    def test_smoothing_reacts_slower_than_raw(self):
        self.session.set_smoothing(1.0)
        for k in range(300):
            self.session.add_packet([sample(v=5.0, t=1000 + k * 0.01)])
        before = self.session.snapshot()["smooth"]["v"]
        self.assertAlmostEqual(before, 5.0, places=2)
        for k in range(100):  # 1 秒的 6V
            self.session.add_packet([sample(v=6.0, t=1003 + k * 0.01)])
        snap = self.session.snapshot()
        self.assertAlmostEqual(snap["last"]["v"], 6.0, places=3)
        self.assertGreater(snap["smooth"]["v"], 5.4)
        self.assertLess(snap["smooth"]["v"], 5.8)

    def test_smoothing_off_is_raw(self):
        self.session.set_smoothing(0)
        self.session.add_packet([sample(v=7.5)])
        snap = self.session.snapshot()
        self.assertAlmostEqual(snap["smooth"]["v"], 7.5, places=6)


if __name__ == "__main__":
    unittest.main()
