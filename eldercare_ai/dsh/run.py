#!/usr/bin/env python3
"""Start FallGuard's local API and the official DeepSeek Harness chat UI."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FALLGUARD = ROOT.parent / "fall_guard"
DSH = ROOT / "node_modules" / ".bin" / "dsh"
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for_backend(proc: subprocess.Popen, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"FallGuard API exited with status {proc.returncode}")
        try:
            with LOCAL_HTTP.open("http://127.0.0.1:8777/api/status", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("FallGuard API did not become ready on 127.0.0.1:8777")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if not DSH.is_file():
        parser.error("先在 eldercare_ai/dsh 目录执行 npm install")

    backend = subprocess.Popen([
        sys.executable, "main.py", "web", "--host", "127.0.0.1",
        "--port", "8777", "--no-browser",
    ], cwd=FALLGUARD)
    dsh = None
    try:
        wait_for_backend(backend)
        with tempfile.TemporaryDirectory(prefix="caregiving-dsh-") as tmp:
            patch = Path(tmp) / "cordis.patch.yml"
            # JSON strings are also valid YAML scalars; this handles spaces in paths.
            patch.write_text("- insert:\n    - id: caregiving-fallguard\n"
                             f"      name: {json.dumps(str(ROOT / 'fallguard-plugin.mjs'))}\n",
                             encoding="utf-8")
            env = dict(os.environ)
            env["FALLGUARD_API_URL"] = "http://127.0.0.1:8777"
            command = [str(DSH), "web", "--patch", str(patch)]
            if args.no_open:
                command.append("--no-open")
            print("FallGuard API: http://127.0.0.1:8777")
            print("DeepSeek Harness chat: http://127.0.0.1:3080")
            dsh = subprocess.Popen(command, cwd=FALLGUARD, env=env)
            try:
                return dsh.wait()
            except KeyboardInterrupt:
                return 0
    finally:
        for proc in (dsh, backend):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
