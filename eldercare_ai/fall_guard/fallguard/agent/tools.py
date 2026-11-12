# -*- coding: utf-8 -*-
"""智能体工具集。

每个工具都是"声明式 schema + 幂等实现"，与技术文档第 5.4 节的工具表对应。
工具自身承担两件事：执行动作、把动作写进事件记录（留痕）。
智能体只能通过这些工具影响世界——它拿不到网络、文件系统和 shell。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..alerts.call import CallGateway, CallResult
from ..alerts.local import LocalAlerter
from ..events import EventRecord, iso


@dataclass
class ToolContext:
    """工具运行所需的一切；避免工具直接访问全局状态。

    narration 由感知插件的 narrate() 提供，决定播报说什么——框架与工具
    都不知道"跌倒"是什么，事件语义全部来自插件。
    memory 是住户记忆摘要，进智能体 prompt。

    state 是工具私有的幂等状态袋：工具自己往里放键，不要为了一个工具的
    去重需求去改这个 dataclass（原先的 _spoken 就是这么长出来的）。
    """

    record: EventRecord
    gateway: CallGateway
    alerter: LocalAlerter
    scene: dict[str, str]
    slots: dict[str, str]
    called: list[CallResult] = field(default_factory=list)
    log: Callable[[str], None] = print
    narration: dict[str, str] = field(default_factory=dict)
    memory: str = ""
    #: 健康趋势摘要（聚合统计量，不含逐条读数）。隐私边界在这里划定：
    #: 原始时序会暴露住户的作息细节（几点起夜、几点出门），
    #: 而趋势判断用不到那个粒度。
    health: str = ""
    state: dict[str, Any] = field(default_factory=dict)

    def note(self, text: str) -> None:
        self.record.notes.append(text)
        self.log(text)

    def scratch(self, key: str, factory: Callable[[], Any]) -> Any:
        """取/建某个工具的私有状态。"""
        if key not in self.state:
            self.state[key] = factory()
        return self.state[key]


def _record_action(ctx: ToolContext, tool: str, args: dict[str, Any],
                   output: dict[str, Any], status: str, t0: float) -> None:
    ctx.record.actions.append({
        "at": iso(time.time()),
        "tool": tool,
        "input": args,
        "output": output,
        "status": status,
        "latency_ms": int((time.time() - t0) * 1000),
    })


# --------------------------------------------------------------------- 工具
def tool_speak(ctx: ToolContext, text: str = "", **_) -> dict[str, Any]:
    """现场语音播报。同一段文本只播一次，防止智能体重复调用轰炸现场。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "text 为空"}
    spoken = ctx.scratch("spoken", set)
    key = text[:80]
    if key in spoken:
        return {"ok": True, "skipped": "该文本已播报过"}
    spoken.add(key)
    ctx.alerter.speak(text)
    return {"ok": True, "spoken": text}


def tool_place_call(ctx: ToolContext, contact_id: str = "", reason: str = "",
                    **_) -> dict[str, Any]:
    """向指定联系人拨打语音电话。"""
    contact = ctx.gateway.contact_by_id(contact_id)
    if contact is None:
        return {
            "ok": False,
            "reason": f"未知 contact_id={contact_id!r}",
            "available": [c.id for c in ctx.gateway.contacts],
        }
    if any(r.contact_id == contact.id and r.ok for r in ctx.called):
        return {"ok": True, "skipped": "该联系人本次事件已呼通"}
    if len(ctx.called) >= ctx.gateway.max_calls:
        return {"ok": False, "reason": f"已达本次事件外呼上限 {ctx.gateway.max_calls}"}

    res = ctx.gateway.call_one(contact, ctx.slots, ctx.record.event_id)
    ctx.called.append(res)
    ctx.note(f"[外呼] {contact.name}（{contact.role}）→ "
             f"{'成功' if res.ok else '失败'} {res.code} {res.message} "
             f"{'[演练]' if res.dry_run else ''} 理由：{reason or '未说明'}")
    return {"ok": res.ok, "contact": contact.name, "role": contact.role,
            "code": res.code, "message": res.message, "dry_run": res.dry_run}


def tool_notify_desktop(ctx: ToolContext, title: str = "", message: str = "",
                        **_) -> dict[str, Any]:
    """弹出系统通知。"""
    title = title or ctx.narration.get("title", "预警")
    ctx.alerter.notify(title, message or ctx.narration.get("desktop", "检测到异常"))
    return {"ok": True}


def tool_get_contacts(ctx: ToolContext, **_) -> dict[str, Any]:
    """查询可呼叫的联系人列表。"""
    return {"contacts": [
        {"id": c.id, "name": c.name, "role": c.role} for c in ctx.gateway.contacts
    ], "max_calls_per_event": ctx.gateway.max_calls}


