# -*- coding: utf-8 -*-
"""Web 控制台的 HTTP 服务。

只用标准库 http.server + ThreadingHTTPServer——面板是单机运维工具，
并发量是「一个护士站开着一个浏览器」，引入 Flask/FastAPI 只会多一层依赖。

默认只绑定 127.0.0.1：画面里有住户影像，监听 0.0.0.0 等于把监控视频
暴露给整个局域网。要在护士站远程看，用 --host 显式指定并自行承担风险
（没有鉴权，这是原型的已知边界）。
"""
from __future__ import annotations

import json
import mimetypes
import posixpath
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import config as cfgmod
from . import doctor, schema
from .runner import Runner

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class App:
    """把路由处理与 HTTP 细节分开，便于不起服务器直接测。"""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path
        self.runner = Runner()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 配置
    def _cfg(self):
        return cfgmod.load(self.config_path)

    def _event_log(self):
        from ..events import EventLog

        cfg = self._cfg()
        return EventLog(cfg.path(cfg.get("log.dir", "data")))

    # ------------------------------------------------------------ 路由
    def get(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if path == "/api/status":
            return 200, self._status()
        if path == "/api/schema":
            return 200, {"sections": schema.schema_json(),
                         "slot_keys": list(schema.SLOT_KEYS)}
        if path == "/api/config":
            return 200, self._config_payload()
        if path == "/api/doctor":
            return 200, doctor.report(self.config_path,
                                      running=self.runner.alive)
        if path == "/api/logs":
            since = _int(query.get("since", ["0"])[0], 0)
            rows, latest = self.runner.hub.logs(since)
            return 200, {"logs": rows, "latest": latest}
        if path == "/api/events":
            return 200, self._events(_int(query.get("limit", ["50"])[0], 50))
        if path.startswith("/api/events/"):
            return self._event_detail(path[len("/api/events/"):])
        return 404, {"error": "未知接口"}

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if path == "/api/config/validate":
            return 200, self._validate(body)
        if path == "/api/config":
            return self._save(body)
        if path == "/api/run/start":
            return self._start(body)
        if path == "/api/run/stop":
            return 200, self.runner.stop()
        if path == "/api/run/key":
            return 200, self.runner.send_key(str(body.get("key") or ""))
        if path == "/api/call-test":
            return self._call_test(body)
        if path.startswith("/api/events/") and path.endswith("/label"):
            event_id = path[len("/api/events/"):-len("/label")]
            return self._label(event_id, body)
        return 404, {"error": "未知接口"}

    # ------------------------------------------------------------ 实现
    def _status(self) -> dict[str, Any]:
        status = self.runner.status()
        try:
            cfg = self._cfg()
            status["config"] = {
                "path": str(cfgmod.config_source(self.config_path)),
                "saved": cfgmod.config_source(self.config_path).exists(),
                "place": str(cfg.scene.place),
                "resident": str(cfg.scene.resident_name),
                "source": cfg.camera.source,
                "backend": str(cfg.detector.backend),
                "provider": str(cfg.call.provider),
                "dry_run": bool(cfg.call.dry_run),
                "contacts": len(cfg.get("call.contacts") or []),
                "agent_enabled": bool(cfg.agent.enabled),
                "agent_key": bool(str(cfg.agent.api_key or "")),
                "cancel_seconds": float(cfg.alert.cancel_seconds),
            }
            status["config_error"] = ""
        except Exception as exc:
            status["config"] = None
            status["config_error"] = f"{type(exc).__name__}: {exc}"
        try:
            status["event_stats"] = self._event_log().stats()
        except Exception:
            status["event_stats"] = None
        return status

    def _config_payload(self) -> dict[str, Any]:
        raw = schema.read_raw(self.config_path)
        _data, errors, warnings = schema.validate({}, base=raw)
        return {
            "values": raw,
            "path": str(schema.config_path(self.config_path)),
            "saved": schema.config_path(self.config_path).exists(),
            "errors": errors,
            "warnings": warnings,
        }

    def _validate(self, body: dict[str, Any]) -> dict[str, Any]:
        base = schema.read_raw(self.config_path)
        _merged, errors, warnings = schema.validate(
            body.get("values") or {}, base=base)
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def _save(self, body: dict[str, Any]) -> tuple[int, Any]:
        base = schema.read_raw(self.config_path)
        merged, errors, warnings = schema.validate(
            body.get("values") or {}, base=base)
        if errors:
            return 400, {"ok": False, "errors": errors, "warnings": warnings}
        with self._lock:
            path = schema.write_raw(merged, self.config_path)
        self.runner.hub.log(f"[配置] 已保存到 {path}")
        note = ""
        if self.runner.alive:
            note = "系统正在运行，新配置要停止后重新启动才会生效。"
        return 200, {"ok": True, "errors": [], "warnings": warnings,
                     "path": str(path), "note": note,
                     "values": schema.read_raw(self.config_path)}

    def _start(self, body: dict[str, Any]) -> tuple[int, Any]:
        if self.runner.alive:
            return 409, {"ok": False, "reason": "已在运行中"}
        try:
            cfg = self._cfg()
        except Exception as exc:
            return 400, {"ok": False, "reason": f"配置加载失败：{exc}"}

        options: dict[str, Any] = {}
        raw = cfg.raw()
        source = str(body.get("source") or "").strip()
        if source:
            raw["camera"]["source"] = (int(source) if source.isdigit() else source)
            options["source"] = raw["camera"]["source"]
        backend = str(body.get("backend") or "").strip()
        if backend and backend != "auto":
            options["backend"] = backend
        if body.get("no_call"):
            raw["call"]["dry_run"] = True
            options["no_call"] = True
        if body.get("no_agent"):
            raw["agent"]["enabled"] = False
            options["no_agent"] = True
        if body.get("quiet"):
            raw["alert"]["speak"] = False
            raw["alert"]["alarm_sound"] = False
            options["quiet"] = True
        if body.get("cancel_seconds") not in (None, ""):
            try:
                raw["alert"]["cancel_seconds"] = float(body["cancel_seconds"])
                options["cancel_seconds"] = raw["alert"]["cancel_seconds"]
            except (TypeError, ValueError):
                return 400, {"ok": False, "reason": "cancel_seconds 不是数字"}
        if body.get("max_seconds") not in (None, ""):
            try:
                options["max_seconds"] = float(body["max_seconds"])
            except (TypeError, ValueError):
                return 400, {"ok": False, "reason": "max_seconds 不是数字"}

        result = self.runner.start(cfg, options)
        return (200 if result.get("ok") else 400), result

    def _call_test(self, body: dict[str, Any]) -> tuple[int, Any]:
        from ..alerts import CallGateway

        try:
            cfg = self._cfg()
        except Exception as exc:
            return 400, {"ok": False, "reason": f"配置加载失败：{exc}"}
        real = bool(body.get("real"))
        if real:
            cfg.raw()["call"]["dry_run"] = False
        gw = CallGateway(cfg)
        if not gw.contacts:
            return 400, {"ok": False,
                         "reason": "未配置联系人，先在「配置 → 联系人」里添加"}
        contact_id = str(body.get("contact_id") or "").strip()
        contact = gw.contact_by_id(contact_id) if contact_id else gw.contacts[0]
        if contact is None:
            return 400, {"ok": False,
                         "reason": f"未找到联系人 id={contact_id!r}",
                         "available": [c.id for c in gw.contacts]}

        slots = {
            "name": str(cfg.scene.resident_name),
            "time": time.strftime("%H点%M分"),
            "place": str(cfg.scene.place),
            "event": "跌倒",
        }
        res = gw.call_one(contact, slots, "CALLTEST")
        self.runner.hub.log(
            f"[外呼测试] {contact.name} → ok={res.ok} code={res.code} "
            f"{res.message}{'（演练）' if res.dry_run else ''}")
        return 200, {"ok": True, "describe": gw.describe(), "slots": slots,
                     "result": res.to_dict(), "params": res.params}

    def _events(self, limit: int) -> dict[str, Any]:
        try:
            log = self._event_log()
        except Exception as exc:
            return {"events": [], "stats": None, "error": str(exc)}
        rows = log.events(limit=max(limit, 1))
        brief = [{
            "event_id": r.get("event_id", ""),
            "detected_at": r.get("detected_at", ""),
            "level": r.get("level", ""),
            "trigger": r.get("trigger", ""),
            "confidence": r.get("confidence", 0),
            "room": r.get("room", ""),
            "resident": r.get("resident", ""),
            "outcome": r.get("outcome", ""),
            "cancelled": bool(r.get("cancelled")),
            "fallback_fired": bool(r.get("fallback_fired")),
            "calls_ok": sum(1 for c in (r.get("agent") or {}).get("called") or []
                            if c.get("ok")),
            "agent_mode": (r.get("agent") or {}).get("mode", ""),
            "keyframes": len((r.get("evidence") or {}).get("keyframes") or []),
            "has_clip": bool((r.get("evidence") or {}).get("clip")),
            "label": r.get("label"),
        } for r in reversed(rows)]
        return {"events": brief, "stats": log.stats(), "error": ""}

    def _event_detail(self, rest: str) -> tuple[int, Any]:
        # 路径段来自 URL，必须解码：关键帧文件名形如 keyframe_t+0.0s.jpg，
        # 里面的 + 会被前端编码成 %2B，不解码就永远找不到文件
        parts = [urllib.parse.unquote(p) for p in rest.split("/") if p]
        if not parts or not EVENT_ID_RE.match(parts[0]):
            return 404, {"error": "事件号不合法"}
        event_id = parts[0]
        rows = self._event_log().events()
        record = next((r for r in rows if r.get("event_id") == event_id), None)
        if record is None:
            return 404, {"error": f"未找到事件 {event_id}"}
        if len(parts) == 1:
            return 200, {"event": record}
        if parts[1] == "evidence" and len(parts) == 3:
            return self._evidence_file(record, parts[2])
        return 404, {"error": "未知接口"}

    def _evidence_file(self, record: dict[str, Any], name: str) -> tuple[int, Any]:
        base = (record.get("evidence") or {}).get("dir")
        if not base:
            return 404, {"error": "该事件没有留证目录"}
        root = Path(base).resolve()
        target = (root / name).resolve()
        # 证据目录来自事件记录，文件名来自 URL——必须确认没跳出目录
        if not str(target).startswith(str(root) + "/") or not target.is_file():
            return 404, {"error": "文件不存在"}
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return 200, ("raw", ctype, target.read_bytes())

    def _label(self, event_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        if not EVENT_ID_RE.match(event_id):
            return 400, {"ok": False, "reason": "事件号不合法"}
        is_false = bool(body.get("is_false_alarm", True))
        payload = self._event_log().label(
            event_id, is_false, str(body.get("reason") or ""), by="web")
        self.runner.hub.log(
            f"[标注] {event_id} → {'误报' if is_false else '真实事件'}")
        # 标注为误报通常是为了立刻复测，顺手解除静默期
        guard = self.runner.guard
        if is_false and guard is not None and self.runner.alive:
            guard.detector.reset_cooldown()
        return 200, {"ok": True, "label": payload}


# --------------------------------------------------------------------- HTTP
def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "FallGuardWeb/1.0"
    app: App = None                      # 由 serve() 注入

    def log_message(self, fmt: str, *args: Any) -> None:
        return                            # 访问日志会把面板轮询刷满屏幕

    # ------------------------------------------------------------ 工具
    def _send(self, code: int, payload: Any) -> None:
        if isinstance(payload, tuple) and payload and payload[0] == "raw":
            _tag, ctype, blob = payload
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)
            return
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        safe = posixpath.normpath(name).lstrip("./")
        target = (STATIC_DIR / safe).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            self._send(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        blob = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _stream(self) -> None:
        boundary = b"fgframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=" +
                         boundary.decode())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in self.app.runner.mjpeg(boundary):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return                        # 浏览器切页面就会断，属正常

    # ------------------------------------------------------------ 入口
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/stream.mjpg":
            self._stream()
            return
        if path.startswith("/api/"):
            query = urllib.parse.parse_qs(parsed.query)
            try:
                code, payload = self.app.get(path, query)
            except Exception as exc:
                code, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
            self._send(code, payload)
            return
        self._static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        length = _int(self.headers.get("Content-Length", "0"), 0)
        if length > MAX_BODY:
            self._send(413, {"error": "请求体过大"})
            return
        body: dict[str, Any] = {}
        if length > 0:
            raw = self.rfile.read(length)
            try:
                parsed_body = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(parsed_body, dict):
                    body = parsed_body
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
                return
        try:
            code, payload = self.app.post(parsed.path, body)
        except Exception as exc:
            code, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._send(code, payload)


def serve(host: str = "127.0.0.1", port: int = 8777,
          config_path: str | None = None, *, open_browser: bool = True) -> int:
    app = App(config_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    url = f"http://{shown}:{port}/"
    print("=" * 62)
    print("守望者 FallGuard · Web 控制台")
    print("=" * 62)
    print(f"  地址     {url}")
    print(f"  配置     {schema.config_path(config_path)}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  警告     正在监听 {host}，面板没有鉴权，"
              f"同网段任何人都能看到监控画面与住户信息")
    print("  停止     Ctrl-C")
    print()

    if open_browser:
        def launch() -> None:
            import webbrowser

            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=launch, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 收到 Ctrl-C")
    finally:
        if app.runner.alive:
            print("[停止] 等待监测线程收尾…")
            app.runner.stop(timeout=30)
        httpd.shutdown()
        httpd.server_close()
    return 0
