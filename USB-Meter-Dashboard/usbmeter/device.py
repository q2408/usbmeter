"""设备服务：连接、读取、断线重连，以及诊断信息缓存。"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

from . import protocol
from .hidlink import MeterConnection, enum_diag, hid_available, import_error, list_meters
from .protocol import Sample
from .session import Session


def _mask_serial(value) -> str:
    """诊断信息里不显示完整序列号，避免截图/日志泄漏设备身份。"""
    return "****" if value else ""


def _mask_device(device: Dict, privacy: bool = False) -> Dict:
    item = dict(device)
    if privacy:
        item["serial"] = _mask_serial(item.get("serial"))
        item["vendor_id"] = 0
        item["product_id"] = 0
        item["path"] = "…"
    return item


class MeterService:
    def __init__(
        self,
        session: Session,
        on_samples: Callable[[List[Sample]], None],
        poll: bool = True,
        send_stop: bool = True,
    ):
        self.session = session
        self._on_samples = on_samples
        self._poll = bool(poll)
        self._send_stop = bool(send_stop)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._want_disconnect = threading.Event()
        self._conn: Optional[MeterConnection] = None
        self._thread: Optional[threading.Thread] = None
        self._started_at = time.time()
        self._no_device_logged = False
        self._last_close = 0.0
        self._privacy = False
        self._status: Dict = {
            "connected": False,
            "model": "",
            "serial": "",
            "product": "",
            "message": "正在扫描设备…",
            "error": "",
            "rate": 0.0,
        }
        self._raw: deque = deque(maxlen=48)
        self._events: deque = deque(maxlen=200)
        self._rate_window: deque = deque()
        self._status_sent = None
        self._fail_count = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fnb58-meter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """让读取线程自己收尾（发 STOP + 关句柄），避免和 read 并发。"""
        self._stop.set()
        self._want_disconnect.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)

    def set_privacy(self, value: bool) -> None:
        """隐私模式：界面/日志/诊断里的序列号打码。"""
        self._privacy = bool(value)

    def _serial_text(self, value) -> str:
        return "****" if (self._privacy and value) else (value or "")

    def _graceful_stop(self, conn) -> None:
        """尽量给设备发一条 STOP，再关句柄：避免设备一直往空管道里发数据卡死，
        下次必须拔插才能恢复。"""
        if self._send_stop:
            try:
                conn.write(protocol.COMMAND_STOP)
                self._event("info", "发送 STOP 命令")
            except Exception as exc:  # noqa: BLE001
                self._event("warn", f"STOP 失败（忽略）: {exc}")
        try:
            time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def rescan(self) -> None:
        """让读取线程自己退出重连（不在这里直接关句柄，避免和 read 并发）。"""
        self._event("info", "手动触发重新扫描")
        self._want_disconnect.set()

    def disconnect(self) -> None:
        """手动断开：读取线程先收尾再关闭，然后自动重连。"""
        self._event("info", "手动断开设备")
        self._want_disconnect.set()

    def info(self) -> Dict:
        with self._lock:
            data = dict(self._status)
        data["rate"] = self._rate()
        data["hid"] = hid_available()
        data["poll"] = self._poll
        data["uptime"] = int(time.time() - self._started_at)
        return data

    def probe(self) -> Dict:
        with self._lock:
            return {
                "devices": [_mask_device(d, self._privacy) for d in list_meters()],
                "enum": enum_diag(),
                "hid_error": None if hid_available() else str(import_error()),
                "recent": list(self._raw)[-8:],
            }

    def debug_snapshot(self) -> Dict:
        with self._lock:
            raw = list(self._raw)
            events = list(self._events)
        return {
            "info": self.info(),
            "raw": raw[-24:],
            "raw_total": len(raw),
            "events": events[-60:],
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "machine": platform.machine(),
            "platform": platform.platform(),
        }

    # ------------------------------------------------------------------
    def _rate(self) -> float:
        now = time.monotonic()
        with self._lock:
            while self._rate_window and now - self._rate_window[0][0] > 1.5:
                self._rate_window.popleft()
            return sum(n for _, n in self._rate_window) / 1.5

    def _event(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._events.append({"t": time.time(), "level": level, "text": text})
        line = f"[{stamp}] {text}"
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)
            snap = dict(self._status)
        self._status_sent = snap

    def _emit(self, samples: List[Sample], crc_ok: bool = True, raw: bytes = b"") -> None:
        self.session.add_packet(samples, crc_ok)
        self._fail_count = 0
        first = samples[0] if samples else None
        with self._lock:
            self._rate_window.append((time.monotonic(), len(samples)))
            if raw:
                self._raw.append(
                    {
                        "t": time.time(),
                        "len": len(raw),
                        "crc": crc_ok,
                        "hex": raw.hex(" "),
                        "v": round(first.v, 5) if first else 0,
                        "i": round(first.i, 5) if first else 0,
                        "dp": round(first.dp, 3) if first else 0,
                        "dn": round(first.dn, 3) if first else 0,
                        "temp": round(first.temp, 1) if first else 0,
                    }
                )
        self._on_samples(samples)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            # 设备被关闭后会在 USB 上重新枚举，太快重开会打开到半初始化的实例，
            # 写命令直接返回 -1。这里等 2.5 秒让它稳定下来。
            if time.monotonic() - self._last_close < 2.5:
                self._sleep(0.3)
                continue
            meters = list_meters()
            if not meters:
                diag = enum_diag()
                if diag["native"] == 0 and diag["hidapi"] == 0:
                    message = (
                        "Windows 没有暴露任何 HID 设备：可能被禁用或驱动异常。"
                        "请拔插一次 USB 线，或在设备管理器里检查"
                    )
                else:
                    message = f"已枚举到 {diag['native']} 个 HID 设备，但没有找到支持的 USB 功率计"
                if not self._no_device_logged:
                    self._no_device_logged = True
                    self._event(
                        "warn",
                        f"未找到仪表（native={diag['native']} hidapi={diag['hidapi']} "
                        f"err={diag['error'] or '-'}）",
                    )
                self._set_status(
                    connected=False,
                    model="",
                    serial="",
                    product="",
                    error="" if hid_available() else f"hidapi 不可用：{import_error()}",
                    message=message,
                )
                self._sleep(0.8)
                continue
            self._no_device_logged = False
            for meter in meters:
                if self._stop.is_set():
                    break
                try:
                    self._run_device(meter)
                except Exception as exc:  # noqa: BLE001
                    self._fail_count += 1
                    name = meter.get("model", "设备")
                    text = f"{type(exc).__name__}: {exc}"
                    self._event("error", f"{name} 异常: {text}")
                    diag = enum_diag()
                    if diag["native"] == 0 and diag["hidapi"] == 0:
                        msg = (
                            "USB 连接中断：设备已从系统消失。"
                            "检查数据线 / USB 口；虚拟机请检查 USB 直通。插回后会自动重连"
                        )
                    elif isinstance(exc, TimeoutError):
                        msg = f"{name} 已连上但没收到数据，重试中…"
                    elif "hid_write" in str(exc) or "write" in str(exc).lower():
                        msg = f"{name} 命令写入失败，重试中…"
                    else:
                        msg = f"{name} 通信失败，重试中…"
                    if self._fail_count >= 3:
                        msg = f"连续 {self._fail_count} 次失败，请拔插一次 USB 线。{msg}"
                    self._set_status(
                        connected=False,
                        model=meter.get("model", ""),
                        serial=self._serial_text(meter.get("serial", "")),
                        product=meter.get("product", ""),
                        error=text,
                        message=msg,
                    )
                    # 连续失败时放慢重试，避免反复开关把设备状态弄得更糟
                    self._sleep(min(1.0 + 0.5 * self._fail_count, 5.0))
            self._sleep(0.5)

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ------------------------------------------------------------------
    def _run_device(self, meter: dict) -> None:
        name = meter.get("model", "设备")
        self._want_disconnect.clear()
        self._set_status(
            connected=False,
            model=name,
            serial=self._serial_text(meter.get("serial", "")),
            product=meter.get("product", ""),
            error="",
            message=f"正在连接 {name}…",
        )
        self._event(
            "info",
            f"打开 {name} SN={self._serial_text(meter.get('serial')) or '-'}",
        )
        conn = MeterConnection(meter["path"])
        self._conn = conn
        try:
            self._event(
                "info",
                f"报告长度 in={conn.input_report_len} out={conn.output_report_len} "
                f"feature={conn.feature_report_len} 前缀={'0x00' if conn._report_prefix else '无'}",
            )
            commands = [("INIT", protocol.COMMAND_INIT), ("START", protocol.COMMAND_START)]
            if meter.get("protocol") == "v2":
                commands.append(("START", protocol.COMMAND_START))
            else:
                commands.append(("POLL", protocol.COMMAND_POLL))
            # 不再在 INIT 前发 STOP：设备刚上电/刚重连时多发一条命令反而容易把它带乱。
            for label, cmd in commands:
                try:
                    written = conn.write(cmd)
                    self._event("info", f"发送 {label} -> {written} 字节")
                except Exception as exc:  # noqa: BLE001
                    self._event("error", f"发送 {label} 失败: {type(exc).__name__}: {exc}")
                    raise

            self._set_status(connected=True, message=f"已连接 {name}", error="")
            self._event("info", f"等待数据…（周期请求{'开' if self._poll else '关'}）")

            last_poll = time.monotonic()
            last_data = last_poll
            first_data = True
            crc_checked = 0
            crc_bad = 0
            check_crc = True
            seen_types = set()

            while not self._stop.is_set() and not self._want_disconnect.is_set():
                now = time.monotonic()
                if self._poll and now - last_poll >= 1.0:
                    last_poll = now
                    try:
                        conn.write(protocol.COMMAND_POLL)
                    except Exception as exc:  # noqa: BLE001
                        self._event("warn", f"周期请求失败: {exc}")

                data = conn.read(200)
                if not data:
                    if now - last_data > 4.0:
                        self._event("error", "超过 4 秒没有收到数据，重连")
                        raise TimeoutError("超过 4 秒没有收到数据")
                    continue

                decoded = protocol.decode_packet(data, time.time(), check_crc=check_crc)
                if decoded.kind == "data":
                    last_data = now
                    if first_data:
                        first_data = False
                        self._event("info", f"收到首包 ({len(data)} 字节): {data[:16].hex(' ')} …")
                    if check_crc:
                        crc_checked += 1
                        if not decoded.crc_ok:
                            crc_bad += 1
                        if crc_checked >= 200 and crc_bad > crc_checked * 0.5:
                            check_crc = False
                            self._event("warn", "CRC 规则与预期不同，已跳过校验（读数不受影响）")
                    self._emit(decoded.samples, decoded.crc_ok, decoded.raw)
                else:
                    typ = decoded.raw[1] if len(decoded.raw) > 1 else 0
                    if typ not in seen_types:
                        seen_types.add(typ)
                        self._event(
                            "info",
                            f"收到控制包 type=0x{typ:02x}: {decoded.raw[:20].hex(' ')} …",
                        )
                    with self._lock:
                        self._raw.append(
                            {"t": time.time(), "len": len(data), "crc": True,
                             "hex": data.hex(" "), "kind": decoded.kind,
                             "v": 0, "i": 0, "dp": 0, "dn": 0, "temp": 0}
                        )
        finally:
            if self._conn is conn:
                self._conn = None
            self._graceful_stop(conn)
            self._last_close = time.monotonic()
            self._event("info", "连接已关闭")
