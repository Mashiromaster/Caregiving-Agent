# -*- coding: utf-8 -*-
"""后台运行器：把 FallGuard 主循环搬进线程，画面经 MJPEG 推给浏览器。

接入点只有一个 `_show()`——主循环与取消窗口的 poll 都会调用它，
原本用来 imshow 并返回按键码。这里覆写为「把渲染结果塞进 Hub + 返回
注入的按键码」，于是：

  · 画面：render.draw 的结果直接编码成 JPEG，浏览器看到的和预览窗一模一样
  · 按键：网页上点「取消外呼」就等价于在预览窗按 c，复用同一条 _handle_key 路径

这样取消、误报标注、手动触发三个动作不需要在 pipeline 里新开接口，
也不会出现「网页取消」和「预览窗取消」两套语义不一致的实现。
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable

import cv2

from .. import render
from ..pipeline import FallGuard

STREAM_FPS = 12
LOG_LIMIT = 600


class Hub:
    """帧与日志的中转站。单生产者（运行线程）多消费者（HTTP 线程）。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._obs: dict[str, Any] = {}
        self._keys: collections.deque[int] = collections.deque(maxlen=16)
        self._logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._log_lock = threading.Lock()
        self._last_encode = 0.0
        # 每条 MJPEG 连接占一个"席位"。浏览器切换标签页时会新建连接，
        # 而旧连接要等 TCP 超时才断——不设上限的话连接会越积越多，
        # 每条都在推同一份帧，画面 fps 被活活拖垮（实测 6 条连接 → 30fps 掉到 11.8）。
        self._stream_epoch = 0

    def claim_stream(self) -> int:
        """申请一个推流席位，返回本次的 epoch。

        新连接到来时 epoch 自增，老连接发现 epoch 变了就自行退出——
        实时画面只需要最新那一条连接，旧的留着纯属浪费。
        """
        with self._cv:
            self._stream_epoch += 1
            self._cv.notify_all()
            return self._stream_epoch

    def stream_current(self, epoch: int) -> bool:
        with self._cv:
            return epoch == self._stream_epoch

    # ----------------------------------------------------------- 帧
    def publish(self, frame, obs_dict: dict[str, Any]) -> None:
        now = time.time()
        # 判定线程可能跑在 60fps 的视频文件上，没必要每帧都编码
        if now - self._last_encode < 1.0 / STREAM_FPS:
            with self._cv:
                self._obs = obs_dict
            return
        self._last_encode = now
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if not ok:
            return
        with self._cv:
            self._jpeg = enc.tobytes()
            self._seq += 1
            self._obs = obs_dict
            self._cv.notify_all()

    def latest(self) -> tuple[bytes | None, int]:
        with self._cv:
            return self._jpeg, self._seq

    def wait_frame(self, since: int, timeout: float = 5.0) -> tuple[bytes | None, int]:
        with self._cv:
            if self._seq == since:
                self._cv.wait(timeout)
            return self._jpeg, self._seq

    def observation(self) -> dict[str, Any]:
        with self._cv:
            return dict(self._obs)

    def clear_frame(self) -> None:
        with self._cv:
            self._jpeg = None
            self._seq += 1
            self._obs = {}
            self._cv.notify_all()

    # ----------------------------------------------------------- 按键
    def push_key(self, key: str) -> None:
        if key:
            self._keys.append(ord(key[0]))

    def pop_key(self) -> int:
        try:
            return self._keys.popleft()
        except IndexError:
            return -1

    # ----------------------------------------------------------- 日志
    def log(self, message: str) -> None:
        text = str(message).rstrip("\n")
        with self._log_lock:
            self._log_seq += 1
            self._logs.append({
                "seq": self._log_seq,
                "at": time.strftime("%H:%M:%S"),
                "text": text,
            })
        print(text)

    def logs(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._log_lock:
            rows = [r for r in self._logs if r["seq"] > since]
            return rows, self._log_seq


def _obs_dict(obs) -> dict[str, Any]:
    return {
        "state": obs.state,
        "has_person": obs.person is not None,
        "person_score": round(obs.person.score, 3) if obs.person else None,
        "torso_angle": round(obs.torso_angle, 1) if obs.torso_angle is not None else None,
        "aspect": round(obs.aspect, 3) if obs.aspect is not None else None,
        "descent_rate": round(obs.descent_rate, 3),
        "motion": round(obs.motion, 5),
        "lying_for": round(obs.lying_for, 2),
        "centroid_y": round(obs.centroid[1], 3) if obs.centroid else None,
        "zone_suppressed": obs.suppressed_by_zone,
    }


class WebGuard(FallGuard):
    """把渲染与按键重定向到 Hub 的 FallGuard。"""

    def __init__(self, cfg, hub: Hub, *, force_backend: str | None = None):
        super().__init__(cfg, show=False, log=hub.log, force_backend=force_backend)
        self.hub = hub

    def show_frame(self, frame, obs) -> int:
        """覆写渲染出口：画面推给 Hub，按键从 Hub 取。

        这是 Web 层对运行时的**唯一**注入点。因为覆写的是同一个方法，
        网页上的「取消外呼」与预览窗按 c 走的是完全相同的 handle_key 路径，
        不存在两套语义不一致的实现。
        """
        banner = self.active.banner if self.active else ""
        color = self.active.color if self.active else render.RED
        img = render.draw(frame, obs, backend=self._backend_name(),
                          fps=self.fps_estimate, cfg=self.cfg, banner=banner,
                          banner_color=color, stats=self.stats.as_dict())
        self.hub.publish(img, _obs_dict(obs))
        return self.hub.pop_key()

    # 旧方法名仍被 pipeline 兼容壳引用
    _show = show_frame


class Runner:
    """运行器的生命周期管理。同一时刻只允许一个 FallGuard 实例。"""

    def __init__(self, open_guard: Callable[[Callable[[], WebGuard]], WebGuard] | None = None) -> None:
        self.hub = Hub()
        self._open_guard = open_guard or (lambda create: create())
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.guard: WebGuard | None = None
        self.state = "idle"               # idle | starting | running | stopping | stopped
        self.error = ""
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.options: dict[str, Any] = {}

    # ----------------------------------------------------------- 状态
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        guard = self.guard
        active: dict[str, Any] | None = None
        if guard is not None and guard.active is not None:
            rec = guard.active.record
            handling = (guard.active.thread is not None
                        and guard.active.thread.is_alive())
            active = {
                "event_id": rec.event_id,
                "event_type": rec.event_type,
                "level": rec.level,
                "trigger": rec.trigger,
                "confidence": round(rec.confidence, 3),
                "banner": guard.active.banner,
                "outcome": rec.outcome,
                "cancelled": rec.cancelled,
                "started_ago": round(time.time() - guard.active.started, 1),
                "handling": handling,
                "done": guard.active.done.is_set(),
                "cancellable": not guard.active.done.is_set() and not handling,
                "notes": list(rec.notes),
                "grading": dict(rec.grading),
                "memory": (rec.memory or {}).get("summary", ""),
                "calls": guard.active.calls,
            }
        risk = guard.last_risk if guard else None
        return {
            "state": "running" if self.alive else self.state,
            "error": self.error,
            "options": dict(self.options),
            # 框架通用字段
            "gateway": guard.gateway.describe() if guard else None,
            "scene": ({"place": str(guard.cfg.scene.place),
                       "resident": str(guard.cfg.scene.resident_name)}
                      if guard else None),
            "fps": round(guard.fps_estimate, 1) if guard else None,
            "stats": guard.stats.as_dict() if guard else
                     {"frames": 0, "events": 0, "calls": 0, "cancelled": 0,
                      "warnings": 0},
            "uptime": round(time.time() - self.started_at, 1) if self.started_at
                      and self.alive else None,
            "observation": self.hub.observation(),
            "active": active,
            "last_event_id": guard.last_event_id if guard else None,
            # 插件自报，框架不解释
            "sensors": guard.describe_sensors() if guard else [],
            "devices": guard.describe_devices() if guard else [],
            "identity": guard.identity.describe() if guard else [],
            "backend": guard._backend_name() if guard else None,
            "risk": risk.to_dict() if risk is not None else None,
            "signals": guard.signals.snapshot() if guard else {},
        }

    # ----------------------------------------------------------- 启停
    def start(self, cfg, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.alive:
                return {"ok": False, "reason": "已在运行中"}
            self.error = ""
            self.state = "starting"
            self.options = dict(options)
            self.hub.clear_frame()
            self.guard = None
            self.started_at = None
            try:
                guard = self._open_guard(lambda: WebGuard(
                    cfg, self.hub, force_backend=options.get("backend") or None))
            except Exception as exc:
                self.state = "stopped"
                self.error = f"{type(exc).__name__}: {exc}"
                self.hub.log(f"[启动失败] {self.error}")
                return {"ok": False, "reason": self.error}

            self.guard = guard
            self.started_at = time.time()
            self.stopped_at = None

            max_seconds = options.get("max_seconds") or None

            def work() -> None:
                self.state = "running"
                try:
                    guard.run(max_seconds=max_seconds)
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.hub.log(f"[运行异常] {self.error}")
                finally:
                    self.state = "stopped"
                    self.stopped_at = time.time()
                    self.hub.clear_frame()

            self._thread = threading.Thread(target=work, name="fallguard-run",
                                            daemon=True)
            self._thread.start()
            return {"ok": True}

    def stop(self, timeout: float = 2.0) -> dict[str, Any]:
        """请求停止。**不阻塞等待收尾**。

        这里曾经是 thread.join(timeout=30)，而 stop() 跑在 HTTP 请求线程里，
        于是点一次停止就占住一个线程最长 30 秒；前端每秒轮询 /api/status，
        这些请求全排在后面，界面直接卡死——用户越点越卡。

        收尾本身可能真的要十几秒（等后置证据窗口收满、合成 mp4），
        那是正常的，但不该让界面陪着等。所以只给一个很短的宽限，
        没退出就直接返回，让前端靠轮询 state 自己发现何时真正停下。
        """
        guard, thread = self.guard, self._thread
        if guard is None or thread is None or not thread.is_alive():
            self.state = "stopped" if self.guard else "idle"
            return {"ok": True, "reason": "未在运行"}

        already = self.state == "stopping"
        self.state = "stopping"
        if not already:
            # 重复点停止时不再刷日志——之前每点一次就写一行"收到停止指令"，
            # 用户看到一串相同的日志会以为系统没反应，其实是在收尾
            self.hub.log("[停止] 收到停止指令，等待事件处置与留证收尾…")
        guard.request_stop()

        thread.join(timeout=timeout)
        if not thread.is_alive():
            self.state = "stopped"
            return {"ok": True, "reason": ""}
        return {"ok": True, "pending": True,
                "reason": "正在收尾（等待后置证据与事件归档），完成后会自动转为已停止"}

    def send_key(self, key: str) -> dict[str, Any]:
        if not self.alive:
            return {"ok": False, "reason": "未在运行"}
        if key not in {"c", "f", "t"}:
            return {"ok": False, "reason": f"不支持的按键 {key!r}"}
        self.hub.push_key(key)
        label = {"c": "取消外呼", "f": "标注误报", "t": "手动触发演练"}[key]
        self.hub.log(f"[网页] {label}")
        return {"ok": True, "action": label}

    # ----------------------------------------------------------- MJPEG
    def mjpeg(self, boundary: bytes = b"fgframe"):
        """生成 multipart/x-mixed-replace 流。

        只保留最新一条连接：切换标签页、刷新页面都会新建连接，而旧连接
        要等 TCP 超时才断。若放任不管，每条连接都在推同一份帧，
        实时画面会被拖垮（实测 6 条并存时 fps 从 30 掉到 11.8）。
        """
        epoch = self.hub.claim_stream()
        seq = -1
        idle_rounds = 0
        while True:
            # 有新连接进来就让位——实时画面只需要最新那一条
            if not self.hub.stream_current(epoch):
                return
            jpeg, seq = self.hub.wait_frame(seq, timeout=2.0)
            if jpeg is None:
                idle_rounds += 1
                # 没在跑就别一直挂着连接，浏览器会自己重连
                if idle_rounds > 3 or not self.alive:
                    return
                continue
            idle_rounds = 0
            yield (b"--" + boundary + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")
