# -*- coding: utf-8 -*-
"""主流水线：抽帧 → 姿态 → 判定 → 留证 → 取消窗口 → 智能体处置 → 落盘。

关键时序约束：判定成立后，后置证据（跌倒后 10 秒）必须持续采集，
而取消窗口与智能体处置同时在跑。所以取消窗口的 poll 回调里要继续抽帧，
智能体处置放到后台线程，主循环不阻塞——否则证据会断档，
预览画面也会冻住（现场看到卡死的监控画面会以为系统挂了）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import cv2

from . import render
from .agent import ToolContext, handle as agent_handle
from .alerts import CallGateway, CancelWindow, EvidenceRecorder, LocalAlerter
from .capture import RingBuffer, SyntheticSource, open_source
from .detector import FallDetector, FallVerdict, Observation
from .events import EventLog, EventRecord, new_event_id
from .pose import create as create_backend


def _in_window(now: float, windows) -> str | None:
    """判断当前时刻是否落在 "HH:MM-HH:MM" 抑制时段内（支持跨午夜）。"""
    lt = time.localtime(now)
    minutes = lt.tm_hour * 60 + lt.tm_min
    for win in windows or []:
        try:
            start_s, end_s = str(win).split("-")
            sh, sm = (int(v) for v in start_s.strip().split(":"))
            eh, em = (int(v) for v in end_s.strip().split(":"))
        except Exception:
            continue
        start, end = sh * 60 + sm, eh * 60 + em
        hit = (start <= minutes <= end) if start <= end \
            else (minutes >= start or minutes <= end)
        if hit:
            return str(win)
    return None


@dataclass
class Stats:
    frames: int = 0
    events: int = 0
    calls: int = 0
    cancelled: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"frames": self.frames, "events": self.events,
                "calls": self.calls, "cancelled": self.cancelled}


@dataclass
class ActiveEvent:
    record: EventRecord
    started: float
    banner: str = ""
    color: tuple = render.RED
    thread: threading.Thread | None = None
    ctx: ToolContext | None = None
    done: threading.Event = field(default_factory=threading.Event)


class FallGuard:
    def __init__(self, cfg, *, show: bool = True, log: Callable[[str], None] = print,
                 force_backend: str | None = None):
        self.cfg = cfg
        self.show = show
        self.log = log

        self.source = open_source(cfg)
        self.fps = int(cfg.camera.fps)
        backend_name = force_backend or str(cfg.detector.backend)
        if backend_name == "auto" and isinstance(self.source, SyntheticSource):
            # 合成场景画的是灰色矩形，mediapipe/yolo 这类真人模型一个都检不出，
            # auto 选中它们会让自检永远不触发。合成源固定走 motion。
            backend_name = "motion"
        self.backend = create_backend(backend_name)
        self.detector = FallDetector(cfg, self.fps)

        self.buffer = RingBuffer(float(cfg.evidence.pre_seconds) + 1.0, self.fps)
        self.evidence = EvidenceRecorder(cfg, self.fps)
        self.alerter = LocalAlerter(cfg)
        self.gateway = CallGateway(cfg)
        self.events = EventLog(cfg.path(cfg.get("log.dir", "data")))

        self.stats = Stats()
        self.active: ActiveEvent | None = None
        self.last_event_id: str | None = None
        self._cancel_flag = threading.Event()
        self._fps_ema = float(self.fps)
        self._last_frame_t: float | None = None
        self._manual_trigger = False
        self._stop = False

    # ------------------------------------------------------------------ 单帧
    def _tick(self) -> tuple[float, Any, Observation, FallVerdict | None] | None:
        item = self.source.read()
        if item is None:
            return None
        ts, frame = item
        self.stats.frames += 1
        self.buffer.push(ts, frame)

        if self._last_frame_t is not None:
            dt = ts - self._last_frame_t
            if dt > 1e-4:
                self._fps_ema = self._fps_ema * 0.9 + (1.0 / dt) * 0.1
        self._last_frame_t = ts

        people = self.backend.infer(frame)
        obs, verdict = self.detector.update(ts, frame, people)

        for event_id in self.evidence.pending_ids():
            self.evidence.feed(event_id, ts, frame)
        return ts, frame, obs, verdict

    def _show(self, frame, obs: Observation) -> int:
        if not self.show:
            return -1
        banner = self.active.banner if self.active else ""
        color = self.active.color if self.active else render.RED
        img = render.draw(frame, obs, backend=self.backend.name, fps=self._fps_ema,
                          cfg=self.cfg, banner=banner, banner_color=color,
                          stats=self.stats.as_dict())
        cv2.imshow("FallGuard", img)
        return cv2.waitKey(1) & 0xFF

    def _handle_key(self, key: int) -> None:
        if key in (ord("q"), 27):
            self._stop = True
        elif key == ord("c"):
            if self.active and not self.active.done.is_set():
                self._cancel_flag.set()
                self.log("[操作] 收到取消信号")
        elif key == ord("f"):
            if self.last_event_id:
                self.events.label(self.last_event_id, True, "预览窗按 f 标注")
                self.log(f"[标注] {self.last_event_id} 已标记为误报")
                self.detector.reset_cooldown()
        elif key == ord("t"):
            self._manual_trigger = True

    # -------------------------------------------------------------- 事件处置
    def _build_record(self, verdict: FallVerdict) -> EventRecord:
        scene = self.cfg.scene
        # verdict.ts 可能是视频源的虚拟时钟（文件/合成源从 0 开始计），
        # 事件号与外呼播报的时间必须用真实墙钟，否则电话里会报出错误时间。
        wall = time.time()
        rec = EventRecord(
            event_id=new_event_id(wall),
            detected_at=wall,
            trigger=verdict.trigger,
            confidence=verdict.confidence,
            room=str(scene.place),
            resident=str(scene.resident_name),
            features=dict(verdict.features),
        )
        rec.features["source_ts"] = round(verdict.ts, 3)
        rec.level = "L2" if verdict.trigger == "lying_only" else "L3"
        rec.stage("detected", trigger=verdict.trigger,
                  confidence=round(verdict.confidence, 3))
        return rec

    def _slots(self, rec: EventRecord) -> dict[str, str]:
        lt = time.localtime(rec.detected_at)
        return {
            "name": rec.resident,
            "time": time.strftime("%H点%M分", lt),
            "place": rec.room,
            "event": "跌倒",
        }

    def on_fall(self, verdict: FallVerdict, frame) -> None:
        rec = self._build_record(verdict)
        self.stats.events += 1
        self.last_event_id = rec.event_id
        self.log(f"\n[判定] {rec.event_id} level={rec.level} "
                 f"trigger={rec.trigger} conf={rec.confidence:.2f}")
        self.log(f"        特征 {rec.features}")

        # 留证按源时钟检索缓冲区，事件记录按墙钟——两个时钟不能混用
        bundle = self.evidence.capture_pre(rec.event_id, verdict.ts, self.buffer, frame)
        rec.evidence = bundle.to_dict()
        rec.stage("evidence_captured", keyframes=len(bundle.keyframes))

        self.active = ActiveEvent(record=rec, started=time.time())
        self._cancel_flag.clear()

        # 本地报警立刻执行，不等任何远程调用
        self.alerter.alarm(times=3)
        self.alerter.notify("跌倒预警", f"{rec.room} {rec.resident} 疑似跌倒")
        cancel_s = float(self.cfg.alert.cancel_seconds)
        self.alerter.speak(
            f"检测到跌倒。{int(cancel_s)}秒后将自动拨打电话，如误报请按取消。")
        rec.stage("local_alert")

        cancelled = False
        if cancel_s > 0:
            def tick(remain: float) -> None:
                if self.active:
                    self.active.banner = f"CALLING IN {remain:4.1f}s - press C to cancel"
                    self.active.color = render.AMBER
                self.log(f"  [倒计时] {remain:4.1f}s 后外呼（按 c 或回车取消）")

            window = CancelWindow(cancel_s, on_tick=tick)

            def poll() -> None:
                if self._cancel_flag.is_set():
                    window.cancel()
                    return
                got = self._tick()
                if got is None:
                    return
                _ts, f2, obs2, _ = got
                key = self._show(f2, obs2)
                if key not in (-1, 255):
                    self._handle_key(key)
                if self._cancel_flag.is_set():
                    window.cancel()

            cancelled = window.run(poll=poll)

        if cancelled:
            rec.cancelled = True
            rec.outcome = "cancelled"
            rec.stage("cancelled_by_operator")
            self.stats.cancelled += 1
            self.active.banner = "CANCELLED BY OPERATOR"
            self.active.color = render.BLUE
            self.alerter.speak("已取消外呼。")
            self.log(f"[取消] {rec.event_id} 人工取消，不外呼")
            self.events.label(rec.event_id, True, "取消窗口内人工取消", by="operator")
            self.active.done.set()
            self._finish(rec, None)
            return

        suppressed = _in_window(time.time(), self.cfg.alert.suppress_call_windows)
        if suppressed:
            rec.outcome = "suppressed"
            rec.notes.append(f"命中抑制时段 {suppressed}，仅记录不外呼")
            rec.stage("call_suppressed", window=suppressed)
            self.active.banner = f"SUPPRESSED ({suppressed})"
            self.active.color = render.BLUE
            self.log(f"[抑制] 命中时段 {suppressed}，仅记录不外呼")
            self.active.done.set()
            self._finish(rec, None)
            return

        self.active.banner = "AGENT HANDLING - placing call"
        self.active.color = render.RED
        rec.stage("agent_start")

        ctx = ToolContext(
            record=rec, gateway=self.gateway, alerter=self.alerter,
            scene={"place": rec.room, "resident_name": rec.resident,
                   "institution": str(self.cfg.scene.institution)},
            slots=self._slots(rec), log=self.log,
        )
        self.active.ctx = ctx
        active = self.active

        def work() -> None:
            outcome = None
            try:
                outcome = agent_handle(self.cfg, ctx)
            except Exception as exc:
                self.log(f"[智能体异常] {type(exc).__name__}: {exc}")
            ok = sum(1 for r in ctx.called if r.ok)
            self.stats.calls += ok
            rec.outcome = "called" if ok else "no_call"
            rec.stage("agent_done", calls_ok=ok,
                      mode=outcome.mode if outcome else "error")
            active.banner = (f"CALLED ({ok} ok) - press f if false alarm"
                             if ok else "NO CALL PLACED - check config")
            active.color = render.GREEN if ok else render.RED
            self._finish(rec, outcome)
            active.done.set()

        thread = threading.Thread(target=work, daemon=True)
        self.active.thread = thread
        thread.start()

    def _finish(self, rec: EventRecord, agent_outcome) -> None:
        ctx = self.active.ctx if self.active else None
        if ctx is not None:
            rec.agent.setdefault("called", [r.to_dict() for r in ctx.called])
        if agent_outcome is not None:
            rec.agent.update(agent_outcome.to_dict())

        def seal() -> None:
            # 后置证据窗口可能还没收满，等它结束再合成 mp4
            deadline = time.time() + float(self.cfg.evidence.post_seconds) + 4.0
            while time.time() < deadline:
                if rec.event_id not in self.evidence.pending_ids():
                    break
                time.sleep(0.2)
            bundle = self.evidence.finalize(rec.event_id, rec.to_dict())
            if bundle:
                rec.evidence = bundle.to_dict()
            self.events.write(rec)
            self.log(f"[归档] {rec.event_id} outcome={rec.outcome} "
                     f"证据 {rec.evidence.get('dir')}")

        threading.Thread(target=seal, daemon=True).start()

    # ------------------------------------------------------------------ 主循环
    def run(self, max_seconds: float | None = None) -> Stats:
        self.log(f"[启动] 姿态后端={self.backend.name}　外呼={self.gateway.describe()}")
        self.log(f"[启动] 场景={self.cfg.scene.place} / {self.cfg.scene.resident_name}")
        if self.show:
            cv2.namedWindow("FallGuard", cv2.WINDOW_NORMAL)
            self.log("[提示] 预览窗按键：q 退出 / c 取消外呼 / f 标注误报 / t 手动触发")
        else:
            self.log("[提示] 无窗口模式；倒计时内在终端按回车可取消外呼")

        wall0 = time.time()
        try:
            while not self._stop:
                if max_seconds and time.time() - wall0 > max_seconds:
                    self.log("[结束] 达到最大运行时长")
                    break
                got = self._tick()
                if got is None:
                    self.log("[结束] 视频源结束")
                    break
                ts, frame, obs, verdict = got

                if self._manual_trigger:
                    self._manual_trigger = False
                    verdict = FallVerdict(ts=ts, trigger="manual_test", confidence=0.99,
                                          features={"note": "预览窗手动触发"})

                key = self._show(frame, obs)
                if key not in (-1, 255):
                    self._handle_key(key)

                if verdict is not None:
                    busy = (self.active and self.active.thread
                            and self.active.thread.is_alive())
                    if busy:
                        self.log("[忽略] 上一事件仍在处置中")
                    else:
                        self.on_fall(verdict, frame)
        except KeyboardInterrupt:
            self.log("\n[中断] 收到 Ctrl-C")
        finally:
            self.close()
        return self.stats

    def close(self) -> None:
        if self.active and self.active.thread and self.active.thread.is_alive():
            self.log("[等待] 事件处置收尾…")
            self.active.thread.join(timeout=25)
        # 继续抽帧让后置证据窗口走完
        deadline = time.time() + float(self.cfg.evidence.post_seconds) + 4.0
        while self.evidence.pending_ids() and time.time() < deadline:
            if self._tick() is None:
                time.sleep(0.1)
        time.sleep(0.8)
        self.source.close()
        self.backend.close()
        self.alerter.close()
        if self.show:
            cv2.destroyAllWindows()
        self.log(f"[统计] {self.stats.as_dict()}")
