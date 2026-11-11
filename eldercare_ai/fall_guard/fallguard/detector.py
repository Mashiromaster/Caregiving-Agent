# -*- coding: utf-8 -*-
"""跌倒判定状态机。

判定思路与技术文档中的 R-FALL-01/02/03 一致，核心是两条判据的组合：

  ① 下降过程：质心在 descent_window_s 内的下降速率 ≥ descent_min
  ② 躺倒状态：躯干与竖直方向夹角 ≥ lying_angle（无骨骼点时退化为框宽高比）
     且质心位于地面高度、且体动能量低于 static_motion_max

  双条件成立（先降后躺）     → 置信度高，trigger=descent+lying
  仅②成立且持续足够久       → 置信度低，trigger=lying_only（兜底，防漏报）

"主动躺下"与"跌倒"的区分全靠下降速率和落地后体动：缓慢坐下的下降速率低，
主动躺卧后仍会翻身调整（体动能量高），两者都不会命中双条件路径。
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from .pose import Person


@dataclass
class Observation:
    """单帧的观测结果，供渲染与调试使用。"""

    ts: float
    person: Person | None
    lying: bool = False
    centroid: tuple[float, float] | None = None
    torso_angle: float | None = None
    aspect: float | None = None
    descent_rate: float = 0.0
    motion: float = 0.0
    lying_for: float = 0.0
    state: str = "idle"
    suppressed_by_zone: bool = False


@dataclass
class FallVerdict:
    """判定成立时返回的结论。"""

    ts: float
    trigger: str
    confidence: float
    features: dict[str, Any] = field(default_factory=dict)


def _in_zones(point: tuple[float, float], zones) -> bool:
    x, y = point
    for z in zones or []:
        if len(z) != 4:
            continue
        x1, y1, x2, y2 = (float(v) for v in z)
        if min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2):
            return True
    return False


class FallDetector:
    def __init__(self, cfg, fps: int):
        d = cfg.detector
        self.lying_angle = float(d.lying_angle_deg)
        self.lying_angle_hyst = float(d.lying_angle_hysteresis)
        self.lying_aspect = float(d.lying_aspect)
        self.floor_y = float(d.floor_y)
        self.descent_min = float(d.descent_rate_min)
        self.descent_window = float(d.descent_window_s)
        self.lying_seconds = float(d.lying_seconds)
        self.lying_only_seconds = float(d.lying_only_seconds)
        self.static_motion_max = float(d.static_motion_max)
        self.cooldown = float(d.cooldown_seconds)
        self.min_score = float(d.min_person_score)
        self.zones = list(d.exclude_zones or [])
        # 姿态估计在地面姿态上会抖动：实测真人跌倒序列里躯干角在
        # 124°→33°→丢失→171° 之间跳变。躺倒计时若被单帧打断就清零，
        # 一次真跌倒会被反复重置而永远攒不够时长。
        self.lying_grace = float(d.lying_grace_s)
        self.lying_density = float(d.lying_min_density)

        self.fps = max(fps, 1)
        self._centroids: collections.deque = collections.deque(maxlen=self.fps * 4 + 8)
        self._motion: collections.deque = collections.deque(maxlen=self.fps * 3 + 8)
        self._prev_gray: np.ndarray | None = None
        self._lying_since: float | None = None
        self._lying_last: float | None = None      # 最近一次观测到躺倒的时刻
        self._lying_hits: collections.deque = collections.deque(maxlen=self.fps * 20)
        self._descent_peak: float = 0.0
        self._descent_at: float | None = None
        # None 而非 0.0：视频文件与合成源的时钟从 0 开始，
        # 用 0 会让 ts - _last_fire < cooldown 从第一帧起恒成立，系统永久静默。
        self._last_fire: float | None = None
        self.state = "idle"

    # ------------------------------------------------------------ 特征
    def _frame_motion(self, frame: np.ndarray) -> float:
        """全画面平均帧间差分，归一化到 0~1。用于判断躺倒者是否还在动。"""
        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is None:
            self._prev_gray = gray
            return 0.0
        diff = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray
        return float(np.mean(diff)) / 255.0

    def _descent_rate(self, now: float) -> float:
        """窗口内质心的最大下降速率（归一化高度/秒）。"""
        pts = [(ts, y) for ts, y in self._centroids if now - ts <= self.descent_window]
        if len(pts) < 3:
            return 0.0
        t0, y0 = pts[0]
        best = 0.0
        for ts, y in pts[1:]:
            dt = ts - t0
            if dt >= 0.2:
                best = max(best, (y - y0) / dt)
        return max(best, 0.0)

    def _is_lying(self, person: Person) -> tuple[bool, float | None, float]:
        angle = person.torso_angle_deg()
        aspect = person.aspect
        if angle is not None:
            # 迟滞：已判定躺倒后，退出门限比进入门限低 lying_angle_hyst 度。
            # 实测 UR Fall 序列里有人倒地后躯干角长期在 56~60° 之间摆动，
            # 单一硬阈值会让状态反复横跳、时长永远攒不够而漏报。
            enter = self.lying_angle
            exit_ = self.lying_angle - self.lying_angle_hyst
            thr = exit_ if self._lying_since is not None else enter
            return angle >= thr, angle, aspect
        # 无可用骨骼点：退化为人体框宽高比
        return aspect >= self.lying_aspect, None, aspect

    def _clear_lying(self) -> None:
        self._lying_since = None
        self._lying_last = None
        self._lying_hits.clear()

    def _note_lying(self, ts: float, is_lying: bool) -> float:
        """累积躺倒时长，容忍短暂中断。返回当前已持续时长。

        姿态估计在地面姿态上并不稳定——实测真人跌倒序列中，躯干角会在
        124° → 33° → 检不出 → 171° 之间跳变。若一帧不满足就把计时清零，
        真跌倒永远攒不够时长（实测峰值仅 0.63s / 需 2.5s），直接漏报。

        所以：中断在 lying_grace 秒内不清零，但要求窗口内躺倒帧占比不低于
        lying_density——这样"人走出画面"（占比骤降）与"躺着但姿态抖动"
        （占比仍高）能被区分开，不会让离场被误计成持续躺倒。
        """
        self._lying_hits.append((ts, bool(is_lying)))
        if is_lying:
            if self._lying_since is None:
                self._lying_since = ts
            self._lying_last = ts
        elif self._lying_since is not None:
            gap = ts - (self._lying_last or ts)
            if gap > self.lying_grace:
                self._clear_lying()
                return 0.0

        if self._lying_since is None:
            return 0.0

        window = [(t, hit) for t, hit in self._lying_hits if t >= self._lying_since]
        if len(window) >= 4:
            density = sum(1 for _t, hit in window if hit) / len(window)
            if density < self.lying_density:
                self._clear_lying()
                return 0.0
        return ts - self._lying_since

    # ------------------------------------------------------------ 主入口
    def update(self, ts: float, frame: np.ndarray,
               people: list[Person]) -> tuple[Observation, FallVerdict | None]:
        motion = self._frame_motion(frame)
        self._motion.append((ts, motion))

        candidates = [p for p in people if p.score >= self.min_score]
        person = max(candidates, key=lambda p: p.area, default=None)

        obs = Observation(ts=ts, person=person, motion=motion)

        if person is None:
            # 检不出人可能是真离场，也可能是地面姿态下的瞬时丢失。
            # 交给 _note_lying 的宽限窗口裁决，不在这里直接清零。
            lying_for = self._note_lying(ts, False)
            obs.lying_for = lying_for
            if lying_for <= 0.0:
                self._descent_peak = 0.0
                self._descent_at = None
                if self.state != "cooldown":
                    self.state = "idle"
                obs.state = self.state
                return obs, None
            obs.state = self.state = "lying"
            return obs, None

        centroid = person.centroid()
        lying, angle, aspect = self._is_lying(person)
        obs.centroid = centroid
        obs.torso_angle = angle
        obs.aspect = aspect

        self._centroids.append((ts, centroid[1]))
        rate = self._descent_rate(ts)
        obs.descent_rate = rate
        if rate >= self.descent_min:
            self._descent_peak = max(self._descent_peak, rate)
            self._descent_at = ts

        if _in_zones(centroid, self.zones):
            obs.suppressed_by_zone = True
            self._clear_lying()
            obs.state = self.state = "zone_suppressed"
            return obs, None

        on_floor = centroid[1] >= self.floor_y if self.floor_y > 0 else True
        lying_now = lying and on_floor
        obs.lying = lying_now

        lying_for = self._note_lying(ts, lying_now)
        obs.lying_for = lying_for

        if lying_for <= 0.0:
            if self.state not in {"cooldown"}:
                self.state = "upright"
            obs.state = self.state
            return obs, None

        # 躺倒期间的平均体动：主动躺卧会持续翻身调整，跌倒昏迷则接近静止
        recent = [m for t, m in self._motion if ts - t <= min(lying_for, 2.0)]
        avg_motion = float(np.mean(recent)) if recent else motion
        still = avg_motion <= self.static_motion_max

        if self._last_fire is not None and ts - self._last_fire < self.cooldown:
            obs.state = self.state = "cooldown"
            return obs, None

        had_descent = (self._descent_at is not None
                       and ts - self._descent_at <= self.descent_window + self.lying_seconds + 1.0)

        verdict: FallVerdict | None = None
        if had_descent and lying_for >= self.lying_seconds and still:
            verdict = FallVerdict(
                ts=ts,
                trigger="descent+lying",
                confidence=min(0.72 + min(self._descent_peak, 1.0) * 0.25, 0.97),
            )
        elif lying_for >= self.lying_only_seconds and still:
            verdict = FallVerdict(ts=ts, trigger="lying_only", confidence=0.55)

        if verdict is None:
            obs.state = self.state = "lying"
            return obs, None

        verdict.features = {
            "torso_angle_deg": round(angle, 1) if angle is not None else None,
            "bbox_aspect": round(aspect, 3),
            "centroid_y": round(centroid[1], 3),
            "descent_rate": round(self._descent_peak, 3),
            "lying_seconds": round(lying_for, 2),
            "avg_motion": round(avg_motion, 5),
            "person_score": round(person.score, 3),
            "on_floor": bool(on_floor),
        }
        self._last_fire = ts
        self._clear_lying()
        self._descent_peak = 0.0
        self._descent_at = None
        obs.state = self.state = "fall"
        return obs, verdict

    def reset_cooldown(self) -> None:
        """误报取消后立即解除静默，便于现场复测。"""
        self._last_fire = None
        self._clear_lying()
        self.state = "idle"
