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

# 按"先专用后兜底"排序，与 pose/__init__.py 的 ORDER 同一思路。
# fall_vision 必须排在 help_gesture 前面——它负责跑姿态推理并把结果
# 挂到 ctx.person 上，手势插件直接复用，不重复推理。
ORDER = ("fall_vision", "help_gesture")


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

    **两个时钟**，这是容易出错的地方：
      ts（传给 observe 的参数）  源时钟。视频文件与合成源从 0 开始计，
                                 判定逻辑与留证检索都用它。
      now()                      墙钟。连续信号必须用它打时间戳。

    为什么信号要用墙钟：风险评分、Web 面板、设备线程全都在墙钟上工作。
    合成源 296 帧只花 0.03 秒墙钟就跑完，若信号带的是源时钟（0~19.7），
    评分器按墙钟判断新鲜度时会认为这些读数来自 1970 年而全部丢弃——
    摄像头信号就此永远不参与评分。
    """

    def __init__(self, signals, scene: dict[str, str], log=print):
        self.signals = signals
        self.scene = dict(scene)
        self.log = log
        self._wall = 0.0
        #: 本帧的主目标（Person）。由产出骨骼点的插件填写，后续插件复用——
        #: 否则每个需要骨骼点的插件都要自己跑一次姿态推理，帧率会成倍下降。
        self.person: Any = None

    def tick(self, wall: float) -> None:
        """由 runtime 每帧更新，避免每个插件各自调 time.time()。"""
        self._wall = wall
        self.person = None

    def now(self) -> float:
        import time

        return self._wall or time.time()


def available() -> dict[str, bool]:
    """各插件是否可用。与 pose.available() 一样用 find_spec 探测，不 import。"""
    import importlib.util

    has_cv2 = importlib.util.find_spec("cv2") is not None
    return {"fall_vision": has_cv2, "help_gesture": has_cv2}


def create(name: str, cfg, fps: int, **kwargs: Any) -> Sensor:
    """按名称创建插件。"""
    from ..sensors.fall_vision import FallVisionSensor
    from ..sensors.help_gesture import HelpGestureSensor

    builders = {"fall_vision": FallVisionSensor,
                "help_gesture": HelpGestureSensor}
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
    # help_gesture 复用 fall_vision 挂在 ctx 上的骨骼点，单独启用会永远
    # 检不到人——与其静默失效，不如在日志里说清楚
    if "help_gesture" in names and "fall_vision" not in names:
        raise RuntimeError(
            "help_gesture 依赖 fall_vision 提供骨骼点（它不自己跑姿态推理），"
            "请在 sensors.enabled 里同时启用 fall_vision")
    # 顺序很重要：产出骨骼点的插件必须先跑
    names.sort(key=lambda n: ORDER.index(n) if n in ORDER else len(ORDER))
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
