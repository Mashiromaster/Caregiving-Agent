# -*- coding: utf-8 -*-
"""配置加载：TOML（stdlib tomllib）+ env: 占位符解析。"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "scene": {"resident_name": "住户", "place": "监护区域", "institution": ""},
    "camera": {"source": 0, "width": 1280, "height": 720, "fps": 15, "flip": False},
    "detector": {
        "backend": "auto",
        "min_person_score": 0.5,
        "lying_angle_deg": 60.0,
        "lying_angle_hysteresis": 8.0,
        "lying_aspect": 1.25,
        "floor_y": 0.50,
        "descent_rate_min": 0.25,
        "descent_window_s": 1.2,
        "lying_seconds": 2.5,
        "lying_only_seconds": 8.0,
        "lying_grace_s": 1.0,
        "lying_min_density": 0.6,
        "static_motion_max": 0.012,
        "cooldown_seconds": 120,
        "exclude_zones": [],
    },
    "evidence": {
        "dir": "data/evidence",
        "pre_seconds": 5,
        "post_seconds": 10,
        "keyframe_offsets": [0.0, 2.0, 5.0],
    },
    "alert": {
        "cancel_seconds": 10,
        # 住户主动求助的取消窗口。设得短是因为意图明确，不该让求助者干等；
        # 但不设 0——误识别一次就是一通真实电话，2 秒够按下取消
        "help_cancel_seconds": 2,
        "cancel_seconds_by_type": {},
        "speak": True,
        "alarm_sound": True,
        "voice": "",
        "suppress_call_windows": [],
    },
    # 求助手势：举手至耳边并保持
    "gesture": {
        "near_ratio": 0.75,            # 手腕到耳朵距离 ÷ 肩宽，小于此值算"靠近"
        "hold_seconds": 3.0,           # 保持多久才触发
        "cooldown_seconds": 60.0,      # 触发后的静默期
        # 手腕关键点置信度阈值。实测自然垂手时只有 0.25~0.45（手臂常被
        # 身体遮挡），举到耳边时会升高但未必过 0.5。设太高会让手势根本
        # 触发不了；真正防误触发的是"保持 3 秒 + 高于肩膀"两条判据。
        "min_keypoint_conf": 0.3,
        "grace_seconds": 0.6,          # 容忍关键点短暂丢失
    },
    "agent": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.2,
        "max_steps": 8,
        "deadline_seconds": 20,
    },
    "call": {
        "provider": "none",
        "dry_run": True,
        "play_times": 2,
        "max_calls_per_event": 2,
        "call_all_contacts": False,
        "contacts": [],
        "aliyun": {},
        "tencent": {},
        "volc": {},
        "spug": {},
    },
    "log": {"dir": "data"},
    # 感知插件：不配时默认只启跌倒视觉，与引入插件机制之前的行为一致
    "sensors": {"enabled": ["fall_vision", "help_gesture"]},
    # 身份识别链：按顺序尝试，先返回非空者胜出。认不出人不阻断处置
    "identity": {"providers": ["wristband_id", "room_binding"],
                 "rooms": [], "bands": []},
    # 外部设备（手环等）。空列表 = 不接设备，纯视觉模式
    "devices": {"sources": []},
    # 风险预警通路：只记录与提示，不外呼
    "risk": {"enabled": True, "warn_score": 45.0, "elevated_score": 70.0,
             "cooldown_seconds": 600},
    "memory": {"dir": "data/memory", "history_days": 7},
    # 可穿戴健康数据的长期档案。与 SignalBus 分开存：总线只留几分钟
    # （当前读数），档案留数周（个体基线）
    "health": {
        "dir": "data/health",
        "min_interval_seconds": 60,     # 同一指标的最小记录间隔，防手环刷屏
        "baseline_days": 30,            # 个体基线取多少天
        "trend_days": 14,               # 趋势比较的窗口
    },
}


class Config:
    """点号访问的配置包装：cfg.detector.lying_seconds / cfg.get('call.provider')。"""

    def __init__(self, data: dict[str, Any], root: Path):
        self._data = data
        self.root = root

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:  # pragma: no cover - 配置键缺失属编程错误
            raise AttributeError(name) from exc
        return Config(value, self.root) if isinstance(value, dict) else value

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def raw(self) -> dict[str, Any]:
        return self._data

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_env(node: Any) -> Any:
    """把 "env:NAME" 替换为环境变量值；缺失则为空字符串。"""
    if isinstance(node, str) and node.startswith("env:"):
        return os.environ.get(node[4:], "")
    if isinstance(node, dict):
        return {k: _resolve_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env(v) for v in node]
    return node


def load(config_path: str | os.PathLike | None = None) -> Config:
    """加载配置。未指定路径时依次尝试 config.toml / config.example.toml。"""
    pkg_root = Path(__file__).resolve().parent.parent
    if config_path:
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在：{path}")
    else:
        candidates = [pkg_root / "config.toml", pkg_root / "config.example.toml"]
        path = next((c for c in candidates if c.exists()), None)
        if path is None:
            raise FileNotFoundError(
                f"未找到配置文件，请先复制 {candidates[1].name} 为 config.toml"
            )

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    merged = _merge(DEFAULTS, data)
    return Config(_resolve_env(merged), pkg_root)


def config_source(config_path: str | os.PathLike | None = None) -> Path:
    """返回实际会被加载的配置文件路径（用于提示）。"""
    pkg_root = Path(__file__).resolve().parent.parent
    if config_path:
        return Path(config_path).expanduser().resolve()
    for name in ("config.toml", "config.example.toml"):
        if (pkg_root / name).exists():
            return pkg_root / name
    return pkg_root / "config.toml"
