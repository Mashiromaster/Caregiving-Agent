# -*- coding: utf-8 -*-
"""可穿戴/外部设备接入契约。

与感知插件（Sensor）的区别：
  Sensor        吃视频帧，由主循环按帧驱动，会产出离散事件
  DeviceSource  自带节奏（手环按秒推、云 API 按轮询），只产出连续信号

所以设备跑在自己的后台线程里，往 SignalBus 推数据，不参与主循环。
这样手环掉线、云 API 超时都不会拖慢画面处理——反过来，摄像头卡顿
也不会让手环数据断流。

真实设备接入点是 http_push.HttpPushDevice：厂商网关 POST 过来即可，
不需要为每个品牌写 SDK 适配。BLE 直连留到有具体型号时再说。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..core import signals as sig


class DeviceSource:
    """设备接口。鸭子类型，与 Sensor / PoseBackend 同风格。"""

    name = "base"
    kind = "generic"
    #: 该设备会产出哪些信号名，供体检页核对链路
    emits_signals: tuple[str, ...] = ()

    def start(self, bus: sig.SignalBus) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind}

    @property
    def online(self) -> bool:
        return False


class PollingDevice(DeviceSource):
    """按固定间隔轮询的设备的公共骨架。

    子类只需实现 read() 返回一组 (信号名, 值, quality)，线程管理、
    异常隔离、在线状态都在这里处理——设备读数失败是常态（手环离身、
    蓝牙丢包、网络抖动），每个子类各写一遍 try/except 必然有人写漏。
    """

    interval = 1.0

    def __init__(self, device_id: str = ""):
        self.device_id = device_id or self.name
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bus: sig.SignalBus | None = None
        self._last_ok: float | None = None
        self._errors = 0

    def read(self) -> list[tuple[str, float, float]]:
        """返回 [(signal_name, value, quality), ...]。"""
        raise NotImplementedError

    def start(self, bus: sig.SignalBus) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._bus = bus
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"device-{self.device_id}")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                readings = self.read()
                now = time.time()
                for name, value, quality in readings:
                    if self._bus is not None:
                        self._bus.emit(name, value, ts=now, source=self.device_id,
                                       quality=quality)
                if readings:
                    self._last_ok = now
                    self._errors = 0
            except Exception:
                # 设备读数失败不能让线程退出——手环走出蓝牙范围再回来
                # 应该自动恢复，而不是需要重启整个系统
                self._errors += 1
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def online(self) -> bool:
        """最近一次成功读数在 3 个轮询周期内即视为在线。"""
        if self._last_ok is None:
            return False
        return (time.time() - self._last_ok) < max(self.interval * 3, 5.0)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "device_id": self.device_id,
                "online": self.online, "errors": self._errors,
                "last_ok": round(self._last_ok, 1) if self._last_ok else None,
                "emits": list(self.emits_signals)}


# --------------------------------------------------------------- 注册表
ORDER = ("synthetic_band", "http_push", "apple_health")


def available() -> dict[str, bool]:
    return {"synthetic_band": True, "http_push": True, "apple_health": True}


def create(name: str, cfg, conf: dict[str, Any] | None = None) -> DeviceSource:
    from .apple_health import AppleHealthDevice
    from .http_push import HttpPushDevice
    from .synthetic import SyntheticBand

    builders = {"synthetic_band": SyntheticBand, "http_push": HttpPushDevice,
                "apple_health": AppleHealthDevice}
    name = (name or "").lower()
    if name not in builders:
        raise ValueError(f"未知设备类型 {name!r}，可选：{', '.join(builders)}")
    return builders[name](cfg, conf or {})


def create_enabled(cfg) -> list[DeviceSource]:
    """按配置创建设备。没配就返回空列表——设备是可选的。"""
    out: list[DeviceSource] = []
    for entry in (cfg.get("devices.sources") or []):
        if not entry.get("enabled", True):
            continue
        try:
            out.append(create(str(entry.get("type") or ""), cfg, dict(entry)))
        except Exception:
            continue
    return out


__all__ = ["DeviceSource", "PollingDevice", "ORDER", "available", "create",
           "create_enabled"]
