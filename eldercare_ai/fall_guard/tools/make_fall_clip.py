#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用一张真人照片合成一段跌倒视频，用于验证 mediapipe + 判定链路。

合成场景（SyntheticSource）画的是灰色矩形，真人姿态模型一个都检不出，
因此无法验证 mediapipe 路径。这里换一种做法：**像素是真人的**，只有运动
是合成的——把真人照片按角度旋转并向下平移，模拟站立→倒地→静止躺卧。

它不能替代真实录像（没有真实的遮挡、运动模糊、光照变化），但它是第一个
能让 mediapipe 输出**真实关键点**并驱动完整判定链路的测试素材。

  python3 tools/make_fall_clip.py --out /tmp/fall_real.mp4
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

FPS = 15


def build(src_img, w=960, h=720, fps=FPS,
          stand_s=4.0, fall_s=0.8, lie_s=14.0):
    """站立 → 快速倒地 → 静止躺卧。"""
    ph, pw = src_img.shape[:2]
    # 缩放人物到画面高度的 ~55%，留出下落空间
    target_h = int(h * 0.55)
    scale = target_h / ph
    person = cv2.resize(src_img, (int(pw * scale), target_h),
                        interpolation=cv2.INTER_AREA)
    ph, pw = person.shape[:2]
    diag = int(np.hypot(pw, ph)) + 4

    # 把人物放进一个方形画布中心，旋转时不会被裁掉
    pad = np.full((diag, diag, 3), 60, dtype=np.uint8)
    oy, ox = (diag - ph) // 2, (diag - pw) // 2
    pad[oy:oy + ph, ox:ox + pw] = person

    rng = np.random.default_rng(11)
    bg = np.full((h, w, 3), 58, dtype=np.uint8)
    bg[int(h * 0.80):] = 46          # 地面稍暗，给一条视觉地平线

    n_stand = int(stand_s * fps)
    n_fall = int(fall_s * fps)
    n_lie = int(lie_s * fps)
    frames = []

    for i in range(n_stand + n_fall + n_lie):
        if i < n_stand:
            prog = 0.0
            jitter = int(2 * np.sin(i * 0.5))        # 站立时的轻微摇晃
        elif i < n_stand + n_fall:
            prog = (i - n_stand) / n_fall
            jitter = 0
        else:
            prog = 1.0
            jitter = 0

        angle = 90.0 * prog
        # 质心从画面中部下落到接近地面
        cy = int(h * (0.44 + 0.34 * prog))
        cx = int(w * 0.50) + jitter

        M = cv2.getRotationMatrix2D((diag / 2, diag / 2), -angle, 1.0)
        rot = cv2.warpAffine(pad, M, (diag, diag), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT,
                             borderValue=(60, 60, 60))

        frame = bg.copy()
        x1, y1 = cx - diag // 2, cy - diag // 2
        x2, y2 = x1 + diag, y1 + diag
        sx1, sy1 = max(x1, 0), max(y1, 0)
        sx2, sy2 = min(x2, w), min(y2, h)
        if sx2 > sx1 and sy2 > sy1:
            frame[sy1:sy2, sx1:sx2] = rot[sy1 - y1:sy2 - y1, sx1 - x1:sx2 - x1]

        # 轻微噪声，避免完全静止帧让运动能量恒为 0
        noise = rng.integers(-2, 3, frame.shape, dtype=np.int16)
        frames.append(np.clip(frame.astype(np.int16) + noise, 0, 255)
                      .astype(np.uint8))
    return frames


def main() -> int:
    p = argparse.ArgumentParser(description="用真人照片合成跌倒测试视频")
    p.add_argument("--photo", default="/tmp/pose_test.jpg",
                   help="真人全身照片")
    p.add_argument("--out", default="/tmp/fall_real.mp4")
    p.add_argument("--lie-seconds", type=float, default=14.0)
    args = p.parse_args()

    img = cv2.imread(args.photo)
    if img is None:
        print(f"无法读取照片：{args.photo}")
        return 1

    frames = build(img, lie_s=args.lie_seconds)
    h, w = frames[0].shape[:2]
    out = Path(args.out)
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"),
                             float(FPS), (w, h))
    if not writer.isOpened():
        print("VideoWriter 打开失败")
        return 1
    for f in frames:
        writer.write(f)
    writer.release()
    print(f"已生成 {out}  {len(frames)} 帧 / {len(frames)/FPS:.1f}s  {w}x{h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
