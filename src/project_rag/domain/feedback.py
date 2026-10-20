"""
答案反馈的领域类型(「有帮助 / 没帮助」)。

**为什么记录里带问答快照**:反馈的用途是复盘 badcase —— 只留一个计数,
维护者无法回答"被踩的那条到底答了什么、问的是什么"。快照是**用户点按钮那一刻**
主动交出来的那一条,不是事后去翻他的全部历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field

FEEDBACK_RATINGS = ("up", "down")
"""评级闭集。``none`` 只出现在"撤销"这个动作里,不是一种可存储的评级。"""


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """一条反馈(含被反馈的那一轮问答)。"""

    id: int
    session_id: str
    # 谁点的。``username`` 是展示用的登录名(账号被删后给空串,不编占位名)。
    user_id: str
    username: str
    rating: str
    question: str
    answer: str
    # 这一轮走的是哪条通道(faq / rag / llm);老数据或调用方没给时是空串。
    channel: str
    comment: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        """输入:无。行为:校验评级与必填字段。输出:无。"""
        if self.rating not in FEEDBACK_RATINGS:
            raise ValueError(f"评级只能是 {FEEDBACK_RATINGS},收到 {self.rating!r}")
        if not self.session_id.strip():
            raise ValueError("session_id 不能为空")


@dataclass(frozen=True, slots=True)
class FeedbackCounts:
    """一段窗口里的计数。**分子分母都给**:``3/4`` 与 ``300/400`` 都是 75%,但一个说明不了任何事。"""

    up: int = 0
    down: int = 0

    @property
    def total(self) -> int:
        """输入:无。行为:总条数。输出:整数。"""
        return self.up + self.down

    @property
    def negative_rate(self) -> float | None:
        """输入:无。行为:负反馈率。输出:0~1;一条都没有时给 ``None``(不是 0)。"""
        return None if self.total == 0 else self.down / self.total


@dataclass(frozen=True, slots=True)
class FeedbackOverview:
    """运维台上的一个窗口:计数 + 最近的条目。"""

    counts: FeedbackCounts = field(default_factory=FeedbackCounts)
    entries: tuple[FeedbackRecord, ...] = ()
