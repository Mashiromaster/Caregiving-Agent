# -*- coding: utf-8 -*-
"""姿态观测的统一数据结构。

不同后端（MediaPipe 33 点 / YOLO-Pose COCO 17 点 / 纯运动检测无点）输出差异很大，
这里统一成"命名关键点字典 + 归一化人体框"，让检测逻辑与后端解耦。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 检测逻辑真正用到的点位，其余一律丢弃。
# 前 9 个供跌倒判定（躯干角、质心）；后 4 个供手势识别（手腕相对耳朵的位置）。
KEYS = ("nose", "l_sho", "r_sho", "l_hip", "r_hip", "l_knee", "r_knee", "l_ank", "r_ank",
        "l_wri", "r_wri", "l_ear", "r_ear")


@dataclass
class Person:
    """一个被观测到的人。坐标均为画面归一化值（0~1），y 向下为正。"""

    bbox: tuple[float, float, float, float]          # x1, y1, x2, y2
    score: float
    keypoints: dict[str, tuple[float, float, float]] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return max(self.bbox[2] - self.bbox[0], 1e-6)

    @property
    def height(self) -> float:
        return max(self.bbox[3] - self.bbox[1], 1e-6)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        """宽高比：站立 < 1，躺倒 > 1。"""
        return self.width / self.height

    def point(self, name: str, min_conf: float = 0.3):
        kp = self.keypoints.get(name)
        if kp is None or kp[2] < min_conf:
            return None
        return kp[0], kp[1]

    def midpoint(self, left: str, right: str, min_conf: float = 0.3):
        a = self.point(left, min_conf)
        b = self.point(right, min_conf)
        if a and b:
            return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return a or b

    def centroid(self) -> tuple[float, float]:
        """优先用髋部中点，其次肩髋平均，最后退化到框中心。"""
        hip = self.midpoint("l_hip", "r_hip")
        if hip:
            return hip
        sho = self.midpoint("l_sho", "r_sho")
        if sho:
            return sho
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) / 2, (y1 + y2) / 2

    def torso_angle_deg(self) -> float | None:
        """躯干与竖直方向的夹角：0°=直立，90°=水平躺卧。"""
        import math

        sho = self.midpoint("l_sho", "r_sho")
        hip = self.midpoint("l_hip", "r_hip")
        if not sho or not hip:
            return None
        dx = hip[0] - sho[0]
        dy = hip[1] - sho[1]
        norm = math.hypot(dx, dy)
        if norm < 1e-4:
            return None
        # 与竖直向下向量 (0, 1) 的夹角
        cos = max(-1.0, min(1.0, dy / norm))
        return math.degrees(math.acos(cos))


class PoseBackend:
    """后端接口。"""

    name = "base"
    provides_keypoints = False

    def infer(self, frame_bgr) -> list[Person]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:
        return None
