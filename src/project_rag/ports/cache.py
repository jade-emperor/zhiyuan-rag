"""
端口:键值缓存能力(旁路缓存 cache-aside 的"读/写"两侧)。

只依赖标准库 —— 缓存的 value 就是答案字符串,不需要 domain 类型。
"""

from __future__ import annotations

from typing import Protocol


class Cache(Protocol):
    """
    任何故障都由调用方降级处理:缓存是纯加速,不该降低可用性。
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl: int) -> None: ...
