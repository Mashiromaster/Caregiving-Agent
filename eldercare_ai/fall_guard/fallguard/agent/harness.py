# -*- coding: utf-8 -*-
"""最小化智能体 harness：OpenAI 兼容的 function-calling 循环。

对接 DeepSeek（api.deepseek.com/chat/completions），也可指向任意 OpenAI 兼容端点
（vLLM / Ollama / 本地推理服务），只需改 agent.base_url 与 model。
只用标准库 urllib，不引入 SDK。

两条底线写在代码里而不是提示词里：
  · deadline —— 超过 deadline_seconds 立即中止循环，交给确定性兜底
  · fallback —— 循环结束后若一通电话都没打出去，由兜底逻辑强制外呼

所以模型不可靠（超时、胡说、拒绝回答、API 欠费）都不会导致"没人去救"。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import tools as T

SYSTEM_PROMPT = """你是养老机构的值班看护智能体，负责处置刚刚判定成立的异常事件。

你的职责：在最短时间内让"能去现场的人"知道并前往，并留下可追溯的处置记录。

处置纪律（必须遵守）：
1. 先呼护理方（role=caregiver），这是能最快到现场的人；确认护理方无法联系后才呼家属（role=family）。
2. 外呼理由必须具体，写清为什么呼这个人。
3. 可以用 speak 在现场播报，安抚老人并告知"已通知工作人员"。若事件是跌倒类，
   务必提醒不要自行起身——跌倒后自行起身是二次伤害的主因。
4. 不要重复呼叫同一个人；不要超过外呼上限。
5. 住户历史误报多时处置可更谨慎地措辞，但**不得因此跳过外呼**——
   判定已由确定性规则作出，你的职责是执行处置而不是推翻判定。
6. 可穿戴设备的健康趋势（静息心率、步行稳定性等）按天更新，说明的是近期
   状态走向，**不代表此刻的紧急程度**。可以用它让通知内容更有信息量
   （如"该住户近两周步行稳定性持续下降"），但不得用它推翻现场判定。
7. 全部动作完成后调用 finish 给出一句话摘要和最终等级。

