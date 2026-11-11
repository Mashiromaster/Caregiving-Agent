# -*- coding: utf-8 -*-
"""跌倒检测插件：视觉单源。

这是把原 pipeline._tick() 里的 `backend.infer` + `detector.update` 两行
包装成插件，**判定算法本身一行未改**——detector.py 里那些带实测数据的
注释（迟滞阈值的由来、lying_grace 为什么必须存在）是在真实跌倒序列上
踩出来的，照搬即可，不要顺手"优化"。

插件在原有职责之外多做一件事：每帧往信号总线喂连续读数
（姿态稳定度 / 体动能量 / 有人与否）。这些读数不触发任何处置，
只供 RiskScorer 做趋势判断——"步态越来越不稳"要在跌倒之前就能看出来，
而那需要连续信号，不是离散事件。
"""
from __future__ import annotations

from typing import Any

from ..core import signals as sig
from ..core.events import SensorEvent
from ..core.sensors import Sensor, SensorContext
from ..detector import FallDetector
from ..pose import create as create_backend


class FallVisionSensor(Sensor):
    kind = "fall"
    name = "fall_vision"
    emits_signals = (sig.POSTURE_STABILITY, sig.MOTION_ENERGY, sig.PRESENCE)
    needs_frame = True

    def __init__(self, cfg, fps: int, force_backend: str | None = None,
                 synthetic_source: bool = False):
        self.cfg = cfg
        backend_name = force_backend or str(cfg.detector.backend)
        if backend_name == "auto" and synthetic_source:
            # 合成场景画的是灰色矩形，mediapipe/yolo 这类真人模型一个都检不出，
            # auto 选中它们会让自检永远不触发。合成源固定走 motion。
            backend_name = "motion"
        self.backend = create_backend(backend_name)
        self.detector = FallDetector(cfg, fps)
        self._lying_angle = float(cfg.detector.lying_angle_deg)

    # ------------------------------------------------------------ 观测
    def observe(self, ts: float, frame: Any, ctx: SensorContext
                ) -> tuple[Any, SensorEvent | None]:
        people = self.backend.infer(frame)
        obs, verdict = self.detector.update(ts, frame, people)
        self._emit_signals(ts, obs, ctx)
        if verdict is None:
            return obs, None
        return obs, SensorEvent(
            kind=self.kind, ts=verdict.ts, confidence=verdict.confidence,
            trigger=verdict.trigger, features=dict(verdict.features), frame=frame)

    def _emit_signals(self, ts: float, obs, ctx: SensorContext) -> None:
        """把逐帧观测折算成连续信号。

        姿态稳定度定义为"离躺倒还有多远"：躯干角 0°（直立）记 1.0，
        到达躺倒阈值记 0.0。这样"快要倒下"在信号上表现为缓慢下滑，
        而不是等判定成立才从 1 跳到 0。

        检不出骨骼点时不发姿态信号（而不是发 0）——没有读数与读数为 0
        是两回事，混为一谈会让风险评分把"人不在画面里"当成"姿态极不稳"。
        """
        ctx.signals.emit(sig.PRESENCE, 1.0 if obs.person is not None else 0.0,
                         ts=ts, source=self.name)
        ctx.signals.emit(sig.MOTION_ENERGY, obs.motion, ts=ts, source=self.name)

        if obs.torso_angle is not None:
            stability = 1.0 - min(obs.torso_angle / max(self._lying_angle, 1e-6), 1.0)
            quality = 1.0
        elif obs.aspect is not None:
            # 退化判据：宽高比 1.0（站立）→ 1.0，到躺倒阈值 → 0.0。
            # 置信度标低，让风险评分知道这是兜底来源。
            span = max(float(self.cfg.detector.lying_aspect) - 1.0, 1e-6)
            stability = 1.0 - min(max(obs.aspect - 1.0, 0.0) / span, 1.0)
            quality = 0.5
        else:
            return
        ctx.signals.emit(sig.POSTURE_STABILITY, stability, ts=ts,
                         source=self.name, quality=quality)

    # ------------------------------------------------------------ 表达
    def narrate(self, event: SensorEvent, scene: dict[str, str]) -> dict[str, str]:
        place = scene.get("place", "")
        who = scene.get("resident_name", "")
        return {
            "title": "跌倒预警",
            "noun": "跌倒",
            "announce": "检测到跌倒。{seconds}秒后将自动拨打电话，如误报请按取消。",
            "reassure": "检测到跌倒，已通知工作人员，请不要自行起身，我们马上到。",
            "desktop": f"{place} {who} 疑似跌倒",
            # 跌倒后自行起身是二次伤害的主因，这句要进智能体 prompt
            "brief": "跌倒事件已判定成立，现场无人值守，倒地者未自行起身。"
                     "取消窗口已走完，未被人工取消。",
        }

    def describe(self) -> dict[str, Any]:
        d = self.cfg.detector
        return {
            "kind": self.kind,
            "name": self.name,
            "backend": self.backend.name,
            "detail": f"躺倒角 ≥{d.lying_angle_deg}°　下降速率 ≥{d.descent_rate_min}/s　"
                      f"双条件 {d.lying_seconds}s　单条件 {d.lying_only_seconds}s",
            "state": self.detector.state,
            "zones": len(list(d.exclude_zones or [])),
        }

    def on_false_alarm(self, event_id: str) -> None:
        # 误报标注后立即解除静默，便于现场复测
        self.detector.reset_cooldown()

    def close(self) -> None:
        self.backend.close()
