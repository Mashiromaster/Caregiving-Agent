# -*- coding: utf-8 -*-
"""通用 HTTP 推送接收端：真实设备的接入点。

选它而不是为每个品牌写 SDK，理由是现实：手环厂商各有各的 BLE 协议与
云 API，但几乎都支持配置一个 webhook。让厂商网关 POST 过来，
一个端点吃掉所有品牌。

设备侧 POST /api/device/push，body 形如：

    {"device_id": "band-01", "band_id": "BAND-0001", "worn": true,
     "heart_rate": 78, "spo2": 97, "accel_mag": 1.02, "gait_stability": 0.8}

字段名到内部信号名的映射见 FIELD_MAP，未知字段忽略而不是报错——
厂商多推几个字段不应该导致整条链路失败。
"""
from __future__ import annotations

import time
from typing import Any

from ..core import signals as sig
from .base import DeviceSource

FIELD_MAP = {
    "heart_rate": sig.HEART_RATE,
    "hr": sig.HEART_RATE,
    "spo2": sig.SPO2,
    "blood_oxygen": sig.SPO2,
    "accel_mag": sig.ACCEL_MAG,
    "acceleration": sig.ACCEL_MAG,
    "gait_stability": sig.GAIT_STABILITY,
    "gait": sig.GAIT_STABILITY,
    "worn": sig.WEAR_STATE,
    "wearing": sig.WEAR_STATE,
}


class HttpPushDevice(DeviceSource):
    """被动接收型设备：不自己轮询，等 HTTP 推送喂进来。"""

    name = "http_push"
    kind = "wristband"
    emits_signals = (sig.HEART_RATE, sig.SPO2, sig.ACCEL_MAG,
                     sig.GAIT_STABILITY, sig.WEAR_STATE)

    def __init__(self, cfg, conf: dict[str, Any] | None = None):
        conf = conf or {}
        self.cfg = cfg
        self.device_id = str(conf.get("device_id") or "http-device")
        self.band_id = str(conf.get("band_id") or "")
        self.stale_after = float(conf.get("stale_after", 90.0))
        self._bus: sig.SignalBus | None = None
        self._last_ok: float | None = None
        self._count = 0
        self._rejected = 0

    def start(self, bus: sig.SignalBus) -> None:
        self._bus = bus

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一次推送。由 web 层的 /api/device/push 调用。"""
        if self._bus is None:
            return {"ok": False, "reason": "设备未启动，请先启动监测"}

        device_id = str(payload.get("device_id") or self.device_id)
        ts = payload.get("ts")
        now = float(ts) if isinstance(ts, (int, float)) else time.time()
        # 佩戴状态决定其余读数可不可信：没戴在身上的手环，心率是环境噪声
        worn = payload.get("worn", payload.get("wearing"))
        quality = 1.0 if worn in (None, True, 1, "true", "1") else 0.15

        accepted: list[str] = []
        for key, value in payload.items():
            name = FIELD_MAP.get(key)
            if name is None:
                continue
            if isinstance(value, bool):
                value = 1.0 if value else 0.0
            if not isinstance(value, (int, float)):
                self._rejected += 1
                continue
            q = 1.0 if name == sig.WEAR_STATE else quality
            self._bus.emit(name, float(value), ts=now, source=device_id, quality=q)
            accepted.append(name)

        if accepted:
            self._last_ok = now
            self._count += 1
        band = str(payload.get("band_id") or self.band_id)
        if band:
            self.band_id = band
        return {"ok": bool(accepted), "accepted": accepted,
                "ignored": [k for k in payload
                            if k not in FIELD_MAP and k not in
                            {"device_id", "band_id", "ts"}]}

    @property
    def online(self) -> bool:
        if self._last_ok is None:
            return False
        return (time.time() - self._last_ok) < self.stale_after

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "device_id": self.device_id,
                "band_id": self.band_id, "online": self.online,
                "received": self._count, "rejected": self._rejected,
                "emits": list(self.emits_signals),
                "detail": "POST /api/device/push 接收厂商网关推送"}
