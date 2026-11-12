# -*- coding: utf-8 -*-
"""求助手势插件：把手举到耳边保持数秒 = 主动呼救。

与跌倒检测的根本区别在于**谁发起**：

  跌倒  系统判定住户出事了 → 可能误判 → 必须给取消窗口
  手势  住户自己举手求助   → 意图明确 → 不该让他等

所以这里的取消窗口很短（默认 2 秒，由 runtime 按事件类型取配置）。
完全去掉取消窗口是危险的——误识别一次就是一通真实电话，而 2 秒足够
在预览窗/网页上按下取消，又不会让真正需要帮助的人干等。

**为什么不识别"比六"的手型**：MediaPipe Pose 只有 33 个身体关键点，
手部最远到手腕，看不到手指。要认手型得再挂一个 MediaPipe Hands 模型，
每帧多一次推理（实测帧率从 30 掉到 20 左右），而且老人手指关节不灵活时
手型往往摆不标准。改用姿势判据：手腕靠近耳朵 + 抬到肩以上 + 保持住。
这个动作与"打电话"的姿势一致，老人容易理解也容易做到。

误触发的主要来源是真打电话、挠耳朵、撩头发。靠三条过滤：
  · 必须持续 hold_seconds 秒（挠耳朵是瞬时的）
  · 手腕要明显高于肩膀（自然垂手时抬不到）
  · 触发后进静默期，避免同一动作反复触发
"""
from __future__ import annotations

import math
from typing import Any

from ..core import signals as sig
from ..core.events import SensorEvent
from ..core.sensors import Sensor, SensorContext

