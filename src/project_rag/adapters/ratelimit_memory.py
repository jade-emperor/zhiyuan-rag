"""
限流的**进程内存**实现:滑动窗口日志。

**它现在有两个身份**(都要知道):

1. **``api.rate_limit.backend = "memory"`` 时的实现** —— 单进程、无 Redis 的部署能用,
   但**计数重启即清零、多 worker 各算各的**(要跨进程就用 Redis 那一份);
2. **验收与离线测试的替身** —— 契约里绝大部分语义检查跑的是它:
   不需要容器、结果确定(时钟可注入)。

语义与 ``adapters/ratelimit_redis.py`` **必须一字不差**(同一套用例跑两个实现):
窗口内到 ``limit`` 为止放行、第 ``limit+1`` 次拒绝并给 ≥1 秒、窗口滑过后再放行、
``limit <= 0`` 关掉这一档。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from project_rag.ports import RateLimiter


@dataclass(slots=True)
class MemoryRateLimiter(RateLimiter):
    """一把键一个窗口:记下窗口内的命中时刻,超了就说"等几秒再来"。

    **只保留窗口内的时间戳**(每次判定顺手裁掉过期的):不裁的话,一个长期运行的进程
    会为每个键攒下无限多个浮点数 —— 那是一种只会慢慢发作的内存泄漏。
    """

    hits: dict[str, list[float]] = field(default_factory=dict)
    # 出现过多少个键(排障要看的:键会不会无限增长)
    keys_seen: int = 0

    def check(self, key: str, *, limit: int, window_s: float) -> int:
        """输入:键、上限、窗口秒数。行为:裁剪 → 计数 → 判定 → 记一次。输出:0 或等待秒数。"""
        return self._check_at(key, limit=limit, window_s=window_s, now=time.monotonic())

    def forget(self, key: str) -> None:
        """输入:键。行为:清掉它的窗口。输出:无。"""
        self.hits.pop(key, None)

    def _check_at(self, key: str, *, limit: int, window_s: float, now: float) -> int:
        """输入:键、上限、窗口、当前时刻。行为:同上,但时钟由调用方给(验收要用假时钟)。输出:同上。"""
        if limit <= 0:
            return 0
        if window_s <= 0:
            raise ValueError(f"window_s 必须为正,收到 {window_s}")

        marks = self.hits.get(key)
        if marks is None:
            marks = []
            self.hits[key] = marks
            self.keys_seen += 1
        cutoff = now - window_s
        marks[:] = [mark for mark in marks if mark > cutoff]

        if len(marks) >= limit:
            oldest = min(marks)
            return max(1, int(window_s - (now - oldest)) + 1)
        marks.append(now)
        return 0
