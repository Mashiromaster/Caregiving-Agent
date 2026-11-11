# -*- coding: utf-8 -*-
"""姿态后端工厂。"""
from __future__ import annotations

from .base import KEYS, Person, PoseBackend

ORDER = ("mediapipe", "yolo", "motion")


def available() -> dict[str, bool]:
    import importlib.util

    return {
        "mediapipe": importlib.util.find_spec("mediapipe") is not None,
        "yolo": importlib.util.find_spec("ultralytics") is not None,
        "motion": True,
    }


def create(name: str = "auto") -> PoseBackend:
    """按名称创建后端；auto 时按 精度优先 的顺序挑第一个可用的。"""
    from .backends import MediaPipeBackend, MotionBackend, YoloPoseBackend

    builders = {
        "mediapipe": MediaPipeBackend,
        "yolo": YoloPoseBackend,
        "motion": MotionBackend,
    }
    name = (name or "auto").lower()

    if name == "auto":
        have = available()
        errors = []
        for candidate in ORDER:
            if not have.get(candidate):
                continue
            try:
                return builders[candidate]()
            except Exception as exc:          # 装了但初始化失败（版本不兼容等）
                errors.append(f"{candidate}: {exc}")
        raise RuntimeError("没有可用的姿态后端：" + "; ".join(errors))

    if name not in builders:
        raise ValueError(f"未知后端 {name!r}，可选：{', '.join(builders)} 或 auto")
    return builders[name]()


__all__ = ["KEYS", "Person", "PoseBackend", "available", "create", "ORDER"]
