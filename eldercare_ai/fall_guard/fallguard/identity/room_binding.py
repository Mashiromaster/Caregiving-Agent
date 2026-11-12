# -*- coding: utf-8 -*-
"""房间/床位绑定识别：零依赖，覆盖绝大多数机构场景。

养老机构的房间与住户是登记对应的，单人间里出现的人就是登记住户——
这是重构前的隐含假设（config.scene.resident_name），这里把它显式化为
一个 provider，于是它可以被手环、人脸等更精确的方式覆盖。

置信度给 0.6 而不是 1.0：多人间、探视家属、护工进房都会让这个假设失效。
标低它，使更精确的 provider 排在前面时能够胜出。
"""
from __future__ import annotations

from typing import Any

from ..core.identity import Identity, IdentityProvider, IdentitySignals


class RoomBindingIdentity(IdentityProvider):
    name = "room_binding"

    def __init__(self, cfg):
        self.cfg = cfg
        # 配置形如 [[identity.rooms]] room=... resident_id=... name=...
        self.rooms = {str(r.get("room") or ""): r
                      for r in (cfg.get("identity.rooms") or [])}
        self.default_room = str(cfg.scene.place)
        self.default_name = str(cfg.scene.resident_name)
        self.default_id = str(cfg.get("scene.resident_id") or "")

    def resolve(self, signals: IdentitySignals) -> Identity | None:
        room = signals.room or self.default_room
        entry = self.rooms.get(room)
        if entry:
            return Identity(
                resident_id=str(entry.get("resident_id") or ""),
                name=str(entry.get("name") or self.default_name),
                confidence=0.6, method=self.name,
                detail=f"房间 {room} 登记住户")

        # 没有房间表时退回 scene 配置。resident_id 未配则由房间名派生，
        # 这样即使用户没填 id，记忆也有一个稳定的键可用。
        if not self.default_room:
            return None
        rid = self.default_id or f"room:{self.default_room}"
        return Identity(resident_id=rid, name=self.default_name,
                        confidence=0.6, method=self.name,
                        detail=f"场景默认住户（{self.default_room}）")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "rooms": len(self.rooms) or 1,
                "detail": "按房间/床位登记关系识别，单人间可靠，多人间会退化"}
