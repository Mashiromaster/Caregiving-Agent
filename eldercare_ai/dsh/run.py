#!/usr/bin/env python3
"""Start the official DeepSeek Harness with the Caregiving plugin."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DSH = ROOT / "node_modules" / ".bin" / "dsh"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if not DSH.is_file():
        parser.error("先在 eldercare_ai/dsh 目录执行 npm ci")

    with tempfile.TemporaryDirectory(prefix="caregiving-dsh-") as tmp:
        patch = Path(tmp) / "cordis.patch.yml"
        patch.write_text("- insert:\n    - id: caregiving-fallguard\n"
                         f"      name: {json.dumps(str(ROOT / 'fallguard-plugin.mjs'))}\n",
                         encoding="utf-8")
        env = dict(os.environ)
        env["FALLGUARD_PYTHON"] = sys.executable
        command = [str(DSH), "web", "--patch", str(patch)]
        if args.no_open:
            command.append("--no-open")
        print("守望者 Agent 入口：http://127.0.0.1:3080", flush=True)
        process = subprocess.Popen(command, cwd=ROOT.parent / "fall_guard", env=env)
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
