#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守望者 FallGuard 命令行入口。

  python3 main.py web                      打开 Web 控制台（配置 + 监控 + 复核）
  python3 main.py run                     摄像头实时监测
  python3 main.py run --source demo        合成场景自检（无需摄像头）
  python3 main.py run --source clip.mp4    回放视频文件
  python3 main.py doctor                   环境与配置体检
  python3 main.py call-test                外呼链路连通性测试
  python3 main.py events                   查看事件记录
  python3 main.py label EV... --false      标注误报
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fallguard import config as cfgmod


def cmd_run(args) -> int:
    from fallguard.pipeline import FallGuard

    cfg = cfgmod.load(args.config)
    if args.source is not None:
        cfg.raw()["camera"]["source"] = args.source
    if args.no_call:
        cfg.raw()["call"]["dry_run"] = True
    if args.cancel_seconds is not None:
        cfg.raw()["alert"]["cancel_seconds"] = args.cancel_seconds
    if args.no_agent:
        cfg.raw()["agent"]["enabled"] = False
    if args.quiet:
        cfg.raw()["alert"]["speak"] = False
        cfg.raw()["alert"]["alarm_sound"] = False

    guard = FallGuard(cfg, show=not args.headless, force_backend=args.backend)
    stats = guard.run(max_seconds=args.max_seconds)
    return 0 if stats.frames > 0 else 1


def cmd_web(args) -> int:
    from fallguard.web import serve

    return serve(host=args.host, port=args.port, config_path=args.config,
                 open_browser=not args.no_browser)


def cmd_doctor(args) -> int:
    import importlib.util
    import shutil

    print("=" * 62)
    print("守望者 FallGuard · 环境体检")
    print("=" * 62)

    print(f"\nPython      {sys.version.split()[0]}  ({sys.platform})")
    src = cfgmod.config_source(args.config)
    print(f"配置文件     {src}  {'存在' if src.exists() else '缺失'}")

    print("\n[依赖]")
    try:
        import cv2
        print(f"  opencv       {cv2.__version__}")
    except Exception as exc:
        print(f"  opencv       缺失 ({exc}) → pip3 install opencv-python")
        return 1

    from fallguard.pose import available
    have = available()
    for name, ok in have.items():
        note = {"mediapipe": "骨骼点最准，推荐", "yolo": "多人场景更好，依赖 torch",
                "motion": "零依赖兜底，无骨骼点"}[name]
        print(f"  {name:12s} {'可用' if ok else '未安装'}　{note}")
    if not have["mediapipe"] and not have["yolo"]:
        print("    提示：pip3 install mediapipe   可显著提升判定精度")

    print("\n[语音与通知]")
    for tool, desc in (("say", "语音播报"), ("afplay", "告警音"),
                       ("osascript", "系统通知")):
        print(f"  {tool:12s} {'可用' if shutil.which(tool) else '缺失'}　{desc}")

    cfg = cfgmod.load(args.config)

    print("\n[摄像头]")
    try:
        from fallguard.capture import open_source
        source = open_source(cfg)
        item = source.read()
        if item is None:
            print("  打开成功但读帧失败——检查是否被其他程序占用")
        else:
            h, w = item[1].shape[:2]
            print(f"  读帧成功 {w}x{h}  source={cfg.camera.source}")
        source.close()
    except Exception as exc:
        print(f"  失败：{exc}")
        print("  macOS 需在 系统设置 → 隐私与安全性 → 摄像头 中授权终端")
        print("  若终端宿主无法获得授权，改用屏幕捕获：")
        print("    python3 main.py run --source screen")
        print("  或无摄像头自检：python3 main.py run --source demo")

    print("\n[屏幕捕获后备]")
    try:
        from fallguard.screen import list_devices
        print("  avfoundation 设备：")
        for line in list_devices().splitlines():
            print(f"    {line}")
    except Exception as exc:
        print(f"  不可用：{exc}")

    print("\n[外呼]")
    from fallguard.alerts import CallGateway
    gw = CallGateway(cfg)
    print(f"  通道         {gw.describe()}")
    for c in gw.contacts:
        print(f"    - {c.id:16s} {c.name}（{c.role}）")
    lack = gw.provider.missing()
    if lack:
        print(f"  待补配置     {', '.join(lack)}")
    if gw.dry_run:
        print("  当前为演练模式，不会真实拨号；联调通过后将 call.dry_run 改为 false")

    print("\n[智能体]")
    a = cfg.agent
    if not bool(a.enabled):
        print("  已关闭，将使用确定性处置（规则 planner）")
    elif not str(a.api_key):
        print(f"  已开启但缺少 api_key，将降级为确定性处置")
        print(f"  设置方式：export DEEPSEEK_API_KEY=sk-xxx")
    else:
        print(f"  {a.model} @ {a.base_url}　deadline={a.deadline_seconds}s")

    print("\n[判定阈值]")
    d = cfg.detector
    print(f"  躺倒角度 ≥{d.lying_angle_deg}°　下降速率 ≥{d.descent_rate_min}/s")
    print(f"  双条件保持 {d.lying_seconds}s　单条件保持 {d.lying_only_seconds}s")
    print(f"  静止阈值 ≤{d.static_motion_max}　静默期 {d.cooldown_seconds}s")
    print("\n体检完成。")
    return 0


