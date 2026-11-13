"""本地 HTTP / SSE 服务与前端静态资源。"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from .device import MeterService
from .hidlink import enum_diag, list_all_hid, list_meters
from .session import Session

DEFAULT_CONFIG: Dict = {
    "alarms": {"v": 28.0, "i": 8.0, "p": 224.0, "t": 80.0},
    "temperature_unit": "C",
    "window": 30,
    "smooth": 0.0,
    "stop_cmd": False,
    "privacy": False,
}

_hid_cache: Dict = {"t": 0.0, "data": []}
_hid_lock = threading.Lock()


def list_all_hid_cached(ttl: float = 2.0) -> list:
    now = time.time()
    with _hid_lock:
        if now - _hid_cache["t"] > ttl:
            _hid_cache["t"] = now
            _hid_cache["data"] = list_all_hid()
        return list(_hid_cache["data"])


def ensure_data_root(root: Path) -> tuple:
    """配置和日志固定放在程序目录（解压出来的文件夹），不写到别处。"""
    try:
        (root / "logs").mkdir(parents=True, exist_ok=True)
        probe = root / "logs" / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return root, True
    except OSError:
        return root, False


class Hub:
    """极简发布/订阅，用于 SSE 广播。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=240)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, message: Dict) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def _mask_serials(items: List[Dict]) -> List[Dict]:
    out = []
    for item in items:
        row = dict(item)
        if row.get("serial"):
            row["serial"] = "****"
        # 诊断页/导出的 JSON 里不暴露 USB 厂商号、产品号和设备路径
        row["vendor_id"] = 0
        row["product_id"] = 0
        row["path"] = "…"
        out.append(row)
    return out


def _mask_path(value: str) -> str:
    """诊断信息里用 %USERPROFILE% 代替用户目录，避免泄漏 Windows 用户名。"""
    if not value:
        return value
    home = str(Path.home())
    if home and value.lower().startswith(home.lower()):
        return "%USERPROFILE%" + value[len(home):]
    return value


