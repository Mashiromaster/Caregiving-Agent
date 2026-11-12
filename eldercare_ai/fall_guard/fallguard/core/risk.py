# -*- coding: utf-8 -*-
"""风险评分：多源连续信号 → 风险分 → 预警。

这是"预警、防范"与"事后告警"的分界线。跌倒检测只能在人倒下之后开口；
风险分看的是趋势——步态稳定度在往下滑、心率在往上窜、夜里起身次数
变多——在事件成立之前就能提示"这位住户现在状态不对，去看一眼"。

**两条通路必须独立，这是本模块最重要的约束：**

    信号流 ──> RiskScorer ──> 预警 L1/L2 ──> 看板记录 / App 提示（不打扰）
       │
       └────> Sensor ──────> 告警 L3/L4 ──> 外呼 + 声光（打扰）

风险分再高也**不会**自动外呼；跌倒判定也**不等**风险分同意。
理由是故障隔离：评分算法是启发式的、参数是拍脑袋调的、将来还可能换成
模型——这类东西出问题不能连累"有人跌倒要打电话"这条命脉。
反过来，确定性判定漏报时，风险预警能提供一层补充提示。

评分方式是加权因子求和而非模型：每一分的来源都要能说清楚
（"步态稳定度 0.31，低于 0.45 阈值，贡献 28 分"），这样护工看到预警
能判断要不要去看，而不是面对一个无法解释的数字。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import signals as sig

# 预警阈值。刻意设得比"确信"更低——预警的代价是看一眼，
# 漏掉前兆的代价是一次跌倒，不对称。
WARN_SCORE = 45.0
ELEVATED_SCORE = 70.0

#: 同一住户两次预警之间的最小间隔，防止分数在阈值附近抖动时刷屏
WARN_COOLDOWN_S = 600.0


@dataclass
class Factor:
    """一个评分因子。

    每个因子独立计算、独立解释。weight 是满分贡献，score() 返回 0~1 的
    触发程度，两者相乘即该因子的得分。
    """

    id: str
    label: str
    weight: float
    score: Callable[["RiskInput"], tuple[float, str] | None]
    #: 慢性因子（天级趋势）。它们会连续数天保持命中，预警静默期要按天算，
    #: 否则一条\"静息心率偏高\"会每 10 分钟提醒一次。
    slow: bool = False


@dataclass
class RiskInput:
    bus: sig.SignalBus
    now: float
    profile: dict[str, Any] = field(default_factory=dict)
    #: 健康档案。有它才能把读数跟**这位住户自己的**历史比——
    #: "心率 85"本身没有意义，"比他自己的静息基线高 18"才有。
    health: Any = None
    resident_id: str = ""

    def deviation(self, name: str, value: float):
        """当前值偏离个体基线几个标准差。没有档案或样本不足时返回 None。"""
        if self.health is None or not self.resident_id:
            return None
        return self.health.deviation(self.resident_id, name, value,
                                     now=self.now)


@dataclass
class RiskAssessment:
    score: float
    level: str                                   # normal | warn | elevated
    factors: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    at: float = 0.0
    #: 是否主要由慢性健康趋势推上去的（急性分不足以单独越阈）。
    #: 决定静默期按分钟算还是按天算。
    slow_driven: bool = False
    acute_score: float = 0.0

    @property
    def should_warn(self) -> bool:
        return self.level in ("warn", "elevated")

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 1), "level": self.level,
                "factors": self.factors, "summary": self.summary,
                "at": round(self.at, 1),
                "slow_driven": self.slow_driven,
                "acute_score": round(self.acute_score, 1)}


# --------------------------------------------------------------- 因子
#
# 所有因子取读数一律走 bus.fresh()，**不要自己写 max_age**。
# 新鲜度跟着信号本身走（见 signals.MAX_AGE）：摄像头 15 秒没数据就是出问题，
# 而 Apple Watch 的静息心率 6 小时前的读数完全正常。这里曾经把 max_age
# 写死成 60/120 秒（为 0.5 秒一推的模拟手环调的），接入手表后每条读数
# 都会被静默丢弃——信号进了总线、评分恒为 0 且不报错。

def _steadiness_norm(signal) -> float | None:
    """把不同来源的\"稳定度\"归一到 0~1（越高越稳）。

    苹果的 walking_steadiness 是 0~100 的百分比，手环/摄像头的是 0~1。
    不归一就会把 \"步行稳定性 85%\" 当成 85 分的超稳，或者反过来。
    """
    if signal is None:
        return None
    if signal.name == sig.WALKING_STEADINESS:
        return max(0.0, min(signal.value / 100.0, 1.0))
    return max(0.0, min(signal.value, 1.0))


#: 稳定度的来源优先级：苹果的步行稳定性是它自家的跌倒风险指标，最可信；
#: 没有就退回手环步态；再没有才用摄像头姿态——但姿态异常往往与跌倒几乎
#: 同时发生，提前量很小，只能算兜底。
STEADINESS_SOURCES = (
    (sig.WALKING_STEADINESS, "步行稳定性"),
    (sig.GAIT_STABILITY, "手环步态"),
    (sig.POSTURE_STABILITY, "姿态"),
)


def _gait_unstable(inp: RiskInput) -> tuple[float, str] | None:
    """步态/姿态不稳。按来源优先级取第一个还新鲜的。"""
    for name, label in STEADINESS_SOURCES:
        got = inp.bus.fresh(name, now=inp.now)
        if got is None or got.quality < 0.5:
            continue
        value = _steadiness_norm(got)
        if value is None or value >= 0.60:
            return None
        # 0.60 → 0 分，0.10 → 满分
        trigger = min((0.60 - value) / 0.50, 1.0)
        shown = f"{got.value:.0f}%" if name == sig.WALKING_STEADINESS \
            else f"{value:.2f}"
        return trigger, f"{label} {shown}，低于安全区间"
    return None


def _gait_declining(inp: RiskInput) -> tuple[float, str] | None:
    """稳定度持续下滑——比绝对值更早的信号。

    手表类与实时类的窗口差了三个量级：摄像头看 90 秒内的抖动，
    步行稳定性要看几天的走向，拿同一个窗口去问两者只会都问不出东西。
    """
    for name, label, window, rate in (
            (sig.WALKING_STEADINESS, "步行稳定性", 14 * 86400.0, 0.00002),
            (sig.WALKING_SPEED, "步速", 14 * 86400.0, 0.0000002),
            (sig.GAIT_STABILITY, "手环步态", 90.0, 0.004),
            (sig.POSTURE_STABILITY, "姿态", 90.0, 0.004)):
        slope = inp.bus.slope(name, window, now=inp.now)
        if slope >= -rate:
            continue
        trigger = min(abs(slope) / (rate * 5), 1.0)
        if window > 86400.0:
            per_week = slope * 7 * 86400.0
            return trigger, f"{label}近两周持续下滑（每周 {per_week:+.1f}）"
        return trigger, f"{label}稳定度在 90 秒内持续下滑（{slope * 60:.2f}/分钟）"
    return None


def _heart_rate_abnormal(inp: RiskInput) -> tuple[float, str] | None:
    """瞬时心率偏离个体基线。

    基线优先用住户自己的历史（健康档案里 30 天的中位数），其次才是
    画像里人工填的值，最后才是全院默认 75。这个顺序就是技术文档说的
    \"把阈值从全院统一改为按人设定\"。
    """
    hr = inp.bus.first_fresh((sig.HEART_RATE, sig.WATCH_HEART_RATE), now=inp.now)
    if hr is None or hr.quality < 0.5:
        return None

    dev = inp.deviation(sig.WATCH_RESTING_HR, hr.value)
    if dev is not None:
        z, base = dev
        if abs(z) < 2.5:
            return None
        trigger = min((abs(z) - 2.5) / 2.5, 1.0)
        word = "高于" if z > 0 else "低于"
        return trigger, (f"心率 {hr.value:.0f}，{word}本人静息基线 "
                         f"{base['median']:.0f}（{abs(z):.1f} 个标准差，"
                         f"基线取自近 {base['days']:.0f} 天 {base['n']} 次测量）")

    baseline = float(inp.profile.get("hr_baseline") or 75.0)
    if hr.value > baseline + 25:
        trigger = min((hr.value - baseline - 25) / 30.0, 1.0)
        return trigger, f"心率 {hr.value:.0f}，高于基线 {baseline:.0f} 超过 25"
    if hr.value < max(baseline - 25, 40):
        trigger = min((baseline - 25 - hr.value) / 20.0, 1.0)
        return trigger, f"心率 {hr.value:.0f}，显著低于基线 {baseline:.0f}"
    return None


def _heart_rate_surge(inp: RiskInput) -> tuple[float, str] | None:
    slope = inp.bus.slope(sig.HEART_RATE, 60.0, now=inp.now)
    if slope <= 0.25:                            # bpm/秒
        return None
    return min(slope / 1.0, 1.0), f"心率在 60 秒内快速上升（{slope * 60:.0f} bpm/分钟）"


def _resting_hr_drift(inp: RiskInput) -> tuple[float, str] | None:
    """静息心率相对本人基线上移。

    这是感染、脱水、心衰加重的早期共同表现，比任何单次读数都稳——
    静息心率一天才算一次，不受活动干扰。但也正因为慢，它只能提示
    \"最近状态在变差\"，不能用来判断此刻有没有危险。
    """
    got = inp.bus.fresh(sig.WATCH_RESTING_HR, now=inp.now)
    if got is None:
        return None
    dev = inp.deviation(sig.WATCH_RESTING_HR, got.value)
    if dev is None:
        return None
    z, base = dev
    if z < 1.5:
        return None
    trigger = min((z - 1.5) / 2.0, 1.0)
    return trigger, (f"静息心率 {got.value:.0f}，高于本人基线 "
                     f"{base['median']:.0f} 达 {z:.1f} 个标准差")


def _hrv_drop(inp: RiskInput) -> tuple[float, str] | None:
    """心率变异性下降 = 自主神经调节储备下降。越低越该关注。"""
    got = inp.bus.fresh(sig.WATCH_HRV, now=inp.now)
    if got is None:
        return None
    dev = inp.deviation(sig.WATCH_HRV, got.value)
    if dev is None:
        return None
    z, base = dev
    if z > -1.5:
        return None
    trigger = min((abs(z) - 1.5) / 2.0, 1.0)
    return trigger, (f"心率变异性 {got.value:.0f}ms，低于本人基线 "
                     f"{base['median']:.0f}ms 达 {abs(z):.1f} 个标准差")


def _spo2_low(inp: RiskInput) -> tuple[float, str] | None:
    """血氧偏低。这里用绝对阈值而非个体基线——血氧的安全下限是生理常数，
    不因人而异，个体基线在这一项上帮不上忙。"""
    got = inp.bus.fresh(sig.WATCH_SPO2, now=inp.now)
    if got is None or got.value <= 0:
        return None
    if got.value >= 94.0:
        return None
    trigger = min((94.0 - got.value) / 6.0, 1.0)
    return trigger, f"血氧 {got.value:.0f}%，低于 94%"


def _sleep_fragmented(inp: RiskInput) -> tuple[float, str] | None:
    """夜间觉醒增多。与夜间跌倒直接相关——起夜次数越多，暴露越多。"""
    got = inp.bus.fresh(sig.SLEEP_AWAKENINGS, now=inp.now)
    if got is None:
        return None
    dev = inp.deviation(sig.SLEEP_AWAKENINGS, got.value)
    if dev is None:
        if got.value < 4:
            return None
        return min((got.value - 4) / 4.0, 1.0), f"夜间觉醒 {got.value:.0f} 次"
    z, base = dev
    if z < 1.5:
        return None
    return min((z - 1.5) / 2.0, 1.0), (
        f"夜间觉醒 {got.value:.0f} 次，多于本人常态 {base['median']:.0f} 次")


def _impact_detected(inp: RiskInput) -> tuple[float, str] | None:
    """加速度尖峰。注意这只是风险信号，不等于判定跌倒——
    坐下过猛、手环磕碰都会产生尖峰，所以它只加分不触发告警。"""
    items = inp.bus.window_of(sig.ACCEL_MAG, 20.0, now=inp.now)
    peak = max((s.value for s in items if s.quality >= 0.5), default=0.0)
    if peak < 2.0:
        return None
    return min((peak - 2.0) / 1.5, 1.0), f"近 20 秒检测到加速度尖峰 {peak:.1f}g"


def _night_time(inp: RiskInput) -> tuple[float, str] | None:
    hour = time.localtime(inp.now).tm_hour
    if hour >= 22 or hour < 6:
        return 1.0, "当前为夜间时段（22:00-06:00），无人在场概率更高"
    return None


def _known_high_risk(inp: RiskInput) -> tuple[float, str] | None:
    p = inp.profile
    tags = []
    if p.get("high_fall_risk"):
        tags.append("标记为高跌倒风险")
    if p.get("dementia"):
        tags.append("认知症")
    if (p.get("fall_history") or 0) >= 1:
        tags.append(f"{p['fall_history']} 次跌倒史")
    if not tags:
        return None
    return min(len(tags) / 2.0, 1.0), "住户画像：" + "、".join(tags)


DEFAULT_FACTORS: list[Factor] = [
    # 急性因子：分钟级变化，提示"此刻需要去看一眼"
    Factor("gait_unstable", "步态不稳", 30.0, _gait_unstable),
    Factor("gait_declining", "稳定度下滑", 20.0, _gait_declining),
    Factor("hr_abnormal", "心率异常", 20.0, _heart_rate_abnormal),
    Factor("hr_surge", "心率骤升", 15.0, _heart_rate_surge),
    Factor("impact", "疑似撞击", 15.0, _impact_detected),
    Factor("night", "夜间时段", 8.0, _night_time),
    Factor("profile", "住户高危", 12.0, _known_high_risk),
    # 慢性因子：天级变化，提示"最近状态在变差"。权重刻意压低——
    # 它们说明不了此刻有没有危险，不该单凭它们把护工叫过去。
    Factor("resting_hr_drift", "静息心率上移", 12.0, _resting_hr_drift, slow=True),
    Factor("hrv_drop", "心率变异性下降", 10.0, _hrv_drop, slow=True),
    Factor("spo2_low", "血氧偏低", 14.0, _spo2_low, slow=True),
    Factor("sleep_fragmented", "睡眠碎片化", 8.0, _sleep_fragmented, slow=True),
]

#: 慢性因子主导时的预警静默期。健康趋势会连续几天维持在阈值以上，
#: 按急性因子的 10 分钟静默期算就是一天 144 条——告警疲劳是同类产品
#: 落地失败的首因，预警路径同样适用这条。一天提醒一次足够了。
SLOW_COOLDOWN_S = 20 * 3600.0


class RiskScorer:
    def __init__(self, bus: sig.SignalBus, factors: list[Factor] | None = None,
                 warn_score: float = WARN_SCORE,
                 elevated_score: float = ELEVATED_SCORE):
        self.bus = bus
        self.factors = list(factors if factors is not None else DEFAULT_FACTORS)
        self.warn_score = float(warn_score)
        self.elevated_score = float(elevated_score)
        self._last_warn_at: float | None = None
        self._last: RiskAssessment | None = None

    def assess(self, profile: dict[str, Any] | None = None,
               now: float | None = None, health: Any = None,
               resident_id: str = "") -> RiskAssessment:
        ref = now if now is not None else time.time()
        inp = RiskInput(bus=self.bus, now=ref, profile=profile or {},
                        health=health, resident_id=resident_id)

        total = 0.0
        acute = 0.0
        hits: list[dict[str, Any]] = []
        for factor in self.factors:
            try:
                got = factor.score(inp)
            except Exception:
                # 单个因子异常不能让整体评分失败
                continue
            if got is None:
                continue
            trigger, reason = got
            points = max(0.0, min(trigger, 1.0)) * factor.weight
            if points <= 0:
                continue
            total += points
            if not factor.slow:
                acute += points
            hits.append({"id": factor.id, "label": factor.label,
                         "points": round(points, 1), "reason": reason,
                         "slow": factor.slow})

        hits.sort(key=lambda h: h["points"], reverse=True)
        level = ("elevated" if total >= self.elevated_score
                 else "warn" if total >= self.warn_score else "normal")
        assessment = RiskAssessment(score=total, level=level, factors=hits, at=ref)
        # 急性分不足以单独越阈时，这条预警是被健康趋势推上去的——
        # 它说的是"最近状态在变差"，不是"此刻有危险"，静默期要按天算
        assessment.slow_driven = acute < self.warn_score
        assessment.acute_score = acute
        assessment.summary = self._summarize(assessment)
        self._last = assessment
        return assessment

    def _summarize(self, a: RiskAssessment) -> str:
        if not a.factors:
            return "各项信号正常。"
        top = "；".join(f["reason"] for f in a.factors[:3])
        head = {"elevated": "风险偏高", "warn": "需要关注", "normal": "风险可控"}[a.level]
        return f"{head}（{a.score:.0f} 分）：{top}。"

    def should_emit_warning(self, assessment: RiskAssessment,
                            cooldown: float = WARN_COOLDOWN_S,
                            slow_cooldown: float = SLOW_COOLDOWN_S) -> bool:
        """是否该发预警。

        带静默期：分数在阈值附近抖动时不应该反复提示——告警疲劳是
        同类产品落地失败的首因，预警路径同样适用这条。

        **慢性趋势用更长的静默期。** 静息心率偏高这类状态会连续几天
        维持在阈值以上，按急性因子的 10 分钟静默期算就是一天 144 条
        内容完全相同的预警，看的人很快就会直接无视这个面板。
        """
        if not assessment.should_warn:
            return False
        gap = slow_cooldown if assessment.slow_driven else cooldown
        if self._last_warn_at is not None and \
                assessment.at - self._last_warn_at < gap:
            return False
        self._last_warn_at = assessment.at
        return True

    @property
    def last(self) -> RiskAssessment | None:
        return self._last


__all__ = ["RiskScorer", "RiskAssessment", "Factor", "RiskInput",
           "DEFAULT_FACTORS", "WARN_SCORE", "ELEVATED_SCORE",
           "SLOW_COOLDOWN_S", "STEADINESS_SOURCES"]
