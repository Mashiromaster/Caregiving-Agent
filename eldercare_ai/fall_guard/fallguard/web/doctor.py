# -*- coding: utf-8 -*-
"""结构化体检：main.py doctor 的同款检查，输出 JSON 供面板渲染。

每项给出 status（ok | warn | fail | skip）、一句结论、以及可操作的修复建议——
面板上的「体检」页只负责把它画出来，判断逻辑都在这里。

摄像头探测会真的打开一次设备（这是唯一能确认授权是否到位的方法），
所以运行中不做此项：设备已被运行器占用，探测必然失败而且会干扰取帧。
"""
from __future__ import annotations

import shutil
import sys
from typing import Any

from .. import config as cfgmod


def _item(name: str, status: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _deps() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        import cv2
        out.append(_item("opencv", "ok", f"{cv2.__version__}"))
    except Exception as exc:
        out.append(_item("opencv", "fail", str(exc),
                         "pip3 install opencv-python —— 缺它整个系统跑不起来"))
        return out

    from ..pose import available
    have = available()
    notes = {"mediapipe": "骨骼点判定，推荐",
             "yolo": "多人场景更准，依赖 torch（约 2GB）",
             "motion": "零依赖兜底，无骨骼点"}
    installs = {"mediapipe": "pip3 install mediapipe",
                "yolo": "pip3 install ultralytics"}
    for name, ok in have.items():
        if name == "motion":
            out.append(_item(f"姿态后端 {name}", "ok", notes[name]))
            continue
        out.append(_item(f"姿态后端 {name}", "ok" if ok else "warn", notes[name],
                         "" if ok else installs[name]))
    if not have["mediapipe"] and not have["yolo"]:
        out.append(_item("判定精度", "warn", "只有 motion 后端可用，判定退化为人体框宽高比",
                         "pip3 install mediapipe 可显著提升精度"))
    return out


def _local_tools() -> list[dict[str, Any]]:
    out = []
    targets = (("say", "语音播报"), ("afplay", "告警音"), ("osascript", "系统通知"))
    if sys.platform != "darwin":
        targets = (("espeak-ng", "语音播报"), ("paplay", "告警音"),
                   ("notify-send", "系统通知"))
    for tool, desc in targets:
        ok = shutil.which(tool) is not None
        out.append(_item(f"{desc}（{tool}）", "ok" if ok else "warn",
                         "可用" if ok else "未找到该命令",
                         "" if ok else f"缺失时{desc}会降级为终端打印，不影响外呼"))
    return out


def _camera(cfg, running: bool) -> list[dict[str, Any]]:
    if running:
        return [_item("视频源", "skip", "系统正在运行，跳过探测",
                      "运行中设备已被占用，停止后再体检可验证授权")]
    source = cfg.camera.source
    try:
        from ..capture import open_source
        src = open_source(cfg)
        try:
            item = src.read()
            if item is None:
                return [_item("视频源", "fail", f"source={source} 打开成功但读帧失败",
                              "检查设备是否被其他程序（会议软件、另一个 FallGuard）占用")]
            h, w = item[1].shape[:2]
            return [_item("视频源", "ok", f"source={source} 读帧成功 {w}x{h}")]
        finally:
            src.close()
    except Exception as exc:
        hint = "改用 source=demo 做无摄像头自检，或 source=screen 走屏幕捕获"
        if sys.platform == "darwin":
            hint = ("macOS 需在 系统设置 → 隐私与安全性 → 摄像头 中授权运行本程序的终端；"
                    "终端宿主拿不到授权时改用 source=screen，无设备时用 source=demo")
        return [_item("视频源", "fail", f"source={source} 打开失败：{exc}", hint)]


def _call(cfg) -> list[dict[str, Any]]:
    from ..alerts import CallGateway

    out: list[dict[str, Any]] = []
    gw = CallGateway(cfg)
    has_provider = gw.provider_name != "none"
    out.append(_item("外呼通道", "ok" if has_provider else "warn", gw.describe(),
                     "" if has_provider else "provider=none 时只记录事件不拨号"))

    if not gw.contacts:
        out.append(_item("联系人", "fail", "未配置任何联系人",
                         "在「配置 → 联系人」里至少加一个 role=caregiver 的号码，"
                         "否则判定成立后无人可呼"))
    else:
        roles = ", ".join(f"{c.id}({c.role})" for c in gw.contacts)
        has_caregiver = any(c.role == "caregiver" for c in gw.contacts)
        out.append(_item("联系人", "ok" if has_caregiver else "warn",
                         f"{len(gw.contacts)} 位：{roles}",
                         "" if has_caregiver
                         else "没有 caregiver 角色，升级链会退化为按配置顺序呼叫"))

    lack = gw.provider.missing()
    if not has_provider:
        pass
    elif lack:
        out.append(_item("通道凭据", "warn" if gw.dry_run else "fail",
                         "待补：" + ", ".join(lack),
                         "演练模式下不影响链路验证；关闭演练前必须补齐"
                         if gw.dry_run else "真实拨号会直接返回 CONFIG_MISSING"))
    else:
        out.append(_item("通道凭据", "ok", "密钥与模板均已配置"))

    dry = gw.dry_run
    out.append(_item("拨号模式", "ok" if dry else "warn",
                     "演练（dry_run=true），只打印参数不拨号" if dry
                     else "真实拨号，判定成立会产生费用",
                     "联调通过后在「配置 → 外呼」里关闭演练模式" if dry else ""))

    if gw.provider_name == "spug":
        # 配置全齐、体检全绿，真实拨号仍可能 403——Spug 按公网 IP 鉴权，
        # 而家用宽带的 IP 会变。这是静态检查发现不了的失败模式，
        # 只能靠提示让人记住去确认。
        out.append(_item(
            "IP 白名单", "warn",
            "Spug 按调用方公网 IP 鉴权，不在白名单会返回 403",
            "在 push.spug.cc 的「个人设置 → IP 白名单」里加入本机公网 IP。"
            "家用宽带 IP 会变动，换网络或重拨光猫后需要重新添加——"
            "届时配置看起来完全正常，但电话打不出去"))
    return out


def _agent(cfg) -> list[dict[str, Any]]:
    a = cfg.agent
    if not bool(a.enabled):
        return [_item("智能体", "ok", "已关闭，走确定性规则处置",
                      "规则处置同样完成外呼与播报，只是不会按现场情况调整顺序")]
    if not str(a.api_key):
        return [_item("智能体", "warn", "已启用但 api_key 为空，将降级为规则处置",
                      "export DEEPSEEK_API_KEY=sk-xxx 后重启面板；"
                      '配置里填 "env:DEEPSEEK_API_KEY" 即可读取')]
    return [_item("智能体", "ok",
                  f"{a.model} @ {a.base_url}，时限 {a.deadline_seconds}s",
                  "模型超时或报错时确定性兜底会接管，不会导致没人去救")]


def _thresholds(cfg) -> list[dict[str, Any]]:
    d = cfg.detector
    zones = list(d.exclude_zones or [])
    cancel_s = float(cfg.alert.cancel_seconds)
    return [
        _item("判定阈值", "ok",
              f"躺倒角 ≥{d.lying_angle_deg}°　下降速率 ≥{d.descent_rate_min}/s　"
              f"双条件 {d.lying_seconds}s　单条件 {d.lying_only_seconds}s　"
              f"静默期 {d.cooldown_seconds}s"),
        _item("排除区域", "ok" if zones else "warn",
              f"{len(zones)} 个" if zones else "未设置",
              "" if zones
              else "画面里有床铺时建议框出来，否则正常躺卧会被判成跌倒"),
        _item("取消窗口", "ok" if cancel_s > 0 else "warn", f"{cancel_s:g}s",
              "" if cancel_s > 0 else "为 0 时误报没有撤销机会，会直接外呼"),
    ]


def _sensors(cfg) -> list[dict[str, Any]]:
    """感知插件自报状态。新增插件会自动出现在这里，不用改体检代码。"""
    from ..core import sensors as sensors_mod

    out: list[dict[str, Any]] = []
    enabled = list(cfg.get("sensors.enabled") or ["fall_vision"])
    have = sensors_mod.available()
    if not enabled:
        return [_item("感知插件", "fail", "未启用任何插件",
                      "系统不会产生任何事件。在「配置 → 感知插件」里至少启用一个")]
    for name in enabled:
        if not have.get(name):
            out.append(_item(f"插件 {name}", "fail", "依赖不满足或插件不存在",
                             "检查插件名拼写，或安装缺失依赖"))
            continue
        # 真正实例化一次：配置错误（如后端不可用）只有这时才暴露
        try:
            sensor = sensors_mod.create(name, cfg, int(cfg.camera.fps))
            desc = sensor.describe()
            sensor.close()
            detail = desc.get("detail", "")
            extra = f"　后端 {desc['backend']}" if desc.get("backend") else ""
            out.append(_item(f"插件 {name}", "ok", f"{detail}{extra}"))
        except Exception as exc:
            out.append(_item(f"插件 {name}", "fail", f"初始化失败：{exc}",
                             "检查该插件对应的配置项"))
    return out


def _identity(cfg) -> list[dict[str, Any]]:
    from ..core import identity as identity_mod

    out: list[dict[str, Any]] = []
    resolver = identity_mod.create_enabled(cfg)
    if not resolver.providers:
        return [_item("身份识别", "warn", "没有可用的识别器",
                      "事件将无法归属到住户，记忆与个体化分级都不会生效")]
    names = " → ".join(p.name for p in resolver.providers)
    out.append(_item("识别顺序", "ok", names,
                     "按此顺序尝试，先返回结果者胜出；认不出人不会阻断处置"))

    rooms = len(cfg.get("identity.rooms") or [])
    bands = len(cfg.get("identity.bands") or [])
    out.append(_item("绑定关系", "ok" if (rooms or bands) else "warn",
                     f"{rooms} 条房间绑定，{bands} 条手环绑定",
                     "" if (rooms or bands)
                     else "未登记绑定关系，会退回使用「场景」里配的住户，"
                          "多住户场景下记忆会混在一起"))

    if not identity_mod.available().get("face"):
        out.append(_item("人脸识别", "skip", "接口预留，未实现",
                         "原因见 fallguard/identity/face.py："
                         "夜间与倒地侧脸识别率不足，且人脸底库属敏感生物特征"))
    return out


def _devices(cfg) -> list[dict[str, Any]]:
    from .. import devices as devices_mod

    entries = list(cfg.get("devices.sources") or [])
    if not entries:
        return [_item("外部设备", "warn", "未接入任何设备（纯视觉模式）",
                      "接入手环可提供心率与步态信号，让风险预警在跌倒前就能发出。"
                      "没有实物时可先配 type=synthetic_band 的模拟手环验证链路")]
    out: list[dict[str, Any]] = []
    have = devices_mod.available()
    for entry in entries:
        dtype = str(entry.get("type") or "")
        did = str(entry.get("device_id") or dtype)
        if not entry.get("enabled", True):
            out.append(_item(f"设备 {did}", "skip", f"{dtype}（已禁用）"))
            continue
        if not have.get(dtype):
            out.append(_item(f"设备 {did}", "fail", f"未知设备类型 {dtype!r}",
                             f"可选：{', '.join(sorted(have))}"))
            continue
        note = "模拟数据，仅用于验证逻辑，上线前须换成 http_push" \
            if dtype == "synthetic_band" else "等待厂商网关 POST /api/device/push"
        out.append(_item(f"设备 {did}", "ok", f"{dtype} — {note}"))
    return out


def _risk(cfg) -> list[dict[str, Any]]:
    enabled = bool(cfg.get("risk.enabled", True))
    if not enabled:
        return [_item("风险预警", "warn", "已关闭",
                      "关闭后系统只能在事件发生后告警，失去预测能力")]
    warn = float(cfg.get("risk.warn_score", 45.0))
    elev = float(cfg.get("risk.elevated_score", 70.0))
    out = [_item("风险预警", "ok", f"关注 ≥{warn:g} 分　偏高 ≥{elev:g} 分",
                 "预警走不打扰路径：只记录与提示，绝不外呼。"
                 "跌倒告警不受它影响，反之亦然")]
    if not list(cfg.get("devices.sources") or []):
        out.append(_item("预警信号源", "warn", "只有摄像头的姿态稳定度",
                         "心率、步态等信号需要手环。纯视觉下预警能力有限，"
                         "因为姿态异常往往与跌倒几乎同时发生"))
    return out


def report(config_path: str | None = None, *, running: bool = False) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    src = cfgmod.config_source(config_path)
    groups.append({"title": "环境", "items": [
        _item("Python", "ok", f"{sys.version.split()[0]} ({sys.platform})"),
        _item("配置文件", "ok" if src.exists() else "warn", str(src),
              "" if src.exists() else "尚未保存过配置，当前使用示例配置的默认值"),
    ] + _deps()})
    groups.append({"title": "本机告警", "items": _local_tools()})

    try:
        cfg = cfgmod.load(config_path)
    except Exception as exc:
        groups.append({"title": "配置", "items": [
            _item("加载配置", "fail", str(exc), "检查 config.toml 语法")]})
        return _summarize(groups)

    groups.append({"title": "视频源", "items": _camera(cfg, running)})
    groups.append({"title": "感知插件", "items": _sensors(cfg)})
    groups.append({"title": "外部设备", "items": _devices(cfg)})
    groups.append({"title": "身份识别", "items": _identity(cfg)})
    groups.append({"title": "风险预警", "items": _risk(cfg)})
    groups.append({"title": "外呼", "items": _call(cfg)})
    groups.append({"title": "智能体", "items": _agent(cfg)})
    groups.append({"title": "判定", "items": _thresholds(cfg)})
    return _summarize(groups)


def _summarize(groups: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for g in groups:
        for it in g["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")
    return {"groups": groups, "counts": counts, "overall": overall}