class App:
    def __init__(self, root: Path, poll: bool = True):
        self.root = root
        self.web = root / "web"
        # 配置和日志固定放在程序目录，随文件夹一起走。
        self.data, self.data_writable = ensure_data_root(root)
        self.config_path = self.data / "config.json"
        self.config = self._load_config()
        self.hub = Hub()
        self.session = Session(self.data / "logs", writable=self.data_writable)
        self.session.set_smoothing(self.config.get("smooth", 0))
        self.service = MeterService(
            self.session,
            on_samples=self._on_samples,
            poll=poll,
            send_stop=bool(self.config.get("stop_cmd", True)),
        )
        self.service.set_privacy(bool(self.config.get("privacy", False)))
        self._stop = threading.Event()
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        self.server = None
        self.host = "0.0.0.0"

    # ------------------------------------------------------------------
    def rebind(self, new_port: int) -> bool:
        """在网页里改端口：先在新端口监听成功，再关掉旧端口。"""
        old = self.server
        try:
            new = StudioServer((self.host, int(new_port)), self)
        except OSError:
            return False
        self.server = new
        self.config["port"] = int(new.server_address[1])
        self.save_config()
        threading.Thread(target=_serve_forever, args=(new,), name="fnb58-http", daemon=True).start()
        if old is not None and old is not new:
            def _close_old():
                try:
                    old.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    old.server_close()
                except Exception:  # noqa: BLE001
                    pass

            threading.Timer(0.6, _close_old).start()
        actual = int(new.server_address[1])
        print(f"\n  端口已切换: http://127.0.0.1:{actual}/  （配置已保存）\n")
        return True

    # ------------------------------------------------------------------
    def _load_config(self) -> Dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
                    cfg["alarms"] = {**DEFAULT_CONFIG["alarms"], **data.get("alarms", {})}
                    cfg.pop("demo", None)  # 旧版本残留
        except Exception:  # noqa: BLE001
            pass
        return cfg

    def save_config(self) -> None:
        try:
            self.config_path.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    def _on_samples(self, samples) -> None:
        self.hub.publish(
            {
                "type": "data",
                "s": [
                    [
                        round(s.t, 3),
                        round(s.v, 5),
                        round(s.i, 5),
                        round(s.dp, 3),
                        round(s.dn, 3),
                        round(s.temp, 2),
                    ]
                    for s in samples
                ],
            }
        )

    def _stats_loop(self) -> None:
        while not self._stop.is_set():
            self.hub.publish(
                {
                    "type": "stats",
                    "stats": self.session.snapshot(),
                    "device": self.service.info(),
                }
            )
            self._stop.wait(0.4)

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.service.start()
        self._stats_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.service.stop()
        self.session.close()

    def state(self) -> Dict:
        return {
            "device": self.service.info(),
            "stats": self.session.snapshot(),
            "config": self.config,
        }

    def debug_payload(self) -> Dict:
        payload = self.service.debug_snapshot()
        privacy = bool(self.config.get("privacy", False))
        if privacy:
            # 隐私模式：只列仪表，且 ID/序列号/路径打码
            payload["devices"] = _mask_serials(list_meters())
            payload["hid_total"] = len(list_all_hid_cached())
            paths = {
                "root": _mask_path(str(self.root)),
                "data": _mask_path(str(self.data)),
                "logs": _mask_path(str(self.data / "logs")),
                "config": _mask_path(str(self.config_path)),
            }
        else:
            payload["devices"] = list_all_hid_cached()
            paths = {
                "root": str(self.root),
                "data": str(self.data),
                "logs": str(self.data / "logs"),
                "config": str(self.config_path),
            }
        payload["enum"] = enum_diag()
        payload["session"] = self.session.snapshot()
        payload["config"] = self.config
        payload["paths"] = paths
        import platform
        import sys

        payload["env"] = {
            "python": sys.version,
            "executable": _mask_path(sys.executable) if privacy else sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }
        return payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "USB-Meter-Dashboard/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003
        return  # 安静

    def handle_one_request(self):
        # 浏览器/客户端提前断开时不要往控制台打 traceback
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/stream":
            return self._sse()
        if path == "/api/state":
            return self._json(self.app.state())
        if path == "/api/probe":
            return self._json(self.app.service.probe())
        if path == "/api/debug":
            return self._json(self.app.debug_payload())
        if path == "/api/debug/export":
            return self._debug_export()
        if path == "/api/export":
            return self._export()
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path in ("/debug", "/debug/"):
            return self._static("debug.html")
        return self._static(path.lstrip("/"))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/api/session/new":
            self.app.session.reset()
            return self._json({"ok": True})
        if path == "/api/record":
            want = bool(body.get("recording"))
            ok = self.app.session.set_recording(want)
            return self._json(
                {
                    "ok": ok,
                    "recording": want and ok,
                    "error": "" if ok else "程序目录不可写，无法记录 CSV",
                },
                200 if ok else 400,
            )
        if path == "/api/rescan":
            self.app.service.rescan()
            return self._json({"ok": True})
        if path == "/api/device/disconnect":
            self.app.service.disconnect()
            return self._json({"ok": True})
        if path == "/api/port":
            try:
                new_port = int(body.get("port"))
            except (TypeError, ValueError):
                return self._json({"error": "端口必须是 1024-65535 的整数"}, 400)
            if not (1024 <= new_port <= 65535):
                return self._json({"error": "端口范围 1024-65535"}, 400)
            current = self.app.server.server_address[1] if self.app.server else 0
            if new_port == current:
                return self._json({"ok": True, "port": current, "unchanged": True})
            if self.app.rebind(new_port):
                return self._json({"ok": True, "port": new_port})
            return self._json({"error": f"端口 {new_port} 无法监听（可能被占用）"}, 400)
        if path == "/api/settings":
            alarms = body.get("alarms")
            if isinstance(alarms, dict):
                for key in ("v", "i", "p", "t"):
                    if key in alarms:
                        try:
                            self.app.config["alarms"][key] = float(alarms[key])
                        except (TypeError, ValueError):
                            pass
            if "temperature_unit" in body:
                self.app.config["temperature_unit"] = body["temperature_unit"]
            if "window" in body:
                self.app.config["window"] = int(body["window"])
            if "smooth" in body:
                try:
                    smooth = max(0.0, min(10.0, float(body["smooth"])))
                except (TypeError, ValueError):
                    smooth = 0.0
                self.app.config["smooth"] = smooth
                self.app.session.set_smoothing(smooth)
            if "privacy" in body:
                self.app.config["privacy"] = bool(body["privacy"])
                self.app.service.set_privacy(bool(body["privacy"]))
            self.app.save_config()
            return self._json({"ok": True, "config": self.app.config})
        return self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    def _static(self, rel: str) -> None:
        target = (self.app.web / rel).resolve()
        try:
            target.relative_to(self.app.web.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _export(self) -> None:
        session = self.app.session
        session.flush()
        path = session.csv_path
        if not path.exists():
            return self._json({"error": "还没有记录文件：先在页面上点「未记录」开始记录"}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{path.name}"'
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _debug_export(self) -> None:
        payload = self.app.debug_payload()
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        name = time.strftime("meter-debug-%Y%m%d-%H%M%S.json")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.app.hub.subscribe()
        try:
            hello = {
                "type": "hello",
                "device": self.app.service.info(),
                "stats": self.app.session.snapshot(),
                "config": self.app.config,
            }
            self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: App):
        super().__init__(address, _Handler)
        self.app = app


def _open_window(url: str) -> bool:
    """优先用 Edge 的应用窗口（没有地址栏，更像桌面程序）。"""
    import os
    import subprocess

    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for exe in candidates:
        if os.path.isfile(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1420,920"])
                return True
            except Exception:  # noqa: BLE001
                pass
    return False


def lan_addresses() -> list:
    """列出本机局域网 IPv4，用于启动时提示访问地址。"""
    import socket

    ips = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))  # 不会真的发包，只让系统选出口网卡
            ips.append(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _spawn_live_console(root: Path, url: str) -> bool:
    """在独立 CMD 窗口里打开实时读数（tools/live.py）。"""
    import subprocess
    import sys

    script = root / "tools" / "live.py"
    if not script.exists():
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(script), "--url", url],
            cwd=str(root),
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _serve_forever(server) -> None:
    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001
        pass


