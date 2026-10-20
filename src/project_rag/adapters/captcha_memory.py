"""验证码存放:进程内,带 TTL,取即删除。

单进程部署(``uvicorn.run`` 不带 workers)下一张票据必定回到同一个进程,所以不引 Redis。
多 worker / 多副本部署要换成 Redis 实现 —— 端口不变,只换装配。
"""

from __future__ import annotations

import threading
import time


class MemoryCaptchaStore:
    """``ticket -> (answer, 过期时刻)``。过期项在读写时顺手清掉。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, ticket: str, answer: str, ttl_s: int) -> None:
        """输入:票据、答案、有效期。行为:存下。输出:无。"""
        with self._lock:
            self._purge()
            self._items[ticket] = (answer, time.monotonic() + max(1, ttl_s))

    def take(self, ticket: str) -> str | None:
        """输入:票据。行为:取出并删除。输出:答案;失效给 None。"""
        with self._lock:
            self._purge()
            item = self._items.pop(ticket, None)
        if item is None:
            return None
        answer, deadline = item
        return answer if deadline > time.monotonic() else None

    def size(self) -> int:
        """输入:无。行为:数当前还在的票据(验收用)。输出:条数。"""
        with self._lock:
            self._purge()
            return len(self._items)

    def _purge(self) -> None:
        now = time.monotonic()
        for ticket in [key for key, (_, deadline) in self._items.items() if deadline <= now]:
            self._items.pop(ticket, None)
