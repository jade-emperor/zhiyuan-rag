"""
端口:答案反馈的存取。

只依赖 domain + 标准库。**写入是 upsert**:同一个人对同一轮重复点同一个按钮,
不该多出一行(那样"15 条没帮助"可能只是同一个人在反复点)。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from project_rag.domain import FeedbackRecord


class FeedbackStore(Protocol):
    """反馈的读写(运维台读,对话页写)。"""

    def upsert(
        self,
        *,
        turn_key: str,
        session_id: str,
        user_id: str,
        rating: str,
        question: str,
        answer: str,
        channel: str,
        comment: str,
    ) -> FeedbackRecord:
        """输入:这一轮的稳定键 + 快照 + 评级。行为:按 ``turn_key`` **覆盖写**。输出:落库后的记录。"""
        ...

    def remove(self, turn_key: str, user_id: str) -> int:
        """输入:稳定键、当前用户。行为:撤掉自己的一条反馈(别人的删不掉)。输出:删掉的条数。"""
        ...

    def list_recent(
        self, *, limit: int, rating: str | None = None, since: datetime | None = None
    ) -> Sequence[FeedbackRecord]:
        """输入:最多几条、只要某一档(可空)、起始时刻(可空)。行为:按时间倒序取。输出:记录序列。"""
        ...

    def counts(self, *, since: datetime | None = None) -> tuple[int, int]:
        """输入:起始时刻(可空)。行为:数一数窗口内的两档。输出:``(up, down)``。"""
        ...
