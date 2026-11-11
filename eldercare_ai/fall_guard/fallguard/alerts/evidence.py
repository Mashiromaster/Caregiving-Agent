# -*- coding: utf-8 -*-
"""现场留证：关键帧截图 + 跌倒前后短视频。

落证分两步，因为"跌倒后 10 秒"在判定成立的那一刻还没发生：
  1. capture_pre()  判定瞬间立刻把回溯帧与关键帧写盘（即使进程随后被杀也有证据）
  2. finalize()     后置窗口收集完毕后合成完整 mp4
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..capture import RingBuffer, decode


@dataclass
class EvidenceBundle:
    dir: Path
    keyframes: list[str] = field(default_factory=list)
    clip: str | None = None
    pre_frames: int = 0
    post_frames: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir": str(self.dir),
            "keyframes": self.keyframes,
            "clip": self.clip,
            "pre_frames": self.pre_frames,
            "post_frames": self.post_frames,
        }


class EvidenceRecorder:
    def __init__(self, cfg, fps: int):
        ev = cfg.evidence
        self.root = cfg.path(ev.dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pre_s = float(ev.pre_seconds)
        self.post_s = float(ev.post_seconds)
        self.offsets = [float(o) for o in (ev.keyframe_offsets or [0.0])]
        self.fps = max(fps, 1)
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------- 判定瞬间
    def capture_pre(self, event_id: str, t0: float, buffer: RingBuffer,
                    frame_now: np.ndarray) -> EvidenceBundle:
        out_dir = self.root / event_id
        out_dir.mkdir(parents=True, exist_ok=True)
        bundle = EvidenceBundle(dir=out_dir)

        pre = buffer.window(t0 - self.pre_s, t0 + 0.05)
        bundle.pre_frames = len(pre)

        # 判定瞬间的原始帧一定要存，它是最具说服力的单张证据
        hit_path = out_dir / "keyframe_t+0.0s.jpg"
        cv2.imwrite(str(hit_path), frame_now, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        bundle.keyframes.append(hit_path.name)

        # 回溯关键帧：跌倒前 1s / 2s / 3s 各一张，用于复核下降过程
        for back in (1.0, 2.0, 3.0):
            if back > self.pre_s:
                continue
            item = buffer.nearest(t0 - back)
            if item is None:
                continue
            img = cv2.imdecode(np.frombuffer(item[1], np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            name = f"keyframe_t-{back:.1f}s.jpg"
            cv2.imwrite(str(out_dir / name), img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            bundle.keyframes.append(name)

        self._pending[event_id] = {"t0": t0, "pre": pre, "post": [], "bundle": bundle}
        return bundle

    # ------------------------------------------------------- 后置窗口
    def feed(self, event_id: str, ts: float, frame: np.ndarray) -> bool:
        """持续喂入后置帧；返回 True 表示该事件的后置窗口已满。"""
        job = self._pending.get(event_id)
        if job is None:
            return True
        t0 = job["t0"]
        if ts - t0 > self.post_s:
            return True

        bundle: EvidenceBundle = job["bundle"]
        for off in self.offsets:
            if off <= 0:
                continue
            name = f"keyframe_t+{off:.1f}s.jpg"
            if name in bundle.keyframes:
                continue
            if ts - t0 >= off:
                cv2.imwrite(str(bundle.dir / name), frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                bundle.keyframes.append(name)

        small = frame
        h, w = frame.shape[:2]
        if w > 640:
            scale = 640 / w
            small = cv2.resize(frame, (640, max(int(h * scale), 2)),
                               interpolation=cv2.INTER_AREA)
        job["post"].append((ts, small))
        return False

    def finalize(self, event_id: str, record_meta: dict[str, Any] | None = None
                 ) -> EvidenceBundle | None:
        job = self._pending.pop(event_id, None)
        if job is None:
            return None
        bundle: EvidenceBundle = job["bundle"]
        frames: list[np.ndarray] = []
        for _ts, img in decode(job["pre"]):
            frames.append(img)
        for _ts, img in job["post"]:
            frames.append(img)
        bundle.post_frames = len(job["post"])

        if frames:
            target = frames[-1].shape[1], frames[-1].shape[0]
            clip_path = bundle.dir / "clip.mp4"
            writer = cv2.VideoWriter(str(clip_path),
                                     cv2.VideoWriter_fourcc(*"mp4v"),
                                     float(self.fps), target)
            if writer.isOpened():
                for img in frames:
                    if (img.shape[1], img.shape[0]) != target:
                        img = cv2.resize(img, target, interpolation=cv2.INTER_AREA)
                    writer.write(img)
                writer.release()
                bundle.clip = clip_path.name
            else:
                writer.release()

        if record_meta is not None:
            with open(bundle.dir / "event.json", "w", encoding="utf-8") as fh:
                json.dump(record_meta, fh, ensure_ascii=False, indent=2)
        return bundle

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def bundle_of(self, event_id: str) -> EvidenceBundle | None:
        job = self._pending.get(event_id)
        return job["bundle"] if job else None
