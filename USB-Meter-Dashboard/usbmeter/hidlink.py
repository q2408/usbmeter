"""Windows HID 访问层。

枚举走系统 SetupAPI（比 hidapi 的 enumerate 更底层、更可靠），
读写仍用随包附带的 hidapi（hid.cp314-win_amd64.pyd）。
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

# 已知机型
KNOWN_DEVICES: Dict[Tuple[int, int], str] = {
    (0x2E3C, 0x5558): "FNB58",
    (0x2E3C, 0x0049): "FNB48S",
    (0x0483, 0x003A): "FNB48",
    (0x0483, 0x003B): "C1",
}

_PREFERENCE = {"FNB58": 0, "FNB48S": 1, "FNB48": 2, "C1": 3}
_PATH_VIDPID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.IGNORECASE)

try:  # pragma: no cover - 依赖本机环境
    import hid  # type: ignore
except Exception as exc:  # noqa: BLE001
    hid = None
    _HID_IMPORT_ERROR = exc
else:
    _HID_IMPORT_ERROR = None

_enum_error: str = ""


def hid_available() -> bool:
    return hid is not None


def import_error() -> Optional[BaseException]:
    return _HID_IMPORT_ERROR


# ----------------------------------------------------------------------
# SetupAPI / hid.dll 原生封装
# ----------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class _Api:
    def __init__(self) -> None:
        self.ok = False
        try:
            self.hid = ctypes.WinDLL("hid.dll")
            self.setup = ctypes.WinDLL("setupapi.dll")
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError as exc:  # pragma: no cover
            global _enum_error
            _enum_error = f"WinDLL: {exc}"
            return

        k, h, s = self.k32, self.hid, self.setup
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CloseHandle.argtypes = [ctypes.c_void_p]

        h.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
        h.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDD_ATTRIBUTES)]
        h.HidD_GetAttributes.restype = wintypes.BOOLEAN
        h.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        h.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
        h.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        h.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        h.HidP_GetCaps.restype = wintypes.ULONG
        for name in ("HidD_GetProductString", "HidD_GetManufacturerString", "HidD_GetSerialNumberString"):
            fn = getattr(h, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
            fn.restype = wintypes.BOOLEAN

        s.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
        ]
        s.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        s.SetupDiEnumDeviceInterfaces.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
            wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ]
        s.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        s.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        s.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        s.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        self.ok = True

    # -- helpers -------------------------------------------------------
    def open_path(self, path: str):
        handle = self.k32.CreateFileW(
            path,
            0,
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        return handle

    def close(self, handle) -> None:
        if handle:
            self.k32.CloseHandle(handle)

    def read_wide(self, fn, handle) -> str:
        buf = ctypes.create_unicode_buffer(256)
        if fn(handle, buf, ctypes.sizeof(buf)):
            return buf.value
        return ""

    def caps(self, handle) -> Optional[dict]:
        preparsed = ctypes.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            return None
        try:
            caps = _HIDP_CAPS()
            if self.hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != 0x00110000:
                return None
            return {
                "input": int(caps.InputReportByteLength),
                "output": int(caps.OutputReportByteLength),
                "feature": int(caps.FeatureReportByteLength),
                "usage_page": int(caps.UsagePage),
                "usage": int(caps.Usage),
            }
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)

    # -- enumeration ---------------------------------------------------
    def paths(self) -> List[str]:
        if not self.ok:
            return []
        guid = _GUID()
        self.hid.HidD_GetHidGuid(ctypes.byref(guid))
        hdev = self.setup.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, 0x00000012  # PRESENT | DEVICEINTERFACE
        )
        if not hdev or hdev == ctypes.c_void_p(-1).value:
            return []
        out: List[str] = []
        try:
            index = 0
            while True:
                ifdata = _SP_DEVICE_INTERFACE_DATA()
                ifdata.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
                if not self.setup.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid), index, ctypes.byref(ifdata)
                ):
                    break
                index += 1
                required = wintypes.DWORD()
                self.setup.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifdata), None, 0, ctypes.byref(required), None
                )
                size = int(required.value) or 1024
                buf = ctypes.create_string_buffer(size)
                ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = (
                    8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                )
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(ifdata), buf, size, ctypes.byref(required), None
                ):
                    continue
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(hdev)
        return out

    def describe(self, path: str) -> dict:
        info: dict = {"path": path, "vendor_id": 0, "product_id": 0,
                      "product": "", "manufacturer": "", "serial": "",
                      "usage_page": None, "usage": None, "reports": None}
        handle = self.open_path(path)
        if not handle:
            info["error"] = "CreateFile 失败"
            return info
        try:
            attrs = _HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(_HIDD_ATTRIBUTES)
            if self.hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                info["vendor_id"] = int(attrs.VendorID)
                info["product_id"] = int(attrs.ProductID)
                info["version"] = int(attrs.VersionNumber)
            info["product"] = self.read_wide(self.hid.HidD_GetProductString, handle)
            info["manufacturer"] = self.read_wide(self.hid.HidD_GetManufacturerString, handle)
            info["serial"] = self.read_wide(self.hid.HidD_GetSerialNumberString, handle)
            caps = self.caps(handle)
            if caps:
                info["reports"] = {
                    "input": caps["input"],
                    "output": caps["output"],
                    "feature": caps["feature"],
                }
                info["usage_page"] = caps["usage_page"]
                info["usage"] = caps["usage"]
        finally:
            self.close(handle)
        return info


_api: Optional[_Api] = None


def _get_api() -> Optional[_Api]:
    global _api
    if sys.platform != "win32":
        return None
    if _api is None:
        _api = _Api()
        if not _api.ok:
            return None
    return _api


def _path_vid_pid(path: str) -> Optional[Tuple[int, int]]:
    m = _PATH_VIDPID.search(path)
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)


def enum_diag() -> Dict:
    """枚举诊断：两种方式各看到多少设备、有没有报错。"""
    diag = {"native": 0, "hidapi": 0, "error": _enum_error}
    api = _get_api()
    if api:
        try:
            diag["native"] = len(api.paths())
        except Exception as exc:  # noqa: BLE001
            diag["error"] = f"native: {exc}"
    if hid is not None:
        try:
            diag["hidapi"] = len(hid.enumerate())
        except Exception as exc:  # noqa: BLE001
            diag["error"] = f"hidapi: {exc}"
    return diag


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------
def _model_for(vid: int, pid: int, product: str, manufacturer: str) -> Optional[str]:
    model = KNOWN_DEVICES.get((vid, pid))
    if model:
        return model
    p, m = (product or "").upper(), (manufacturer or "").upper()
    if "FNIRSI" in m or "FNIRSI" in p or p.startswith("FNB"):
        return (product or "FNIRSI").strip()
    return None


def list_meters() -> List[dict]:
    """返回当前连接的 FNIRSI 仪表（可能有多台）。"""
    api = _get_api()
    found: List[dict] = []
    native_paths = api.paths() if api else []

    if native_paths:
        for path in native_paths:
            vp = _path_vid_pid(path)
            if not vp:
                continue
            vid, pid = vp
            model = KNOWN_DEVICES.get((vid, pid))
            if not model and vid != 0x2E3C:
                # 0x0483 是 ST 通用 VID，不能见一个认一个；FNIRSI 自己的 2E3C 才兜底
                continue
            info = api.describe(path)
            if not model:
                model = _model_for(vid, pid, info["product"], info["manufacturer"])
            if not model:
                model = info["product"] or "FNIRSI"
            info["model"] = model
            info["protocol"] = "v2" if vid == 0x2E3C else "v1"
            found.append(info)
    elif hid is not None:
        # SetupAPI 什么都没枚举到 / 非 Windows 时退回 hidapi
        try:
            for entry in hid.enumerate():
                vid = entry.get("vendor_id") or 0
                pid = entry.get("product_id") or 0
                product = entry.get("product_string") or ""
                manufacturer = entry.get("manufacturer_string") or ""
                model = _model_for(vid, pid, product, manufacturer)
                if not model:
                    continue
                path = entry.get("path") or b""
                if isinstance(path, bytes):
                    path = path.decode("latin1", "replace")
                found.append({
                    "path": path, "vendor_id": vid, "product_id": pid,
                    "product": product, "manufacturer": manufacturer,
                    "serial": entry.get("serial_number") or "",
                    "usage_page": entry.get("usage_page"),
                    "usage": entry.get("usage"), "reports": hid_report_sizes(path),
                    "model": model, "protocol": "v2" if vid == 0x2E3C else "v1",
                })
        except Exception:  # noqa: BLE001
            pass

    found.sort(key=lambda d: (_PREFERENCE.get(d.get("model"), 9), d["path"]))
    return found


def list_all_hid() -> List[dict]:
    """列出所有 HID 设备（诊断页用），附带报告长度。"""
    api = _get_api()
    native_paths = api.paths() if api else []
    if native_paths:
        out = []
        for path in native_paths:
            info = api.describe(path)
            vp = (info["vendor_id"], info["product_id"])
            if vp == (0, 0):
                vp = _path_vid_pid(path) or (0, 0)
                info["vendor_id"], info["product_id"] = vp
            info["model"] = _model_for(vp[0], vp[1], info["product"], info["manufacturer"])
            out.append(info)
        out.sort(key=lambda d: (d.get("model") is None, d["vendor_id"], d["product_id"], d["path"]))
        return out

    if hid is None:
        return []
    out = []
    try:
        for entry in hid.enumerate():
            vid = entry.get("vendor_id") or 0
            pid = entry.get("product_id") or 0
            product = entry.get("product_string") or ""
            manufacturer = entry.get("manufacturer_string") or ""
            path = entry.get("path") or b""
            if isinstance(path, bytes):
                path = path.decode("latin1", "replace")
            out.append({
                "path": path, "vendor_id": vid, "product_id": pid,
                "product": product, "manufacturer": manufacturer,
                "serial": entry.get("serial_number") or "",
                "usage_page": entry.get("usage_page"),
                "usage": entry.get("usage"),
                "reports": hid_report_sizes(path),
                "model": _model_for(vid, pid, product, manufacturer),
            })
    except Exception:  # noqa: BLE001
        return []
    out.sort(key=lambda d: (d.get("model") is None, d["vendor_id"], d["product_id"], d["path"]))
    return out


def hid_report_sizes(path: str) -> Optional[dict]:
    api = _get_api()
    if not api:
        return None
    handle = api.open_path(path)
    if not handle:
        return None
    try:
        caps = api.caps(handle)
        if not caps:
            return None
        return {"input": caps["input"], "output": caps["output"], "feature": caps["feature"]}
    finally:
        api.close(handle)


class MeterConnection:
    """一条打开的 HID 通道。"""

    def __init__(self, path: str):
        if hid is None:
            raise RuntimeError(f"hidapi 不可用: {_HID_IMPORT_ERROR}")
        self.path = path
        self._dev = hid.device()
        # hidapi 的 open_path 只接受 bytes（char*），传 str 会抛 TypeError
        open_path = path.encode("utf-8") if isinstance(path, str) else path
        try:
            self._dev.open_path(open_path)
        except TypeError:
            self._dev.open_path(path)
        self._dev.set_nonblocking(0)
        sizes = hid_report_sizes(path) or {}
        self.input_report_len = sizes.get("input", 0)
        self.output_report_len = sizes.get("output", 0)
        self.feature_report_len = sizes.get("feature", 0)
        self._report_prefix = self.output_report_len != 64

    # ------------------------------------------------------------------
    def write(self, packet: bytes) -> int:
        if len(packet) != 64:
            raise ValueError("命令必须是 64 字节")
        attempts = [b"\x00" + packet, packet] if self._report_prefix else [packet]
        last = -1
        for data in attempts:
            try:
                ret = self._dev.write(data)
            except Exception:  # noqa: BLE001
                ret = -1
            if ret and ret > 0:
                self._report_prefix = len(data) == 65
                return ret
            last = ret
        raise OSError(f"hid_write 失败（返回 {last}），设备可能需要拔插一次")

    def read(self, timeout_ms: int = 200) -> bytes:
        data = self._dev.read(65, timeout_ms)
        if not data:
            return b""
        return bytes(data)

    def close(self) -> None:
        try:
            self._dev.close()
        except Exception:  # noqa: BLE001
            pass