风险等级口径：L4=疑似生命体征危急；L3=需立即到场；L2=需关注；L1=仅记录。
事件的初始等级已由规则引擎给出，除非你发现了规则未知的信息，否则沿用它。
动作要快，不要反复查询信息，最多调用 2 次查询类工具。你的回复不会被人读到，只有工具调用才有效果。"""


@dataclass
class AgentOutcome:
    mode: str                                   # llm | planner | fallback
    steps: int = 0
    summary: str = ""
    error: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "steps": self.steps,
            "summary": self.summary,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "transcript": self.transcript,
        }


def _chat(base_url: str, api_key: str, payload: dict[str, Any],
          timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/v1/chat/completions" if "/v1" not in url \
            else url + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _brief(ctx: T.ToolContext) -> str:
    """事件情境。事件语义全部来自插件的 narrate()，这里不认识任何具体事件类型。"""
    rec = ctx.record
    payload = {
        "event_id": rec.event_id,
        "event_type": rec.event_type,
        "detected_at": ctx.slots.get("time"),
        "place": ctx.scene.get("place"),
        "resident": ctx.scene.get("resident_name"),
        "level": rec.level,
        "grading": {k: rec.grading.get(k) for k in ("rule_id", "reason")},
        "trigger": rec.trigger,
        "confidence": rec.confidence,
        "features": rec.features,
        "situation": ctx.narration.get("brief", "异常事件已判定成立，请立即处置。"),
    }
    if ctx.memory:
        payload["resident_memory"] = ctx.memory
    if ctx.health:
        # 只放聚合摘要。逐条读数会暴露住户作息细节（几点起夜、几点出门），
        # 而处置判断用不到那个粒度——这是发给外部模型的隐私边界。
        payload["health_trend"] = ctx.health
    return json.dumps(payload, ensure_ascii=False)


class RulePlanner:
    """无 LLM 时的确定性处置：严格按升级链执行。

    也是 LLM 路径的参照实现——两者调用同一套工具，便于对比行为差异。
    """

    mode = "planner"

    def run(self, ctx: T.ToolContext) -> AgentOutcome:
        t0 = time.time()
        out = AgentOutcome(mode=self.mode)
        narration = ctx.narration
        noun = narration.get("noun", "异常")
        T.execute("notify_desktop", {
            "title": narration.get("title", f"{noun}预警"),
            "message": narration.get(
                "desktop",
                f"{ctx.scene.get('place','')} {ctx.scene.get('resident_name','')} "
                f"疑似{noun}，正在外呼"),
        }, ctx)
        reassure = narration.get("reassure")
        if reassure:
            T.execute("speak", {"text": reassure}, ctx)

        caregivers = [c for c in ctx.gateway.contacts if c.role == "caregiver"]
        others = [c for c in ctx.gateway.contacts if c.role != "caregiver"]
        for contact in (caregivers + others)[: ctx.gateway.max_calls]:
            res = T.execute("place_call", {
                "contact_id": contact.id,
                "reason": "按升级链顺序外呼（确定性兜底）",
            }, ctx)
            out.steps += 1
            if res.get("ok") and not ctx.gateway.call_all:
                break
        T.execute("finish", {
            "summary": "按预设升级链完成外呼与现场播报",
            "level": ctx.record.level,
        }, ctx)
        out.summary = ctx.record.agent.get("summary", "")
        out.elapsed_ms = int((time.time() - t0) * 1000)
        return out


class LLMAgent:
    """function-calling 循环。"""

    mode = "llm"

    def __init__(self, cfg_agent):
        self.base_url = str(cfg_agent.base_url)
        self.api_key = str(cfg_agent.api_key or "")
        self.model = str(cfg_agent.model)
        self.temperature = float(cfg_agent.temperature)
        self.max_steps = int(cfg_agent.max_steps)
        self.deadline = float(cfg_agent.deadline_seconds)

    @property
    def usable(self) -> bool:
        return bool(self.api_key)

    def run(self, ctx: T.ToolContext) -> AgentOutcome:
        started = time.time()
        out = AgentOutcome(mode=self.mode)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "事件已判定成立，请立即处置。事件详情：\n"
                                        + _brief(ctx)},
        ]

        for step in range(self.max_steps):
            remain = self.deadline - (time.time() - started)
            if remain <= 1.0:
                out.error = f"超过 deadline {self.deadline:.0f}s，移交确定性兜底"
                break
            try:
                resp = _chat(self.base_url, self.api_key, {
                    "model": self.model,
                    "messages": messages,
                    "tools": T.SCHEMAS,
                    "tool_choice": "auto",
                    "temperature": self.temperature,
                }, timeout=min(remain, 15.0))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                out.error = f"HTTP {exc.code}: {detail}"
                break
            except Exception as exc:
                out.error = f"{type(exc).__name__}: {exc}"
                break

            choices = resp.get("choices") or []
            if not choices:
                out.error = "模型返回为空"
                break
            msg = choices[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            })
            out.transcript.append({
                "step": step + 1,
                "content": (msg.get("content") or "")[:400],
                "tool_calls": [c.get("function", {}).get("name") for c in calls],
            })

            if not calls:
                # 模型只说话不动手：再给一次机会，明确要求调用工具
                if step == 0:
                    messages.append({
                        "role": "user",
                        "content": "请不要只回复文字，直接调用工具执行处置。",
                    })
                    continue
                out.summary = (msg.get("content") or "")[:200]
                break

            finished = False
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                result = T.execute(name, args, ctx)
                out.steps += 1
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": T.dumps(result),
                })
                if name == "finish":
                    finished = True
            if finished:
                break

        out.summary = out.summary or ctx.record.agent.get("summary", "")
        out.elapsed_ms = int((time.time() - started) * 1000)
        return out


def handle(cfg, ctx: T.ToolContext) -> AgentOutcome:
    """处置入口：LLM 优先，失败降级规则planner，最后强制兜底外呼。"""
    agent_cfg = cfg.agent
    outcome: AgentOutcome

    if os.environ.get("FALLGUARD_DSH_MODE") == "1":
        # DSH 承担唯一模型循环；实时告警仍由确定性处置保证时限与外呼。
        outcome = RulePlanner().run(ctx)
    elif bool(agent_cfg.enabled):
        llm = LLMAgent(agent_cfg)
        if llm.usable:
            outcome = llm.run(ctx)
            if outcome.error:
                ctx.note(f"[智能体] LLM 异常：{outcome.error}")
        else:
            ctx.note("[智能体] 未配置 agent.api_key，走确定性处置")
            outcome = RulePlanner().run(ctx)
            outcome.error = "agent.api_key 未配置"
    else:
        outcome = RulePlanner().run(ctx)

    # 兜底：无论上面发生了什么，至少要有一通电话打出去
    succeeded = any(r.ok for r in ctx.called)
    if not succeeded and ctx.gateway.contacts:
        attempted = {r.contact_id for r in ctx.called}
        remaining = [c for c in ctx.gateway.contacts if c.id not in attempted]
        if remaining or not ctx.called:
            ctx.note("[兜底] 智能体未完成有效外呼，按升级链强制拨号")
            ctx.record.fallback_fired = True
            for contact in (remaining or ctx.gateway.contacts)[: ctx.gateway.max_calls]:
                res = T.execute("place_call", {
                    "contact_id": contact.id,
                    "reason": "确定性兜底：智能体未完成有效外呼",
                }, ctx)
                if res.get("ok") and not ctx.gateway.call_all:
                    break
    return outcome
