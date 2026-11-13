"""USB Meter Dashboard 控制台实时读数。

由服务端自动拉起（独立 CMD 窗口），也可以手动运行：

    runtime\\python.exe tools/live.py
    runtime\\python.exe tools/live.py --url http://<IP>:8765

只读 /api/state，不碰 USB，关掉它不影响记录。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import urllib.request

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
ORANGE = "\x1b[33m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
GREY = "\x1b[90m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
HOME = "\x1b[H"
CLEAR_LINE = "\x1b[K"
CLEAR_DOWN = "\x1b[J"


def enable_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001
        pass


def set_title(text: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:  # noqa: BLE001
        pass


def width_of(text: str) -> int:
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return total


def pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - width_of(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def fmt(value, digits: int, fallback: str = "—") -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return fallback


def duration(sec) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def fetch(url: str, timeout: float = 1.5) -> dict:
    with urllib.request.urlopen(url + "/api/state", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_lines(state: dict | None, error: str, cols: int) -> list:
    width = max(56, min(cols - 2, 78))
    out = []

    def line(text: str = "", color: str = "") -> None:
        out.append((color + pad(text, width) + RESET) if color else pad(text, width))

    if state is None:
        line()
        line("  USB Meter Dashboard · 实时读数", BOLD + CYAN)
        line()
        line("  正在等待服务…", ORANGE)
        if error:
            line(f"  {error}", GREY)
        line()
        line("  确认 start.bat 的窗口还在运行；服务停止后本窗口会自动关闭。", GREY)
        return out

    device = state.get("device") or {}
    stats = state.get("stats") or {}
    last = stats.get("last") or {}
    # 设置里开了平滑就用平滑值（和网页大数字一致）
    disp = stats.get("smooth") or last
    connected = bool(device.get("connected"))

    line()
    title = "  USB Meter Dashboard · 实时读数"
    status = "● 已连接" if connected else "○ 未连接"
    gap = max(1, width - width_of(title) - width_of(status) - 4)
    out.append(BOLD + CYAN + title + RESET + " " * gap + (GREEN if connected else RED) + status + RESET)
    line()

    if connected:
        line(
            f"  {device.get('model') or 'USB 功率计'}   SN {device.get('serial') or '—'}"
            f"   {device.get('rate', 0):.0f} sps",
            GREY,
        )
    else:
        line("  " + (device.get("message") or "未检测到仪表"), GREY)
    line("  " + "─" * (width - 4), GREY)

    col = (width - 4) // 3
    line("  " + pad("电压 V", col, "center") + pad("电流 A", col, "center") + pad("功率 W", col, "center"), GREY)
    if connected:
        v = fmt(disp.get("v"), 4)
        i = fmt(disp.get("i"), 4)
        p = fmt(disp.get("p"), 3)
        line(
            "  " + BOLD + CYAN + pad(v, col, "center") + RESET
            + BOLD + ORANGE + pad(i, col, "center") + RESET
            + BOLD + GREEN + pad(p, col, "center") + RESET
        )
    else:
        line("  " + pad("—", col, "center") + pad("—", col, "center") + pad("—", col, "center"), GREY)

    line("  " + "─" * (width - 4), GREY)
    line(
        f"  T {fmt(disp.get('temp'), 1)}°C"
        f"   容量 {fmt(stats.get('capacity_ah'), 4)} Ah"
        f"   能量 {fmt(stats.get('energy_wh'), 4)} Wh",
        "",
    )
    line(
        f"  D+ {fmt(last.get('dp'), 3)} V"
        f"   D- {fmt(last.get('dn'), 3)} V"
        f"   运行 {duration(stats.get('duration'))}",
        "",
    )
    line(
        f"  峰值 {fmt(stats.get('peak_power'), 2)} W"
        f"   数据包 {stats.get('packets', 0)}"
        f"   CRC 异常 {stats.get('crc_errors', 0)}",
        GREY,
    )
    ripple = stats.get("ripple") or {}
    if connected and ripple.get("n"):
        line(
            f"  纹波 Vpp {fmt(ripple.get('vpp', 0) * 1000, 1)} mV"
            f"   Vrms {fmt(ripple.get('vrms', 0) * 1000, 1)} mV",
            GREY,
        )
    if connected and device.get("error"):
        line("  " + str(device["error"]), RED)
    line("  " + "─" * (width - 4), GREY)
    line(f"  更新 {time.strftime('%H:%M:%S')}        Ctrl+C 退出", GREY)
    return out


def draw(lines: list) -> None:
    buf = [HOME]
    for text in lines:
        buf.append(text + CLEAR_LINE + "\n")
    buf.append(CLEAR_DOWN)
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="USB Meter Dashboard 控制台实时读数")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="服务地址")
    parser.add_argument("--interval", type=float, default=0.5, help="刷新间隔（秒）")
    args = parser.parse_args()

    enable_ansi()
    set_title("USB Meter Dashboard - 实时读数")
    sys.stdout.write(HIDE_CURSOR)

    fails = 0
    state = None
    error = ""
    try:
        while True:
            try:
                state = fetch(args.url, timeout=0.8)
                error = ""
                fails = 0
            except Exception as exc:  # noqa: BLE001
                fails += 1
                state = None
                error = f"{type(exc).__name__}: {exc}"
                # 服务端关掉后跟着退出（约 2~3 秒），窗口随进程一起关闭
                if fails >= 3:
                    draw(build_lines(None, "服务已停止，窗口自动关闭…", 80))
                    time.sleep(0.5)
                    return 0
            cols = 80
            try:
                import shutil

                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:  # noqa: BLE001
                pass
            draw(build_lines(state, error, cols))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
