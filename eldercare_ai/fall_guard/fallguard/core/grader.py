# -*- coding: utf-8 -*-
"""确定性分级：事件 + 住户画像 + 时段 → L1~L4。

**大模型不参与分级。** 这是技术文档 ADR 决策 3 的硬要求，原因是代价不对称：
L3/L4 直接决定是否深夜叫醒护工、是否呼叫 120，漏报危及生命，误报摧毁信任。
监管核查与纠纷举证要求能回答"为什么当时打了这个电话"，答案必须是
"规则 R-FALL-01 命中，因为下降速率 0.44/s 且静止 2.53s"，
而不是"模型判断应该打"。

所以这里是一张规则表，每条有 ID、有条件、有人话理由，命中结果连同
理由一起写进事件 timeline，可按事件号回放完整决策链。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

LEVELS = ("L1", "L2", "L3", "L4")
LEVEL_DESC = {
    "L1": "提示 · 仅记录，计入日报，不打扰",
    "L2": "关注 · 通知责任护工，超时升级",
    "L3": "紧急 · 护士站声光 + 外呼，30 秒未确认升级主管",
    "L4": "危急 · 并发呼叫多方 + 120",
}


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


@dataclass
class Rule:
    """一条分级规则。

    when 收到 (event, profile, context) 三元组，返回 bool。
    故意不做表达式 DSL——真要做必须带沙箱（技术文档要求无网络无文件访问、
    50ms 超时），而现阶段规则都由代码定义，Python 函数既可读又可测。
    配置化留到有人需要在界面上改规则时再说。
    """

    id: str
    kind: str                                    # 匹配的事件类型，"*" 为通配
    level: str
    reason: str
    when: Callable[[Any, dict, dict], bool]
    priority: int = 0                            # 同等级下谁先被记为命中理由


@dataclass
class Grading:
    level: str
    rule_id: str
    reason: str
    matched: list[dict[str, str]] = field(default_factory=list)
    escalated_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "rule_id": self.rule_id, "reason": self.reason,
                "matched": self.matched, "escalated_by": self.escalated_by,
                "description": LEVEL_DESC.get(self.level, "")}


def _high_risk(profile: dict) -> bool:
    """住户是否高跌倒风险。对应技术文档的住户画像输入。"""
    if not profile:
        return False
    return bool(profile.get("high_fall_risk")
                or profile.get("dementia")
                or profile.get("anticoagulant")
                or (profile.get("fall_history") or 0) >= 1)


# 规则 ID 沿用技术文档的命名（R-<域>-<序号>），便于与文档对照
DEFAULT_RULES: list[Rule] = [
    Rule("R-FALL-01", "fall", "L3",
         "双条件路径成立（捕捉到下降过程且随后静止躺倒）",
         lambda ev, prof, ctx: ev.trigger == "descent+lying",
         priority=10),
    Rule("R-FALL-02", "fall", "L3",
         "单条件路径，但住户为高跌倒风险（跌倒史/认知症/抗凝药）",
         lambda ev, prof, ctx: ev.trigger != "descent+lying" and _high_risk(prof),
         priority=9),
    Rule("R-FALL-03", "fall", "L2",
         "单条件路径且住户非高危，置信度不足以直接判紧急",
         lambda ev, prof, ctx: ev.trigger != "descent+lying" and not _high_risk(prof),
         priority=8),
    Rule("R-TEST-01", "manual_test", "L2",
         "人工演练触发，不代表真实事件",
         lambda ev, prof, ctx: True,
         priority=5),
]


class Grader:
    def __init__(self, rules: list[Rule] | None = None):
        self.rules = list(rules if rules is not None else DEFAULT_RULES)

    def register(self, rule: Rule) -> None:
        self.rules.append(rule)

    def grade(self, event, profile: dict | None = None,
              context: dict | None = None) -> Grading:
        profile = profile or {}
        context = context or {}
        matched: list[dict[str, str]] = []
        best: Rule | None = None

        for rule in self.rules:
            if rule.kind not in ("*", event.kind):
                continue
            try:
                hit = bool(rule.when(event, profile, context))
            except Exception:
                # 规则异常按未命中处理：一条坏规则不能让整个分级失败，
                # 否则事件会卡在无级可定的状态而根本不被处置
                hit = False
            if not hit:
                continue
            matched.append({"rule_id": rule.id, "level": rule.level,
                            "reason": rule.reason})
            # 同一事件命中多条时取最高等级；同级取 priority 大者作为主理由
            if best is None or (_rank(rule.level), rule.priority) > \
                    (_rank(best.level), best.priority):
                best = rule

        if best is None:
            # 规则无命中 → 降级为 L1 记录（技术文档的降级策略）。
            # 不能因为没写规则就把事件丢掉。
            return Grading(level="L1", rule_id="R-DEFAULT",
                           reason=f"无匹配规则，按提示级记录（事件类型 {event.kind}）",
                           matched=matched)

        grading = Grading(level=best.level, rule_id=best.id, reason=best.reason,
                          matched=matched)
        self._apply_night_shift(grading, context)
        return grading

    def _apply_night_shift(self, grading: Grading, context: dict) -> None:
        """夜间修正：22:00-06:00 的 L1 提升为 L2。

        依据是技术文档的时段修正条款——夜间无人在场的概率更高，
        同样的现象在白天可能有人顺手处理，夜里不会。
        只升一级且只作用于最低级，不碰 L3/L4（那两级本来就要立即处置）。
        """
        if grading.level != "L1":
            return
        hour = context.get("hour")
        if hour is None:
            hour = time.localtime(context.get("now") or time.time()).tm_hour
        if hour >= 22 or hour < 6:
            grading.level = "L2"
            grading.escalated_by = "夜间时段（22:00-06:00）无人在场概率更高，提升一级"


__all__ = ["Grader", "Grading", "Rule", "DEFAULT_RULES", "LEVELS", "LEVEL_DESC"]
