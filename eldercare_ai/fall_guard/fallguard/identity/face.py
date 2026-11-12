# -*- coding: utf-8 -*-
"""人脸识别：接口预留，**未实现**。

框架留出这个位置，使得接入时只需实现 resolve() 而不动其他任何代码。
之所以没有直接实现，有三个具体原因，写在这里避免下一个人以为是漏做了：

1. **识别率与场景强负相关，而最需要它的场景恰恰最差。**
   跌倒多发于夜间；倒地后多为侧脸或面部被遮挡；养老院夜间照度通常
   很低。这三项都是人脸识别准确率的杀手。拿白天正脸的指标来估计夜间
   倒地场景的表现会严重高估。

2. **人脸底库是敏感生物特征，合规门槛远高于普通视频。**
   需要配套的底库加密存储、住户与监护人的单独授权、留存期限与删除
   机制。技术文档第 9 章对此有要求，当前原型连本地留证都没加密
   （见 README 的已知边界），不具备承载底库的条件。

3. **边际收益有限。** 单人间场景下房间绑定已经够用；多人活动区可以用
   手环。人脸真正的增量价值在"住户走到非绑定区域且未戴手环"，这在
   现阶段不是主要场景。

真要实现时的建议：用 insightface 的 ArcFace 嵌入 + 余弦相似度，
底库存嵌入向量而非原图，相似度阈值宁可保守（认不出好过认错——
认错会把 A 的病史推给 B，在急救场景下是严重事故）。
"""
from __future__ import annotations

from typing import Any

from ..core.identity import Identity, IdentityProvider, IdentitySignals


class FaceIdentity(IdentityProvider):
    name = "face"

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def available() -> bool:
        """恒为 False：接口在，实现不在。

        不用 find_spec 探测 insightface——装了库也不代表有底库和授权，
        让它在 available() 里就显式返回 False，避免 create_enabled()
        把一个会抛异常的 provider 串进识别链。
        """
        return False

    def resolve(self, signals: IdentitySignals) -> Identity | None:
        raise NotImplementedError(
            "人脸识别尚未实现。原因见 fallguard/identity/face.py 模块文档："
            "夜间低照度与倒地侧脸场景识别率不可靠，且人脸底库属敏感生物特征，"
            "需先补齐加密存储与授权流程。当前请使用 room_binding 或 wristband_id。")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": False,
                "detail": "接口预留未实现：夜间/侧脸识别率不足，且底库合规门槛未达成"}
