"""端口:图形验证码的渲染与存放。

渲染(像素)与存放(TTL/一次性)分开:前者依赖图像库,后者只是键值,两条都能离线替换。
验证码答案不进日志、不进响应 —— 只有 ``take`` 取回来做比较。
"""

from __future__ import annotations

from typing import Protocol


class CaptchaRenderer(Protocol):
    """把验证码文本画成图片。"""

    def render(self, text: str) -> bytes:
        """输入:验证码文本。行为:画一张 PNG。输出:PNG 字节。"""
        ...


class CaptchaStore(Protocol):
    """验证码的一次性存放。"""

    def put(self, ticket: str, answer: str, ttl_s: int) -> None:
        """输入:票据、答案、有效期。行为:存下(同票据覆盖)。输出:无。"""
        ...

    def take(self, ticket: str) -> str | None:
        """输入:票据。行为:取出并**立即删除**(一次性)。输出:答案;没票据/已过期给 None。"""
        ...
