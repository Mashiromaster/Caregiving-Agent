#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：覆盖判定状态机、取消窗口、外呼签名、智能体兜底、事件日志。

  python3 tools/selftest.py

不依赖摄像头与网络，可在 CI 中运行。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from fallguard import config as cfgmod
from fallguard.agent import ToolContext, RulePlanner, handle as agent_handle
from fallguard.alerts import CallGateway, CancelWindow, LocalAlerter
from fallguard.capture import RingBuffer, SyntheticSource
from fallguard.detector import FallDetector
from fallguard.events import EventLog, EventRecord, new_event_id
from fallguard.pipeline import _in_window
from fallguard.pose import create as create_backend

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def base_cfg(tmp: Path):
    cfg = cfgmod.load()
    raw = cfg.raw()
    raw["camera"]["source"] = "synthetic"
    raw["alert"]["speak"] = False
    raw["alert"]["alarm_sound"] = False
    raw["call"]["dry_run"] = True
    raw["agent"]["enabled"] = False
    raw["evidence"]["dir"] = str(tmp / "evidence")
    raw["log"]["dir"] = str(tmp)
    return cfg


# ----------------------------------------------------------------- 判定逻辑
def test_detector_fires(tmp: Path) -> None:
    print("\n[1] 判定状态机")
    cfg = base_cfg(tmp)
    src = SyntheticSource(fps=15)
    backend = create_backend("motion")
    det = FallDetector(cfg, 15)
    fired = []
    states = set()
    while True:
        item = src.read()
        if item is None:
            break
        ts, frame = item
        obs, v = det.update(ts, frame, backend.infer(frame))
        states.add(obs.state)
        if v:
            fired.append(v)
    check("合成跌倒场景触发一次", len(fired) == 1, f"实际 {len(fired)} 次")
    if fired:
        v = fired[0]
        check("走双条件路径", v.trigger == "descent+lying", v.trigger)
        check("置信度 >0.7", v.confidence > 0.7, f"{v.confidence:.2f}")
        check("记录下降速率", v.features.get("descent_rate", 0) >= 0.25,
              str(v.features.get("descent_rate")))
        check("判定在地面高度", v.features.get("on_floor") is True)
    check("经历 upright→lying→fall", {"upright", "lying", "fall"} <= states,
          str(sorted(states)))
    check("触发后进入静默期", "cooldown" in states)


def test_detector_no_false_fire(tmp: Path) -> None:
    print("\n[2] 静态场景不误报")
    cfg = base_cfg(tmp)
    backend = create_backend("motion")
    det = FallDetector(cfg, 15)
    rng = np.random.default_rng(3)
    bg = rng.integers(30, 40, size=(360, 640, 3)).astype(np.uint8)
    fired = 0
    for i in range(150):
        frame = bg.copy()
        frame[:] = np.clip(bg + rng.integers(-2, 3, bg.shape), 0, 255).astype(np.uint8)
        _obs, v = det.update(i / 15.0, frame, backend.infer(frame))
        if v:
            fired += 1
    check("空场景 150 帧零触发", fired == 0, f"实际 {fired} 次")


def test_cooldown_clock(tmp: Path) -> None:
    print("\n[3] 静默期时钟（回归：虚拟时钟不得被当作 Unix 时间戳）")
    cfg = base_cfg(tmp)
    det = FallDetector(cfg, 15)
    check("_last_fire 初值为 None", det._last_fire is None)
    src = SyntheticSource(fps=15)
    backend = create_backend("motion")
    got = False
    while True:
        item = src.read()
        if item is None:
            break
        ts, frame = item
        _obs, v = det.update(ts, frame, backend.infer(frame))
        if v:
            got = True
            break
    check("从 t=0 起的时钟仍能触发", got)


def test_exclude_zone(tmp: Path) -> None:
    print("\n[4] 排除区域抑制")
    cfg = base_cfg(tmp)
    cfg.raw()["detector"]["exclude_zones"] = [[0.0, 0.4, 1.0, 1.0]]
    src = SyntheticSource(fps=15)
    backend = create_backend("motion")
    det = FallDetector(cfg, 15)
    fired, suppressed = 0, False
    while True:
        item = src.read()
        if item is None:
            break
        ts, frame = item
        obs, v = det.update(ts, frame, backend.infer(frame))
        if obs.state == "zone_suppressed":
            suppressed = True
        if v:
            fired += 1
    check("命中排除区域", suppressed)
    check("排除区域内不触发", fired == 0, f"实际 {fired} 次")


