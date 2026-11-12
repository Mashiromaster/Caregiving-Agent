# -*- coding: utf-8 -*-
"""住户记忆：画像 + 按身份召回的历史。

技术文档把个体基线列为"误报率从 1.2 降到 0.27 次/床·月的首要贡献项"，
而重构前的系统每次事件都像第一次见这位住户——同一个床位连续三晚
同一时间被判成跌倒、每次都被护工标成误报，系统第四晚还是照打不误。

存储沿用 EventLog 的做法：JSONL 只追加、读时按 id 合并取最后一条。
不引数据库——这是单机原型，住户数量是几十不是几万，而只追加的文件
天然可审计（"这条画像是什么时候被谁改的"直接能查）。

召回分两部分：
  profile  人工维护的画像（跌倒史、认知症标记、用药），进分级规则
  history  从 events.jsonl 聚合的近期行为，进智能体上下文
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .events import iso

DAY = 86400.0


class Memory:
    def __init__(self, root: Path, event_log=None):
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.residents_path = self.dir / "residents.jsonl"
        self.event_log = event_log
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 画像
    def _read(self, path: Path) -> Iterator[dict[str, Any]]:
        if not path.exists():
            return
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def profiles(self) -> dict[str, dict[str, Any]]:
        """同一住户多次写入时后写入者生效。"""
        out: dict[str, dict[str, Any]] = {}
        for row in self._read(self.residents_path):
            rid = str(row.get("resident_id") or "").strip()
            if rid:
                out[rid] = {**out.get(rid, {}), **row}
        return out

    def profile(self, resident_id: str) -> dict[str, Any]:
        if not resident_id:
            return {}
        return self.profiles().get(resident_id, {})

    def upsert_profile(self, resident_id: str, **fields: Any) -> dict[str, Any]:
        if not resident_id:
            raise ValueError("resident_id 不能为空")
        payload = {"resident_id": resident_id, "updated_at": iso(time.time())}
        payload.update(fields)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with open(self.residents_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        return self.profile(resident_id)

    # ------------------------------------------------------------ 历史
    def history(self, resident_id: str, days: float = 7.0,
                now: float | None = None) -> dict[str, Any]:
        """从事件日志聚合近 N 天的行为。

        不另存一份摘要——events.jsonl 已经是完整事实，再维护一份派生
        数据只会带来不一致。住户规模小，每次重算的开销可以忽略。
        """
        empty = {"days": days, "total": 0, "false_alarms": 0, "cancelled": 0,
                 "called": 0, "by_type": {}, "last_event_at": "", "night_events": 0}
        if self.event_log is None or not resident_id:
            return empty

        ref = now if now is not None else time.time()
        cutoff = ref - days * DAY
        rows = []
        for row in self.event_log.events():
            if str(row.get("resident_id") or "") != resident_id:
                continue
            ts = row.get("detected_at_ts")
            if not isinstance(ts, (int, float)) or ts < cutoff:
                continue
            rows.append(row)

        if not rows:
            return empty

        by_type: dict[str, int] = {}
        for r in rows:
            key = str(r.get("event_type") or "unknown")
            by_type[key] = by_type.get(key, 0) + 1

        def night(r: dict[str, Any]) -> bool:
            hour = time.localtime(r["detected_at_ts"]).tm_hour
            return hour >= 22 or hour < 6

        return {
            "days": days,
            "total": len(rows),
            "false_alarms": sum(1 for r in rows
                                if (r.get("label") or {}).get("is_false_alarm")),
            "cancelled": sum(1 for r in rows if r.get("cancelled")),
            "called": sum(1 for r in rows if r.get("outcome") == "called"),
            "by_type": by_type,
            "last_event_at": max(r.get("detected_at", "") for r in rows),
            "night_events": sum(1 for r in rows if night(r)),
        }

    # ------------------------------------------------------------ 召回
    def recall(self, resident_id: str, days: float = 7.0,
               now: float | None = None) -> dict[str, Any]:
        """一次取齐分级与处置需要的全部记忆。"""
        profile = self.profile(resident_id)
        history = self.history(resident_id, days=days, now=now)
        return {
            "resident_id": resident_id,
            "profile": profile,
            "history": history,
            "summary": self.summarize(resident_id, profile, history),
        }

    def summarize(self, resident_id: str, profile: dict[str, Any],
                  history: dict[str, Any]) -> str:
        """把记忆压成一句话，进智能体 prompt。

        刻意写成人话而不是 JSON：这段会出现在给 LLM 的上下文里，
        也会出现在事件档案里被人看到。"近 7 天 3 次误报"比
        {"false_alarms": 3} 更能让人（和模型）意识到该谨慎。
        """
        if not resident_id:
            return "未识别住户身份，按通用预案处置。"

        bits: list[str] = []
        name = profile.get("name") or resident_id
        tags = []
        if profile.get("dementia"):
            tags.append("认知症")
        if profile.get("anticoagulant"):
            tags.append("服用抗凝药")
        if (profile.get("fall_history") or 0) > 0:
            tags.append(f"有 {profile['fall_history']} 次跌倒史")
        if profile.get("high_fall_risk"):
            tags.append("标记为高跌倒风险")
        bits.append(f"{name}：" + ("、".join(tags) if tags else "无特殊风险标记"))

        total = history.get("total", 0)
        if total:
            seg = f"近 {history['days']:.0f} 天共 {total} 起事件"
            if history.get("false_alarms"):
                seg += f"，其中 {history['false_alarms']} 起被标注为误报"
            if history.get("night_events"):
                seg += f"，{history['night_events']} 起发生在夜间"
            bits.append(seg)
        else:
            bits.append(f"近 {history.get('days', 7):.0f} 天无事件记录")

        # 这条提示直接影响处置judgment，单独成句让它更显眼
        if history.get("false_alarms", 0) >= 2:
            bits.append("该住户近期误报偏多，处置时请留意是否为主动躺卧等正常行为，"
                        "但不得因此跳过外呼")
        return "。".join(bits) + "。"


__all__ = ["Memory"]
