"""FNB58 / FNB48S / FNB48 / C1 的 USB HID 协议。

协议来自对官方 UsbMeterTool 的数据流与社区逆向结果（didim99、baryluk 等）：

数据包（HID 输入报告，64 字节）
    0       0xAA            帧头
    1       0x04            数据类型（0x04 = 采样数据）
    2..61   4 组 x 15 字节  每组：V(u32 LE, /100000)、I(u32 LE, /100000)、
                            D+(u16 LE, /1000)、D-(u16 LE, /1000)、flags(u8)、
                            T(u16 LE, /10)
    62      ?               半恒定字节（用途未知）
    63      CRC8            对 byte[1..62] 计算，poly=0x39 init=0x42

命令（HID 输出报告，64 字节，同样以 0xAA 开头）
    AA 81 .. 8E   初始化
    AA 82 .. 96   开始上传
    AA 83 .. 9E   周期请求（FNB58/FNB48S 约 1s 一次）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

PACKET_LEN = 64
DATA_TYPE = 0x04
SAMPLES_PER_PACKET = 4
SAMPLE_BYTES = 15
SAMPLE_INTERVAL = 0.01  # 100 sps
DEFAULT_SAMPLE_RATE = 100.0


def crc8(data: bytes, poly: int = 0x39, init: int = 0x42) -> int:
    """CRC-8/poly 0x39, init 0x42, 不反转，xorout 0。"""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# 命令帧按官方/社区实现逐字节写死（FNB58 与 FNB48S 用同一套）。
# 末字节规律：0x8E / 0x96 / 0x9E，正好是 (type * 8 + 0x86) & 0xFF，
# 据此推出 0x84 的停止帧 A6。BLE 协议里 0x84 也是“停止”。
COMMAND_INIT = bytes.fromhex("aa81" + "00" * 61 + "8e")
COMMAND_START = bytes.fromhex("aa82" + "00" * 61 + "96")
COMMAND_POLL = bytes.fromhex("aa83" + "00" * 61 + "9e")
COMMAND_STOP = bytes.fromhex("aa84" + "00" * 61 + "a6")

COMMANDS = {
    "init": COMMAND_INIT,
    "start": COMMAND_START,
    "poll": COMMAND_POLL,
    "stop": COMMAND_STOP,
}


@dataclass(slots=True)
class Sample:
    t: float
    v: float
    i: float
    dp: float
    dn: float
    temp: float

    @property
    def p(self) -> float:
        return self.v * self.i


@dataclass(slots=True)
class Decoded:
    kind: str  # "data" | "control" | "invalid"
    samples: List[Sample]
    crc_ok: bool
    raw: bytes


def normalize(buf: bytes) -> bytes:
    """兼容 hapi 是否带 report id 前缀 / 多读字节。"""
    if len(buf) >= PACKET_LEN + 1 and buf[0] == 0x00 and buf[1] == 0xAA:
        return buf[1 : PACKET_LEN + 1]
    if len(buf) > PACKET_LEN:
        return buf[:PACKET_LEN]
    return buf


def decode_packet(buf: bytes, t_end: float, check_crc: bool = True) -> Decoded:
    raw = normalize(buf)
    if len(raw) < PACKET_LEN or raw[0] != 0xAA:
        return Decoded("invalid", [], False, raw)
    if raw[1] != DATA_TYPE:
        return Decoded("control", [], True, raw)

    crc_ok = not check_crc
    if check_crc:
        crc_ok = crc8(raw[1:-1]) == raw[-1]

    samples: List[Sample] = []
    for idx in range(SAMPLES_PER_PACKET):
        off = 2 + SAMPLE_BYTES * idx
        v = int.from_bytes(raw[off : off + 4], "little") / 100000.0
        i = int.from_bytes(raw[off + 4 : off + 8], "little") / 100000.0
        dp = int.from_bytes(raw[off + 8 : off + 10], "little") / 1000.0
        dn = int.from_bytes(raw[off + 10 : off + 12], "little") / 1000.0
        temp = int.from_bytes(raw[off + 13 : off + 15], "little") / 10.0
        samples.append(
            Sample(
                t=t_end - (SAMPLES_PER_PACKET - 1 - idx) * SAMPLE_INTERVAL,
                v=v,
                i=i,
                dp=dp,
                dn=dn,
                temp=temp,
            )
        )
    return Decoded("data", samples, crc_ok, raw)


def make_packet(payload: bytes, fill: int = 0) -> bytes:
    """用于构造/测试：payload 从第 0 字节开始，其余补 fill。"""
    body = bytearray([fill] * PACKET_LEN)
    body[: len(payload)] = payload
    return bytes(body)


def clock_command(kind: str) -> bytes:
    return COMMANDS[kind]
