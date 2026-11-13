"""原始数据抓取工具：验证协议 / 排查问题。

    python tools/dump.py            # 打印 40 个包
    python tools/dump.py --save     # 同时保存原始字节到 logs/raw-*.bin
    python tools/dump.py --all      # 遍历所有匹配的 HID 集合
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from usbmeter import protocol  # noqa: E402
from usbmeter.hidlink import MeterConnection, list_meters  # noqa: E402


def dump_one(meter: dict, count: int, save: bool) -> bool:
    print(f"\n== {meter['model']} {meter['vendor_id']:04X}:{meter['product_id']:04X} "
          f"usage={meter.get('usage_page')}/{meter.get('usage')}")
    print(f"   path: {meter['path']}")
    conn = MeterConnection(meter["path"])
    print(f"   report: in={conn.input_report_len} out={conn.output_report_len} "
          f"prefix={'0x00' if conn._report_prefix else '无'}")
    out = None
    if save:
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        out = open(log_dir / f"raw-{time.strftime('%Y%m%d-%H%M%S')}.bin", "wb")
    try:
        conn.write(protocol.COMMAND_INIT)
        conn.write(protocol.COMMAND_START)
        if meter.get("protocol") == "v2":
            conn.write(protocol.COMMAND_START)
        else:
            conn.write(protocol.COMMAND_POLL)

        got = 0
        deadline = time.time() + 6
        while got < count and time.time() < deadline:
            data = conn.read(250)
            if not data:
                continue
            if out:
                out.write(len(data).to_bytes(2, "little") + data)
            got += 1
            decoded = protocol.decode_packet(data, time.time())
            head = data[:24].hex(" ")
            if decoded.kind == "data":
                s = decoded.samples[0]
                print(f"  #{got:02d} data  crc={'OK ' if decoded.crc_ok else 'BAD'} "
                      f"V={s.v:8.4f} I={s.i:8.4f} D+={s.dp:5.3f} D-={s.dn:5.3f} "
                      f"T={s.temp:5.1f}  {head}")
            else:
                print(f"  #{got:02d} {decoded.kind:7s} len={len(data)}  {head}")
        return got > 0
    finally:
        if out:
            out.close()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    meters = list_meters()
    if not meters:
        print("没有找到支持的 USB 功率计")
        print("请确认：数据线、USB 口、仪表已开机、官方上位机已关闭。")
        return 2
    for m in meters:
        print(f"发现 {m['model']}  SN={'****' if m['serial'] else '-'}  {m['product']}")
    targets = meters if args.all else meters[:1]
    ok = False
    for meter in targets:
        try:
            ok = dump_one(meter, args.count, args.save) or ok
        except Exception as exc:  # noqa: BLE001
            print(f"  打开失败: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
