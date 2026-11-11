# -*- coding: utf-8 -*-
"""连续信号层：设备与传感器持续产出的读数。

与事件（SensorEvent）的区别是整个框架的核心分层，值得说清楚：

  Signal      连续读数 —— 心率 72、步态稳定度 0.61、画面有人。
              高频、可丢、不触发任何处置动作。
  SensorEvent 离散判定 —— 跌倒成立。低频、不可丢、触发完整处置链路。

摄像头同时产出两者：每帧喂一个姿态稳定度 Signal 给 RiskScorer，
只在判定成立时发一个 SensorEvent。手环只产出 Signal。

为什么要分开：预警与告警必须是两条独立通路。风险分再高也不自动外呼，
跌倒判定也不等风险分点头——否则评分算法一出问题就会连累"有人跌倒
要打电话"这条命脉。这一点在 risk.py 里还会再强调一次。
"""
from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# 信号名约定：<来源域>.<量纲>。来源域用于在风险评分里区分多源，
# 量纲相同的信号（如 camera.motion 与 radar.motion）可以互为备份。
POSTURE_STABILITY = "camera.posture_stability"   # 0~1，越低越不稳
MOTION_ENERGY = "camera.motion"                  # 0~1，画面体动能量
PRESENCE = "camera.presence"                     # 0/1，画面内是否有人
HEART_RATE = "band.heart_rate"                   # bpm
SPO2 = "band.spo2"                               # %
ACCEL_MAG = "band.accel_mag"                     # g，合加速度
WEAR_STATE = "band.worn"                         # 0/1，是否佩戴中
GAIT_STABILITY = "band.gait_stability"           # 0~1，越低越不稳


@dataclass
class Signal:
    """一次读数。

    quality 用来表达"这个读数有多可信"——手环没戴好时心率仍有数值但不可信，
    摄像头逆光时姿态估计同理。风险评分按 quality 加权，低于阈值的直接忽略，
    这比让每个消费者各自判断"要不要信"要可靠。
    """

    name: str
    value: float
    ts: float
    source: str = ""                       # 设备/传感器实例 id
    quality: float = 1.0                   # 0~1
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "ts": round(self.ts, 3),
                "source": self.source, "quality": round(self.quality, 3)}


class SignalBus:
    """信号总线：保留每个信号名的滑动窗口，供风险评分做趋势判断。

    不是消息队列——单进程内的订阅分发加一个带时间窗的环形缓冲。
    引入 NATS/Redis 这类东西对单机原型是纯负担，等真要跨进程再说。
    """

    def __init__(self, window_seconds: float = 300.0, max_per_name: int = 2000):
        self.window = float(window_seconds)
        self.max_per_name = int(max_per_name)
        self._series: dict[str, collections.deque] = {}
        self._latest: dict[str, Signal] = {}
        self._subs: list[Callable[[Signal], None]] = []
        self._lock = threading.Lock()

    # ----------------------------------------------------------- 写
    def publish(self, signal: Signal) -> None:
        with self._lock:
            series = self._series.get(signal.name)
            if series is None:
                series = collections.deque(maxlen=self.max_per_name)
                self._series[signal.name] = series
            series.append(signal)
            self._latest[signal.name] = signal
            # 过期裁剪放在写入侧：读侧（风险评分）可能每秒跑一次，
            # 让它每次都扫全量是浪费；写入时顺手丢掉窗口外的点更划算。
            cutoff = signal.ts - self.window
            while series and series[0].ts < cutoff:
                series.popleft()
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(signal)
            except Exception:
                # 订阅者异常不能拖垮产信号的设备线程
                pass

    def emit(self, name: str, value: float, ts: float | None = None,
             source: str = "", quality: float = 1.0, **meta: Any) -> Signal:
        sig = Signal(name=name, value=float(value), ts=ts if ts is not None else time.time(),
                     source=source, quality=float(quality), meta=meta)
        self.publish(sig)
        return sig

    def subscribe(self, fn: Callable[[Signal], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    # ----------------------------------------------------------- 读
    def latest(self, name: str, max_age: float | None = None,
               now: float | None = None) -> Signal | None:
        """最新读数。max_age 限定新鲜度——陈旧信号比没有信号更危险，
        因为它看起来是有效的（手环掉线后最后一次心率会一直停在那儿）。"""
        with self._lock:
            sig = self._latest.get(name)
        if sig is None:
            return None
        if max_age is not None:
            ref = now if now is not None else time.time()
            if ref - sig.ts > max_age:
                return None
        return sig

    def value(self, name: str, default: float = 0.0,
              max_age: float | None = None, now: float | None = None) -> float:
        sig = self.latest(name, max_age=max_age, now=now)
        return sig.value if sig is not None else default

    def window_of(self, name: str, seconds: float,
                  now: float | None = None) -> list[Signal]:
        ref = now if now is not None else time.time()
        with self._lock:
            series = list(self._series.get(name) or ())
        return [s for s in series if ref - s.ts <= seconds]

    def mean(self, name: str, seconds: float, default: float = 0.0,
             min_quality: float = 0.0, now: float | None = None) -> float:
        items = [s for s in self.window_of(name, seconds, now=now)
                 if s.quality >= min_quality]
        return sum(s.value for s in items) / len(items) if items else default

    def slope(self, name: str, seconds: float, now: float | None = None) -> float:
        """窗口内的变化速率（单位/秒）。心率骤升、姿态稳定度下滑都靠它。

        用首尾两点而不是最小二乘：信号本身噪声大，最小二乘的额外精度
        换不来判断上的差别，而首尾差更容易在日志里被人工复核。
        """
        items = self.window_of(name, seconds, now=now)
        if len(items) < 2:
            return 0.0
        dt = items[-1].ts - items[0].ts
        if dt < 1e-6:
            return 0.0
        return (items[-1].value - items[0].value) / dt

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._series)

    def snapshot(self, max_age: float | None = 30.0,
                 now: float | None = None) -> dict[str, dict[str, Any]]:
        """给 Web 面板用的当前读数表。"""
        out: dict[str, dict[str, Any]] = {}
        for name in self.names():
            sig = self.latest(name, max_age=max_age, now=now)
            if sig is not None:
                out[name] = sig.to_dict()
        return out

    def clear(self) -> None:
        with self._lock:
            self._series.clear()
            self._latest.clear()


def quality_weighted(signals: Iterable[Signal], min_quality: float = 0.3
                     ) -> float | None:
    """按 quality 加权平均。多个来源测同一量纲时用它融合。"""
    items = [s for s in signals if s.quality >= min_quality]
    if not items:
        return None
    total = sum(s.quality for s in items)
    if total <= 0:
        return None
    return sum(s.value * s.quality for s in items) / total
