#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 Spug 语音模板配置是否能和本系统对接。

  python3 tools/check_spug.py A27L0abcbgEY               # 只看参数，不拨号
  python3 tools/check_spug.py A27L0abcbgEY --real 138... # 真实拨号到指定号码

它能发现的问题：
  · 模板编码写错 / 模板不存在
  · 模板变量名与系统能提供的槽位对不上（最常见，会播成空值）
  · 模板通道不是电话（配成短信/微信了，不会响铃）
  · targets 没生效（接收人被模板写死，升级链会失效）

为什么要单独做这个：Spug 的模板内容在平台侧，本地看不到，只能靠
实际调用的返回来反推。而演练模式不发请求，验证不了这些。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "https://push.spug.cc"
SLOT_KEYS = ("name", "time", "place", "event")


def post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300]}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    p = argparse.ArgumentParser(description="检查 Spug 语音模板能否对接本系统")
    p.add_argument("template_id", help="Spug 模板编码（调用 URL 里 /send/ 后那段）")
    p.add_argument("--real", metavar="PHONE",
                   help="真实拨号到该号码（会产生费用并真的响铃）")
    p.add_argument("--keys", default="name,time,place",
                   help="模板里用到的变量名，逗号分隔，默认 name,time,place")
    args = p.parse_args()

    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    print("=" * 62)
    print("Spug 语音模板体检")
    print("=" * 62)

    # 1. 变量名是否在系统能提供的槽位内
    print(f"\n[1] 模板变量与系统槽位")
    print(f"  系统能提供：{' / '.join(SLOT_KEYS)}")
    print(f"  你填写的  ：{' / '.join(keys)}")
    bad = [k for k in keys if k not in SLOT_KEYS]
    if bad:
        print(f"  ✗ {', '.join(bad)} 不在槽位内，这些变量会播成空值")
        print(f"    改用 {' / '.join(SLOT_KEYS)} 中的名字，并同步改 Spug 模板")
    else:
        print("  ✓ 全部可填充")

    slots = {"name": "张**", "time": time.strftime("%H点%M分"),
             "place": "3号楼412房间", "event": "跌倒"}
    body = {k: slots.get(k, "") for k in keys}

    # 2. 实际调用
    url = f"{BASE}/send/{args.template_id}"
    print(f"\n[2] 调用 {url}")
    if args.real:
        digits = "".join(ch for ch in args.real if ch.isdigit())
        body["targets"] = digits
        print(f"  targets={digits[:3]}****{digits[-4:]}（真实拨号）")
    else:
        print("  未加 --real，仅验证模板是否存在；不会拨号")
        print(f"  请求体：{json.dumps(body, ensure_ascii=False)}")
        print("\n  要真正验证（会响铃、产生费用）：")
        print(f"    python3 tools/check_spug.py {args.template_id} --real 你的手机号")
        return 0

    status, resp = post(url, body)
    print(f"  HTTP {status}  返回 {json.dumps(resp, ensure_ascii=False)}")

    # 3. 判读
    print("\n[3] 判读")
    code = resp.get("code")
    if code == 200:
        print("  ✓ 提交成功，电话应在几秒内打到。请确认三件事：")
        print("    a) 真的响铃了吗？没响说明模板通道不是「电话」，去控制台改")
        print("    b) 播报内容完整吗？出现空白说明变量名对不上")
        print("    c) 打到的是你刚填的号码吗？若是别的号，"
              "说明模板把接收人写死了，targets 没生效——")
        print("       这会导致升级链失效（永远只呼同一个人）")
        print(f"\n  都正常的话，在配置页填入：")
        print(f"    外呼通道   = spug")
        print(f"    模板编码   = {args.template_id}")
        print(f"    模板变量名 = {' / '.join(keys)}")
        return 0

    print(f"  ✗ 失败：{resp.get('msg') or resp}")
    hints = {
        "模板不存在": "模板编码写错了，去控制台复制 URL 里 /send/ 后面那段",
        "余额": "账户余额不足，语音电话按次计费，需要充值",
        "频率": "触发了频率限制，等几分钟再试",
        "参数": "变量名与模板里的不一致，核对控制台模板内容",
    }
    msg = str(resp.get("msg", ""))
    for key, hint in hints.items():
        if key in msg:
            print(f"    → {hint}")
            break
    else:
        print("    → 去控制台核对：模板是否已启用、通道是否为「电话」、账户是否有余额")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
