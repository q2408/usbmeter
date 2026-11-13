"""协议解析单元测试：python -m unittest discover -s tests"""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usbmeter import protocol


def build_packet(v, i, dp, dn, temp, ptype=protocol.DATA_TYPE, bad_crc=False):
    body = bytearray([0xAA, ptype])
    for _ in range(protocol.SAMPLES_PER_PACKET):
        body += struct.pack("<II", int(v * 100000), int(i * 100000))
        body += struct.pack("<HH", int(dp * 1000), int(dn * 1000))
        body += bytes([1])
        body += struct.pack("<H", int(temp * 10))
    body += bytes([0x00])
    crc = protocol.crc8(bytes(body[1:]))
    body += bytes([crc ^ (0xFF if bad_crc else 0)])
    assert len(body) == protocol.PACKET_LEN, len(body)
    return bytes(body)


class ProtocolTests(unittest.TestCase):
    def test_crc_roundtrip(self):
        pkt = build_packet(12.0, 2.35, 3.3, 0.6, 38.4)
        self.assertTrue(protocol.decode_packet(pkt, 0.0).crc_ok)

    def test_decode_values(self):
        pkt = build_packet(12.0034, 2.3512, 3.3, 0.6, 38.4)
        decoded = protocol.decode_packet(pkt, 100.0)
        self.assertEqual(decoded.kind, "data")
        self.assertEqual(len(decoded.samples), 4)
        s = decoded.samples[-1]
        self.assertAlmostEqual(s.v, 12.0034, places=4)
        self.assertAlmostEqual(s.i, 2.3512, places=4)
        self.assertAlmostEqual(s.dp, 3.3, places=3)
        self.assertAlmostEqual(s.dn, 0.6, places=3)
        self.assertAlmostEqual(s.temp, 38.4, places=1)
        self.assertAlmostEqual(s.p, 12.0034 * 2.3512, places=2)

    def test_timestamps_are_10ms_apart(self):
        pkt = build_packet(5.0, 1.0, 0.0, 0.0, 25.0)
        d = protocol.decode_packet(pkt, 1000.0)
        ts = [s.t for s in d.samples]
        self.assertAlmostEqual(ts[-1] - ts[0], 0.03, places=6)
        self.assertAlmostEqual(ts[-1], 1000.0, places=6)

    def test_report_id_prefix(self):
        pkt = build_packet(9.0, 1.5, 0.6, 0.6, 30.0)
        d = protocol.decode_packet(b"\x00" + pkt, 0.0)
        self.assertEqual(d.kind, "data")
        self.assertAlmostEqual(d.samples[0].v, 9.0, places=4)

    def test_control_packet(self):
        pkt = bytearray(64)
        pkt[0], pkt[1] = 0xAA, 0x03
        d = protocol.decode_packet(bytes(pkt), 0.0)
        self.assertEqual(d.kind, "control")

    def test_bad_crc_flagged(self):
        pkt = build_packet(5.0, 1.0, 0, 0, 20.0, bad_crc=True)
        d = protocol.decode_packet(pkt, 0.0)
        self.assertFalse(d.crc_ok)

    def test_commands(self):
        for cmd in protocol.COMMANDS.values():
            self.assertEqual(len(cmd), 64)
            self.assertEqual(cmd[:2], b"\xAA" + cmd[1:2])

    def test_garbage(self):
        self.assertEqual(protocol.decode_packet(b"\x00" * 10, 0.0).kind, "invalid")
        self.assertEqual(protocol.decode_packet(b"garbage" * 20, 0.0).kind, "invalid")


if __name__ == "__main__":
    unittest.main()
