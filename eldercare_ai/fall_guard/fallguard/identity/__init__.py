# -*- coding: utf-8 -*-
"""身份识别器实现。新增识别方式在这里加文件，并在 core/identity.py 登记。"""
from .face import FaceIdentity
from .room_binding import RoomBindingIdentity
from .wristband_id import WristbandIdentity

__all__ = ["FaceIdentity", "RoomBindingIdentity", "WristbandIdentity"]