# ----------------------------------------------------------------- 取消窗口
def test_cancel_window() -> None:
    print("\n[5] 取消窗口")
    w = CancelWindow(0.6)
    t0 = time.time()
    got = w.run()
    check("未取消时走完窗口", got is False and time.time() - t0 >= 0.55)

    w2 = CancelWindow(5.0)
    import threading
    threading.Timer(0.3, w2.cancel).start()
    t1 = time.time()
    got2 = w2.run()
    elapsed = time.time() - t1
    check("注入取消信号立即返回", got2 is True and elapsed < 1.5, f"{elapsed:.2f}s")

    check("窗口为 0 时直接放行", CancelWindow(0).run() is False)


def test_suppress_window() -> None:
    print("\n[6] 抑制时段匹配")
    lt = time.localtime()
    now_hm = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    check("命中当前时段", _in_window(time.time(), [f"{now_hm}-{now_hm}"]) is not None)
    check("不命中无关时段", _in_window(time.time(), ["03:00-03:01"]) is None
          or lt.tm_hour == 3)
    check("跨午夜时段可解析",
          _in_window(time.time(), ["23:00-07:00"]) is not None
          or not (lt.tm_hour >= 23 or lt.tm_hour < 7))
    check("非法格式被忽略", _in_window(time.time(), ["bogus", "9-9"]) is None)


