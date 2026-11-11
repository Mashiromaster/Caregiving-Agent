# -*- coding: utf-8 -*-
"""Web 控制台：配置面板 + 实时监控 + 事件复核 + 体检。

只用标准库起 HTTP 服务，前端是无构建步骤的静态单页。
入口：python3 main.py web
"""
from .server import App, serve

__all__ = ["App", "serve"]
