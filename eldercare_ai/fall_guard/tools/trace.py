#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐帧打印判定特征，用于调阈值与排查漏报/误报。

  python3 tools/trace.py --source demo
  python3 tools/trace.py --source clip.mp4 --backend motion
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fallguard import config as cfgmod
from fallguard.capture import open_source
from fallguard.detector import FallDetector
from fallguard.pose import create as create_backend


def main() -> int:
    p = argparse.ArgumentParser(description="逐帧打印跌倒判定特征")
    p.add_argument("-c", "--config")
    p.add_argument("--source")
    p.add_argument("--backend")
    p.add_argument("--every", type=int, default=3, help="每 N 帧打印一行")
    p.add_argument("--max-frames", type=int, default=600)
    args = p.parse_args()

    cfg = cfgmod.load(args.config)
    if args.source is not None:
        cfg.raw()["camera"]["source"] = args.source
    source = open_source(cfg)
    backend = create_backend(args.backend or str(cfg.detector.backend))
    det = FallDetector(cfg, int(cfg.camera.fps))

    d = cfg.detector
    print(f"后端={backend.name}  阈值: angle>={d.lying_angle_deg} "
          f"aspect>={d.lying_aspect} drop>={d.descent_rate_min} "
          f"floor_y>={d.floor_y} hold>={d.lying_seconds}s "
          f"motion<={d.static_motion_max}")
    print(f"{'t':>7}{'state':>16}{'score':>7}{'angle':>7}{'ar':>6}"
          f"{'cy':>6}{'drop':>6}{'motion':>8}{'lying':>7}")
    print("-" * 72)

    fired = 0
    n = 0
    while n < args.max_frames:
        item = source.read()
        if item is None:
            break
        ts, frame = item
        n += 1
        people = backend.infer(frame)
        obs, verdict = det.update(ts, frame, people)
        if n % args.every == 0 or verdict is not None:
            ang = f"{obs.torso_angle:6.1f}" if obs.torso_angle is not None else "   n/a"
            ar = f"{obs.aspect:5.2f}" if obs.aspect is not None else "  n/a"
            cy = f"{obs.centroid[1]:5.2f}" if obs.centroid else "  n/a"
            sc = f"{obs.person.score:6.2f}" if obs.person else "   n/a"
            print(f"{ts:7.2f}{obs.state:>16}{sc}{ang}{ar}{cy}"
                  f"{obs.descent_rate:6.2f}{obs.motion:8.4f}{obs.lying_for:7.1f}")
        if verdict is not None:
            fired += 1
            print(f"  >>> 判定成立 trigger={verdict.trigger} "
                  f"conf={verdict.confidence:.2f}")
            print(f"      {verdict.features}")

    source.close()
    backend.close()
    print(f"\n共 {n} 帧，判定成立 {fired} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
