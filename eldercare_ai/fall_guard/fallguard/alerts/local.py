# -*- coding: utf-8 -*-
"""本机告警：语音播报、告警音、倒计时取消窗口。

取消窗口是压制误报扰人的关键机制：判定成立后先本地报警，
给现场一个按键取消的机会，只有窗口走完才真正外呼。
窗口内的取消信号可以来自终端回车，也可以来自预览窗按 C 键
（预览窗在主循环里读键，通过 cancel() 注入）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

IS_MAC = sys.platform == "darwin"


class LocalAlerter:
    def __init__(self, cfg):
        a = cfg.alert
        self.speak_enabled = bool(a.speak)
        self.sound_enabled = bool(a.alarm_sound)
        self.voice = (a.voice or "").strip()
        self._procs: list[subprocess.Popen] = []

    # -------------------------------------------------------------- 语音
    def speak(self, text: str, blocking: bool = False) -> None:
        if not self.speak_enabled or not text:
            return
        cmd: list[str] | None = None
        if IS_MAC and shutil.which("say"):
            cmd = ["say"]
            if self.voice:
                cmd += ["-v", self.voice]
            cmd.append(text)
        elif shutil.which("espeak-ng"):
            cmd = ["espeak-ng", "-v", "zh", text]
        elif shutil.which("spd-say"):
            cmd = ["spd-say", text]
        if cmd is None:
            print(f"[语音] {text}")
            return
        try:
            if blocking:
                subprocess.run(cmd, check=False, timeout=25)
            else:
                self._procs.append(subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except Exception as exc:
            print(f"[语音失败] {exc}；文本：{text}")

    # -------------------------------------------------------------- 告警音
    def alarm(self, times: int = 3) -> None:
        if not self.sound_enabled:
            return

        def run():
            for _ in range(times):
                played = False
                if IS_MAC and shutil.which("afplay"):
                    for path in ("/System/Library/Sounds/Sosumi.aiff",
                                 "/System/Library/Sounds/Ping.aiff"):
                        try:
                            subprocess.run(["afplay", path], check=True, timeout=6)
                            played = True
                            break
                        except Exception:
                            continue
                elif shutil.which("paplay"):
                    for path in ("/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                                 "/usr/share/sounds/freedesktop/stereo/bell.oga"):
                        try:
                            subprocess.run(["paplay", path], check=True, timeout=6)
                            played = True
                            break
                        except Exception:
                            continue
                if not played:
                    # 终端响铃作为最后兜底
                    sys.stdout.write("\a")
                    sys.stdout.flush()
                    time.sleep(0.45)

        threading.Thread(target=run, daemon=True).start()

    # -------------------------------------------------------------- 弹窗
    def notify(self, title: str, message: str) -> None:
        try:
            if IS_MAC and shutil.which("osascript"):
                script = (f'display notification "{message}" '
                          f'with title "{title}" sound name "Sosumi"')
                subprocess.Popen(["osascript", "-e", script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "-u", "critical", title, message],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def close(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        self._procs.clear()


class CancelWindow:
    """倒计时取消窗口。

    同时监听终端回车与外部注入的取消信号（预览窗按键）。
    """

    def __init__(self, seconds: float, on_tick: Callable[[float], None] | None = None):
        self.seconds = max(float(seconds), 0.0)
        self.on_tick = on_tick
        self._cancelled = threading.Event()
        self._reader: threading.Thread | None = None

    def cancel(self, _reason: str = "") -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _watch_stdin(self) -> None:
        try:
            if sys.stdin is None or sys.stdin.closed:
                return
            # 不限定 tty：管道输入也应能取消（便于脚本化与联调）。
            # /dev/null 会立即 EOF 返回空串，不会误触发。
            line = sys.stdin.readline()
            if line:
                self._cancelled.set()
        except Exception:
            return

    def run(self, poll: Callable[[], None] | None = None) -> bool:
        """阻塞等待窗口结束。返回 True 表示被取消。

        poll 在每个 tick 被调用一次，用于让主循环继续抽帧（后置证据不能断）。
        """
        if self.seconds <= 0:
            return False
        self._reader = threading.Thread(target=self._watch_stdin, daemon=True)
        self._reader.start()

        deadline = time.time() + self.seconds
        last_shown = -1
        while time.time() < deadline:
            if self._cancelled.is_set():
                return True
            remain = deadline - time.time()
            if int(remain) != last_shown:
                last_shown = int(remain)
                if self.on_tick:
                    self.on_tick(remain)
            if poll:
                poll()
            time.sleep(0.05)
        return self._cancelled.is_set()
