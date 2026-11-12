# -*- coding: utf-8 -*-
"""合成手环：让融合与预测逻辑在没有真实硬件时也能被验证。

照抄 capture.SyntheticSource 的思路——虚拟时钟驱动一条确定性剧本，
不依赖真实耗时，所以自检能在 CI 里跑。

剧本（默认 45 秒一轮，可循环）：
  平静期   心率 72±2，加速度 ~1g，步态稳
  前兆期   心率缓升到 105，步态稳定度从 0.9 滑到 0.35  ← 风险分应在此越阈
  跌落     加速度尖峰 3.2g，随后归于静止
  恢复期   心率缓降

前兆期是整个设计的重点：它模拟"人还没倒，但身体已经出状况"的那段时间。
风险分应该在跌落**之前**就升高并发出 L1/L2 预警——这正是"预警、防范"
区别于"事后告警"的地方。
"""
from __future__ import annotations

import math
import time
from typing import Any

from ..core import signals as sig
from .base import PollingDevice

CALM_S = 8.0
ONSET_S = 12.0
FALL_S = 2.0
RECOVER_S = 23.0
CYCLE_S = CALM_S + ONSET_S + FALL_S + RECOVER_S


class SyntheticBand(PollingDevice):
    name = "synthetic_band"
    kind = "wristband"
    emits_signals = (sig.HEART_RATE, sig.SPO2, sig.ACCEL_MAG,
                     sig.GAIT_STABILITY, sig.WEAR_STATE)
    interval = 0.5

    def __init__(self, cfg, conf: dict[str, Any] | None = None):
        conf = conf or {}
        super().__init__(device_id=str(conf.get("device_id") or "band-sim-01"))
        self.cfg = cfg
        self.band_id = str(conf.get("band_id") or "BAND-0001")
        self.loop = bool(conf.get("loop", True))
        self.interval = float(conf.get("interval", 0.5))
        self.worn = bool(conf.get("worn", True))
        self._t = 0.0

    def read(self) -> list[tuple[str, float, float]]:
        t = self._t
        self._t += self.interval
        if self.loop:
            t = t % CYCLE_S
        elif t > CYCLE_S:
            return []

        hr, gait, accel = self._script(t)
        # 轻微抖动，避免信号恒定导致 slope 恒为 0（真实设备不会这么干净）
        jitter = math.sin(t * 3.7) * 0.6
        quality = 1.0 if self.worn else 0.15
        return [
            (sig.WEAR_STATE, 1.0 if self.worn else 0.0, 1.0),
            (sig.HEART_RATE, hr + jitter, quality),
            (sig.SPO2, 97.0 + math.sin(t * 1.3) * 0.8, quality),
            (sig.ACCEL_MAG, accel, quality),
            (sig.GAIT_STABILITY, max(0.0, min(1.0, gait)), quality),
        ]

    @staticmethod
    def _script(t: float) -> tuple[float, float, float]:
        """返回 (心率, 步态稳定度, 合加速度)。"""
        if t < CALM_S:
            return 72.0, 0.90, 1.0

        if t < CALM_S + ONSET_S:
            # 前兆：心率缓升、步态缓降。线性即可——真实的生理变化更复杂，
            # 但这里要验证的是"风险分能否在事件前越阈"，不是生理模型保真度。
            prog = (t - CALM_S) / ONSET_S
            return 72.0 + 33.0 * prog, 0.90 - 0.55 * prog, 1.0 + 0.15 * prog

        if t < CALM_S + ONSET_S + FALL_S:
            # 跌落：加速度尖峰
            prog = (t - CALM_S - ONSET_S) / FALL_S
            return 112.0, 0.10, 1.0 + 2.2 * math.sin(prog * math.pi)

        prog = (t - CALM_S - ONSET_S - FALL_S) / RECOVER_S
        return 112.0 - 35.0 * min(prog, 1.0), 0.15 + 0.5 * min(prog, 1.0), 1.0

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base.update({"band_id": self.band_id, "simulated": True,
                     "detail": f"模拟手环，{CYCLE_S:.0f}s 一轮："
                               f"平静 → 心率骤升步态不稳 → 跌落 → 恢复"})
        return base
