"""HTTP/WebSocket 入口层。

和 ``cli/`` 平级:两者都是**组合根**(可以 import adapters / application),区别只是
一个把人话打到终端、一个把 JSON 打给浏览器。

只导出工厂 ``create_app`` —— 应用实例由调用方建(测试注入假依赖、生产用
``build_real_deps``),模块级不留 import 期副作用。
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
