"""
端口:键值缓存能力(旁路缓存 cache-aside 的"读/写"两侧)。

只依赖标准库 —— 缓存的 value 是**不透明字符串**,不需要 domain 类型。
里面装什么由用例层决定(当前是 ``"<source>\\x1f<answer>"``,来源跟着值走),
适配器只管存取,不解释内容。
"""

from __future__ import annotations

from typing import Protocol


class Cache(Protocol):
    """
    任何故障都由调用方降级处理:缓存是纯加速,不该降低可用性。
    """

    def get(self, key: str) -> str | None:
        """输入:缓存键。行为:读缓存,失败由调用方降级。输出:缓存值,没命中给 None。"""
        ...

    def set(self, key: str, value: str, ttl: int) -> None:
        """输入:键、值、过期秒数。行为:写缓存并设置 TTL。输出:无。"""
        ...