# ----------------------------------------------------------------- 外呼
def test_call_signing(tmp: Path) -> None:
    print("\n[7] 外呼签名与参数")
    from fallguard.alerts.call import AliyunVoice, Contact, TencentVoice

    contact = Contact(id="t", name="测试", phone="13800000001", role="caregiver")
    slots = {"name": "张**", "time": "02点14分", "place": "3号楼412房间"}

    ali = AliyunVoice({"access_key_id": "AKID", "access_key_secret": "SECRET",
                       "tts_code": "TTS_123456", "tts_param_keys":
                       ["name", "time", "place"]}, 2)
    params = ali.build_params(contact, slots, "EVTEST")
    sig = ali._sign(params)
    check("阿里云签名非空且可复现", bool(sig) and sig == ali._sign(params), sig[:12])
    check("TtsParam 含三要素",
          all(k in params["TtsParam"] for k in ("name", "time", "place")))
    check("语速放慢", params.get("Speed") == "-10")

    ten = TencentVoice({"secret_id": "ID", "secret_key": "KEY",
                        "voice_sdk_app_id": "1400", "template_id": "999",
                        "template_param_keys": ["name", "time", "place"]}, 2)
    payload = ten.build_payload(contact, slots)
    headers = ten._headers(payload)
    check("腾讯云 TC3 签名头完整",
          headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=ID/"))
    check("号码规范为 E.164", payload["CalledNumber"] == "+8613800000001",
          payload["CalledNumber"])
    check("模板参数按序填充", payload["TemplateParamSet"] ==
          ["张**", "02点14分", "3号楼412房间"])

    cfg = base_cfg(tmp)
    gw = CallGateway(cfg)
    res = gw.call_one(gw.contacts[0], slots, "EVTEST")
    check("演练模式不发网络请求", res.ok and res.code == "DRY_RUN")
    check("日志中号码已脱敏", "****" in res.to_dict()["phone"],
          res.to_dict()["phone"])


# ----------------------------------------------------------------- 智能体
def test_agent_fallback(tmp: Path) -> None:
    print("\n[8] 智能体与兜底")
    cfg = base_cfg(tmp)
    gw = CallGateway(cfg)
    rec = EventRecord(event_id=new_event_id(), detected_at=time.time(),
                      trigger="descent+lying", confidence=0.85,
                      room="3号楼412房间", resident="张**")
    ctx = ToolContext(record=rec, gateway=gw, alerter=LocalAlerter(cfg),
                      scene={"place": rec.room, "resident_name": rec.resident},
                      slots={"name": rec.resident, "time": "02点14分",
                             "place": rec.room},
                      log=lambda _m: None)
    out = RulePlanner().run(ctx)
    check("规则 planner 完成外呼", any(r.ok for r in ctx.called))
    check("优先呼护理方", ctx.called[0].role == "caregiver", ctx.called[0].role)
    check("动作已留痕", len(rec.actions) >= 3, f"{len(rec.actions)} 条")
    check("工具调用含 place_call",
          any(a["tool"] == "place_call" for a in rec.actions))
    check("给出处置摘要", bool(rec.agent.get("summary")))

    # LLM 不可用时必须兜底
    cfg2 = base_cfg(tmp)
    cfg2.raw()["agent"]["enabled"] = True
    cfg2.raw()["agent"]["api_key"] = ""
    rec2 = EventRecord(event_id=new_event_id(), detected_at=time.time(),
                      trigger="descent+lying", confidence=0.85)
    ctx2 = ToolContext(record=rec2, gateway=CallGateway(cfg2),
                       alerter=LocalAlerter(cfg2), scene={}, slots={},
                       log=lambda _m: None)
    agent_handle(cfg2, ctx2)
    check("无 api_key 时仍完成外呼", any(r.ok for r in ctx2.called))

    # 外呼上限
    cfg3 = base_cfg(tmp)
    cfg3.raw()["call"]["max_calls_per_event"] = 1
    cfg3.raw()["call"]["call_all_contacts"] = True
    rec3 = EventRecord(event_id=new_event_id(), detected_at=time.time())
    ctx3 = ToolContext(record=rec3, gateway=CallGateway(cfg3),
                       alerter=LocalAlerter(cfg3), scene={}, slots={},
                       log=lambda _m: None)
    RulePlanner().run(ctx3)
    check("遵守外呼上限", len(ctx3.called) <= 1, f"{len(ctx3.called)} 通")


def test_tool_guards(tmp: Path) -> None:
    print("\n[9] 工具护栏")
    from fallguard.agent import execute

    cfg = base_cfg(tmp)
    rec = EventRecord(event_id=new_event_id(), detected_at=time.time())
    ctx = ToolContext(record=rec, gateway=CallGateway(cfg),
                      alerter=LocalAlerter(cfg), scene={}, slots={},
                      log=lambda _m: None)
    r1 = execute("place_call", {"contact_id": "nobody", "reason": "x"}, ctx)
    check("未知联系人被拒绝", r1.get("ok") is False and "available" in r1)
    r2 = execute("no_such_tool", {}, ctx)
    check("未知工具被拒绝", r2.get("ok") is False)
    execute("speak", {"text": "测试"}, ctx)
    r3 = execute("speak", {"text": "测试"}, ctx)
    check("重复播报被去重", "skipped" in r3)
    r4 = execute("place_call", {"contact_id": "nurse_station", "reason": "一次"}, ctx)
    r5 = execute("place_call", {"contact_id": "nurse_station", "reason": "重复"}, ctx)
    check("同一联系人不重复呼叫", r4.get("ok") and "skipped" in r5)
    check("异常工具不中断流程", len(rec.actions) >= 5, f"{len(rec.actions)} 条")


# ----------------------------------------------------------------- 日志与缓冲
def test_event_log(tmp: Path) -> None:
    print("\n[10] 事件日志与标注")
    log = EventLog(tmp / "log")
    rec = EventRecord(event_id="EVTEST-001", detected_at=time.time(),
                      trigger="descent+lying", confidence=0.9, outcome="called")
    rec.stage("detected")
    log.write(rec)
    rows = log.events()
    check("事件可读回", len(rows) == 1 and rows[0]["event_id"] == "EVTEST-001")
    check("时间为 ISO 带时区", "T" in rows[0]["detected_at"])

    log.label("EVTEST-001", True, "测试误报")
    check("标注生效", (log.events()[0]["label"] or {}).get("is_false_alarm") is True)
    log.label("EVTEST-001", False, "复核为真")
    check("后写入的标注覆盖先前",
          (log.events()[0]["label"] or {}).get("is_false_alarm") is False)
    check("原始事件未被改写",
          sum(1 for _ in open(log.events_path, encoding="utf-8")) == 1)
    s = log.stats()
    check("统计正确", s["total"] == 1 and s["called"] == 1, str(s))


def test_ring_buffer() -> None:
    print("\n[11] 滚动缓冲")
    buf = RingBuffer(2.0, 15)
    for i in range(60):
        frame = np.full((360, 640, 3), i % 255, dtype=np.uint8)
        buf.push(i / 15.0, frame)
    check("按时长限长", len(buf) <= buf.maxlen, f"{len(buf)}/{buf.maxlen}")
    win = buf.window(2.0, 3.0)
    check("时间窗检索", all(2.0 <= ts <= 3.0 for ts, _ in win) and len(win) > 5,
          f"{len(win)} 帧")
    near = buf.nearest(3.0)
    check("最近帧检索", near is not None and abs(near[0] - 3.0) < 0.2)
    check("空缓冲安全", RingBuffer(1.0, 15).nearest(0.0) is None)


def test_config() -> None:
    print("\n[12] 配置")
    import os

    os.environ["FG_TEST_KEY"] = "secret-value"
    cfg = cfgmod.load()
    check("默认值合并", float(cfg.detector.lying_angle_deg) > 0)
    check("点号路径取值", cfg.get("call.provider") is not None)
    check("缺失键返回默认", cfg.get("no.such.key", "dflt") == "dflt")
    raw = dict(cfg.raw())
    raw2 = cfgmod._resolve_env({"k": "env:FG_TEST_KEY"})
    check("env: 占位符解析", raw2["k"] == "secret-value")
    check("未设置的 env 变空", cfgmod._resolve_env({"k": "env:FG_NOPE"})["k"] == "")
    check("相对路径基于项目根", cfg.path("data").is_absolute())


def test_torso_geometry() -> None:
    print("\n[13] 躯干角几何")
    from fallguard.pose.base import Person

    def mk(sho, hip):
        kp = {"l_sho": (sho[0], sho[1], 1.0), "r_sho": (sho[0], sho[1], 1.0),
              "l_hip": (hip[0], hip[1], 1.0), "r_hip": (hip[0], hip[1], 1.0)}
        return Person(bbox=(0.1, 0.1, 0.9, 0.9), score=1.0, keypoints=kp)

    for name, sho, hip, want in [
        ("站立 → 0°", (0.5, 0.30), (0.5, 0.70), 0.0),
        ("前倾 → 45°", (0.5, 0.30), (0.78, 0.58), 45.0),
        ("水平躺倒 → 90°", (0.3, 0.50), (0.7, 0.50), 90.0),
        ("反向躺倒 → 90°", (0.7, 0.50), (0.3, 0.50), 90.0),
    ]:
        got = mk(sho, hip).torso_angle_deg()
        check(name, abs(got - want) < 1.0, f"{got:.1f}°")

    check("站立不判为躺倒", mk((0.5, 0.3), (0.5, 0.7)).torso_angle_deg() < 60)
    check("躺倒判为躺倒", mk((0.3, 0.5), (0.7, 0.5)).torso_angle_deg() >= 60)
    # 肩髋重合时无法定义角度，必须返回 None 而不是 0（0 会被当成\"站立\"）
    check("肩髋重合返回 None",
          mk((0.5, 0.5), (0.5, 0.5)).torso_angle_deg() is None)
    check("缺关键点返回 None",
          Person(bbox=(0, 0, 1, 1), score=1.0).torso_angle_deg() is None)


def test_backend_selection(tmp: Path) -> None:
    print("\n[14] 后端选择（回归：合成源必须用 motion）")
    from fallguard.pipeline import FallGuard
    from fallguard.pose import available

    cfg = base_cfg(tmp)
    guard = FallGuard(cfg, show=False, log=lambda _m: None)
    name = guard.backend.name
    guard.source.close()
    guard.backend.close()
    # 合成场景画的是灰色矩形，真人模型一个都检不出；auto 若选中它们，
    # --source demo 自检会永远不触发。
    check("合成源 + auto → motion", name == "motion", name)
    if available().get("mediapipe"):
        check("mediapipe 已安装时 auto 仍不用于合成源", name == "motion")


def test_mediapipe_span_guard() -> None:
    print("\n[15] mediapipe 跨度护栏")
    from fallguard.pose import available

    if not available().get("mediapipe"):
        print("  (跳过：mediapipe 未安装)")
        return
    from fallguard.pose.backends import MediaPipeBackend

    b = MediaPipeBackend()
    check("默认跨度下限已设", b.min_span > 0, f"{b.min_span}")
    # 回归：护栏曾被设成 0.18，把 UR Fall 真人跌倒序列中"躺在地上且离镜头远"
    # 的人整段丢弃，造成漏报。真实倒地 bbox 跨度实测低至 0.11。
    check("倒地小目标不被丢弃（真实跌倒 span≈0.11~0.18）",
          b._plausible((0.40, 0.50, 0.51, 0.62), {}) is True)
    check("正常 bbox 被保留", b._plausible((0.28, 0.40, 0.73, 0.95), {}) is True)
    check("荒谬输出仍被丢弃", b._plausible((0.50, 0.50, 0.51, 0.51), {}) is False)
    b.close()


def test_lying_grace() -> None:
    print("\n[16] 躺倒计时的抖动容忍（回归：真人序列漏报）")
    import numpy as np

    from fallguard.detector import FallDetector
    from fallguard.pose.base import Person

    cfg = cfgmod.load()
    frame = np.zeros((360, 640, 3), np.uint8)

    def lying():
        kp = {"l_sho": (0.3, 0.8, 1.0), "r_sho": (0.3, 0.8, 1.0),
              "l_hip": (0.7, 0.8, 1.0), "r_hip": (0.7, 0.8, 1.0)}
        return Person(bbox=(0.2, 0.7, 0.8, 0.9), score=0.9, keypoints=kp)

    # 躺倒期间每 5 帧丢失 1 帧（模拟地面姿态估计抖动）
    det = FallDetector(cfg, 30)
    fired = 0
    for i in range(400):
        people = [] if (i > 30 and i % 5 == 0) else [lying()]
        _o, v = det.update(i / 30.0, frame, people)
        if v:
            fired += 1
    check("间歇丢帧下仍能判定（此前会被反复清零而漏报）", fired >= 1, f"{fired} 次")

    # 人真正离场：不能把离场时间计成躺倒
    det2 = FallDetector(cfg, 30)
    fired2 = 0
    for i in range(400):
        people = [lying()] if i < 40 else []
        _o, v = det2.update(i / 30.0, frame, people)
        if v:
            fired2 += 1
    check("人离场后不误判为持续躺倒", fired2 == 0, f"{fired2} 次")


def test_angle_hysteresis() -> None:
    print("\n[17] 躯干角迟滞（回归：阈值附近横跳漏报）")
    import numpy as np

    from fallguard.detector import FallDetector
    from fallguard.pose.base import Person

    cfg = cfgmod.load()
    frame = np.zeros((360, 640, 3), np.uint8)

    def at_angle(deg):
        import math
        r = math.radians(deg)
        sx, sy = 0.5, 0.75
        hx, hy = sx + 0.2 * math.sin(r), sy + 0.2 * math.cos(r)
        kp = {"l_sho": (sx, sy, 1.0), "r_sho": (sx, sy, 1.0),
              "l_hip": (hx, hy, 1.0), "r_hip": (hx, hy, 1.0)}
        return Person(bbox=(0.3, 0.7, 0.8, 0.9), score=0.9, keypoints=kp)

    # 在 56~62° 之间摆动——实测 UR Fall fall-03 的真实行为
    det = FallDetector(cfg, 30)
    fired = 0
    for i in range(400):
        deg = 62.0 if i % 2 == 0 else 56.0
        _o, v = det.update(i / 30.0, frame, [at_angle(deg)])
        if v:
            fired += 1
    check("阈值附近摆动仍能判定", fired >= 1, f"{fired} 次")

    # 明确站立不应因迟滞而误报
    det2 = FallDetector(cfg, 30)
    fired2 = 0
    for i in range(400):
        _o, v = det2.update(i / 30.0, frame, [at_angle(5.0)])
        if v:
            fired2 += 1
    check("站立不因迟滞误报", fired2 == 0, f"{fired2} 次")


def main() -> int:
    print("=" * 64)
    print("守望者 FallGuard · 自检")
    print("=" * 64)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_detector_fires(tmp)
        test_detector_no_false_fire(tmp)
        test_cooldown_clock(tmp)
        test_exclude_zone(tmp)
        test_cancel_window()
        test_suppress_window()
        test_call_signing(tmp)
        test_agent_fallback(tmp)
        test_tool_guards(tmp)
        test_event_log(tmp)
        test_ring_buffer()
        test_config()
        test_torso_geometry()
        test_backend_selection(tmp)
        test_mediapipe_span_guard()
        test_lying_grace()
        test_angle_hysteresis()

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  FAIL  {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