#: 这个信号让风险评分也能看到"有人在求助"，但求助本身走事件路径
HELP_GESTURE = "camera.help_gesture"


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class HelpGestureSensor(Sensor):
    kind = "help_gesture"
    name = "help_gesture"
    emits_signals = (HELP_GESTURE,)
    needs_frame = True

    def __init__(self, cfg, fps: int, **_kwargs):
        self.cfg = cfg
        g = cfg.get("gesture") or {}
        # 手腕到耳朵的距离阈值，按肩宽归一化——不同距离下人在画面里大小
        # 差很多，用绝对像素会让远处的人永远触发不了
        self.near_ratio = float(g.get("near_ratio", 0.75))
        self.hold_seconds = float(g.get("hold_seconds", 3.0))
        self.cooldown_seconds = float(g.get("cooldown_seconds", 60.0))
        self.min_conf = float(g.get("min_keypoint_conf", 0.5))
        self.grace_seconds = float(g.get("grace_seconds", 0.6))

        self._since: float | None = None
        self._last_hit: float | None = None
        self._last_fire: float | None = None
        self._side = ""

    # ------------------------------------------------------------ 判据
    def _gesture_now(self, person) -> tuple[bool, str, float]:
        """返回 (是否成立, 哪只手, 归一化距离)。"""
        l_sho = person.point("l_sho", self.min_conf)
        r_sho = person.point("r_sho", self.min_conf)
        if not l_sho or not r_sho:
            return False, "", 0.0
        shoulder_w = _dist(l_sho, r_sho)
        if shoulder_w < 1e-4:
            return False, "", 0.0

        best_ratio = 9.9
        best_side = ""
        for side, wri_key, ear_key, sho in (
                ("left", "l_wri", "l_ear", l_sho),
                ("right", "r_wri", "r_ear", r_sho)):
            wri = person.point(wri_key, self.min_conf)
            if not wri:
                continue
            ear = person.point(ear_key, self.min_conf) or person.point("nose",
                                                                      self.min_conf)
            if not ear:
                continue
            # y 轴向下为正：手腕 y 小于肩 y 才是"抬起来了"。
            # 自然垂手、插兜、扶桌子都达不到，这一条过滤掉大部分日常动作。
            if wri[1] >= sho[1]:
                continue
            ratio = _dist(wri, ear) / shoulder_w
            if ratio < best_ratio:
                best_ratio, best_side = ratio, side

        if not best_side:
            return False, "", 0.0
        return best_ratio <= self.near_ratio, best_side, best_ratio

    def observe(self, ts: float, frame: Any, ctx: SensorContext
                ) -> tuple[Any, SensorEvent | None]:
        person = getattr(ctx, "person", None)
        obs = {"state": "idle", "held": 0.0, "ratio": None, "side": ""}

        if person is None:
            # **检不出人要走宽限逻辑，不能直接清零。** 举手时手臂常会挡住
            # 面部，姿态模型丢几帧是常态；一丢就清零的话，真举着手的人
            # 永远攒不够时长。这与 detector.py 里 lying_grace 是同一类问题。
            held = self._accumulate(ts, False)
            obs["held"] = round(held, 2)
            obs["state"] = "holding" if held > 0 else "idle"
            return obs, None

        hit, side, ratio = self._gesture_now(person)
        obs["ratio"] = round(ratio, 3) if ratio else None
        obs["side"] = side
        ctx.signals.emit(HELP_GESTURE, 1.0 if hit else 0.0, ts=ctx.now(),
                         source=self.name)

        held = self._accumulate(ts, hit)
        obs["held"] = round(held, 2)
        if held > 0:
            obs["state"] = "holding"
        if hit:
            self._side = side

        if held < self.hold_seconds:
            return obs, None
        if self._last_fire is not None and ts - self._last_fire < self.cooldown_seconds:
            obs["state"] = "cooldown"
            return obs, None

        self._last_fire = ts
        self._clear()
        obs["state"] = "fired"
        return obs, SensorEvent(
            kind=self.kind, ts=ts, confidence=0.95, trigger="hand_to_ear",
            features={"held_seconds": round(held, 2),
                      "ear_distance_ratio": round(ratio, 3),
                      "side": self._side,
                      "note": "住户主动求助手势"},
            frame=frame)

    def _accumulate(self, ts: float, hit: bool) -> float:
        """累计保持时长，容忍短暂丢点。

        与跌倒判定里的 lying_grace 同一个道理：姿态估计会抖，手腕点在
        举手时尤其容易丢（手挡住脸、动态模糊）。一帧没检出就清零的话，
        真举着手的人永远攒不够时长。
        """
        if hit:
            if self._since is None:
                self._since = ts
            self._last_hit = ts
        elif self._since is not None:
            if ts - (self._last_hit or ts) > self.grace_seconds:
                self._clear()
                return 0.0
        return ts - self._since if self._since is not None else 0.0

    def _clear(self) -> None:
        self._since = None
        self._last_hit = None

    # ------------------------------------------------------------ 表达
    def narrate(self, event: SensorEvent, scene: dict[str, str]) -> dict[str, str]:
        place = scene.get("place", "")
        who = scene.get("resident_name", "")
        return {
            "title": "求助呼叫",
            "noun": "主动求助",
            # 求助是住户自己发起的，播报要让他知道"系统收到了"，
            # 这本身就有安抚作用——求助后最怕的是不知道有没有人听见
            "announce": "已收到您的求助，正在呼叫工作人员，请稍候。",
            "reassure": "已通知工作人员，马上就到，请在原地等候。",
            "desktop": f"{place} {who} 举手求助",
            "brief": "住户做出求助手势（举手至耳边并保持），这是本人主动发起的"
                     "呼救，不是系统推断。请立即联系到场。",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "detail": f"举手至耳边保持 {self.hold_seconds:g}s 触发　"
                      f"距离阈值 {self.near_ratio:g}×肩宽　"
                      f"静默期 {self.cooldown_seconds:g}s",
            "state": "ready",
        }

    def on_false_alarm(self, event_id: str) -> None:
        # 误报后立刻解除静默，便于现场复测手势
        self._last_fire = None
        self._clear()
