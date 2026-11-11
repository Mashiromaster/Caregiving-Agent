# -*- coding: utf-8 -*-
"""感知插件实现。新增插件在这里加文件，并在 core/sensors.py 的注册表登记。"""
from .fall_vision import FallVisionSensor

__all__ = ["FallVisionSensor"]
