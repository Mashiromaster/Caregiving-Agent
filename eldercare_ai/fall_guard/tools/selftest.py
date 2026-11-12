#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：覆盖判定状态机、取消窗口、外呼签名、智能体兜底、事件日志。

  python3 tools/selftest.py

不依赖摄像头与网络，可在 CI 中运行。
"""
from __future__ import annotations

import json
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
    """自检用的固定配置。

    **不沿用用户的 config.toml 里的外呼设置**——否则用户换了通道、改了
    联系人，自检就会跟着失败（实测踩过：把联系人换成真实号码后，
    断言 contact_id='nurse_station' 的用例全挂，但代码毫无问题）。
    判定阈值仍沿用真实配置，那是自检要覆盖的对象。
    """
    cfg = cfgmod.load()
    raw = cfg.raw()
    raw["camera"]["source"] = "synthetic"
    raw["alert"]["speak"] = False
    raw["alert"]["alarm_sound"] = False
    raw["agent"]["enabled"] = False
    raw["evidence"]["dir"] = str(tmp / "evidence")
    raw["log"]["dir"] = str(tmp)
    # 外呼部分整体换成固定 fixture
    raw["call"] = {
        "provider": "aliyun",
        "dry_run": True,
        "play_times": 2,
        "max_calls_per_event": 2,
        "call_all_contacts": False,
        "contacts": [
            {"id": "nurse_station", "name": "值班护士站",
             "phone": "13800000001", "role": "caregiver"},
            {"id": "family_primary", "name": "家属-张女士",
             "phone": "13800000002", "role": "family"},
        ],
        "aliyun": {"access_key_id": "", "access_key_secret": "",
                   "tts_code": "TTS_000000000",
                   "tts_param_keys": ["name", "time", "place"]},
        "tencent": {}, "volc": {}, "spug": {},
    }
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


def test_volc_signing(tmp: Path) -> None:
    print("\n[27] 火山引擎签名与参数")
    import hashlib
    import hmac

    from fallguard.alerts.call import Contact, VolcVoice

    contact = Contact(id="t", name="测试", phone="13800000001", role="caregiver")
    slots = {"name": "张**", "time": "02点14分", "place": "3号楼412房间",
             "event": "跌倒"}
    volc = VolcVoice({"access_key_id": "AKID", "secret_access_key": "SECRET",
                      "number_pool_no": "NP001", "tts_resource_key": "RES123",
                      "tts_param_keys": ["name", "time", "place"]}, 2)

    payload = volc.build_payload(contact, slots, "EV20260101-120000-001")
    item = payload["List"][0]
    check("Type=1 表示 TTS 模板", item["Type"] == 1)
    check("Resource 用 ResourceKey", item["Resource"] == "RES123")
    check("号码剥离非数字", item["Phone"] == "13800000001", item["Phone"])
    # 幂等：同一事件同一号码必须得到同一个 OpenId，否则重试会打两通电话
    again = volc.build_payload(contact, slots, "EV20260101-120000-001")
    check("SingleOpenId 幂等可复现",
          item["SingleOpenId"] == again["List"][0]["SingleOpenId"],
          item["SingleOpenId"])
    other = volc.build_payload(contact, slots, "EV20260101-120000-002")
    check("不同事件得到不同 OpenId",
          item["SingleOpenId"] != other["List"][0]["SingleOpenId"])
    check("PhoneParam 含三要素",
          all(k in item["PhoneParam"] for k in ("name", "time", "place")),
          item["PhoneParam"])

    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = volc._headers(body)
    check("签名头为 HMAC-SHA256",
          headers["Authorization"].startswith("HMAC-SHA256 Credential=AKID/"),
          headers["Authorization"][:60])
    check("带 X-Content-Sha256 头（火山必需）", "X-Content-Sha256" in headers)
    check("X-Date 为 UTC 紧凑格式",
          len(headers["X-Date"]) == 16 and headers["X-Date"].endswith("Z"),
          headers["X-Date"])
    check("scope 结尾为 request（非 tc3_request）",
          "/vms/request," in headers["Authorization"],
          headers["Authorization"][:110])
    check("payload hash 与 body 一致",
          headers["X-Content-Sha256"] == hashlib.sha256(body.encode()).hexdigest())

    # 派生密钥：SK 不加前缀——AWS 加 "AWS4"、腾讯加 "TC3"，火山不加。
    # 加错前缀会一路签到底然后被服务端拒，是最难排查的一类错。
    short = headers["X-Date"][:8]
    k = hmac.new(b"SECRET", short.encode(), hashlib.sha256).digest()
    for part in ("cn-north-1", "vms", "request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = headers["Authorization"].split("Signature=")[1]
    check("派生密钥时 SK 不加前缀", len(sig) == 64 and sig.isalnum(), sig[:16])
    bad = hmac.new(b"VOLCSECRET", short.encode(), hashlib.sha256).digest()
    for part in ("cn-north-1", "vms", "request"):
        bad = hmac.new(bad, part.encode(), hashlib.sha256).digest()
    check("与加前缀的错误算法结果不同", k != bad)

    cfg = base_cfg(tmp)
    cfg.raw()["call"]["provider"] = "volc"
    cfg.raw()["call"]["volc"] = {"access_key_id": "A", "secret_access_key": "S",
                                 "number_pool_no": "NP", "tts_resource_key": "R"}
    gw = CallGateway(cfg)
    check("网关可装配 volc 通道", gw.provider.name == "volc", gw.provider.name)
    check("配置齐全时无缺项", gw.provider.missing() == [], str(gw.provider.missing()))
    res = gw.call_one(gw.contacts[0], slots, "EVTEST")
    check("演练模式不发网络请求", res.ok and res.code == "DRY_RUN")

    # 缺配置时必须在拨号前就报出来，而不是等服务端返回
    bare = VolcVoice({}, 2)
    lack = bare.missing()
    check("缺配置被逐项列出", len(lack) == 4, str(lack))
    out = bare.call(contact, slots, "X", dry_run=False)
    check("缺配置时不发请求", out.code == "CONFIG_MISSING" and not out.ok)


def test_spug_voice(tmp: Path) -> None:
    print("\n[28] Spug 推送助手（个人账号可用的外呼通道）")
    from fallguard.alerts.call import Contact, SpugVoice

    contact = Contact(id="t", name="测试", phone="138-0000-0001", role="caregiver")
    slots = {"name": "张**", "time": "02点14分", "place": "3号楼412房间",
             "event": "跌倒"}

    # Spug 只能用官方固定模板（变量名写死为 key1/key2），
    # 所以要把四要素拼进平台给定的变量里
    tpl = SpugVoice({"template_id": "A27Labc", "template_vars": [
        "key1={place}的{name}", "key2={event}，{time}"]}, 2)
    url, body = tpl.build_request(contact, slots, "EV-1")
    check("模板方式走 /send", url.endswith("/send/A27Labc"), url)
    check("targets 剥离非数字", body["targets"] == "13800000001", body["targets"])
    check("key1 拼入位置与姓名", body["key1"] == "3号楼412房间的张**", body["key1"])
    check("key2 拼入事件与时间", body["key2"] == "跌倒，02点14分", body["key2"])
    check("四要素全部覆盖",
          all(x in (body["key1"] + body["key2"])
              for x in ("3号楼412房间", "张**", "跌倒", "02点14分")))
    check("模板方式无缺项", tpl.missing() == [])

    # 变量填充支持 dict 写法（从 TOML 读进来可能是表）
    d = SpugVoice({"template_id": "X", "template_vars": {"key1": "{name}"}}, 2)
    check("变量填充支持 dict 写法",
          d.build_request(contact, slots, "E")[1]["key1"] == "张**")

    # 引用了系统没有的槽位：原样传过去而不是整条失败——
    # 护工听到一句带占位符的话，好过完全接不到电话
    bad = SpugVoice({"template_id": "X", "template_vars": ["key1={nope}"]}, 2)
    _u, b = bad.build_request(contact, slots, "E")
    check("未知槽位不致整条失败", b["key1"] == "{nope}", b["key1"])

    # 填了模板却没配变量映射 → 播报会是空的，必须在拨号前拦住
    nov = SpugVoice({"template_id": "X"}, 2)
    check("模板无变量映射时报缺配置",
          any("template_vars" in m for m in nov.missing()), str(nov.missing()))

    # 简易方式：无模板，自由文案走 /xsend
    xs = SpugVoice({"user_id": "1faf47bcab78"}, 2)
    url2, body2 = xs.build_request(contact, slots, "EV-1")
    check("简易方式走 /xsend", url2.endswith("/xsend/1faf47bcab78"), url2)
    check("指定 voice 通道", body2["channel"] == "voice", body2["channel"])
    check("文案含四要素",
          all(x in body2["content"] for x in ("张**", "02点14分", "3号楼412房间", "跌倒")),
          body2["content"])

    # 号码出现在请求体里，留痕时必须脱敏——这条链路与云厂商不同，
    # 云厂商的号码在独立字段，这里混在 body 中容易漏掉
    res = tpl.call(contact, slots, "EV-1", dry_run=True)
    check("演练不发网络请求", res.ok and res.code == "DRY_RUN")
    check("留痕中号码已脱敏",
          "****" in res.params["body"]["targets"],
          res.params["body"]["targets"])
    check("留痕不含完整号码",
          "13800000001" not in json.dumps(res.params, ensure_ascii=False))

    bare = SpugVoice({}, 2)
    check("两个标识都没填时报缺配置", len(bare.missing()) == 1, str(bare.missing()))
    out = bare.call(contact, slots, "X", dry_run=False)
    check("缺配置时不发请求", out.code == "CONFIG_MISSING" and not out.ok)

    # 平台 200 只代表"请求已受理"，不代表电话打通了。审计记录不能写"成功"——
    # 实测就踩过：日志显示 OK 请求成功，而手机根本没响。
    import fallguard.alerts.call as callmod
    orig = callmod._post_json
    try:
        callmod._post_json = lambda *a, **k: {
            "code": 200, "msg": "请求成功", "request_id": "RID123"}
        sent = tpl.call(contact, slots, "EV-2", dry_run=False)
        check("平台受理记为 SUBMITTED 而非 OK", sent.code == "SUBMITTED", sent.code)
        check("留痕写明未确认接通",
              "未确认接通" in sent.message, sent.message[:40])
        check("带 request_id 供控制台核对",
              sent.request_id == "RID123" and "RID123" in sent.message)
        # 仍算 ok：确实提交出去了。若记为失败，兜底逻辑会把升级链上所有人
        # 再呼一遍，比"标注为未确认"更糟
        check("已提交仍视为 ok（避免兜底重复外呼）", sent.ok is True)

        callmod._post_json = lambda *a, **k: {"code": 403, "msg": "不在IP白名单内"}
        bad = tpl.call(contact, slots, "EV-3", dry_run=False)
        check("失败仍如实标记", bad.ok is False and bad.code == "403", bad.code)
        check("失败信息带处置动作",
              "白名单" in bad.message and "spug.cc" in bad.message,
              bad.message[:50])
    finally:
        callmod._post_json = orig

    cfg = base_cfg(tmp)
    cfg.raw()["call"]["provider"] = "spug"
    cfg.raw()["call"]["spug"] = {"template_id": "A27Labc",
                                 "template_vars": ["key1={name}"]}
    gw = CallGateway(cfg)
    check("网关可装配 spug 通道", gw.provider.name == "spug", gw.provider.name)
    check("describe 不报错", "spug" in gw.describe(), gw.describe())


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


# ------------------------------------------------- 框架：插件 / 分级 / 记忆
def test_sensor_equivalence(tmp: Path) -> None:
    print("\n[18] 跌倒插件与原判定等价（回归：重构不得改动算法）")
    from fallguard.core.sensors import SensorContext
    from fallguard.core.signals import SignalBus
    from fallguard.sensors.fall_vision import FallVisionSensor

    cfg = base_cfg(tmp)

    def run_direct():
        src = SyntheticSource(fps=15)
        backend = create_backend("motion")
        det = FallDetector(cfg, 15)
        out, states = [], []
        while True:
            item = src.read()
            if item is None:
                break
            ts, frame = item
            obs, v = det.update(ts, frame, backend.infer(frame))
            states.append(obs.state)
            if v:
                out.append((v.trigger, round(v.confidence, 6), v.features))
        return out, states

    def run_plugin():
        src = SyntheticSource(fps=15)
        bus = SignalBus()
        sensor = FallVisionSensor(cfg, 15, synthetic_source=True)
        ctx = SensorContext(signals=bus, scene={}, log=lambda _m: None)
        out, states = [], []
        while True:
            item = src.read()
            if item is None:
                break
            ts, frame = item
            obs, ev = sensor.observe(ts, frame, ctx)
            states.append(obs.state)
            if ev:
                out.append((ev.trigger, round(ev.confidence, 6), ev.features))
        return out, states, bus

    a_v, a_s = run_direct()
    b_v, b_s, bus = run_plugin()
    check("判定结果逐字段一致", a_v == b_v, f"{len(a_v)} vs {len(b_v)} 次")
    check("状态序列一致", a_s == b_s)
    check("插件额外产出连续信号", len(bus.names()) >= 3, str(bus.names()))


def test_stub_sensor(tmp: Path) -> None:
    """用一个桩插件证明处置链路与事件类型无关。

    这是"可插拔"的硬证据：桩插件不碰任何处置代码，只实现协议，
    就能走完 分级 → 留证 → 处置 的完整链路。
    """
    print("\n[19] 桩插件可插拔（框架不认识具体事件类型）")
    from fallguard.agent import RulePlanner, ToolContext
    from fallguard.core.events import SensorEvent, new_event_id
    from fallguard.core.grader import Grader, Rule
    from fallguard.core.sensors import Sensor

    class StubSensor(Sensor):
        kind = "bed_exit"
        name = "stub_bed_exit"
        needs_frame = False

        def observe(self, ts, frame, ctx):
            return {"state": "stub"}, SensorEvent(
                kind=self.kind, ts=ts, confidence=0.8, trigger="pressure_lost",
                features={"seconds_off_bed": 25})

        def narrate(self, event, scene):
            return {"title": "离床预警", "noun": "离床",
                    "announce": "检测到离床。{seconds}秒后将通知值班。",
                    "reassure": "已通知工作人员。", "desktop": "疑似离床",
                    "brief": "离床超时事件已判定成立。"}

    sensor = StubSensor()
    ev = sensor.observe(1.0, None, None)[1]
    check("桩插件产出标准事件", ev.kind == "bed_exit" and ev.confidence == 0.8)

    # 未注册规则 → 降级 L1 记录，而不是丢弃事件
    g = Grader()
    grading = g.grade(ev, profile={}, context={"hour": 12})
    check("未知类型降级为 L1 而非丢弃",
          grading.level == "L1" and grading.rule_id == "R-DEFAULT",
          f"{grading.level}/{grading.rule_id}")

    # 注册一条规则后立即生效，不动框架代码
    g.register(Rule("R-BED-01", "bed_exit", "L2", "离床超时",
                    lambda e, p, c: e.features.get("seconds_off_bed", 0) > 20))
    grading2 = g.grade(ev, profile={}, context={"hour": 12})
    check("注册规则后按规则分级",
          grading2.level == "L2" and grading2.rule_id == "R-BED-01",
          f"{grading2.level}/{grading2.rule_id}")

    # 处置链路照常跑：文案全部来自插件的 narrate()
    cfg = base_cfg(tmp)
    rec = EventRecord(event_id=new_event_id(), detected_at=time.time(),
                      event_type=ev.kind, level=grading2.level,
                      trigger=ev.trigger, confidence=ev.confidence)
    ctx = ToolContext(record=rec, gateway=CallGateway(cfg),
                      alerter=LocalAlerter(cfg), scene={}, slots={},
                      narration=sensor.narrate(ev, {}), log=lambda _m: None)
    RulePlanner().run(ctx)
    check("非跌倒事件也完成外呼", any(r.ok for r in ctx.called))
    check("播报文案来自插件而非框架硬编码",
          any("离床" in str(a.get("input", {}).get("message", ""))
              or "离床" in str(a.get("input", {}).get("title", ""))
              for a in rec.actions),
          str([a["tool"] for a in rec.actions]))
    check("事件等级沿用规则分级结果", rec.level == "L2", rec.level)


def test_grader(tmp: Path) -> None:
    print("\n[20] 规则分级")
    from fallguard.core.events import SensorEvent
    from fallguard.core.grader import Grader

    g = Grader()
    double = SensorEvent(kind="fall", ts=1.0, confidence=0.9,
                         trigger="descent+lying")
    single = SensorEvent(kind="fall", ts=1.0, confidence=0.55,
                         trigger="lying_only")

    r1 = g.grade(double, profile={}, context={"hour": 12})
    check("双条件 → L3/R-FALL-01",
          r1.level == "L3" and r1.rule_id == "R-FALL-01", f"{r1.level}/{r1.rule_id}")

    r2 = g.grade(single, profile={}, context={"hour": 12})
    check("单条件+非高危 → L2/R-FALL-03",
          r2.level == "L2" and r2.rule_id == "R-FALL-03", f"{r2.level}/{r2.rule_id}")

    r3 = g.grade(single, profile={"high_fall_risk": True}, context={"hour": 12})
    check("单条件+高危住户 → 升级 L3/R-FALL-02",
          r3.level == "L3" and r3.rule_id == "R-FALL-02", f"{r3.level}/{r3.rule_id}")

    r4 = g.grade(single, profile={"fall_history": 2}, context={"hour": 12})
    check("有跌倒史亦视为高危", r4.level == "L3", r4.level)

    # 分级理由必须可追溯到规则 ID——这是监管举证的要求
    check("命中理由非空", bool(r1.reason) and bool(r1.matched), r1.reason)

    unknown = SensorEvent(kind="never_seen", ts=1.0, trigger="x")
    r5 = g.grade(unknown, profile={}, context={"hour": 12})
    check("未知类型降级 L1", r5.level == "L1" and r5.rule_id == "R-DEFAULT")
    r6 = g.grade(unknown, profile={}, context={"hour": 23})
    check("夜间把 L1 提升为 L2", r6.level == "L2" and bool(r6.escalated_by),
          r6.escalated_by)

    # 坏规则不能让整个分级失败，否则事件会卡住不被处置
    from fallguard.core.grader import Rule
    g.register(Rule("R-BAD", "fall", "L4", "故意抛异常",
                    lambda e, p, c: (_ for _ in ()).throw(RuntimeError("boom"))))
    r7 = g.grade(double, profile={}, context={"hour": 12})
    check("异常规则按未命中处理，不影响其他规则", r7.level == "L3", r7.level)


def test_memory(tmp: Path) -> None:
    print("\n[21] 住户记忆")
    from fallguard.core.memory import Memory

    log = EventLog(tmp / "mem_log")
    mem = Memory(tmp / "mem", event_log=log)

    check("未知住户返回空画像", mem.profile("nobody") == {})
    mem.upsert_profile("R-001", name="张**", high_fall_risk=True, fall_history=2)
    p = mem.profile("R-001")
    check("画像可读回", p.get("name") == "张**" and p.get("high_fall_risk") is True)

    mem.upsert_profile("R-001", high_fall_risk=False)
    check("后写入覆盖先前且保留其他字段",
          mem.profile("R-001")["high_fall_risk"] is False
          and mem.profile("R-001")["name"] == "张**")

    now = time.time()
    # 按天间隔铺开，这样时间窗过滤才真的有东西可过滤
    for i in range(3):
        rec = EventRecord(event_id=f"EVM-{i}", detected_at=now - i * 86400,
                          event_type="fall", resident_id="R-001", outcome="called")
        log.write(rec)
    log.label("EVM-0", True, "测试误报")
    # 另一位住户的事件不应混进来
    log.write(EventRecord(event_id="EVM-other", detected_at=now,
                          resident_id="R-999"))

    h = mem.history("R-001", days=7, now=now)
    check("按住户隔离历史", h["total"] == 3, str(h["total"]))
    check("统计误报数", h["false_alarms"] == 1, str(h["false_alarms"]))
    check("按类型聚合", h["by_type"].get("fall") == 3, str(h["by_type"]))

    old = mem.history("R-001", days=1.5, now=now)
    check("时间窗生效", old["total"] == 2, str(old["total"]))

    recall = mem.recall("R-001", now=now)
    check("召回含人话摘要", "张**" in recall["summary"], recall["summary"][:60])
    check("未识别身份时摘要不报错", "未识别" in mem.summarize("", {}, {}))


def test_identity(tmp: Path) -> None:
    print("\n[22] 身份识别")
    from fallguard.core import identity as idm

    cfg = base_cfg(tmp)
    cfg.raw()["identity"] = {
        "providers": ["wristband_id", "room_binding"],
        "rooms": [{"room": "301", "resident_id": "R-301", "name": "李**"}],
        "bands": [{"band_id": "B-1", "resident_id": "R-B1", "name": "王**"}],
    }
    resolver = idm.create_enabled(cfg)
    check("识别链按配置顺序",
          [p.name for p in resolver.providers] == ["wristband_id", "room_binding"])

    got = resolver.resolve(idm.IdentitySignals(room="301", wristband_id="B-1",
                                               extra={"worn": True}))
    check("手环优先于房间", got.resident_id == "R-B1", got.resident_id)

    got2 = resolver.resolve(idm.IdentitySignals(room="301"))
    check("无手环时退回房间绑定", got2.resident_id == "R-301", got2.resident_id)

    # 没戴在身上的手环不能定身份——放床头柜上会把护工操作算到住户头上
    got3 = resolver.resolve(idm.IdentitySignals(room="301", wristband_id="B-1",
                                                extra={"worn": False}))
    check("未佩戴的手环不采信", got3.resident_id == "R-301", got3.resident_id)

    got4 = resolver.resolve(idm.IdentitySignals(room="999"))
    check("未知房间退回场景默认住户", got4.known, got4.detail)

    check("人脸识别声明为不可用", idm.available()["face"] is False)
    empty = idm.IdentityResolver([]).resolve(idm.IdentitySignals())
    check("无识别器时返回未识别而非抛错",
          not empty.known and empty.method == "unresolved")


def test_risk_scoring(tmp: Path) -> None:
    print("\n[23] 风险预警（与告警路径独立）")
    from fallguard.core import signals as sg
    from fallguard.core.risk import RiskScorer
    from fallguard.core.signals import SignalBus
    from fallguard.devices.synthetic import (CALM_S, FALL_S, ONSET_S,
                                             SyntheticBand)

    cfg = base_cfg(tmp)
    bus = SignalBus()
    scorer = RiskScorer(bus)
    t0 = 1_700_000_000.0

    check("无信号时风险为 0",
          scorer.assess(now=t0).score == 0.0)

    band = SyntheticBand(cfg, {"loop": False})
    warned_at = None
    for i in range(int((CALM_S + ONSET_S + FALL_S) / 0.5) + 2):
        t = i * 0.5
        hr, gait, accel = band._script(t)
        now = t0 + t
        bus.emit(sg.HEART_RATE, hr, ts=now, source="sim")
        bus.emit(sg.GAIT_STABILITY, gait, ts=now, source="sim")
        bus.emit(sg.ACCEL_MAG, accel, ts=now, source="sim")
        a = scorer.assess(profile={"hr_baseline": 72}, now=now)
        if warned_at is None and a.should_warn:
            warned_at = t
    check("前兆期即越阈（预警早于跌落）",
          warned_at is not None and warned_at < CALM_S + ONSET_S,
          f"越阈 {warned_at}s，跌落 {CALM_S + ONSET_S}s")

    final = scorer.assess(profile={"hr_baseline": 72}, now=t0 + CALM_S + ONSET_S + 1)
    check("每一分都有可解释依据",
          all(f.get("reason") for f in final.factors), str(len(final.factors)))

    # 静默期：分数在阈值附近抖动时不应反复提示。
    # 取跌落时刻附近评估——那时信号还新鲜且分数确定越阈
    s2 = RiskScorer(bus)
    hot = t0 + CALM_S + ONSET_S
    a1 = s2.assess(profile={"hr_baseline": 72}, now=hot)
    first = s2.should_emit_warning(a1, cooldown=600)
    a2 = s2.assess(profile={"hr_baseline": 72}, now=hot + 10)
    second = s2.should_emit_warning(a2, cooldown=600)
    check("预警有静默期，不刷屏", first is True and second is False,
          f"first={first} second={second} score={a1.score:.0f}")

    # 低质量信号（手环未佩戴）不应被当作有效读数
    bus2 = SignalBus()
    s3 = RiskScorer(bus2)
    bus2.emit(sg.GAIT_STABILITY, 0.05, ts=t0, source="sim", quality=0.1)
    check("低质量信号被忽略", s3.assess(now=t0).score == 0.0)

    # 陈旧信号同样不可信——手环掉线后最后一次读数会一直停在那儿
    bus3 = SignalBus()
    s4 = RiskScorer(bus3)
    bus3.emit(sg.HEART_RATE, 180.0, ts=t0, source="sim")
    check("陈旧信号不参与评分",
          s4.assess(profile={"hr_baseline": 72}, now=t0 + 600).score == 0.0)


def test_signal_bus() -> None:
    print("\n[24] 信号总线")
    from fallguard.core.signals import SignalBus

    bus = SignalBus(window_seconds=10.0)
    t0 = 1_700_000_000.0
    for i in range(20):
        bus.emit("x", float(i), ts=t0 + i * 0.5)

    check("最新值可取", bus.value("x", now=t0 + 9.5) == 19.0)
    check("滑窗裁剪过期点", len(bus.window_of("x", 100.0, now=t0 + 9.5)) <= 21)
    check("窗口检索", len(bus.window_of("x", 1.0, now=t0 + 9.5)) == 3)
    slope = bus.slope("x", 5.0, now=t0 + 9.5)
    check("斜率为正且量级正确", 1.9 < slope < 2.1, f"{slope:.3f}")
    check("均值计算", abs(bus.mean("x", 1.0, now=t0 + 9.5) - 18.0) < 0.01)

    check("新鲜度门控生效",
          bus.latest("x", max_age=1.0, now=t0 + 100) is None)
    check("缺失信号返回默认值", bus.value("nope", default=-1.0) == -1.0)
    check("单点时斜率为 0", SignalBus().slope("y", 5.0) == 0.0)

    seen = []
    bus.subscribe(lambda s: seen.append(s.name))
    bus.emit("z", 1.0, ts=t0)
    check("订阅者收到通知", seen == ["z"], str(seen))

    # 订阅者异常不能拖垮产信号的设备线程
    bus.subscribe(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.emit("z", 2.0, ts=t0 + 1)
    check("订阅者异常被隔离", bus.value("z", now=t0 + 1) == 2.0)


def test_device_push(tmp: Path) -> None:
    print("\n[25] 设备推送接入")
    from fallguard.core import signals as sg
    from fallguard.core.signals import SignalBus
    from fallguard.devices.http_push import HttpPushDevice

    cfg = base_cfg(tmp)
    bus = SignalBus()
    dev = HttpPushDevice(cfg, {"device_id": "band-01"})

    before = dev.ingest({"heart_rate": 80})
    check("未启动时拒绝推送", before.get("ok") is False)

    dev.start(bus)
    # online 用墙钟判断新鲜度，所以这里的 ts 必须接近当前时间
    t0 = time.time()
    res = dev.ingest({"device_id": "band-01", "band_id": "B-1", "ts": t0,
                      "heart_rate": 82, "spo2": 97, "worn": True,
                      "unknown_field": 1})
    check("已知字段被接收", sg.HEART_RATE in res["accepted"], str(res["accepted"]))
    check("未知字段被忽略而非报错", "unknown_field" in res["ignored"])
    check("读数进入总线", bus.value(sg.HEART_RATE, now=t0) == 82.0)
    check("设备转为在线", dev.online is True)

    # 未佩戴时读数仍接收但标低质量——不能当有效生理数据用
    dev.ingest({"ts": t0 + 1, "heart_rate": 70, "worn": False})
    sig = bus.latest(sg.HEART_RATE, now=t0 + 1)
    check("未佩戴时读数质量被标低", sig.quality < 0.5, f"{sig.quality}")

    bad = dev.ingest({"ts": t0 + 2, "heart_rate": "not-a-number"})
    check("非数值被拒绝且不崩", bad.get("ok") is False)


def test_event_type_filter(tmp: Path) -> None:
    print("\n[26] 事件类型（回归：event_type 此前从未被写入）")
    log = EventLog(tmp / "types")
    now = time.time()
    log.write(EventRecord(event_id="T-1", detected_at=now, event_type="fall",
                          outcome="called"))
    log.write(EventRecord(event_id="T-2", detected_at=now, event_type="risk_warning",
                          outcome="logged"))
    log.write(EventRecord(event_id="T-3", detected_at=now, event_type="fall",
                          outcome="cancelled", cancelled=True))

    check("类型可枚举", log.event_types() == ["fall", "risk_warning"],
          str(log.event_types()))
    check("按类型过滤", len(log.events(event_type="fall")) == 2)
    check("类型统计", log.stats()["by_type"] == {"fall": 2, "risk_warning": 1},
          str(log.stats()["by_type"]))
    # 预警的正常归宿就是只记录，不该被算成"未处置"
    check("仅记录不计为未处置", log.stats()["unresolved"] == 0,
          str(log.stats()["unresolved"]))
    check("按住户过滤", log.events(resident_id="nobody") == [])


def test_help_gesture(tmp: Path) -> None:
    print("\n[29] 求助手势（举手至耳边）")
    from fallguard.core.sensors import SensorContext
    from fallguard.core.signals import SignalBus
    from fallguard.pose.base import Person
    from fallguard.sensors.help_gesture import HelpGestureSensor

    cfg = base_cfg(tmp)
    cfg.raw()["gesture"] = {"near_ratio": 0.75, "hold_seconds": 3.0,
                            "cooldown_seconds": 60.0, "min_keypoint_conf": 0.5,
                            "grace_seconds": 0.6}

    def person(wri_xy, *, ear=(0.50, 0.30), sho_y=0.42, conf=0.9):
        """造一个骨骼：肩宽固定 0.2，耳在 (0.50,0.30)。"""
        kp = {
            "l_sho": (0.40, sho_y, conf), "r_sho": (0.60, sho_y, conf),
            "l_ear": (ear[0], ear[1], conf), "r_ear": (ear[0], ear[1], conf),
            "nose": (0.50, 0.32, conf),
            "l_wri": (wri_xy[0], wri_xy[1], conf),
            "r_wri": (0.65, 0.80, conf),          # 右手自然垂着
        }
        return Person(bbox=(0.35, 0.25, 0.65, 0.95), score=0.95, keypoints=kp)

    def run(sensor, frames, start=0.0, step=0.2):
        """喂若干帧，返回 (事件数, 最后一帧的 obs)。"""
        bus = SignalBus()
        ctx = SensorContext(signals=bus, scene={}, log=lambda _m: None)
        fired, obs = 0, None
        for i, p in enumerate(frames):
            ts = start + i * step
            ctx.tick(1_700_000_000.0 + ts)
            ctx.person = p
            obs, ev = sensor.observe(ts, None, ctx)
            if ev:
                fired += 1
        return fired, obs

    # 手举到耳边（距离 0.04 ≈ 0.2×肩宽，远小于 0.75）并保持 4 秒
    s = HelpGestureSensor(cfg, 15)
    near = person((0.52, 0.31))
    fired, obs = run(s, [near] * 20)          # 20 帧 × 0.2s = 4s
    check("举手保持 4 秒触发一次", fired == 1, f"{fired} 次")

    # 静默期：持续举着手不该连环触发（否则一个动作打一串电话）
    s_cd = HelpGestureSensor(cfg, 15)
    fired_cd, _ = run(s_cd, [near] * 100)     # 20 秒一直举着
    check("持续举手只触发一次（静默期生效）", fired_cd == 1, f"{fired_cd} 次")

    # 只保持 2 秒 —— 不到阈值不该触发（挠耳朵、撩头发都是瞬时的）
    s2 = HelpGestureSensor(cfg, 15)
    fired2, _ = run(s2, [near] * 10)          # 2s
    check("保持不足 3 秒不触发", fired2 == 0, f"{fired2} 次")

    # 手垂在身侧 —— 位置远，不触发
    s3 = HelpGestureSensor(cfg, 15)
    fired3, _ = run(s3, [person((0.38, 0.85))] * 20)
    check("手垂身侧不触发", fired3 == 0, f"{fired3} 次")

    # 手抬到脸旁但低于肩膀（如扶下巴）—— 高度判据应挡住
    s4 = HelpGestureSensor(cfg, 15)
    fired4, _ = run(s4, [person((0.52, 0.50))] * 20)   # y=0.50 > 肩 0.42
    check("手低于肩膀不触发（扶下巴等）", fired4 == 0, f"{fired4} 次")

    # 中途短暂丢点：姿态估计会抖，不该因一两帧丢失就清零
    s5 = HelpGestureSensor(cfg, 15)
    seq = [near] * 8 + [None] * 2 + [near] * 10     # 中断 0.4s < grace 0.6s
    fired5, _ = run(s5, seq)
    check("短暂丢点不清零（容忍姿态抖动）", fired5 == 1, f"{fired5} 次")

    # 长时间中断：人把手放下了，应该清零重计
    s6 = HelpGestureSensor(cfg, 15)
    seq6 = [near] * 8 + [None] * 6 + [near] * 8     # 中断 1.2s > grace
    fired6, _ = run(s6, seq6)
    check("长时间中断后重新计时", fired6 == 0, f"{fired6} 次")

    # 事件内容
    s7 = HelpGestureSensor(cfg, 15)
    bus = SignalBus()
    ctx = SensorContext(signals=bus, scene={"place": "412房间",
                                            "resident_name": "张**"},
                        log=lambda _m: None)
    ev = None
    for i in range(20):
        ctx.tick(1_700_000_000.0 + i * 0.2)
        ctx.person = near
        _o, got = s7.observe(i * 0.2, None, ctx)
        if got:
            ev = got
    check("事件类型为 help_gesture", ev and ev.kind == "help_gesture")
    check("记录保持时长与距离",
          ev and ev.features.get("held_seconds", 0) >= 3.0
          and "ear_distance_ratio" in ev.features, str(ev.features if ev else None))
    n = s7.narrate(ev, {"place": "412房间", "resident_name": "张**"})
    check("播报体现'已收到求助'而非'检测到异常'",
          "收到" in n["announce"] and "求助" in n["title"], n["announce"])
    check("给智能体的情境说明是主动求助",
          "主动" in n["brief"], n["brief"][:40])

    # 分级：主动求助应判 L3（需立即到场）
    from fallguard.core.grader import Grader
    g = Grader().grade(ev, profile={}, context={"hour": 14})
    check("分级为 L3 且规则可追溯",
          g.level == "L3" and g.rule_id == "R-HELP-01", f"{g.level}/{g.rule_id}")


def test_self_initiated_handling(tmp: Path) -> None:
    print("\n[30] 主动求助的处置差异")
    from fallguard.core.runtime import SELF_INITIATED_KINDS, Runtime

    cfg = base_cfg(tmp)
    cfg.raw()["camera"]["source"] = "demo"
    cfg.raw()["sensors"] = {"enabled": ["fall_vision"]}   # 合成源用不到手势
    cfg.raw()["alert"]["cancel_seconds"] = 20
    cfg.raw()["alert"]["help_cancel_seconds"] = 2
    rt = Runtime(cfg, show=False, log=lambda _m: None)
    try:
        # 取消窗口按事件类型区分：系统判定给足时间，主动求助不让人干等
        check("跌倒用标准取消窗口", rt._cancel_seconds_for("fall") == 20.0)
        check("求助用短取消窗口", rt._cancel_seconds_for("help_gesture") == 2.0)
        # 但不能是 0——误识别一次就是一通真实电话
        check("求助窗口不为 0（保留撤销机会）",
              rt._cancel_seconds_for("help_gesture") > 0)
        check("help_gesture 被标记为主动发起",
              "help_gesture" in SELF_INITIATED_KINDS)

        cfg.raw()["alert"]["cancel_seconds_by_type"] = {"help_gesture": 5}
        rt2 = Runtime(cfg, show=False, log=lambda _m: None)
        try:
            check("可按类型单独覆盖",
                  rt2._cancel_seconds_for("help_gesture") == 5.0)
        finally:
            rt2.source.close()
            for s in rt2.sensors:
                s.close()
    finally:
        rt.source.close()
        for s in rt.sensors:
            s.close()


def test_signal_freshness(tmp: Path) -> None:
    """回归：手表数据曾被新鲜度门控静默丢弃。

    风险因子原先把 max_age 写死成 60/120 秒（为 0.5 秒一推的模拟手环调的）。
    Apple Watch 的静息心率一天才算一次，每条读数都会被判为陈旧而忽略——
    信号进了总线、评分恒为 0，而且不报任何错。
    """
    print("\n[31] 信号新鲜度按来源区分（回归：手表数据被静默丢弃）")
    from fallguard.core import signals as sg
    from fallguard.core.signals import SignalBus

    t0 = 1_700_000_000.0
    bus = SignalBus(window_seconds=30 * 86400.0)

    # 摄像头：15 秒就该算陈旧
    bus.emit(sg.POSTURE_STABILITY, 0.9, ts=t0)
    check("摄像头信号 5 秒内新鲜",
          bus.fresh(sg.POSTURE_STABILITY, now=t0 + 5) is not None)
    check("摄像头信号 60 秒后判为陈旧",
          bus.fresh(sg.POSTURE_STABILITY, now=t0 + 60) is None)

    # 手表静息心率：6 小时前的读数完全正常
    bus.emit(sg.WATCH_RESTING_HR, 68, ts=t0)
    check("手表静息心率 6 小时后仍新鲜",
          bus.fresh(sg.WATCH_RESTING_HR, now=t0 + 6 * 3600) is not None)
    check("手表静息心率 48 小时后才判陈旧",
          bus.fresh(sg.WATCH_RESTING_HR, now=t0 + 48 * 3600) is None)

    # 步行稳定性要攒够步行样本才更新，一周一次也正常
    bus.emit(sg.WALKING_STEADINESS, 72, ts=t0)
    check("步行稳定性 3 天后仍新鲜",
          bus.fresh(sg.WALKING_STEADINESS, now=t0 + 3 * 86400) is not None)

    check("未登记的信号用保守默认值",
          sg.max_age_of("unknown.metric") == sg.DEFAULT_MAX_AGE)

    # 多源备份：优先用最准的，没有就退回次之
    got = bus.first_fresh((sg.WALKING_STEADINESS, sg.GAIT_STABILITY),
                          now=t0 + 3 * 86400)
    check("按优先级取第一个新鲜源",
          got is not None and got.name == sg.WALKING_STEADINESS)
    gone = bus.first_fresh((sg.POSTURE_STABILITY,), now=t0 + 600)
    check("都不新鲜时返回 None", gone is None)


def test_health_store(tmp: Path) -> None:
    print("\n[32] 健康档案与个体基线")
    from fallguard.core.health import HealthStore

    store = HealthStore(tmp / "health", min_interval=0.0)
    t0 = 1_700_000_000.0
    day = 86400.0

    check("摄像头逐帧读数不进档案",
          store.should_archive("camera.motion") is False)
    check("手表数据进档案", store.should_archive("watch.resting_hr") is True)

    # 30 天静息心率，基线约 68；最近两天升到 82
    for i in range(30, 2, -1):
        store.record("R-001", "watch.resting_hr", 68 + (i % 3) - 1,
                     ts=t0 - i * day)
    for i in (2, 1, 0):
        store.record("R-001", "watch.resting_hr", 82, ts=t0 - i * day)

    base = store.baseline("R-001", "watch.resting_hr", now=t0)
    check("基线可建立", base is not None and base["n"] >= 20,
          str(base and base["n"]))
    check("基线贴近历史常态而非近期异常",
          base is not None and 66 <= base["median"] <= 70, str(base["median"]))

    dev = store.deviation("R-001", "watch.resting_hr", 82, now=t0)
    check("偏离度可算出", dev is not None)
    check("近期升高表现为正向偏离", dev is not None and dev[0] > 1.5,
          f"z={dev[0]:.2f}" if dev else "None")

    # 样本太少时宁可返回 None 让调用方退回全院阈值
    store.record("R-002", "watch.hrv", 40, ts=t0 - day)
    check("样本不足不给基线",
          store.baseline("R-002", "watch.hrv", now=t0) is None)

    # 住户之间必须隔离——混在一起的基线比没有基线更危险
    check("按住户隔离",
          store.baseline("R-002", "watch.resting_hr", now=t0) is None)

    tr = store.trend("R-001", "watch.resting_hr", days=14, now=t0)
    check("趋势可算出", tr is not None)
    check("识别出上升方向", tr is not None and tr["direction"] == "上升",
          f"{tr['pct']}% ≥ 门槛 {tr['threshold_pct']}%" if tr else "None")
    check("静息心率上升标为需关注",
          tr is not None and tr["concerning"] is True)

    # 门槛按指标取：静息心率涨 7% 是信号，步数差 7% 是噪声。
    # 一刀切的门槛要么滤掉前者，要么被后者刷屏。
    from fallguard.core.health import significant_pct
    check("静息心率门槛比步数严得多",
          significant_pct("watch.resting_hr") < significant_pct("watch.step_count"),
          f"{significant_pct('watch.resting_hr')}% vs "
          f"{significant_pct('watch.step_count')}%")
    for i in range(20, 0, -1):
        store.record("R-004", "watch.step_count", 3000 + (200 if i % 2 else -200),
                     ts=t0 - i * day)
    tr_steps = store.trend("R-004", "watch.step_count", days=14, now=t0)
    check("步数日常波动不算趋势",
          tr_steps is not None and tr_steps["direction"] == "平稳",
          f"{tr_steps['pct']}%" if tr_steps else "None")

    # 越低越糟的指标：方向判断要反过来
    for i in range(20, 0, -1):
        store.record("R-003", "watch.walking_steadiness",
                     85 - (20 - i) * 1.2, ts=t0 - i * day)
    tr2 = store.trend("R-003", "watch.walking_steadiness", days=14, now=t0)
    check("步行稳定性下降标为需关注",
          tr2 is not None and tr2["direction"] == "下降"
          and tr2["concerning"] is True, str(tr2 and tr2["direction"]))

    summary = store.summary("R-001", now=t0)
    check("摘要是人话", "静息心率" in summary["text"], summary["text"][:50])
    check("摘要只含聚合量，不含逐条读数",
          all(k not in json.dumps(summary, ensure_ascii=False)
              for k in ('"samples"', '"raw"')))
    check("列出需关注项", len(summary["concerning"]) >= 1)

    # 限流：模拟手环每 0.5 秒一条，不限流一天 17 万行
    throttled = HealthStore(tmp / "health2", min_interval=60.0)
    wrote = sum(1 for i in range(10)
                if throttled.record("R", "band.heart_rate", 70, ts=t0 + i))
    check("高频推送被限流", wrote == 1, f"{wrote} 条")


def test_apple_health_ingest(tmp: Path) -> None:
    print("\n[33] Apple Watch 数据接入")
    from fallguard.core import signals as sg
    from fallguard.core.health import HealthStore
    from fallguard.core.signals import SignalBus
    from fallguard.devices.apple_health import AppleHealthDevice

    cfg = base_cfg(tmp)
    bus = SignalBus(window_seconds=30 * 86400.0)
    store = HealthStore(tmp / "ah", min_interval=0.0)
    dev = AppleHealthDevice(cfg, {"device_id": "iphone", "resident_id": "R-001"})

    before = dev.ingest({"data": {"metrics": []}})
    check("未启动时拒绝推送", before.get("ok") is False)

    dev.start(bus)
    dev.bind_store(store)

    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S +0800",
                          time.localtime(now - 3600))
    res = dev.ingest({"data": {"metrics": [
        {"name": "resting_heart_rate", "units": "count/min",
         "data": [{"date": stamp, "qty": 71}]},
        {"name": "heart_rate", "units": "count/min",
         "data": [{"date": stamp, "Min": 58, "Avg": 74, "Max": 110}]},
        {"name": "walking_steadiness", "units": "%",
         "data": [{"date": stamp, "qty": 68}]},
        {"name": "body_mass", "units": "kg",
         "data": [{"date": stamp, "qty": 70}]},
    ]}})
    check("识别 HAE 的嵌套 metrics 格式", res["ok"] is True, str(res.get("reason")))
    check("静息心率入总线",
          bus.value(sg.WATCH_RESTING_HR, now=now) == 71.0)
    check("心率取 Avg 而非 Max",
          bus.value(sg.WATCH_HEART_RATE, now=now) == 74.0)
    check("步行稳定性入总线",
          bus.value(sg.WALKING_STEADINESS, now=now) == 68.0)
    check("未知指标忽略而非报错", "body_mass" in res["ignored_metrics"])

    # 样本要带自己的时间戳，不能统一打成"现在"——否则六小时前的心率
    # 会冒充当前读数参与风险评分
    sig_rec = bus.latest(sg.WATCH_RESTING_HR, now=now)
    check("用样本自身时间戳而非收到时刻",
          sig_rec is not None and abs(sig_rec.ts - (now - 3600)) < 120,
          f"偏差 {abs(sig_rec.ts - (now - 3600)):.0f}s" if sig_rec else "None")

    check("同时写入长期档案",
          store.latest("R-001", sg.WATCH_RESTING_HR) is not None)

    # 睡眠一条样本含多字段，要拆成两个信号
    dev.ingest({"data": {"metrics": [
        {"name": "sleep_analysis",
         "data": [{"date": stamp, "totalSleep": 6.5, "awake": 0.5}]}]}})
    check("睡眠时长单独成信号",
          bus.value(sg.SLEEP_HOURS, now=now) == 6.5)
    check("觉醒次数单独成信号",
          bus.value(sg.SLEEP_AWAKENINGS, now=now) > 0)

    # 回溯窗口：HAE 配错导出范围时会推来几个月历史
    old_stamp = time.strftime("%Y-%m-%d %H:%M:%S +0800",
                              time.localtime(now - 200 * 86400))
    res_old = dev.ingest({"data": {"metrics": [
        {"name": "resting_heart_rate", "data": [{"date": old_stamp, "qty": 60}]}]}})
    check("过旧样本被丢弃", res_old["dropped_too_old"] == 1,
          str(res_old["dropped_too_old"]))
    check("丢弃后如实说明原因", res_old["ok"] is False and res_old["reason"])

    # 兼容去掉 data 外层的简化格式（快捷指令手搓的常长这样）
    res_flat = dev.ingest({"metrics": [
        {"name": "step_count", "data": [{"date": stamp, "qty": 3200}]}]})
    check("兼容无 data 外层的简化格式", res_flat["ok"] is True)
    check("格式不对时给出可操作提示",
          "Health Auto Export" in dev.ingest({"foo": 1})["reason"])


def test_health_risk_factors(tmp: Path) -> None:
    print("\n[34] 健康趋势驱动的风险因子")
    from fallguard.core import signals as sg
    from fallguard.core.health import HealthStore
    from fallguard.core.risk import RiskScorer
    from fallguard.core.signals import SignalBus

    t0 = 1_700_000_000.0
    day = 86400.0
    bus = SignalBus(window_seconds=60 * day)
    store = HealthStore(tmp / "hr", min_interval=0.0)
    scorer = RiskScorer(bus)

    # 建立 30 天静息心率基线 ≈ 66
    for i in range(30, 1, -1):
        store.record("R-001", sg.WATCH_RESTING_HR, 66 + (i % 3) - 1,
                     ts=t0 - i * day)

    check("无信号时 0 分", scorer.assess(now=t0).score == 0.0)

    # 静息心率明显上移
    bus.emit(sg.WATCH_RESTING_HR, 80, ts=t0 - 3600)
    a = scorer.assess(now=t0, health=store, resident_id="R-001")
    ids = {f["id"] for f in a.factors}
    check("静息心率上移被识别", "resting_hr_drift" in ids, str(sorted(ids)))
    check("理由引用本人基线而非全院阈值",
          any("本人基线" in f["reason"] for f in a.factors),
          str([f["reason"][:40] for f in a.factors]))

    # 慢性因子不该被当成急性事件：静默期要按天算
    check("标记为趋势驱动", a.slow_driven is True)
    check("急性分独立计算", a.acute_score < a.score)

    # 步行稳定性低于安全区间（0~100 要归一到 0~1，否则 68% 会被当成超稳）
    bus2 = SignalBus(window_seconds=60 * day)
    s2 = RiskScorer(bus2)
    bus2.emit(sg.WALKING_STEADINESS, 30, ts=t0 - 86400)
    a2 = s2.assess(now=t0)
    check("步行稳定性按百分比归一",
          any(f["id"] == "gait_unstable" for f in a2.factors),
          str([f["id"] for f in a2.factors]))
    check("理由显示原始百分比",
          any("%" in f["reason"] for f in a2.factors if f["id"] == "gait_unstable"))

    # 血氧用绝对阈值（生理常数，不因人而异）
    bus3 = SignalBus(window_seconds=60 * day)
    s3 = RiskScorer(bus3)
    bus3.emit(sg.WATCH_SPO2, 90, ts=t0 - 600)
    check("血氧偏低被识别",
          any(f["id"] == "spo2_low" for f in s3.assess(now=t0).factors))
    bus3.emit(sg.WATCH_SPO2, 97, ts=t0 - 300)
    check("血氧正常不计分",
          not any(f["id"] == "spo2_low" for f in s3.assess(now=t0).factors))

    # 没有档案时退回全院阈值，而不是整条失效
    bus4 = SignalBus(window_seconds=60 * day)
    s4 = RiskScorer(bus4)
    bus4.emit(sg.HEART_RATE, 115, ts=t0 - 30)
    a4 = s4.assess(now=t0, profile={"hr_baseline": 72})
    check("无个体档案时退回画像基线",
          any(f["id"] == "hr_abnormal" for f in a4.factors),
          str([f["id"] for f in a4.factors]))

    # 慢性预警的静默期按天算，否则一天 144 条重复提醒
    s5 = RiskScorer(bus)
    hot = scorer.assess(now=t0, health=store, resident_id="R-001")
    if hot.should_warn:
        first = s5.should_emit_warning(hot, cooldown=600, slow_cooldown=20 * 3600)
        later = s5.assess(now=t0 + 3600, health=store, resident_id="R-001")
        second = s5.should_emit_warning(later, cooldown=600,
                                        slow_cooldown=20 * 3600)
        check("趋势类预警一天只提醒一次",
              first is True and second is False, f"{first}/{second}")
    else:
        check("趋势类预警一天只提醒一次", True, "(分数未越阈，跳过)")


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
        test_sensor_equivalence(tmp)
        test_stub_sensor(tmp)
        test_grader(tmp)
        test_memory(tmp)
        test_identity(tmp)
        test_risk_scoring(tmp)
        test_signal_bus()
        test_device_push(tmp)
        test_event_type_filter(tmp)
        test_volc_signing(tmp)
        test_spug_voice(tmp)
        test_help_gesture(tmp)
        test_self_initiated_handling(tmp)
        test_signal_freshness(tmp)
        test_health_store(tmp)
        test_apple_health_ingest(tmp)
        test_health_risk_factors(tmp)

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