def _another_instance(port: int) -> bool:
    """本机同端口上是不是已经跑着一个 USB Meter Dashboard。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and "device" in data and "stats" in data
    except Exception:  # noqa: BLE001
        return False


def _install_console_close_handler(app: App):
    """关窗口时先给设备发 STOP 再退出，避免设备卡在“一直发数据”的状态。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        prototype = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def handler(event):
            if event in (2, 5, 6):  # CTRL_CLOSE / LOGOFF / SHUTDOWN
                try:
                    app.stop()
                except Exception:  # noqa: BLE001
                    pass
            return 1

        callback = prototype(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
        return callback
    except Exception:  # noqa: BLE001
        return None


def run(
    root: Path,
    host: str = "0.0.0.0",
    port: int = None,
    open_browser: bool = False,
    window: bool = False,
    spawn_console: bool = True,
    poll: bool = True,
):
    app = App(root, poll=poll)
    app.host = host
    if port is None:
        try:
            port = int(app.config.get("port") or 8765)
        except (TypeError, ValueError):
            port = 8765
    if port and _another_instance(port):
        print(f"\n  已有一个 USB Meter Dashboard 运行在 http://127.0.0.1:{port}/，本次不重复启动。\n")
        return port
    try:
        server = StudioServer((host, port), app)
    except OSError:
        # 端口被占用时退回到随机空闲端口
        server = StudioServer((host, 0), app)
    actual_port = server.server_address[1]
    app.server = server
    app.config["port"] = actual_port
    # 0.0.0.0 不能直接放进浏览器地址，本机一律用 127.0.0.1 打开
    wildcard = host in ("0.0.0.0", "::", "")
    local_host = "127.0.0.1" if wildcard else host
    url = f"http://{local_host}:{actual_port}"
    app.start()
    app._console_handler = _install_console_close_handler(app)
    print(f"\n  USB Meter Dashboard 已启动（端口 {actual_port}）")
    if wildcard:
        print(f"  本机访问:   {url}/")
        for ip in lan_addresses():
            print(f"  局域网访问: http://{ip}:{actual_port}/")
        print("  提示: 首次运行如弹出 Windows 防火墙，请勾选「专用网络」允许访问。")
        print("        只想本机使用时，启动时加 --host 127.0.0.1。")
    else:
        print(f"  访问地址:   {url}/（仅监听该地址）")
    print(f"  诊断页面:   {url}/debug")
    print(f"  数据目录: {app.data}\n")
    if spawn_console and sys.platform == "win32" and sys.stdout.isatty():
        if _spawn_live_console(root, url):
            print("  已打开独立的实时读数窗口（关闭它不影响记录）\n")
    if open_browser:
        def _launch():
            if window and _open_window(url):
                return
            webbrowser.open(url)

        threading.Timer(0.6, _launch).start()
    threading.Thread(target=_serve_forever, args=(server,), name="fnb58-http", daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv = app.server or server
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        app.stop()
    return actual_port
