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

# ---- 可穿戴健康数据（Apple Watch 等）。单独用 watch.* 命名空间而不是并进
# band.*，因为**产生节奏完全不同**：手环模拟器每 0.5 秒一条，而 Apple Watch
# 的静息心率一天才算一次、步行稳定度要攒够步数才更新。两者混在一个名字下，
# 新鲜度策略就没法分别设定（见下面的 MAX_AGE）。
WATCH_HEART_RATE = "watch.heart_rate"            # bpm，区间均值
WATCH_RESTING_HR = "watch.resting_hr"            # bpm，静息心率
WATCH_HRV = "watch.hrv"                          # ms，SDNN
WATCH_SPO2 = "watch.spo2"                        # %
WATCH_RESP_RATE = "watch.respiratory_rate"       # 次/分
WALKING_STEADINESS = "watch.walking_steadiness"  # %，苹果自家的跌倒风险指标
WALKING_SPEED = "watch.walking_speed"            # m/s
WALKING_ASYMMETRY = "watch.walking_asymmetry"    # %，左右步态不对称
DOUBLE_SUPPORT = "watch.double_support"          # %，双脚同时着地占比
STEP_COUNT = "watch.step_count"                  # 步
ACTIVE_ENERGY = "watch.active_energy"            # kcal
SLEEP_HOURS = "watch.sleep_hours"                # 小时
SLEEP_AWAKENINGS = "watch.sleep_awakenings"      # 次，夜间觉醒次数
STAND_HOURS = "watch.stand_hours"                # 小时

#: 每个信号「多旧就不该再信」。**新鲜度不是一个全局常量**——摄像头 15 秒
#: 没数据就是出问题了，而 Apple Watch 的静息心率 6 小时前的读数完全正常。
#: 之前风险评分里把 max_age 写死成 60/120 秒（为 0.5 秒一推的模拟手环调的），
#: 接入手表后每一条读数都会被静默丢弃：信号进了总线、评分却恒为 0 且不报错。
#: 所以把容忍度跟着信号本身走，而不是跟着调用点走。
HOUR = 3600.0
MAX_AGE: dict[str, float] = {
    POSTURE_STABILITY: 15.0,
    MOTION_ENERGY: 15.0,
    PRESENCE: 15.0,
    HEART_RATE: 120.0,
    SPO2: 300.0,
    ACCEL_MAG: 60.0,
    WEAR_STATE: 300.0,
    GAIT_STABILITY: 60.0,
    # 手表类：按小时甚至按天更新，给足容忍度
    WATCH_HEART_RATE: 2 * HOUR,
    WATCH_RESTING_HR: 36 * HOUR,
    WATCH_HRV: 36 * HOUR,
    WATCH_SPO2: 6 * HOUR,
    WATCH_RESP_RATE: 12 * HOUR,
    WALKING_STEADINESS: 7 * 24 * HOUR,   # 要攒够步行样本才更新，一周一次也正常
    WALKING_SPEED: 3 * 24 * HOUR,
    WALKING_ASYMMETRY: 3 * 24 * HOUR,
    DOUBLE_SUPPORT: 3 * 24 * HOUR,
    STEP_COUNT: 6 * HOUR,
    ACTIVE_ENERGY: 12 * HOUR,
    SLEEP_HOURS: 36 * HOUR,
    SLEEP_AWAKENINGS: 36 * HOUR,
    STAND_HOURS: 12 * HOUR,
}

#: 查不到时的兜底容忍度。偏保守——宁可把来路不明的信号当成陈旧的忽略掉，
#: 也不要让一个永远不更新的读数一直参与评分（掉线后最后一次心率会停在那儿）。
DEFAULT_MAX_AGE = 300.0


def max_age_of(name: str) -> float:
    return MAX_AGE.get(name, DEFAULT_MAX_AGE)


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

    def fresh(self, name: str, now: float | None = None) -> Signal | None:
        """按该信号自己的节奏判断新鲜度。

        风险因子一律用这个而不是自己写 max_age——否则每接一类新设备，
        都要去翻一遍所有因子改常数，漏一个就是静默失效。
        """
        return self.latest(name, max_age=max_age_of(name), now=now)

    def first_fresh(self, names: Iterable[str], now: float | None = None
                    ) -> Signal | None:
        """按优先级取第一个还新鲜的信号。

        用于同一量纲的多源备份：手表的步行稳定度最准但可能几天才更新一次，
        没有时退回手环步态，再没有才用摄像头姿态。
        """
        for name in names:
            got = self.fresh(name, now=now)
            if got is not None:
                return got
        return None

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
