# -*- coding: utf-8 -*-
"""预览窗渲染。

中文在 OpenCV 的 putText 里会变成问号，所以界面文案走英文 + 数字，
中文只出现在语音播报、系统通知和日志里——那几处都不受此限制。
"""
from __future__ import annotations

import cv2
import numpy as np

from .detector import Observation

GREEN = (120, 210, 120)
BLUE = (200, 150, 60)
AMBER = (60, 180, 245)
RED = (70, 70, 230)
GREY = (170, 170, 170)
WHITE = (245, 245, 245)
DARK = (38, 32, 28)

STATE_STYLE = {
    "idle": ("IDLE", GREY),
    "upright": ("UPRIGHT", GREEN),
    "lying": ("LYING...", AMBER),
    "fall": ("FALL!", RED),
    "cooldown": ("COOLDOWN", BLUE),
    "zone_suppressed": ("ZONE SUPPRESSED", BLUE),
}


def _panel(img, x, y, w, h, alpha=0.55, color=DARK):
    sub = img[max(y, 0):y + h, max(x, 0):x + w]
    if sub.size == 0:
        return
    overlay = np.full(sub.shape, color, dtype=np.uint8)
    cv2.addWeighted(overlay, alpha, sub, 1 - alpha, 0, dst=sub)


def _text(img, s, org, scale=0.5, color=WHITE, thick=1):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0),
                thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                thick, cv2.LINE_AA)


def draw(frame: np.ndarray, obs: Observation, *, backend: str, fps: float,
         cfg, banner: str = "", banner_color=RED, stats: dict | None = None
         ) -> np.ndarray:
    img = frame.copy()
    h, w = img.shape[:2]
    d = cfg.detector

    # 排除区域（床铺等躺倒属正常的地方）
    for zone in (d.exclude_zones or []):
        if len(zone) != 4:
            continue
        x1, y1, x2, y2 = zone
        p1 = (int(x1 * w), int(y1 * h))
        p2 = (int(x2 * w), int(y2 * h))
        cv2.rectangle(img, p1, p2, BLUE, 1)
        _text(img, "EXCLUDED", (p1[0] + 4, p1[1] + 16), 0.42, BLUE)

    # 地面高度参考线
    floor_y = float(d.floor_y)
    if floor_y > 0:
        y = int(floor_y * h)
        cv2.line(img, (0, y), (w, y), (90, 110, 130), 1, cv2.LINE_AA)
        _text(img, f"floor y={floor_y:.2f}", (8, y - 6), 0.4, (150, 170, 185))

    # 人体框与骨骼参考点
    if obs.person is not None:
        x1, y1, x2, y2 = obs.person.bbox
        p1 = (int(x1 * w), int(y1 * h))
        p2 = (int(x2 * w), int(y2 * h))
        box_color = RED if obs.state == "fall" else (AMBER if obs.lying else GREEN)
        cv2.rectangle(img, p1, p2, box_color, 2)
        label = f"person {obs.person.score:.2f}"
        if obs.aspect is not None:
            label += f"  ar={obs.aspect:.2f}"
        _text(img, label, (p1[0], max(p1[1] - 8, 14)), 0.46, box_color)

        for name, (kx, ky, kc) in obs.person.keypoints.items():
            if kc < 0.3:
                continue
            cv2.circle(img, (int(kx * w), int(ky * h)), 3, (235, 220, 130), -1)

        if obs.centroid:
            cx, cy = obs.centroid
            cv2.circle(img, (int(cx * w), int(cy * h)), 6, (255, 255, 255), 2)

    # 左上状态面板
    _panel(img, 0, 0, 268, 112)
    state_text, state_color = STATE_STYLE.get(obs.state, (obs.state.upper(), WHITE))
    _text(img, state_text, (12, 26), 0.72, state_color, 2)
    _text(img, f"backend={backend}  fps={fps:4.1f}", (12, 48), 0.44, WHITE)
    angle = f"{obs.torso_angle:5.1f}" if obs.torso_angle is not None else "  n/a"
    _text(img, f"torso={angle}  drop={obs.descent_rate:4.2f}/s", (12, 68), 0.44, WHITE)
    _text(img, f"motion={obs.motion:6.4f}  lying={obs.lying_for:4.1f}s",
          (12, 88), 0.44, WHITE)
    _text(img, f"thr: angle>={float(d.lying_angle_deg):.0f} drop>="
               f"{float(d.descent_rate_min):.2f} hold>={float(d.lying_seconds):.1f}s",
          (12, 106), 0.38, (170, 190, 205))

    # 右上统计
    if stats:
        box_w = 190
        _panel(img, w - box_w, 0, box_w, 60)
        _text(img, f"events={stats.get('events', 0)}  calls={stats.get('calls', 0)}",
              (w - box_w + 10, 24), 0.44, WHITE)
        _text(img, f"cancelled={stats.get('cancelled', 0)}",
              (w - box_w + 10, 46), 0.44, WHITE)

    # 底部横幅（倒计时 / 处置中 / 已外呼）
    if banner:
        _panel(img, 0, h - 52, w, 52, alpha=0.72, color=(20, 16, 14))
        cv2.rectangle(img, (0, h - 52), (w, h - 49), banner_color, -1)
        _text(img, banner, (14, h - 18), 0.66, banner_color, 2)

    # 操作提示
    _text(img, "q quit   c cancel-call   f mark-false-alarm   t test-trigger",
          (12, h - 60 if banner else h - 12), 0.40, (190, 200, 210))
    return img
