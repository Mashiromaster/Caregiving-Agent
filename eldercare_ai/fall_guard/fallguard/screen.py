# -*- coding: utf-8 -*-
"""屏幕捕获源（macOS）：绕过摄像头 TCC 授权的后备输入。

背景：当终端宿主 App 的安装路径带哈希/.noindex 时，macOS TCC 难以稳定授予
摄像头权限，请求常被静默拒绝。而 avfoundation 的 "Capture screen N" 设备
走的是屏幕录制权限，通常可用。于是：把手机摄像头画面或 Photo Booth 窗口
放到屏幕上，本系统从屏幕读帧——检测与处置链路完全不变。

实现要点（都是踩过的坑）：
  · 必须 -pixel_format uyvy422，屏幕设备不支持 yuv420p
  · 必须持续从 stdout 读帧并丢弃积压，绝不写文件：
    一次失败的落盘尝试在十几秒内写出了 296GB 稀疏文件
  · 必须 -nostdin，否则 ffmpeg 会抢终端输入，取消窗口的回车就失灵了
  · 进程须在退出时确保回收，避免遗留后台 ffmpeg 持续占用屏幕录制
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

import numpy as np


class ScreenSource:
    """从 avfoundation 屏幕设备读取 BGR 帧。"""

    def __init__(self, device: str = "1", width: int = 960, fps: int = 10,
                 crop: tuple[float, float, float, float] | None = None):
        if sys.platform != "darwin":
            raise RuntimeError("屏幕捕获源目前仅支持 macOS")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("未找到 ffmpeg：brew install ffmpeg")

        self.fps = max(int(fps), 1)
        self.width = int(width)
        self._proc: subprocess.Popen | None = None
        self._frame_bytes = 0
        self._shape: tuple[int, int, int] | None = None

        probe = self._probe(device, crop)
        if probe is None:
            raise RuntimeError(
                f"无法从屏幕设备 {device!r} 读帧。请在 "
                "系统设置 → 隐私与安全性 → 屏幕录制 中授权终端，"
                "或改用 --source demo 自检。")
        h, w = probe
        self._shape = (h, w, 3)
        self._frame_bytes = h * w * 3
        self._start(device, crop)

    # ----------------------------------------------------------- 探测尺寸
    def _filters(self, crop) -> str:
        parts = []
        if crop:
            x, y, cw, ch = crop
            parts.append(f"crop=iw*{cw}:ih*{ch}:iw*{x}:ih*{y}")
        parts.append(f"scale={self.width}:-2")
        return ",".join(parts)

    def _cmd(self, device: str, crop, frames: int | None) -> list[str]:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "avfoundation",
            "-pixel_format", "uyvy422",       # 屏幕设备不支持 yuv420p
            "-framerate", str(self.fps),
            "-capture_cursor", "0",
            "-i", device,
            "-vf", self._filters(crop),
            "-pix_fmt", "bgr24",
        ]
        if frames:
            # 探测阶段用 -frames:v 限帧；-t 对 rawvideo 输出不可靠
            cmd += ["-frames:v", str(frames)]
        cmd += ["-f", "rawvideo", "pipe:1"]
        return cmd

    def _probe(self, device: str, crop) -> tuple[int, int] | None:
        """抓 1 帧确定实际分辨率（屏幕设备会忽略请求的尺寸）。"""
        proc = subprocess.Popen(self._cmd(device, crop, frames=1),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, _err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        if not out:
            return None
        # 已知宽度与 3 通道，反推高度；scale=-2 保证高为偶数
        total = len(out)
        if total % (self.width * 3) != 0:
            return None
        h = total // (self.width * 3)
        return (h, self.width) if h > 32 else None

    # ----------------------------------------------------------- 持续读流
    def _start(self, device: str, crop) -> None:
        self._proc = subprocess.Popen(
            self._cmd(device, crop, frames=None),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=self._frame_bytes * 2)

    def read(self):
        if self._proc is None or self._proc.stdout is None:
            return None
        buf = self._proc.stdout.read(self._frame_bytes)
        if not buf or len(buf) < self._frame_bytes:
            return None
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(self._shape)
        return time.time(), frame.copy()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdout:
                proc.stdout.close()
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass

    def __del__(self):
        # 兜底回收：绝不留下后台 ffmpeg 持续占用屏幕录制
        try:
            self.close()
        except Exception:
            pass


def list_devices() -> str:
    if not shutil.which("ffmpeg"):
        return "未找到 ffmpeg"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, timeout=20)
    lines = [ln for ln in (proc.stderr or "").splitlines()
             if "] [" in ln or "devices:" in ln]
    return "\n".join(ln.split("] ", 1)[-1] for ln in lines)
