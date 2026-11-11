# -*- coding: utf-8 -*-
"""事件层：感知插件与框架之间的唯一契约。

SensorEvent 是插件能对框架说的全部内容。插件不知道有没有摄像头、
会不会打电话、谁是责任护工——它只负责说"我判定发生了什么、置信度多少、
依据是哪些特征"。处置由框架统一编排。

EventRecord 从 ../events.py 原样搬来（那里保留了兼容导入），
它是一次事件的完整档案，随处置过程逐步补全后一次性落盘。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


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
class SensorEvent:
    """一次离散判定。

    ts 用的是**源时钟**（视频文件与合成源从 0 开始计），留证按它检索
    缓冲区；事件号与外呼播报的时间另取墙钟——两个时钟不能混用，
    否则电话里会报出错误时间。这个坑在 pipeline._build_record 里踩过。
    """

    kind: str                                    # "fall" / "bed_exit" …→ event_type
    ts: float                                    # 源时钟
    confidence: float = 0.0
    trigger: str = ""                            # 判定路径，如 "descent+lying"
    features: dict[str, Any] = field(default_factory=dict)
    frame: Any = None                            # 判定瞬间的原始帧，可为 None
    resident_hint: str = ""                      # 插件若能提供身份线索（如手环 id）

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ts": round(self.ts, 3),
                "confidence": round(self.confidence, 3),
                "trigger": self.trigger, "features": self.features}


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
    resident_id: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    agent: dict[str, Any] = field(default_factory=dict)
    grading: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    outcome: str = "open"
    cancelled: bool = False
    fallback_fired: bool = False
    notes: list[str] = field(default_factory=list)

    def stage(self, stage: str, **extra: Any) -> None:
        entry = {"at": iso(time.time()), "stage": stage}
        entry.update(extra)
        self.timeline.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "detected_at": iso(self.detected_at),
            "detected_at_ts": round(self.detected_at, 3),
            "event_type": self.event_type,
            "level": self.level,
            "trigger": self.trigger,
            "confidence": round(self.confidence, 3),
            "room": self.room,
            "resident": self.resident,
            "resident_id": self.resident_id,
            "features": self.features,
            "evidence": self.evidence,
            "timeline": self.timeline,
            "actions": self.actions,
            "agent": self.agent,
            "grading": self.grading,
            "memory": self.memory,
            "outcome": self.outcome,
            "cancelled": self.cancelled,
            "fallback_fired": self.fallback_fired,
            "notes": self.notes,
        }