def tool_get_evidence(ctx: ToolContext, **_) -> dict[str, Any]:
    """查询本次事件已留存的证据与判定特征。"""
    return {
        "event_type": ctx.record.event_type,
        "features": ctx.record.features,
        "trigger": ctx.record.trigger,
        "confidence": ctx.record.confidence,
        "grading": ctx.record.grading,
        "evidence": ctx.record.evidence,
    }


def tool_get_resident_history(ctx: ToolContext, **_) -> dict[str, Any]:
    """查询该住户的画像与近期事件史。

    让智能体能主动问"这人以前怎么样"，而不是只能被动接受 prompt 里
    塞的那段摘要。记忆由框架在处置前召回，这里只是暴露给模型。
    """
    return {
        "resident": ctx.scene.get("resident_name", ""),
        "resident_id": ctx.record.resident_id,
        "summary": ctx.memory or "无历史记录",
        "history": (ctx.record.memory or {}).get("history", {}),
        "profile": (ctx.record.memory or {}).get("profile", {}),
    }


def tool_get_health_trend(ctx: ToolContext, **_) -> dict[str, Any]:
    """查询该住户来自可穿戴设备的健康趋势（静息心率、HRV、步态、睡眠等）。

    只返回聚合统计量与趋势方向，不含逐条读数——那个粒度会暴露住户的
    作息细节，而判断趋势用不到。

    注意这些数据**按小时甚至按天更新**，说明的是"最近状态在变差"，
    不是"此刻有危险"。不要拿它推翻刚刚由确定性规则作出的现场判定。
    """
    mem = ctx.record.memory or {}
    text = ctx.health or mem.get("health", "")
    if not text:
        return {"available": False,
                "reason": "该住户没有接入可穿戴设备，或尚未积累足够数据"}
    return {
        "available": True,
        "summary": text,
        "concerning": mem.get("health_concerning", []),
        "note": "按天更新的趋势数据，反映近期状态变化，不代表此刻的紧急程度",
    }


def tool_finish(ctx: ToolContext, summary: str = "", level: str = "",
                **_) -> dict[str, Any]:
    """结束处置并给出结论摘要。"""
    if summary:
        ctx.record.agent["summary"] = summary
    if level in {"L1", "L2", "L3", "L4"}:
        ctx.record.level = level
    return {"ok": True, "summary": summary, "level": ctx.record.level}


REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_contacts": tool_get_contacts,
    "get_evidence": tool_get_evidence,
    "get_resident_history": tool_get_resident_history,
    "get_health_trend": tool_get_health_trend,
    "speak": tool_speak,
    "notify_desktop": tool_notify_desktop,
    "place_call": tool_place_call,
    "finish": tool_finish,
}

SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_contacts",
            "description": "查询本机构可外呼的联系人及本次事件的外呼上限。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evidence",
            "description": "查询本次判定的传感器特征、触发路径、分级依据与已留存证据。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resident_history",
            "description": ("查询该住户的画像（跌倒史、认知症、用药）与近期事件史。"
                            "历史误报多时处置要更谨慎，但不得因此跳过外呼。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_health_trend",
            "description": ("查询该住户来自可穿戴设备（Apple Watch 等）的健康趋势："
                            "静息心率、心率变异性、步行稳定性、睡眠等相对其个体基线"
                            "的变化。这些数据按天更新，说明的是近期状态走向，"
                            "不代表此刻的紧急程度，不要用它推翻现场判定。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "在现场用扬声器播报一句话（安抚老人或呼唤附近人员）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要播报的中文短句"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_desktop",
            "description": "弹出本机系统通知，提醒值班台。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_call",
            "description": ("向指定联系人拨打语音电话并播报四要素（位置、姓名、事件、时间）。"
                            "先呼护理方（role=caregiver），确认无法联系时再呼家属。"),            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "联系人 id"},
                    "reason": {"type": "string", "description": "为什么呼这个人"},
                },
                "required": ["contact_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "处置动作已全部完成时调用，给出结论摘要与最终风险等级。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "一句话处置摘要"},
                    "level": {"type": "string", "enum": ["L1", "L2", "L3", "L4"]},
                },
                "required": ["summary"],
            },
        },
    },
]


def execute(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    fn = REGISTRY.get(name)
    t0 = time.time()
    if fn is None:
        out = {"ok": False, "reason": f"未知工具 {name!r}",
               "available": list(REGISTRY)}
        _record_action(ctx, name, args, out, "failed", t0)
        return out
    try:
        out = fn(ctx, **args)
        status = "ok" if out.get("ok", True) else "failed"
    except TypeError as exc:
        out = {"ok": False, "reason": f"参数错误：{exc}"}
        status = "failed"
    except Exception as exc:                      # 工具异常不能拖垮处置链路
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        status = "failed"
    _record_action(ctx, name, args, out, status, t0)
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
