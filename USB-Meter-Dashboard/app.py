"""USB Meter Dashboard 启动入口。

用法:
    python app.py                   # 启动服务 + 实时读数窗口（默认 0.0.0.0:8765）
    python app.py --browser         # 启动后打开浏览器
    python app.py --no-console      # 不打开实时读数窗口
    python app.py --host 127.0.0.1  # 只允许本机访问
    python app.py --probe           # 列出 HID 设备后退出（排查用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from usbmeter.server import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="USB Meter Dashboard")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="监听地址，默认 0.0.0.0（局域网可访问）；只想本机用 --host 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口；不填则用上次在网页里设置的端口（默认 8765），0 = 随机空闲端口",
    )
    parser.add_argument("--browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--window", action="store_true", help="用 Edge 应用窗口打开（更像桌面程序）")
    parser.add_argument("--no-console", action="store_true", help="不打开独立的实时读数窗口")
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="不发送每秒一次的 AA83 周期请求（排查 USB 掉线用）",
    )
    parser.add_argument("--probe", action="store_true", help="列出 HID 设备并退出")
    args = parser.parse_args()

    try:
        if (sys.stdout.encoding or "").lower() in ("ascii", "ansi_x3.4-1968"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.probe:
        from usbmeter.hidlink import hid_available, import_error, list_meters

        if not hid_available():
            print("hidapi 不可用:", import_error())
            return 2
        devices = list_meters()
        if not devices:
            print("没有找到支持的 USB 功率计")
        for d in devices:
            print(
                f"{d['model']:<7} SN={'****' if d['serial'] else '-':<6} {d['product']}"
            )
        return 0

    run(
        ROOT,
        host=args.host,
        port=args.port,
        open_browser=args.browser,
        window=args.window,
        spawn_console=not args.no_console,
        poll=not args.no_poll,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
