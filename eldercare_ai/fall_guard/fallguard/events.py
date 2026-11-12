# -*- coding: utf-8 -*-
"""事件日志（JSONL 追加写）与误报标注。

设计取舍：events.jsonl 只追加不修改，标注写入独立的 labels.jsonl，
读取时合并。这样原始记录始终可审计，符合技术文档里"动作留痕不可篡改"的要求。

EventRecord / new_event_id / iso 已迁至 core/events.py（框架核心），
这里保留再导出以兼容既有调用方（selftest、web 等）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .core.events import EventRecord, SensorEvent, iso, new_event_id

__all__ = ["EventLog", "EventRecord", "SensorEvent", "iso", "new_event_id"]


class EventLog:
    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.labels_path = self.dir / "labels.jsonl"
        self._lock = threading.Lock()

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    def write(self, record: EventRecord) -> None:
        self._append(self.events_path, record.to_dict())

    def label(self, event_id: str, is_false_alarm: bool, reason: str = "",
              by: str = "operator") -> dict[str, Any]:
        payload = {
            "event_id": event_id,
            "labelled_at": iso(time.time()),
            "is_false_alarm": bool(is_false_alarm),
            "reason": reason,
            "by": by,
        }
        self._append(self.labels_path, payload)
        return payload

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

    def labels(self) -> dict[str, dict[str, Any]]:
        """同一事件多次标注时后写入者生效。"""
        out: dict[str, dict[str, Any]] = {}
        for row in self._read(self.labels_path):
            if row.get("event_id"):
                out[row["event_id"]] = row
        return out

    def events(self, limit: int | None = None, event_type: str | None = None,
               resident_id: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._read(self.events_path))
        labels = self.labels()
        for row in rows:
            label = labels.get(row.get("event_id", ""))
            row["label"] = label
            # 旧记录没有这两个字段（分别在启用 event_type 与身份层之前写入），
            # 补默认值让下游不必到处判空
            row.setdefault("event_type", "fall")
            row.setdefault("resident_id", "")
        if event_type:
            rows = [r for r in rows if r.get("event_type") == event_type]
        if resident_id:
            rows = [r for r in rows if r.get("resident_id") == resident_id]
        return rows[-limit:] if limit else rows

    def event_types(self) -> list[str]:
        """日志里出现过的事件类型，供前端做筛选项。"""
        seen = {str(r.get("event_type") or "fall") for r in self.events()}
        return sorted(seen)

    def stats(self, event_type: str | None = None) -> dict[str, Any]:
        rows = self.events(event_type=event_type)
        # 不假设"处置 = 打电话"：预警类事件的正常归宿就是只记录不外呼，
        # 把它们算成"未处置"会让统计看起来像系统一直在失败
        handled = {"called", "cancelled", "suppressed", "logged"}
        by_type: dict[str, int] = {}
        for r in rows:
            key = str(r.get("event_type") or "fall")
            by_type[key] = by_type.get(key, 0) + 1
        return {
            "total": len(rows),
            "called": sum(1 for r in rows if r.get("outcome") == "called"),
            "cancelled": sum(1 for r in rows if r.get("cancelled")),
            "labelled_false_alarm": sum(
                1 for r in rows if (r.get("label") or {}).get("is_false_alarm")),
            "unlabelled": sum(1 for r in rows if not r.get("label")),
            "unresolved": sum(1 for r in rows
                              if str(r.get("outcome")) not in handled),
            "by_type": by_type,
        }
