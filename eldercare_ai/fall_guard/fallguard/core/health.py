# -*- coding: utf-8 -*-
"""健康数据的长期档案与个体基线。

**为什么不能只用 SignalBus**：总线的滑动窗口默认 300 秒，职责是"当前读数"。
而 Apple Watch 的静息心率一天才算一次、步行稳定度要攒够步数才更新，
个体基线更是要数周数据。两件事放一层会互相拖累——总线要快要小，
档案要久要全。

存储沿用 EventLog 的做法：JSONL 只追加，读时按 (指标, 时间) 去重合并。
不引数据库——住户是几十人不是几万，而只追加的文件天然可审计。

**个体基线是这一层存在的理由。** README 把"无个体基线建模"列为已知边界
的第一条，技术文档则把它列为"误报率从 1.2 降到 0.27 次/床·月的首要
贡献项"。有了逐人的历史，"心率 85"才能从一个无意义的数字变成
"比他自己的静息基线高 18"——同一个读数在不同人身上意味着完全不同的事。
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Iterator

DAY = 86400.0

#: 进档案的信号前缀。摄像头每秒 30 条逐帧读数既会把文件撑爆，又不是
#: "健康史"而是"当下状态"——趋势分析不需要它们。
ARCHIVE_PREFIXES = ("watch.", "band.")

#: 同一指标两条记录之间的最小间隔。模拟手环每 0.5 秒推一次，
#: 不限流的话一天就是 17 万行。真实手表数据远稀疏于此，不受影响。
MIN_INTERVAL_S = 60.0

#: 指标的中文名与单位，用于生成人话摘要
LABELS: dict[str, tuple[str, str]] = {
    "watch.resting_hr": ("静息心率", "次/分"),
    "watch.heart_rate": ("心率", "次/分"),
    "watch.hrv": ("心率变异性", "ms"),
    "watch.spo2": ("血氧", "%"),
    "watch.respiratory_rate": ("呼吸率", "次/分"),
    "watch.walking_steadiness": ("步行稳定性", "%"),
    "watch.walking_speed": ("步速", "m/s"),
    "watch.walking_asymmetry": ("步态不对称", "%"),
    "watch.double_support": ("双脚支撑占比", "%"),
    "watch.step_count": ("步数", "步"),
    "watch.active_energy": ("活动消耗", "kcal"),
    "watch.sleep_hours": ("睡眠时长", "小时"),
    "watch.sleep_awakenings": ("夜间觉醒", "次"),
    "watch.stand_hours": ("站立小时", "小时"),
    "band.heart_rate": ("心率(手环)", "次/分"),
}

#: 这些指标"越低越该关注"，其余默认越高越该关注。
#: 趋势解读要靠它判断变化是好是坏——步速下降是风险，步数下降也是，
#: 但心率下降未必。
LOWER_IS_WORSE = frozenset({
    "watch.hrv", "watch.spo2", "watch.walking_steadiness",
    "watch.walking_speed", "watch.step_count", "watch.active_energy",
    "watch.sleep_hours", "watch.stand_hours",
})

#: 变化超过百分之多少才算"有方向"，而不是噪声。
#:
#: **不能一刀切。** 各指标的生理波动幅度差一个量级：静息心率两周涨 7%
#: 是感染或心衰加重的早期信号，而步数天与天之间差 30% 不过是今天没出门。
#: 用同一个门槛，要么把心率的真信号当噪声滤掉，要么被步数的噪声刷屏。
SIGNIFICANT_PCT: dict[str, float] = {
    "watch.resting_hr": 5.0,          # 静息心率很稳，小幅上移就值得注意
    "watch.spo2": 2.0,                # 生理范围极窄，94→92 就是大事
    "watch.respiratory_rate": 10.0,
    "watch.hrv": 15.0,                # 本身日间波动就大
    "watch.walking_steadiness": 8.0,
    "watch.walking_speed": 8.0,
    "watch.walking_asymmetry": 15.0,
    "watch.double_support": 10.0,
    "watch.sleep_hours": 15.0,
    "watch.sleep_awakenings": 30.0,   # 计数类，基数小时相对变化天然夸张
    "watch.step_count": 25.0,         # 出不出门就能差一倍
    "watch.active_energy": 25.0,
    "watch.stand_hours": 20.0,
}
DEFAULT_SIGNIFICANT_PCT = 8.0


def significant_pct(name: str) -> float:
    return SIGNIFICANT_PCT.get(name, DEFAULT_SIGNIFICANT_PCT)


def label_of(name: str) -> str:
    return LABELS.get(name, (name, ""))[0]


def unit_of(name: str) -> str:
    return LABELS.get(name, (name, ""))[1]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


class HealthStore:
    """健康样本的长期档案。"""

    def __init__(self, root: Path, min_interval: float = MIN_INTERVAL_S):
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "health.jsonl"
        self.min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._last_write: dict[tuple[str, str], float] = {}
        self._cache: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------ 写
    def should_archive(self, name: str) -> bool:
        return name.startswith(ARCHIVE_PREFIXES)

    def record(self, resident_id: str, name: str, value: float,
               ts: float | None = None, source: str = "",
               quality: float = 1.0) -> bool:
        """记一条样本。返回是否真的写入（被限流时返回 False）。"""
        if not self.should_archive(name):
            return False
        stamp = float(ts if ts is not None else time.time())
        key = (resident_id or "", name)
        with self._lock:
            last = self._last_write.get(key)
            # 限流按样本时间而不是写入时间：一批历史数据补传进来时，
            # 样本之间本就隔了一小时，不该被当成"刚写过"而丢掉
            if last is not None and abs(stamp - last) < self.min_interval:
                return False
            self._last_write[key] = stamp
            row = {"resident_id": resident_id or "", "name": name,
                   "value": round(float(value), 4), "ts": round(stamp, 3),
                   "source": source, "quality": round(float(quality), 3)}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            self._cache = None
        return True

    def record_signal(self, signal, resident_id: str = "") -> bool:
        """从 SignalBus 的订阅回调接入。"""
        return self.record(resident_id, signal.name, signal.value,
                           ts=signal.ts, source=signal.source,
                           quality=signal.quality)

    # ------------------------------------------------------------ 读
    def _read(self) -> list[dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        rows: list[dict[str, Any]] = []
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        # 同一 (住户, 指标, 时刻) 去重，后写入者生效——Health Auto Export
        # 会重复推送有交叠的时间窗，不去重的话趋势会被重复样本带偏
        merged: dict[tuple[str, str, float], dict[str, Any]] = {}
        for r in rows:
            merged[(r.get("resident_id", ""), r.get("name", ""),
                    round(float(r.get("ts", 0)), 1))] = r
        out = sorted(merged.values(), key=lambda r: r.get("ts", 0))
        self._cache = out
        return out

    def samples(self, resident_id: str, name: str, days: float = 7.0,
                now: float | None = None) -> list[dict[str, Any]]:
        ref = now if now is not None else time.time()
        cutoff = ref - days * DAY
        return [r for r in self._read()
                if r.get("name") == name
                and (not resident_id or r.get("resident_id") == resident_id)
                and float(r.get("ts", 0)) >= cutoff]

    def names(self, resident_id: str = "") -> list[str]:
        return sorted({r["name"] for r in self._read()
                       if not resident_id or r.get("resident_id") == resident_id})

    def latest(self, resident_id: str, name: str) -> dict[str, Any] | None:
        rows = [r for r in self._read()
                if r.get("name") == name
                and (not resident_id or r.get("resident_id") == resident_id)]
        return rows[-1] if rows else None

    # ------------------------------------------------------------ 基线
    def baseline(self, resident_id: str, name: str, days: float = 30.0,
                 exclude_recent_hours: float = 24.0,
                 now: float | None = None) -> dict[str, Any] | None:
        """住户自己的典型值。

        **排除最近 24 小时**：基线是用来判断"今天是不是不对劲"的参照，
        把今天本身算进去会把异常稀释掉——真出问题时反而更不容易报出来。

        用中位数而非均值：一次测量失误（手表没戴好时的离谱心率）
        不该把整条基线带偏。
        """
        ref = now if now is not None else time.time()
        rows = self.samples(resident_id, name, days=days, now=ref)
        cut = ref - exclude_recent_hours * 3600.0
        values = [float(r["value"]) for r in rows
                  if float(r.get("ts", 0)) <= cut
                  and float(r.get("quality", 1.0)) >= 0.5]
        # 样本太少的基线不可信，宁可返回 None 让调用方退回全院阈值
        if len(values) < 3:
            return None
        return {"name": name, "n": len(values), "median": round(_median(values), 2),
                "std": round(_stdev(values), 2),
                "min": round(min(values), 2), "max": round(max(values), 2),
                "days": days}

    def deviation(self, resident_id: str, name: str, value: float,
                  days: float = 30.0, now: float | None = None
                  ) -> tuple[float, dict[str, Any]] | None:
        """当前值偏离个体基线多少个标准差。返回 (z 分数, 基线)。"""
        base = self.baseline(resident_id, name, days=days, now=now)
        if base is None:
            return None
        std = base["std"]
        if std < 1e-6:
            # 基线完全不波动（样本太集中）时，z 分数会爆到无穷大。
            # 退回用中位数的 10% 作为尺度，避免一点点偏差就报极端值。
            std = max(abs(base["median"]) * 0.1, 1e-3)
        return (value - base["median"]) / std, base

    # ------------------------------------------------------------ 趋势
    def daily(self, resident_id: str, name: str, days: float = 7.0,
              now: float | None = None) -> list[dict[str, Any]]:
        """按天聚合。"""
        ref = now if now is not None else time.time()
        buckets: dict[str, list[float]] = {}
        for r in self.samples(resident_id, name, days=days, now=ref):
            day = time.strftime("%Y-%m-%d", time.localtime(float(r["ts"])))
            buckets.setdefault(day, []).append(float(r["value"]))
        out = []
        for day in sorted(buckets):
            vals = buckets[day]
            out.append({"date": day, "n": len(vals),
                        "mean": round(sum(vals) / len(vals), 2),
                        "min": round(min(vals), 2), "max": round(max(vals), 2)})
        return out

    def trend(self, resident_id: str, name: str, days: float = 14.0,
              now: float | None = None) -> dict[str, Any] | None:
        """近期相对早期的变化。

        切成前后两半比均值，而不是做线性回归：样本稀疏且不等间隔
        （手表可能几天才更新一次步行稳定度），回归的斜率在这种数据上
        意义不大，而且没法在日志里被人工复核。
        """
        ref = now if now is not None else time.time()
        rows = self.samples(resident_id, name, days=days, now=ref)
        rows = [r for r in rows if float(r.get("quality", 1.0)) >= 0.5]
        if len(rows) < 4:
            return None
        mid = ref - days * DAY / 2.0
        early = [float(r["value"]) for r in rows if float(r["ts"]) < mid]
        late = [float(r["value"]) for r in rows if float(r["ts"]) >= mid]
        if len(early) < 2 or len(late) < 2:
            return None
        e = sum(early) / len(early)
        l = sum(late) / len(late)
        delta = l - e
        pct = (delta / e * 100.0) if abs(e) > 1e-6 else 0.0
        worse = (delta < 0) if name in LOWER_IS_WORSE else (delta > 0)
        # 门槛按指标取：各项的生理波动幅度差一个量级，见 SIGNIFICANT_PCT
        gate = significant_pct(name)
        moved = abs(pct) >= gate
        return {
            "name": name, "label": label_of(name), "unit": unit_of(name),
            "days": days, "early_mean": round(e, 2), "late_mean": round(l, 2),
            "delta": round(delta, 2), "pct": round(pct, 1),
            "n_early": len(early), "n_late": len(late),
            "threshold_pct": gate,
            "direction": ("上升" if delta > 0 else "下降") if moved else "平稳",
            "concerning": bool(worse and moved),
        }

    # ------------------------------------------------------------ 摘要
    def summary(self, resident_id: str, days: float = 7.0,
                now: float | None = None) -> dict[str, Any]:
        """给 LLM 与住户记忆用的聚合视图。

        **只含统计量，不含逐条读数。** 这是隐私边界：原始时序会暴露
        住户的作息细节（几点起夜、几点出门），而趋势分析用不到那个粒度。
        """
        ref = now if now is not None else time.time()
        metrics: list[dict[str, Any]] = []
        for name in self.names(resident_id):
            rows = self.samples(resident_id, name, days=days, now=ref)
            if not rows:
                continue
            vals = [float(r["value"]) for r in rows]
            item = {
                "name": name, "label": label_of(name), "unit": unit_of(name),
                "n": len(vals), "mean": round(sum(vals) / len(vals), 2),
                "min": round(min(vals), 2), "max": round(max(vals), 2),
                "latest": round(vals[-1], 2),
                "latest_at": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(float(rows[-1]["ts"]))),
            }
            base = self.baseline(resident_id, name, now=ref)
            if base:
                item["baseline_median"] = base["median"]
                item["baseline_n"] = base["n"]
            tr = self.trend(resident_id, name, now=ref)
            if tr:
                item["trend"] = {k: tr[k] for k in
                                 ("direction", "pct", "delta", "concerning")}
            metrics.append(item)
        return {
            "resident_id": resident_id, "days": days,
            "metric_count": len(metrics),
            "metrics": metrics,
            "concerning": [m for m in metrics
                           if (m.get("trend") or {}).get("concerning")],
            "text": self.describe(metrics, days),
        }

    @staticmethod
    def describe(metrics: list[dict[str, Any]], days: float) -> str:
        """把聚合压成人话。

        写成人话而不是 JSON：这段既会进 LLM 上下文，也会被护工在事件
        档案里看到。"静息心率近 7 天由 68 升到 76"比一堆数字更能让人
        （和模型）意识到该留意什么。
        """
        if not metrics:
            return "暂无可用的健康数据。"
        bits: list[str] = []
        for m in metrics:
            seg = f"{m['label']} 最近 {m['latest']}{m['unit']}"
            if "baseline_median" in m:
                seg += f"（个体基线 {m['baseline_median']}{m['unit']}）"
            tr = m.get("trend")
            if tr and tr["direction"] != "平稳":
                seg += f"，近 {days:.0f} 天{tr['direction']} {abs(tr['pct']):.0f}%"
                if tr["concerning"]:
                    seg += "（需关注）"
            bits.append(seg)
        return "；".join(bits) + "。"

    def clear_cache(self) -> None:
        with self._lock:
            self._cache = None


__all__ = ["HealthStore", "LABELS", "LOWER_IS_WORSE", "SIGNIFICANT_PCT",
           "label_of", "unit_of", "significant_pct", "ARCHIVE_PREFIXES"]
