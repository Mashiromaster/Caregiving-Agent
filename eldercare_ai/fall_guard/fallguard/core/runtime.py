# -*- coding: utf-8 -*-
"""通用处置编排：任何感知插件的事件都走这一条链路。

    判定 → 身份识别 → 记忆召回 → 分级 → 留证 → 本地告警
         → 取消窗口 → 抑制时段 → 智能体处置 → 归档

重构前这一整条写死在 pipeline.on_fall() 里，且每一步都掺着跌倒语义。
现在链路本身不知道"跌倒"是什么——事件类型、播报文案、分级规则
分别由插件的 kind/narrate() 和 Grader 的规则表提供。

与之并行的是风险预警通路（_assess_risk），它只记录不打扰。
两条通路**不共享判定依据也不互相阻塞**，理由见 core/risk.py 的模块文档。

关键时序约束（从 pipeline.py 原样继承，不要"优化"）：
判定成立后，后置证据（事件后 10 秒）必须持续采集，而取消窗口与智能体
处置同时在跑。所以取消窗口的 poll 回调里要继续抽帧，智能体处置放到
后台线程，主循环不阻塞——否则证据会断档，预览画面也会冻住
（现场看到卡死的监控画面会以为系统挂了）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import cv2

from .. import render
from ..agent import ToolContext, handle as agent_handle
from ..alerts import CallGateway, CancelWindow, EvidenceRecorder, LocalAlerter
from ..capture import RingBuffer, SyntheticSource, open_source
from ..events import EventLog
from . import identity as identity_mod
from . import sensors as sensors_mod
from .events import EventRecord, SensorEvent, new_event_id
from .grader import Grader
from .health import HealthStore
from .memory import Memory
from .risk import RiskScorer
from .signals import SignalBus


#: 由住户主动发起的事件类型。这类事件意图明确，不需要长取消窗口——
#: 让求助者对着摄像头干等 20 秒是说不过去的。
SELF_INITIATED_KINDS = frozenset({"help_gesture"})


def in_window(now: float, windows) -> str | None:
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
    warnings: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"frames": self.frames, "events": self.events,
                "calls": self.calls, "cancelled": self.cancelled,
                "warnings": self.warnings}


@dataclass
class ActiveEvent:
    record: EventRecord
    started: float
    banner: str = ""
    color: tuple = render.RED
    thread: threading.Thread | None = None
    ctx: ToolContext | None = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def calls(self) -> list[dict[str, Any]]:
        """外呼结果快照。给 Web 层用，免得它去摸 ctx 的内部状态。"""
        if self.ctx is None:
            return []
        return [c.to_dict() for c in self.ctx.called]


class Runtime:
    """智能体运行时：装载插件、驱动循环、编排处置。"""

    def __init__(self, cfg, *, show: bool = True,
                 log: Callable[[str], None] = print,
                 force_backend: str | None = None):
        self.cfg = cfg
        self.show = show
        self.log = log

        self.source = open_source(cfg)
        self.fps = int(cfg.camera.fps)
        synthetic = isinstance(self.source, SyntheticSource)

        # ---- 框架能力
        self.signals = SignalBus()
        self.events = EventLog(cfg.path(cfg.get("log.dir", "data")))
        self.memory = Memory(cfg.path(cfg.get("memory.dir", "data/memory")),
                             event_log=self.events)
        # 健康档案与信号总线是两层：总线只留几分钟（当前读数），
        # 档案留数周（个体基线）。Apple Watch 的数据按小时到达，
        # 只进总线的话会立刻被滑出窗口，基线永远建不起来。
        self.health = HealthStore(
            cfg.path(cfg.get("health.dir", "data/health")),
            min_interval=float(cfg.get("health.min_interval_seconds", 60.0)))
        self.grader = Grader()
        self.risk = RiskScorer(
            self.signals,
            warn_score=float(cfg.get("risk.warn_score", 45.0)),
            elevated_score=float(cfg.get("risk.elevated_score", 70.0)))
        self.risk_enabled = bool(cfg.get("risk.enabled", True))
        self.risk_cooldown = float(cfg.get("risk.cooldown_seconds", 600))
        self.identity = identity_mod.create_enabled(cfg)

        # ---- 插件
        self.sensors = sensors_mod.create_enabled(
            cfg, self.fps, force_backend=force_backend,
            synthetic_source=synthetic)
        self.sensor_ctx = sensors_mod.SensorContext(
            signals=self.signals,
            scene={"place": str(cfg.scene.place),
                   "resident_name": str(cfg.scene.resident_name)},
            log=log)
        self.devices = self._start_devices()

        # ---- 处置设施
        self.buffer = RingBuffer(float(cfg.evidence.pre_seconds) + 1.0, self.fps)
        self.evidence = EvidenceRecorder(cfg, self.fps)
        self.alerter = LocalAlerter(cfg)
        self.gateway = CallGateway(cfg)

        self.stats = Stats()
        self.active: ActiveEvent | None = None
        self.last_event_id: str | None = None
        self.last_risk = None
        self._cancel_flag = threading.Event()
        self._fps_ema = float(self.fps)
        self._last_frame_t: float | None = None
        self._manual_trigger = False
        self._stop = False
        self._last_risk_at = 0.0

    # ------------------------------------------------------------ 设备
    def _start_devices(self) -> list:
        from .. import devices as devices_mod

        out = []
        for dev in devices_mod.create_enabled(self.cfg):
            try:
                # 健康类设备需要长期档案才能建个体基线；手环类不需要，
                # 所以用可选钩子而不是塞进 start() 的签名
                if hasattr(dev, "bind_store"):
                    dev.bind_store(self.health)
                dev.start(self.signals)
                out.append(dev)
                self.log(f"[设备] {dev.name} 已接入（{dev.device_id}）")
            except Exception as exc:
                self.log(f"[设备] {dev.name} 启动失败：{exc}")
        return out

    def device_by_id(self, device_id: str):
        return next((d for d in self.devices
                     if getattr(d, "device_id", "") == device_id), None)

    # ------------------------------------------------------- 公开访问器
    # Web 层曾经直接读 _fps_ema / 写 _stop，这里给出正式接口
    @property
    def fps_estimate(self) -> float:
        return self._fps_ema

    @property
    def stopping(self) -> bool:
        return self._stop

    def request_stop(self) -> None:
        self._stop = True

    def primary_sensor(self):
        return self.sensors[0] if self.sensors else None

    def sensor_of(self, kind: str):
        return next((s for s in self.sensors if s.kind == kind), None)

    def describe_sensors(self) -> list[dict[str, Any]]:
        return [s.describe() for s in self.sensors]

    def describe_devices(self) -> list[dict[str, Any]]:
        return [d.describe() for d in self.devices]

    # ------------------------------------------------------------ 单帧
    def tick(self) -> tuple[float, Any, Any, SensorEvent | None] | None:
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

        # 多插件 fan-out。当前只有一个插件，但循环结构先到位——
        # 加第二个插件时这里不用改。
        # ts 是源时钟（文件/合成源从 0 开始），连续信号要用墙钟，
        # 所以这里把墙钟交给 ctx，由插件通过 ctx.now() 取。
        self.sensor_ctx.tick(time.time())
        obs = None
        event: SensorEvent | None = None
        for sensor in self.sensors:
            if sensor.needs_frame and frame is None:
                continue
            try:
                got_obs, got_event = sensor.observe(ts, frame, self.sensor_ctx)
            except Exception as exc:
                self.log(f"[插件异常] {sensor.name}: {type(exc).__name__}: {exc}")
                continue
            if obs is None:
                obs = got_obs                     # 渲染用第一个插件的观测
            if got_event is not None and event is None:
                event = got_event

        for event_id in self.evidence.pending_ids():
            self.evidence.feed(event_id, ts, frame)

        self._assess_risk(ts)
        return ts, frame, obs, event

    # ------------------------------------------------------ 风险预警通路
    def _assess_risk(self, ts: float, interval: float = 2.0) -> None:
        """独立于告警的预警通路：只记录、只提示，**绝不外呼**。"""
        if not self.risk_enabled:
            return
        now = time.time()
        if now - self._last_risk_at < interval:
            return
        self._last_risk_at = now

        profile = {}
        ident = self._resolve_identity()
        if ident.known:
            profile = self.memory.profile(ident.resident_id)
        assessment = self.risk.assess(profile=profile, now=now,
                                      health=self.health,
                                      resident_id=ident.resident_id)
        self.last_risk = assessment

        # 评分照常更新（面板要看实时值），但**处置中不发预警**。
        # 跌倒会让姿态稳定度骤降到 0，风险分必然越阈——此时再发一条
        # "需要关注"是对同一现象的重复记录：护工已经被电话叫去现场了，
        # 事件日志里却多出一条看起来像另一起事件的预警。
        # 预警的价值在事件发生**之前**，事件已成立就没有意义了。
        if self._event_in_flight():
            return

        if not self.risk.should_emit_warning(assessment, cooldown=self.risk_cooldown):
            return
        self.stats.warnings += 1
        self.log(f"[预警] 风险分 {assessment.score:.0f}（{assessment.level}）"
                 f"　{assessment.summary}")
        self._archive_warning(assessment, ident, profile)

    def _event_in_flight(self) -> bool:
        """是否有事件正在处置中（含取消窗口与后续归档）。"""
        if self.active is None:
            return False
        if not self.active.done.is_set():
            return True
        thread = self.active.thread
        return bool(thread is not None and thread.is_alive())

    def _archive_warning(self, assessment, ident, profile: dict) -> None:
        """预警也落事件档案，但 outcome=logged 且不触发任何处置动作。

        落档的理由：预警的价值要靠事后复盘验证——"系统提示了但没人去看，
        后来真摔了"这种情况必须留痕，否则无从改进阈值。
        """
        wall = time.time()
        rec = EventRecord(
            event_id=new_event_id(wall), detected_at=wall,
            event_type="risk_warning",
            level="L2" if assessment.level == "elevated" else "L1",
            trigger=f"risk:{assessment.level}",
            confidence=round(min(assessment.score / 100.0, 1.0), 3),
            room=str(self.cfg.scene.place), resident=ident.name or
            str(self.cfg.scene.resident_name),
            resident_id=ident.resident_id,
            features={"score": round(assessment.score, 1),
                      "factors": assessment.factors},
            outcome="logged",
        )
        rec.stage("risk_assessed", score=round(assessment.score, 1),
                  level=assessment.level, slow_driven=assessment.slow_driven)
        rec.notes.append(assessment.summary)
        rec.notes.append("预警为不打扰路径：仅记录与看板提示，不外呼。")
        if assessment.slow_driven:
            # 区分这两件事对看的人很重要：一条说"现在去看一眼"，
            # 另一条说"这个月状态在往下走，该安排复查了"。混为一谈
            # 会让护工用处理急事的方式对待慢性趋势，然后很快放弃看它。
            rec.notes.append(
                "本条由健康趋势推动（急性信号仅 "
                f"{assessment.acute_score:.0f} 分），反映的是近期状态变化趋势，"
                "不代表此刻有紧急情况。")
        if profile:
            rec.memory = {"summary": self.memory.summarize(
                ident.resident_id, profile,
                self.memory.history(ident.resident_id))}
        if ident.known:
            health = self.health.summary(ident.resident_id, now=wall)
            if health.get("metric_count"):
                rec.memory["health"] = health["text"]
        self.events.write(rec)
        self.alerter.notify("风险预警", assessment.summary[:120])

    # ------------------------------------------------------------ 渲染
    def show_frame(self, frame, obs) -> int:
        if not self.show:
            return -1
        banner = self.active.banner if self.active else ""
        color = self.active.color if self.active else render.RED
        img = render.draw(frame, obs, backend=self._backend_name(),
                          fps=self._fps_ema, cfg=self.cfg, banner=banner,
                          banner_color=color, stats=self.stats.as_dict())
        cv2.imshow("FallGuard", img)
        return cv2.waitKey(1) & 0xFF

    def _backend_name(self) -> str:
        sensor = self.primary_sensor()
        return getattr(getattr(sensor, "backend", None), "name", "n/a")

    def handle_key(self, key: int) -> None:
        if key in (ord("q"), 27):
            self._stop = True
        elif key == ord("c"):
            if self.active and not self.active.done.is_set():
                self._cancel_flag.set()
                self.log("[操作] 收到取消信号")
        elif key == ord("f"):
            if self.last_event_id:
                self.mark_false_alarm(self.last_event_id, "预览窗按 f 标注")
        elif key == ord("t"):
            self._manual_trigger = True

    def mark_false_alarm(self, event_id: str, reason: str = "",
                         by: str = "operator") -> dict[str, Any]:
        """标注误报，并让对应插件自我调整（取代原来的 detector.reset_cooldown）。"""
        payload = self.events.label(event_id, True, reason, by=by)
        row = next((r for r in self.events.events()
                    if r.get("event_id") == event_id), None)
        kind = str((row or {}).get("event_type") or "")
        for sensor in self.sensors:
            if not kind or sensor.kind == kind:
                sensor.on_false_alarm(event_id)
        self.log(f"[标注] {event_id} 已标记为误报")
        return payload

    # -------------------------------------------------------- 事件处置
    def _resolve_identity(self):
        band = self.signals.latest("band.worn", max_age=120.0)
        band_id = ""
        for dev in self.devices:
            band_id = getattr(dev, "band_id", "") or band_id
        return self.identity.resolve(identity_mod.IdentitySignals(
            room=str(self.cfg.scene.place),
            wristband_id=band_id,
            extra={"worn": bool(band.value) if band is not None else None}))

    def _build_record(self, event: SensorEvent) -> EventRecord:
        # event.ts 可能是视频源的虚拟时钟（文件/合成源从 0 开始计），
        # 事件号与外呼播报的时间必须用真实墙钟，否则电话里会报出错误时间。
        wall = time.time()
        ident = self._resolve_identity()
        recall = self.memory.recall(ident.resident_id) if ident.known else {}
        profile = recall.get("profile", {})

        grading = self.grader.grade(event, profile=profile, context={"now": wall})

        rec = EventRecord(
            event_id=new_event_id(wall), detected_at=wall,
            event_type=event.kind,
            trigger=event.trigger, confidence=event.confidence,
            room=str(self.cfg.scene.place),
            resident=ident.name or str(self.cfg.scene.resident_name),
            resident_id=ident.resident_id,
            features=dict(event.features))
        rec.features["source_ts"] = round(event.ts, 3)
        rec.level = grading.level
        rec.grading = grading.to_dict()
        rec.grading["identity"] = ident.to_dict()
        if recall:
            rec.memory = {"summary": recall.get("summary", ""),
                          "history": recall.get("history", {}),
                          "profile": profile}
        if ident.known:
            # 健康趋势进处置上下文：护工到场前知道"这人最近静息心率在上移"
            # 比只知道"他摔了"有用。只放聚合摘要，不放逐条读数。
            health = self.health.summary(ident.resident_id, now=wall)
            if health.get("metric_count"):
                rec.memory["health"] = health["text"]
                rec.memory["health_concerning"] = [
                    m["label"] for m in health.get("concerning", [])]
        if self.last_risk is not None:
            rec.grading["risk_at_detection"] = self.last_risk.to_dict()

        rec.stage("detected", event_type=event.kind, trigger=event.trigger,
                  confidence=round(event.confidence, 3))
        rec.stage("identified", resident_id=ident.resident_id or "(未识别)",
                  method=ident.method)
        # 分级理由进时间线：这是"为什么当时打了这个电话"的答案
        rec.stage("graded", level=grading.level, rule_id=grading.rule_id,
                  reason=grading.reason)
        if grading.escalated_by:
            rec.stage("escalated", reason=grading.escalated_by)
        return rec

    def _slots(self, rec: EventRecord, narration: dict[str, str]) -> dict[str, str]:
        lt = time.localtime(rec.detected_at)
        return {
            "name": rec.resident,
            "time": time.strftime("%H点%M分", lt),
            "place": rec.room,
            "event": narration.get("noun", rec.event_type),
        }

    def _cancel_seconds_for(self, kind: str) -> float:
        """按事件类型取取消窗口时长。

        谁发起的决定了该等多久：
          跌倒等系统判定  可能误判 → 给足时间让现场撤销（默认 10-20s）
          住户主动求助    意图明确 → 不该让求助者干等（默认 2s）

        求助类**不设为 0**：误识别一次就是一通真实电话，2 秒足够按下取消，
        又不至于耽误真正需要帮助的人。
        """
        override = (self.cfg.get("alert.cancel_seconds_by_type") or {}).get(kind)
        if override is not None:
            try:
                return max(float(override), 0.0)
            except (TypeError, ValueError):
                pass
        if kind in SELF_INITIATED_KINDS:
            return float(self.cfg.get("alert.help_cancel_seconds", 2.0))
        return float(self.cfg.alert.cancel_seconds)

    def on_event(self, event: SensorEvent, frame) -> None:
        sensor = self.sensor_of(event.kind) or self.primary_sensor()
        scene = dict(self.sensor_ctx.scene)
        narration = sensor.narrate(event, scene) if sensor else {}

        rec = self._build_record(event)
        self.stats.events += 1
        self.last_event_id = rec.event_id
        self.log(f"\n[判定] {rec.event_id} type={rec.event_type} level={rec.level} "
                 f"trigger={rec.trigger} conf={rec.confidence:.2f}")
        self.log(f"        规则 {rec.grading.get('rule_id')} — "
                 f"{rec.grading.get('reason')}")
        if rec.memory.get("summary"):
            self.log(f"        记忆 {rec.memory['summary']}")
        self.log(f"        特征 {rec.features}")

        # 留证按源时钟检索缓冲区，事件记录按墙钟——两个时钟不能混用
        bundle = self.evidence.capture_pre(rec.event_id, event.ts, self.buffer, frame)
        rec.evidence = bundle.to_dict()
        rec.stage("evidence_captured", keyframes=len(bundle.keyframes))

        self.active = ActiveEvent(record=rec, started=time.time())
        self._cancel_flag.clear()

        # 本地报警立刻执行，不等任何远程调用
        self.alerter.alarm(times=3)
        self.alerter.notify(narration.get("title", "预警"),
                            narration.get("desktop", f"{rec.room} {rec.resident}"))
        cancel_s = self._cancel_seconds_for(event.kind)
        announce = narration.get("announce", "")
        if announce:
            self.alerter.speak(announce.replace("{seconds}", str(int(cancel_s))))
        rec.stage("local_alert")

        if self._run_cancel_window(rec, cancel_s):
            return

        suppressed = in_window(time.time(), self.cfg.alert.suppress_call_windows)
        # 抑制时段是为了压制系统误判造成的打扰（查房时段的正常躺卧等），
        # 不该用来压住户本人的呼救——他举手求助时正需要有人来。
        if suppressed and event.kind in SELF_INITIATED_KINDS:
            rec.notes.append(f"处于抑制时段 {suppressed}，但主动求助不受抑制，照常外呼")
            rec.stage("suppression_bypassed", window=suppressed)
            self.log(f"[抑制] 命中时段 {suppressed}，但这是主动求助，照常外呼")
            suppressed = None
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

        self._dispatch(rec, narration)

    def _run_cancel_window(self, rec: EventRecord, cancel_s: float) -> bool:
        """返回 True 表示已被取消并完成收尾。"""
        if cancel_s <= 0:
            return False

        def tick(remain: float) -> None:
            if self.active:
                self.active.banner = f"CALLING IN {remain:4.1f}s - press C to cancel"
                self.active.color = render.AMBER
            self.log(f"  [倒计时] {remain:4.1f}s 后外呼（按 c 或回车取消）")

        window = CancelWindow(cancel_s, on_tick=tick)

        def poll() -> None:
            # 窗口内继续抽帧：后置证据不能断档，预览也不能冻住
            if self._cancel_flag.is_set():
                window.cancel()
                return
            got = self.tick()
            if got is None:
                return
            _ts, f2, obs2, _ = got
            key = self.show_frame(f2, obs2)
            if key not in (-1, 255):
                self.handle_key(key)
            if self._cancel_flag.is_set():
                window.cancel()

        if not window.run(poll=poll):
            return False

        rec.cancelled = True
        rec.outcome = "cancelled"
        rec.stage("cancelled_by_operator")
        self.stats.cancelled += 1
        self.active.banner = "CANCELLED BY OPERATOR"
        self.active.color = render.BLUE
        self.alerter.speak("已取消外呼。")
        self.log(f"[取消] {rec.event_id} 人工取消，不外呼")
        self.events.label(rec.event_id, True, "取消窗口内人工取消", by="operator")
        for sensor in self.sensors:
            if sensor.kind == rec.event_type:
                sensor.on_false_alarm(rec.event_id)
        self.active.done.set()
        self._finish(rec, None)
        return True

    def _dispatch(self, rec: EventRecord, narration: dict[str, str]) -> None:
        self.active.banner = "AGENT HANDLING - placing call"
        self.active.color = render.RED
        rec.stage("agent_start")

        ctx = ToolContext(
            record=rec, gateway=self.gateway, alerter=self.alerter,
            scene={"place": rec.room, "resident_name": rec.resident,
                   "institution": str(self.cfg.scene.institution)},
            slots=self._slots(rec, narration), log=self.log,
            narration=dict(narration),
            memory=rec.memory.get("summary", ""),
            health=rec.memory.get("health", ""))
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

    # ------------------------------------------------------------ 主循环
    def run(self, max_seconds: float | None = None) -> Stats:
        names = ", ".join(s.name for s in self.sensors)
        self.log(f"[启动] 感知插件={names}　后端={self._backend_name()}　"
                 f"外呼={self.gateway.describe()}")
        self.log(f"[启动] 场景={self.cfg.scene.place} / {self.cfg.scene.resident_name}"
                 f"　身份={'/'.join(p.name for p in self.identity.providers) or '无'}")
        if self.devices:
            self.log(f"[启动] 设备={', '.join(d.name for d in self.devices)}")
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
                got = self.tick()
                if got is None:
                    # 一帧都没读到就结束 ≠ 正常播完。摄像头场景下这意味着
                    # 设备打开了但不出画面（被占用、权限、刚断开的连续互通
                    # 相机残留句柄），只说"视频源结束"会让人以为是正常收尾。
                    if self.stats.frames == 0:
                        self.log(f"[失败] 视频源 {self.cfg.camera.source!r} "
                                 f"一帧都没读到。检查：设备是否被其他程序占用、"
                                 f"摄像头权限是否已授权、索引是否正确"
                                 f"（内置通常是 0）。无摄像头可用 source=demo 自检")
                    else:
                        self.log("[结束] 视频源结束")
                    break
                ts, frame, obs, event = got

                if self._manual_trigger:
                    self._manual_trigger = False
                    event = SensorEvent(kind="manual_test", ts=ts, confidence=0.99,
                                        trigger="manual_test",
                                        features={"note": "人工触发演练"})

                key = self.show_frame(frame, obs)
                if key not in (-1, 255):
                    self.handle_key(key)

                if event is not None:
                    # 单事件串行：上一事件处置未完时丢弃新事件。
                    # 多插件场景下这会成为瓶颈（两类事件可能同时成立），
                    # 届时需要改成按事件类型分别排队，当前单插件保持现状。
                    busy = (self.active and self.active.thread
                            and self.active.thread.is_alive())
                    if busy:
                        self.log("[忽略] 上一事件仍在处置中")
                    else:
                        self.on_event(event, frame)
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
            if self.tick() is None:
                time.sleep(0.1)
        time.sleep(0.8)
        for dev in self.devices:
            dev.stop()
        self.source.close()
        for sensor in self.sensors:
            sensor.close()
        self.alerter.close()
        if self.show:
            cv2.destroyAllWindows()
        self.log(f"[统计] {self.stats.as_dict()}")


__all__ = ["Runtime", "Stats", "ActiveEvent", "in_window"]
