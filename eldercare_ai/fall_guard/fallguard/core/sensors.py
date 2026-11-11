# -*- coding: utf-8 -*-
"""感知插件协议与注册表。

协议用鸭子类型而不是 ABC，与 pose/base.py 的 PoseBackend 保持一致：
继承 Sensor 能拿到合理的默认实现，但不继承、只要方法齐全也能用。

插件要回答四个问题：
  observe()       这一刻发生了什么？（并顺便往信号总线喂连续读数）
  narrate()       这件事用人话怎么说？（播报/通知/外呼槽位全从这里取）
  describe()      我是谁、当前什么状态？（给 Web 面板与体检页）
  on_false_alarm() 被判为误报时我该怎么自我调整？

narrate() 是把事件语义从框架里挤出去的关键。重构前"跌倒"二字散落在
harness.py 的 prompt、RulePlanner 的播报、pipeline 的外呼槽位里，
加一类事件就要改三处；现在全部由插件自己回答。
"""
from __future__ import annotations

from typing import Any

from .events import SensorEvent

# 按"先专用后兜底"排序，与 pose/__init__.py 的 ORDER 同一思路
ORDER = ("fall_vision",)


class Sensor:
    """感知插件接口。"""

    kind = "base"
    name = "base"
    #: 这个插件会往 SignalBus 发哪些信号名，供体检页核对链路是否通
    emits_signals: tuple[str, ...] = ()
    #: 是否需要视频帧。手环类插件为 False，runtime 不会给它喂帧
    needs_frame = True

    def observe(self, ts: float, frame: Any, ctx: "SensorContext"
                ) -> tuple[Any, SensorEvent | None]:
        """处理一次观测。

        返回 (observation, event)。observation 是插件自定义的调试载荷，
        框架只把它传给渲染层；event 为 None 表示没有判定成立。
        连续读数应在这里 ctx.signals.emit(...)，不要塞进 observation。
        """
        raise NotImplementedError

    def narrate(self, event: SensorEvent, scene: dict[str, str]) -> dict[str, str]:
        """事件的人话表达。

        约定的键：
          title       系统通知标题，如"跌倒预警"
          noun        事件名词，进外呼播报四要素，如"跌倒"
          announce    现场播报（判定成立时立即播），含倒计时占位 {seconds}
          reassure    处置期间安抚老人的话
          brief       给智能体 prompt 的一句话情境描述
        """
        noun = self.kind
        return {
            "title": f"{noun} 预警",
            "noun": noun,
            "announce": f"检测到{noun}。{{seconds}}秒后将自动拨打电话，如误报请按取消。",
            "reassure": "已通知工作人员，请不要自行起身，我们马上到。",
            "brief": f"{noun}事件已判定成立，请立即处置。",
        }

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name}

    def on_false_alarm(self, event_id: str) -> None:
        """事件被标注为误报。插件可在此解除静默期、回流标注等。"""
        return None

    def close(self) -> None:
        return None


class SensorContext:
    """插件运行所需的框架能力。

    插件通过它拿到信号总线与场景信息，但拿不到外呼网关、事件日志、
    文件系统——感知层不应该有能力直接打电话，那是处置层的职责。
    """

    def __init__(self, signals, scene: dict[str, str], log=print):
        self.signals = signals
        self.scene = dict(scene)
        self.log = log


def available() -> dict[str, bool]:
    """各插件是否可用。与 pose.available() 一样用 find_spec 探测，不 import。"""
    import importlib.util

    has_cv2 = importlib.util.find_spec("cv2") is not None
    return {"fall_vision": has_cv2}


def create(name: str, cfg, fps: int, **kwargs: Any) -> Sensor:
    """按名称创建插件。"""
    from ..sensors.fall_vision import FallVisionSensor

    builders = {"fall_vision": FallVisionSensor}
    name = (name or "").lower()
    if name not in builders:
        raise ValueError(
            f"未知感知插件 {name!r}，可选：{', '.join(builders)}")
    return builders[name](cfg, fps, **kwargs)


def create_enabled(cfg, fps: int, **kwargs: Any) -> list[Sensor]:
    """按配置创建启用的插件集合。

    配置缺失时默认只启 fall_vision——保持与重构前完全一致的行为，
    不能因为引入了插件机制就让原本在跑的检测悄悄停掉。
    """
    names = list(cfg.get("sensors.enabled") or ORDER)
    have = available()
    out: list[Sensor] = []
    errors: list[str] = []
    for name in names:
        if not have.get(name):
            errors.append(f"{name}: 依赖不满足")
            continue
        try:
            out.append(create(name, cfg, fps, **kwargs))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if not out:
        raise RuntimeError("没有可用的感知插件：" + "; ".join(errors or ["未配置"]))
    return out


__all__ = ["Sensor", "SensorContext", "SensorEvent", "ORDER",
           "available", "create", "create_enabled"]
