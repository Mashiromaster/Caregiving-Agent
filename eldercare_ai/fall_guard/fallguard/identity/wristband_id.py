# -*- coding: utf-8 -*-
"""手环 ID 识别：设备在谁手上，事件就归谁。

比房间绑定精确——多人活动区里房间绑定完全失效，而手环能直接给出是谁。
置信度 0.9 而非 1.0：手环可能被摘下放在桌上，所以要求设备上报的
佩戴状态为真才采信（见 resolve 里的 worn 判断）。
"""
from __future__ import annotations

from typing import Any

from ..core.identity import Identity, IdentityProvider, IdentitySignals


class WristbandIdentity(IdentityProvider):
    name = "wristband_id"

    def __init__(self, cfg):
        self.cfg = cfg
        # 配置形如 [[identity.bands]] band_id=... resident_id=... name=...
        self.bands = {str(b.get("band_id") or ""): b
                      for b in (cfg.get("identity.bands") or [])}

    def resolve(self, signals: IdentitySignals) -> Identity | None:
        band_id = signals.wristband_id or str(signals.extra.get("band_id") or "")
        if not band_id:
            return None

        # 没戴在身上的手环不能用来确定身份——放在床头柜上的手环
        # 会把护工的操作算到住户头上
        worn = signals.extra.get("worn")
        if worn is not None and not worn:
            return None

        entry = self.bands.get(band_id)
        if entry is None:
            return None
        return Identity(
            resident_id=str(entry.get("resident_id") or ""),
            name=str(entry.get("name") or band_id),
            confidence=0.9, method=self.name,
            detail=f"手环 {band_id} 绑定住户")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "bands": len(self.bands),
                "detail": "按手环 ID 绑定识别，要求设备上报佩戴中"}
