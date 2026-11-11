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
        "speak": True,
        "alarm_sound": True,
        "voice": "",
        "suppress_call_windows": [],
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
    },
    "log": {"dir": "data"},
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
