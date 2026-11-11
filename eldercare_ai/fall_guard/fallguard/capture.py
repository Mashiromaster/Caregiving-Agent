# -*- coding: utf-8 -*-
"""视频源与滚动缓冲。

缓冲区按 JPEG 存储（640 宽），300 帧约 10～15 MB，
因此可以安全地保留"跌倒前 5 秒 + 后 10 秒"而不吃掉几百兆内存。
"""
from __future__ import annotations

import collections
import math
import time
from typing import Iterable

import cv2
import numpy as np

BUFFER_WIDTH = 640


class CameraSource:
    """摄像头或视频文件。macOS 下优先使用 AVFoundation 后端。"""

    def __init__(self, source, width: int, height: int, fps: int, flip: bool = False):
        self.flip = flip
        self.is_file = isinstance(source, str) and not str(source).isdigit()
        index_or_path = source
        if isinstance(source, str) and source.isdigit():
            index_or_path = int(source)

        backends: list[int] = []
        if not self.is_file:
            import sys

            if sys.platform == "darwin" and hasattr(cv2, "CAP_AVFOUNDATION"):
                backends.append(cv2.CAP_AVFOUNDATION)
        backends.append(cv2.CAP_ANY)

        self.cap = None
        for backend in backends:
            cap = cv2.VideoCapture(index_or_path, backend)
            if cap.isOpened():
                self.cap = cap
                break
            cap.release()
        if self.cap is None:
            raise RuntimeError(
                f"无法打开视频源 {source!r}。"
                "若为摄像头，请在 系统设置 → 隐私与安全性 → 摄像头 中授权当前终端。"
            )

        if not self.is_file:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.declared_fps = self.cap.get(cv2.CAP_PROP_FPS) or fps
        self._file_t = 0.0
        self._file_dt = 1.0 / max(self.declared_fps, 1.0)

    def read(self):
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        if self.flip:
            frame = cv2.flip(frame, 1)
        if self.is_file:
            # 文件源使用"虚拟时钟"，让检测逻辑按视频自身时间线推进
            self._file_t += self._file_dt
            return self._file_t, frame
        return time.time(), frame

    def close(self):
        if self.cap is not None:
            self.cap.release()


class SyntheticSource:
    """合成场景：空背景 → 站立 → 快速倒地 → 静止躺卧。

    用于无摄像头环境下的端到端自检，时间线由虚拟时钟驱动，不依赖真实耗时。
    """

    def __init__(self, fps: int = 15, width: int = 640, height: int = 360,
                 warmup_s: float = 2.0, stand_s: float = 3.0,
                 fall_s: float = 0.7, lie_s: float = 14.0):
        self.fps = fps
        self.w, self.h = width, height
        self.dt = 1.0 / fps
        self.t = 0.0
        self.warmup_s = warmup_s
        self.stand_s = stand_s
        self.fall_s = fall_s
        self.total = warmup_s + stand_s + fall_s + lie_s
        rng = np.random.default_rng(7)
        self.bg = (rng.integers(28, 42, size=(height, width, 3))).astype(np.uint8)

    def read(self):
        if self.t > self.total:
            return None
        frame = self.bg.copy()
        t = self.t
        if t >= self.warmup_s:
            phase_t = t - self.warmup_s
            if phase_t < self.stand_s:
                prog = 0.0
            elif phase_t < self.stand_s + self.fall_s:
                prog = (phase_t - self.stand_s) / self.fall_s
            else:
                prog = 1.0
            prog = min(max(prog, 0.0), 1.0)
            # 站立：窄而高；倒地：宽而扁，并下移到画面下部
            pw = int(60 + 150 * prog)
            ph = int(230 - 160 * prog)
            cx = int(self.w * 0.5)
            cy = int(self.h * 0.52 + self.h * 0.30 * prog)
            x1, y1 = cx - pw // 2, cy - ph // 2
            cv2.rectangle(frame, (x1, y1), (x1 + pw, y1 + ph), (205, 200, 195), -1)
            # 轻微噪声，避免完全静止帧让运动能量恒为 0
            jitter = int(1 + 2 * math.sin(t * 9.0))
            cv2.rectangle(frame, (x1 + jitter, y1), (x1 + pw + jitter, y1 + 4),
                          (180, 178, 175), -1)
        self.t += self.dt
        return self.t, frame

    def close(self):
        return None


def open_source(cfg):
    """按 camera.source 创建视频源。

    支持：0/1=摄像头序号，"demo"=合成自检，"screen"/"screen:N"=屏幕捕获，
    其余按视频文件路径处理。
    """
    src = cfg.camera.source
    token = str(src).strip().lower() if isinstance(src, str) else src

    if token in {"synthetic", "demo"}:
        return SyntheticSource(fps=int(cfg.camera.fps))

    if isinstance(token, str) and token.startswith("screen"):
        from .screen import ScreenSource

        # screen 或 screen:1 —— 冒号后为 avfoundation 设备号
        device = token.split(":", 1)[1] if ":" in token else "1"
        crop = cfg.get("camera.screen_crop") or None
        if crop and len(crop) == 4:
            crop = tuple(float(v) for v in crop)
        else:
            crop = None
        return ScreenSource(device=device, width=int(cfg.camera.width),
                            fps=int(cfg.camera.fps), crop=crop)

    return CameraSource(src, int(cfg.camera.width), int(cfg.camera.height),
                        int(cfg.camera.fps), bool(cfg.camera.flip))


class RingBuffer:
    """时间戳 + JPEG 的滚动缓冲。"""

    def __init__(self, seconds: float, fps: int, quality: int = 70):
        self.maxlen = max(int(seconds * fps) + 8, 16)
        self.quality = quality
        self._buf: collections.deque = collections.deque(maxlen=self.maxlen)

    def push(self, ts: float, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        if w > BUFFER_WIDTH:
            scale = BUFFER_WIDTH / w
            frame = cv2.resize(frame, (BUFFER_WIDTH, max(int(h * scale), 2)),
                               interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if ok:
            self._buf.append((ts, enc.tobytes()))

    def window(self, t_start: float, t_end: float) -> list[tuple[float, bytes]]:
        return [(ts, data) for ts, data in list(self._buf) if t_start <= ts <= t_end]

    def nearest(self, ts: float) -> tuple[float, bytes] | None:
        items = list(self._buf)
        if not items:
            return None
        return min(items, key=lambda item: abs(item[0] - ts))

    def __len__(self) -> int:
        return len(self._buf)


def decode(frames: Iterable[tuple[float, bytes]]):
    for ts, data in frames:
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            yield ts, img