def cmd_call_test(args) -> int:
    import time

    from fallguard.alerts import CallGateway

    cfg = cfgmod.load(args.config)
    if args.real:
        cfg.raw()["call"]["dry_run"] = False
    gw = CallGateway(cfg)
    print(f"[外呼测试] {gw.describe()}")
    if not gw.contacts:
        print("未配置联系人，请在 config.toml 的 [[call.contacts]] 中添加")
        return 1

    slots = {
        "name": str(cfg.scene.resident_name),
        "time": time.strftime("%H点%M分"),
        "place": str(cfg.scene.place),
        "event": "跌倒",
    }
    targets = [gw.contact_by_id(args.contact)] if args.contact else gw.contacts[:1]
    if targets[0] is None:
        print(f"未找到联系人 id={args.contact}")
        print("可用：" + ", ".join(c.id for c in gw.contacts))
        return 1

    for contact in targets:
        print(f"\n→ {contact.name}（{contact.role}）")
        res = gw.call_one(contact, slots, "CALLTEST")
        print(f"  结果 ok={res.ok} code={res.code} message={res.message}")
        if res.params:
            print(f"  请求参数 {res.params}")
    if gw.dry_run:
        print("\n以上为演练输出。确认参数无误后，用 --real 发起真实拨号。")
    return 0


def cmd_events(args) -> int:
    from fallguard.events import EventLog

    cfg = cfgmod.load(args.config)
    log = EventLog(cfg.path(cfg.get("log.dir", "data")))
    rows = log.events(limit=args.limit)
    if not rows:
        print("暂无事件记录。")
        return 0

    print(f"{'事件号':<24}{'时间':<22}{'等级':<6}{'触发':<16}{'结果':<12}标注")
    print("-" * 104)
    for r in rows:
        label = r.get("label") or {}
        mark = "误报" if label.get("is_false_alarm") else ("正常" if label else "-")
        print(f"{r.get('event_id',''):<24}{r.get('detected_at','')[:19]:<22}"
              f"{r.get('level',''):<6}{r.get('trigger',''):<16}"
              f"{r.get('outcome',''):<12}{mark}")
    s = log.stats()
    print(f"\n合计 {s['total']} 起　已外呼 {s['called']}　人工取消 {s['cancelled']}　"
          f"标注误报 {s['labelled_false_alarm']}　未标注 {s['unlabelled']}")
    if args.detail:
        import json
        target = next((r for r in rows if r.get("event_id") == args.detail), None)
        if target is None:
            print(f"\n未找到 {args.detail}")
            return 1
        print("\n" + json.dumps(target, ensure_ascii=False, indent=2))
    return 0


def cmd_label(args) -> int:
    from fallguard.events import EventLog

    cfg = cfgmod.load(args.config)
    log = EventLog(cfg.path(cfg.get("log.dir", "data")))
    payload = log.label(args.event_id, is_false_alarm=not args.true,
                        reason=args.reason or "")
    kind = "误报" if payload["is_false_alarm"] else "真实事件"
    print(f"已标注 {args.event_id} 为 {kind}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="fallguard",
        description="守望者 FallGuard · 摄像头跌倒预警与自动外呼",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("-c", "--config", help="配置文件路径")
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("web", help="打开 Web 控制台")
    w.add_argument("--host", default="127.0.0.1",
                   help="监听地址。默认只监听本机——面板没有鉴权，"
                        "改成 0.0.0.0 等于把监控画面暴露给同网段")
    w.add_argument("--port", type=int, default=8777, help="端口，默认 8777")
    w.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    w.set_defaults(func=cmd_web)

    r = sub.add_parser("run", help="启动实时监测")
    r.add_argument("--source",
                   help='0/1=摄像头序号，demo=合成自检，screen[:N]=屏幕捕获，'
                        '或视频文件路径')
    r.add_argument("--backend", choices=["auto", "mediapipe", "yolo", "motion"],
                   help="强制指定姿态后端")
    r.add_argument("--headless", action="store_true", help="不开预览窗")
    r.add_argument("--no-call", action="store_true", help="强制演练模式，不真实拨号")
    r.add_argument("--no-agent", action="store_true", help="关闭 LLM，仅用确定性处置")
    r.add_argument("--quiet", action="store_true", help="静音（不播报不响铃）")
    r.add_argument("--cancel-seconds", type=float, help="覆盖取消窗口时长")
    r.add_argument("--max-seconds", type=float, help="运行指定秒数后自动退出")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("doctor", help="环境与配置体检")
    d.set_defaults(func=cmd_doctor)

    ct = sub.add_parser("call-test", help="外呼链路测试")
    ct.add_argument("--contact", help="指定联系人 id")
    ct.add_argument("--real", action="store_true", help="真实拨号（会产生费用）")
    ct.set_defaults(func=cmd_call_test)

    ev = sub.add_parser("events", help="查看事件记录")
    ev.add_argument("--limit", type=int, default=20)
    ev.add_argument("--detail", help="打印指定事件的完整 JSON")
    ev.set_defaults(func=cmd_events)

    lb = sub.add_parser("label", help="标注事件")
    lb.add_argument("event_id")
    lb.add_argument("--false", action="store_true", help="标为误报（默认）")
    lb.add_argument("--true", action="store_true", help="标为真实事件")
    lb.add_argument("--reason", help="标注理由")
    lb.set_defaults(func=cmd_label)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
