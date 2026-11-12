# -*- coding: utf-8 -*-
"""兼容壳：FallGuard 现在是 core.Runtime 的一个别名。

处置编排已全部迁至 core/runtime.py，跌倒判定迁至 sensors/fall_vision.py。
保留这个名字是因为 web/runner.py 继承它、selftest 与 main.py 导入它，
换名字的收益抵不过把所有调用点改一遍的风险。

旧字段名（backend / detector / _fps_ema / _stop / _tick / _show / _handle_key）
以属性或方法形式保留，指向新结构。新代码请直接用 Runtime。
"""
from __future__ import annotations

from typing import Any

from .core.runtime import ActiveEvent, Runtime, Stats, in_window

# selftest 直接导入了这个私有名，保留别名
_in_window = in_window


class FallGuard(Runtime):
    """兼容层。新代码直接用 core.Runtime。"""

    # -------------------------------------------------- 旧字段的转发
    @property
    def backend(self):
        """原 self.backend 是姿态后端，现在归属跌倒插件。"""
        sensor = self.primary_sensor()
        return getattr(sensor, "backend", None)

    @property
    def detector(self):
        """原 self.detector 是跌倒状态机，现在归属跌倒插件。"""
        sensor = self.sensor_of("fall") or self.primary_sensor()
        return getattr(sensor, "detector", None)

    # -------------------------------------------------- 旧方法名
    def _tick(self):
        return self.tick()

    def _show(self, frame, obs) -> int:
        return self.show_frame(frame, obs)

    def _handle_key(self, key: int) -> None:
        self.handle_key(key)

    def on_fall(self, verdict: Any, frame) -> None:
        """旧入口。verdict 若是 FallVerdict 则转成 SensorEvent。"""
        from .core.events import SensorEvent

        if isinstance(verdict, SensorEvent):
            event = verdict
        else:
            event = SensorEvent(
                kind=getattr(verdict, "kind", "fall"), ts=verdict.ts,
                confidence=verdict.confidence, trigger=verdict.trigger,
                features=dict(getattr(verdict, "features", {}) or {}))
        self.on_event(event, frame)


__all__ = ["FallGuard", "Runtime", "Stats", "ActiveEvent", "in_window", "_in_window"]
