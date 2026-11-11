# -*- coding: utf-8 -*-
"""最小 TOML 序列化器。

标准库只有 tomllib（只读），而 Web 配置面板必须把表单写回 config.toml，
所以这里补一个写入端。只覆盖本项目配置实际用到的类型：
字符串、布尔、整数、浮点、同类数组、嵌套数组（exclude_zones）、
表（[call.aliyun]）与表数组（[[call.contacts]]）。

**注释不保留**——TOML 的注释不是数据，tomllib 读进来就丢了。
所以面板保存后，手写在 config.toml 里的注释会消失（示例注释见
config.example.toml）。保存前会自动留一份 config.toml.bak。
"""
from __future__ import annotations

import math
from typing import Any

_BARE_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _quote(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _key(name: str) -> str:
    name = str(name)
    if name and all(ch in _BARE_OK for ch in name):
        return name
    return _quote(name)


def _fmt(value: Any) -> str:
    # bool 必须在 int 之前判断：Python 里 True 是 int 的子类
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"TOML 不支持的浮点值：{value}")
        text = repr(value)
        return text if ("." in text or "e" in text or "E" in text) else text + ".0"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    raise TypeError(f"TOML 不支持的类型：{type(value).__name__}")


def _is_table(value: Any) -> bool:
    return isinstance(value, dict)


def _is_table_array(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) > 0
            and all(isinstance(v, dict) for v in value))


def _emit(data: dict[str, Any], prefix: str, out: list[str]) -> None:
    # TOML 要求：表内的普通键必须写在任何子表头之前，否则会被归进子表
    for name, value in data.items():
        if _is_table(value) or _is_table_array(value):
            continue
        out.append(f"{_key(name)} = {_fmt(value)}")

    for name, value in data.items():
        path = f"{prefix}.{_key(name)}" if prefix else _key(name)
        if _is_table_array(value):
            for item in value:
                out.append("")
                out.append(f"[[{path}]]")
                _emit(item, path, out)
        elif _is_table(value):
            out.append("")
            out.append(f"[{path}]")
            _emit(value, path, out)


def dumps(data: dict[str, Any], header: str = "") -> str:
    out: list[str] = []
    if header:
        for line in header.strip("\n").splitlines():
            out.append(f"# {line}" if line else "#")
        out.append("")
    _emit(data, "", out)
    text = "\n".join(out).strip("\n")
    return text + "\n"
