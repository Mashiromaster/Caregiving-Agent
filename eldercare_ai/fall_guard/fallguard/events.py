# -*- coding: utf-8 -*-
"""事件日志（JSONL 追加写）与误报标注。

设计取舍：events.jsonl 只追加不修改，标注写入独立的 labels.jsonl，
读取时合并。这样原始记录始终可审计，符合技术文档里"动作留痕不可篡改"的要求。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def new_event_id(ts: float | None = None) -> str:
    ts = ts or time.time()
    lt = time.localtime(ts)
    return time.strftime("EV%Y%m%d-%H%M%S", lt) + f"-{int(ts * 1000) % 1000:03d}"


def iso(ts: float) -> str:
    lt = time.localtime(ts)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", lt)
    offset = time.strftime("%z", lt)
    return f"{base}{offset[:3]}:{offset[3:]}" if offset else base


@dataclass
class EventRecord:
    """一次事件的完整记录，随处置过程逐步补全后一次性落盘。"""

    event_id: str
    detected_at: float
    event_type: str = "fall"
    level: str = "L3"
    trigger: str = ""
    confidence: float = 0.0
    room: str = ""
    resident: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    agent: dict[str, Any] = field(default_factory=dict)
    outcome: str = "open"
    cancelled: bool = False
    fallback_fired: bool = False
    notes: list[str] = field(default_factory=list)

    def stage(self, stage: str, **extra: Any) -> None:
        entry = {"at": iso(time.time()), "stage": stage}
        entry.update(extra)
        self.timeline.append(entry)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "detected_at": iso(self.detected_at),
            "detected_at_ts": round(self.detected_at, 3),
            "event_type": self.event_type,
            "level": self.level,
            "trigger": self.trigger,
            "confidence": round(self.confidence, 3),
            "room": self.room,
            "resident": self.resident,
            "features": self.features,
            "evidence": self.evidence,
            "timeline": self.timeline,
            "actions": self.actions,
            "agent": self.agent,
            "outcome": self.outcome,
            "cancelled": self.cancelled,
            "fallback_fired": self.fallback_fired,
            "notes": self.notes,
        }
        return data


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

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = list(self._read(self.events_path))
        labels = self.labels()
        for row in rows:
            label = labels.get(row.get("event_id", ""))
            row["label"] = label
        return rows[-limit:] if limit else rows

    def stats(self) -> dict[str, Any]:
        rows = self.events()
        total = len(rows)
        false_alarms = sum(1 for r in rows if (r.get("label") or {}).get("is_false_alarm"))
        cancelled = sum(1 for r in rows if r.get("cancelled"))
        called = sum(1 for r in rows if r.get("outcome") == "called")
        return {
            "total": total,
            "called": called,
            "cancelled": cancelled,
            "labelled_false_alarm": false_alarms,
            "unlabelled": sum(1 for r in rows if not r.get("label")),
        }
