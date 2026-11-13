"""会话统计与 CSV 记录。"""

from __future__ import annotations

import csv
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .protocol import Sample

CSV_HEADER = [
    "time",
    "elapsed_s",
    "voltage_V",
    "current_A",
    "power_W",
    "dplus_V",
    "dminus_V",
    "temp_C",
    "resistance_ohm",
    "energy_Wh",
    "capacity_Ah",
]


class Session:
    """累计统计（内存中），可选把每一帧写入程序目录下的 CSV。"""

    def __init__(self, log_dir: Path, sample_rate: float = 100.0, writable: bool = True):
        self.log_dir = Path(log_dir)
        self.writable = writable
        if self.writable:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.writable = False
        self.sample_rate = sample_rate
        self._lock = threading.RLock()
        self._fp = None
        self._writer = None
        self._last_flush = 0.0
        self.recording = False          # 默认不写 CSV，只显示图表
        self.smooth_tau = 0.0           # 平滑时间常数（秒），0 = 关闭
        self._ema = None
        self.log_error = "" if self.writable else f"目录不可写：{self.log_dir}"
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> str:
        with self._lock:
            self._close_file()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.csv_path = self.log_dir / f"session-{stamp}.csv"
            index = 1
            while self.csv_path.exists():
                self.csv_path = self.log_dir / f"session-{stamp}-{index}.csv"
                index += 1
            self.started_wall = time.time()
            self.started_mono = time.monotonic()
            self.count = 0
            self.packets = 0
            self.crc_errors = 0
            self.energy_wh = 0.0
            self.capacity_ah = 0.0
            self.peak_power = 0.0
            self._wave = deque(maxlen=200)  # 最近 2 秒，用于纹波统计
            self._ema = None
            self.min_v = self.min_i = float("inf")
            self.max_v = self.max_i = 0.0
            self.sum_v = self.sum_i = self.sum_p = 0.0
            self.last: Sample | None = None
            return str(self.csv_path)

    # ------------------------------------------------------------------
    def set_smoothing(self, tau: float) -> None:
        with self._lock:
            self.smooth_tau = max(0.0, float(tau or 0.0))
            self._ema = None

    def open_file(self) -> None:
        """开始记录且收到数据时才建文件，避免留下一堆空 CSV。"""
        with self._lock:
            if self._fp is not None or not self.writable:
                return
            try:
                self._fp = open(self.csv_path, "w", newline="", encoding="utf-8-sig")
            except OSError as exc:
                self.writable = False
                self.log_error = f"目录不可写：{exc}"
                return
            self._writer = csv.writer(self._fp)
            self._writer.writerow(CSV_HEADER)
            self._fp.flush()
            self._last_flush = time.monotonic()

    def set_recording(self, value: bool) -> bool:
        with self._lock:
            if value and not self.writable:
                return False
            self.recording = bool(value)
            return True

    def add_packet(self, samples: List[Sample], crc_ok: bool = True) -> None:
        with self._lock:
            if self.recording and self._fp is None:
                self.open_file()
            self.packets += 1
            if not crc_ok:
                self.crc_errors += 1
            dt = 1.0 / self.sample_rate
            for s in samples:
                self.count += 1
                power = s.v * s.i
                self.energy_wh += power * dt / 3600.0
                self.capacity_ah += s.i * dt / 3600.0
                self.peak_power = max(self.peak_power, power)
                self.min_v = min(self.min_v, s.v)
                self.max_v = max(self.max_v, s.v)
                self.min_i = min(self.min_i, s.i)
                self.max_i = max(self.max_i, s.i)
                self.sum_v += s.v
                self.sum_i += s.i
                self.sum_p += power
                self._wave.append((s.v, s.i))
                self.last = s
                if self.smooth_tau > 0:
                    alpha = 1.0 - math.exp(-dt / self.smooth_tau)
                    if self._ema is None:
                        self._ema = [s.v, s.i, power, s.temp]
                    else:
                        e = self._ema
                        e[0] += alpha * (s.v - e[0])
                        e[1] += alpha * (s.i - e[1])
                        e[2] += alpha * (power - e[2])
                        e[3] += alpha * (s.temp - e[3])
                if self.recording and self._writer is not None:
                    ts = datetime.fromtimestamp(s.t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    self._writer.writerow(
                        [
                            ts,
                            f"{(self.count - 1) * dt:.2f}",
                            f"{s.v:.5f}",
                            f"{s.i:.5f}",
                            f"{power:.5f}",
                            f"{s.dp:.3f}",
                            f"{s.dn:.3f}",
                            f"{s.temp:.1f}",
                            f"{(s.v / s.i):.4f}" if s.i > 1e-6 else "",
                            f"{self.energy_wh:.6f}",
                            f"{self.capacity_ah:.6f}",
                        ]
                    )
            now = time.monotonic()
            if self._fp is not None and now - self._last_flush > 2.0:
                self._last_flush = now
                try:
                    self._fp.flush()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict:
        with self._lock:
            n = max(self.count, 1)
            last = self.last
            ripple = {"vpp": 0.0, "vrms": 0.0, "dc": 0.0, "ipp": 0.0, "n": 0}
            wave = list(self._wave)
            if len(wave) >= 10:
                vs = [w[0] for w in wave]
                dc = sum(vs) / len(vs)
                vpp = max(vs) - min(vs)
                vrms = math.sqrt(sum((v - dc) ** 2 for v in vs) / len(vs))
                iis = [w[1] for w in wave]
                ripple = {
                    "vpp": vpp,
                    "vrms": vrms,
                    "dc": dc,
                    "ipp": max(iis) - min(iis),
                    "n": len(wave),
                }
            return {
                "count": self.count,
                "packets": self.packets,
                "crc_errors": self.crc_errors,
                "duration": self.count / self.sample_rate,
                "energy_wh": self.energy_wh,
                "capacity_ah": self.capacity_ah,
                "peak_power": self.peak_power,
                "min_v": 0.0 if self.count == 0 else self.min_v,
                "max_v": self.max_v,
                "avg_v": 0.0 if self.count == 0 else self.sum_v / n,
                "min_i": 0.0 if self.count == 0 else self.min_i,
                "max_i": self.max_i,
                "avg_i": 0.0 if self.count == 0 else self.sum_i / n,
                "avg_p": 0.0 if self.count == 0 else self.sum_p / n,
                "last": None
                if last is None
                else {
                    "t": last.t,
                    "v": last.v,
                    "i": last.i,
                    "p": last.v * last.i,
                    "dp": last.dp,
                    "dn": last.dn,
                    "temp": last.temp,
                },
                "recording": self.recording,
                "csv": self.csv_path.name if self._fp else None,
                "writable": self.writable,
                "log_error": self.log_error,
                "smooth": None
                if last is None
                else {
                    "v": self._ema[0] if self._ema else last.v,
                    "i": self._ema[1] if self._ema else last.i,
                    "p": self._ema[2] if self._ema else last.v * last.i,
                    "temp": self._ema[3] if self._ema else last.temp,
                },
                "started": self.started_wall,
                "ripple": ripple,
            }

    def flush(self) -> None:
        with self._lock:
            if self._fp is not None:
                try:
                    self._fp.flush()
                except Exception:  # noqa: BLE001
                    pass

    def _close_file(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:  # noqa: BLE001
                pass
            self._fp = None
            self._writer = None

    def close(self) -> None:
        with self._lock:
            self._close_file()
