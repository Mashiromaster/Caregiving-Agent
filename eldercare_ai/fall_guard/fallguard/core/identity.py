# -*- coding: utf-8 -*-
"""身份识别：把一次观测归到某个 resident_id。

为什么要有这层：重构前 resident_name 是配置里的一个常量字符串，
一台机器只服务一个人。要让记忆"按人召回"，先得能回答"这是谁"。

识别方式是可插拔的，因为不同部署条件差别极大：
  room_binding   房间/床位绑定 —— 零依赖，单人间天然成立，覆盖绝大多数场景
  wristband_id   手环 ID —— 多人活动区可用，前提是住户愿意戴
  face           人脸 —— 预留接口，未实现，原因见 identity/face.py

provider 按优先级依次尝试，先返回非空结果者胜出。识别不出时返回
空 resident_id，整条链路仍然照常走——**身份是增强项，不是前置条件**。
认不出人也必须把电话打出去，这一点不能妥协。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ORDER = ("wristband_id", "room_binding")


@dataclass
class Identity:
    resident_id: str = ""
    name: str = ""
    confidence: float = 0.0
    method: str = ""                             # 哪个 provider 给出的
    detail: str = ""

    @property
    def known(self) -> bool:
        return bool(self.resident_id)

    def to_dict(self) -> dict[str, Any]:
        return {"resident_id": self.resident_id, "name": self.name,
                "confidence": round(self.confidence, 3),
                "method": self.method, "detail": self.detail}


@dataclass
class IdentitySignals:
    """识别可用的线索。各 provider 各取所需。"""

    room: str = ""
    wristband_id: str = ""
    frame: Any = None
    event_kind: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class IdentityProvider:
    """识别器接口。鸭子类型，与 Sensor / PoseBackend 同风格。"""

    name = "base"

    def resolve(self, signals: IdentitySignals) -> Identity | None:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}


class IdentityResolver:
    """按优先级串联多个 provider。"""

    def __init__(self, providers: list[IdentityProvider]):
        self.providers = list(providers)

    def resolve(self, signals: IdentitySignals) -> Identity:
        for provider in self.providers:
            try:
                got = provider.resolve(signals)
            except Exception:
                # 识别失败不能阻断处置：认不出人也要打电话
                continue
            if got is not None and got.known:
                return got
        return Identity(method="unresolved",
                        detail="无可用身份线索，按房间默认住户处置")

    def describe(self) -> list[dict[str, Any]]:
        return [p.describe() for p in self.providers]


def available() -> dict[str, bool]:
    from ..identity.face import FaceIdentity

    return {
        "room_binding": True,
        "wristband_id": True,
        "face": FaceIdentity.available(),
    }


def create(name: str, cfg) -> IdentityProvider:
    from ..identity.face import FaceIdentity
    from ..identity.room_binding import RoomBindingIdentity
    from ..identity.wristband_id import WristbandIdentity

    builders = {
        "room_binding": RoomBindingIdentity,
        "wristband_id": WristbandIdentity,
        "face": FaceIdentity,
    }
    name = (name or "").lower()
    if name not in builders:
        raise ValueError(f"未知身份识别器 {name!r}，可选：{', '.join(builders)}")
    return builders[name](cfg)


def create_enabled(cfg) -> IdentityResolver:
    """按配置创建识别链。

    默认 wristband_id → room_binding：手环能区分具体是谁，房间绑定
    只能给出"这个房间登记的住户"，所以前者优先。房间绑定放最后做兜底，
    保证单人间场景下永远能认出人。
    """
    names = list(cfg.get("identity.providers") or ORDER)
    have = available()
    providers: list[IdentityProvider] = []
    for name in names:
        if not have.get(name):
            continue
        try:
            providers.append(create(name, cfg))
        except Exception:
            continue
    return IdentityResolver(providers)


__all__ = ["Identity", "IdentitySignals", "IdentityProvider", "IdentityResolver",
           "ORDER", "available", "create", "create_enabled"]
