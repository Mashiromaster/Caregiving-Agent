# -*- coding: utf-8 -*-
"""Apple Watch / iPhone 健康数据接入。

**苹果没有云端健康 API。** 数据的唯一出口是用户自己的那台 iPhone，
必须有设备侧的东西把它推出来。这里对接的是 Health Auto Export 这类
工具 App 的 REST 推送格式（也兼容快捷指令手搓的简化格式）。

与手环类设备的三点根本差异，决定了它不能照搬 http_push：

1. **载荷形状完全不同**。手环推的是扁平键值 {"heart_rate": 78}；
   HAE 推的是嵌套的 metrics 数组，每个指标带一串带时间戳的样本。
2. **数据是批量历史，不是实时**。HealthKit 后台投递按小时节流，
   一次推送里可能有过去几小时甚至几天的样本。所以每条样本都要用
   **它自己的时间戳**进总线，不能统一打成"现在"——否则六小时前的
   心率会被当成当前读数参与风险评分。
3. **做不了急救告警**。等异常心率传到服务器可能已过去十几分钟。
   所以它只喂信号层与健康档案，不产出事件（needs_frame=False 且
   不实现 observe）。它的价值在个体基线，不在实时性。

HAE 的 REST 格式（简化）：
    {"data": {"metrics": [
        {"name": "heart_rate", "units": "count/min",
         "data": [{"date": "2026-11-11 08:00:00 +0800",
                   "Min": 60, "Avg": 72, "Max": 95}]},
        {"name": "step_count", "units": "count",
         "data": [{"date": "...", "qty": 1234}]}
    ]}}
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..core import health as healthmod
from ..core import signals as sig
from .base import DeviceSource

#: HAE / HealthKit 的指标名 → 内部信号名。
#: 一个指标可能有多个别名（HAE 版本间有差异，快捷指令的命名又不一样），
#: 全部映射到同一个信号，免得用户因为字段名对不上而默默收不到数据。
METRIC_MAP: dict[str, str] = {
    # 心脏
    "heart_rate": sig.WATCH_HEART_RATE,
    "resting_heart_rate": sig.WATCH_RESTING_HR,
    "restingheartrate": sig.WATCH_RESTING_HR,
    "heart_rate_variability": sig.WATCH_HRV,
    "heart_rate_variability_sdnn": sig.WATCH_HRV,
    "hrv": sig.WATCH_HRV,
    # 呼吸
    "blood_oxygen_saturation": sig.WATCH_SPO2,
    "oxygen_saturation": sig.WATCH_SPO2,
    "respiratory_rate": sig.WATCH_RESP_RATE,
    # 步态——苹果自家的跌倒风险指标就在这组里
    "walking_steadiness": sig.WALKING_STEADINESS,
    "apple_walking_steadiness": sig.WALKING_STEADINESS,
    "walking_speed": sig.WALKING_SPEED,
    "walking_asymmetry_percentage": sig.WALKING_ASYMMETRY,
    "walking_double_support_percentage": sig.DOUBLE_SUPPORT,
    # 活动
    "step_count": sig.STEP_COUNT,
    "steps": sig.STEP_COUNT,
    "active_energy": sig.ACTIVE_ENERGY,
    "apple_stand_hour": sig.STAND_HOURS,
    "stand_hours": sig.STAND_HOURS,
}

#: 睡眠要单独处理：HAE 的 sleep_analysis 一条样本里含多个字段
#: （totalSleep / awake / deep / core / rem），不是单一数值。
SLEEP_METRICS = frozenset({"sleep_analysis", "sleep"})

#: 取哪个字段当样本值。HAE 的心率样本给 Min/Avg/Max 三个，
#: 步数之类给 qty。按这个顺序找第一个存在的。
VALUE_KEYS = ("qty", "Avg", "avg", "value", "Max", "max")


def _parse_time(raw: Any) -> float | None:
    """解析 HAE 的时间串。

    格式是 "2026-11-11 08:00:00 +0800"，Python 的 fromisoformat 在 3.11+
    能吃带空格和偏移的变体，但旧格式和快捷指令的 ISO8601 都要兼容，
    所以逐个尝试而不是假定某一种。
    """
    if isinstance(raw, (int, float)):
        # 已经是 epoch。毫秒时间戳（13 位）要换算回秒
        return float(raw) / 1000.0 if raw > 1e11 else float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc).astimezone()
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _pick_value(point: dict[str, Any]) -> float | None:
    for key in VALUE_KEYS:
        if key in point:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                return None
    return None


class AppleHealthDevice(DeviceSource):
    """被动接收型：等 iPhone 把健康数据 POST 进来。"""

    name = "apple_health"
    kind = "wearable"
    emits_signals = tuple(sorted(set(METRIC_MAP.values()))) + (
        sig.SLEEP_HOURS, sig.SLEEP_AWAKENINGS)

    def __init__(self, cfg, conf: dict[str, Any] | None = None):
        conf = conf or {}
        self.cfg = cfg
        self.device_id = str(conf.get("device_id") or "apple-health")
        self.resident_id = str(conf.get("resident_id") or "")
        # 手表数据按小时来，12 小时没推送才算掉线
        self.stale_after = float(conf.get("stale_after", 12 * 3600))
        #: 超过这个天数的样本直接丢弃。HAE 配错导出范围时会一次推来几个月
        #: 的历史，既撑爆档案也让趋势分析失真。
        self.max_backfill_days = float(conf.get("max_backfill_days", 30))
        self._bus: sig.SignalBus | None = None
        self._health: healthmod.HealthStore | None = None
        self._last_ok: float | None = None
        self._samples = 0
        self._pushes = 0
        self._ignored: set[str] = set()

    def start(self, bus: sig.SignalBus) -> None:
        self._bus = bus

    def bind_store(self, store) -> None:
        """由 runtime 注入健康档案。

        总线的滑动窗口只有几分钟，而个体基线要数周数据，两者必须分开存。
        """
        self._health = store

    # ------------------------------------------------------------ 接收
    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._bus is None:
            return {"ok": False, "reason": "设备未启动，请先启动监测"}

        resident = str(payload.get("resident_id") or self.resident_id)
        metrics = self._extract_metrics(payload)
        if metrics is None:
            return {"ok": False,
                    "reason": "载荷里找不到 data.metrics。请确认 Health Auto Export "
                              "的导出格式选的是 JSON（REST API），不是 CSV"}

        now = time.time()
        oldest_ok = now - self.max_backfill_days * 86400.0
        accepted: dict[str, int] = {}
        ignored: list[str] = []
        too_old = 0

        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            raw_name = str(metric.get("name") or "").strip().lower()
            points = metric.get("data") or []
            if raw_name in SLEEP_METRICS:
                n, old = self._ingest_sleep(points, resident, oldest_ok)
                if n:
                    accepted[sig.SLEEP_HOURS] = accepted.get(sig.SLEEP_HOURS, 0) + n
                too_old += old
                continue
            name = METRIC_MAP.get(raw_name)
            if name is None:
                # 未知指标忽略而不是报错——HAE 会推一大堆我们用不上的东西
                # （体重、正念分钟数…），不该因此整条失败
                ignored.append(raw_name)
                self._ignored.add(raw_name)
                continue
            n, old = self._ingest_points(points, name, resident, oldest_ok)
            if n:
                accepted[name] = accepted.get(name, 0) + n
            too_old += old

        total = sum(accepted.values())
        if total:
            self._last_ok = now
            self._samples += total
        self._pushes += 1
        return {
            "ok": bool(total),
            "accepted": accepted,
            "samples": total,
            "ignored_metrics": sorted(set(ignored))[:20],
            "dropped_too_old": too_old,
            "reason": "" if total else
                      ("所有样本都超出回溯窗口" if too_old else
                       "没有可识别的指标。检查 Health Auto Export 里勾选的数据类型"),
        }

    @staticmethod
    def _extract_metrics(payload: dict[str, Any]) -> list | None:
        """兼容几种外层包装。

        HAE 是 {"data": {"metrics": [...]}}；有人会把 data 层拆掉直接发
        {"metrics": [...]}；快捷指令手搓的可能直接是数组。都收下。
        """
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("metrics"), list):
            return data["metrics"]
        if isinstance(payload.get("metrics"), list):
            return payload["metrics"]
        if isinstance(data, list):
            return data
        return None

    def _ingest_points(self, points: Any, name: str, resident: str,
                       oldest_ok: float) -> tuple[int, int]:
        if not isinstance(points, list):
            return 0, 0
        n = old = 0
        for point in points:
            if not isinstance(point, dict):
                continue
            value = _pick_value(point)
            if value is None:
                continue
            # **用样本自己的时间戳**，不是收到的时刻。一次推送里可能含
            # 过去几小时的数据，统一打成"现在"会让陈旧读数冒充当前状态。
            ts = _parse_time(point.get("date") or point.get("ts")
                             or point.get("startDate"))
            if ts is None:
                ts = time.time()
            if ts < oldest_ok:
                old += 1
                continue
            self._emit(name, value, ts, resident)
            n += 1
        return n, old

    def _ingest_sleep(self, points: Any, resident: str,
                      oldest_ok: float) -> tuple[int, int]:
        """睡眠样本一条里含多个字段，拆成时长与觉醒次数两个信号。"""
        if not isinstance(points, list):
            return 0, 0
        n = old = 0
        for point in points:
            if not isinstance(point, dict):
                continue
            ts = _parse_time(point.get("date") or point.get("startDate"))
            if ts is None:
                ts = time.time()
            if ts < oldest_ok:
                old += 1
                continue
            hours = None
            for key in ("totalSleep", "asleep", "total_sleep", "value", "qty"):
                if key in point:
                    try:
                        hours = float(point[key])
                        break
                    except (TypeError, ValueError):
                        continue
            if hours is not None:
                self._emit(sig.SLEEP_HOURS, hours, ts, resident)
                n += 1
            # awake 在 HAE 里是"清醒时长(小时)"而非次数。换算成一个粗略的
            # 觉醒次数（每 10 分钟算一次），只用于趋势比较，不作绝对判断。
            for key in ("awake", "awakenings"):
                if key in point:
                    try:
                        awake = float(point[key])
                    except (TypeError, ValueError):
                        continue
                    count = awake if key == "awakenings" else round(awake * 6, 1)
                    self._emit(sig.SLEEP_AWAKENINGS, count, ts, resident)
                    n += 1
                    break
        return n, old

    def _emit(self, name: str, value: float, ts: float, resident: str) -> None:
        if self._bus is not None:
            self._bus.emit(name, value, ts=ts, source=self.device_id)
        if self._health is not None:
            self._health.record(resident, name, value, ts=ts,
                                source=self.device_id)

    # ------------------------------------------------------------ 状态
    @property
    def online(self) -> bool:
        if self._last_ok is None:
            return False
        return (time.time() - self._last_ok) < self.stale_after

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "device_id": self.device_id,
            "online": self.online, "pushes": self._pushes,
            "samples": self._samples,
            "resident_id": self.resident_id,
            "last_ok": round(self._last_ok, 1) if self._last_ok else None,
            "ignored_metrics": sorted(self._ignored)[:20],
            "emits": list(self.emits_signals),
            "detail": "POST /api/device/push 接收 iPhone 健康数据。"
                      "手表数据按小时到达，只参与风险评分与个体基线，"
                      "不用于实时告警",
        }
