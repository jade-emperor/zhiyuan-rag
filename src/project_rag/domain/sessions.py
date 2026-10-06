"""
会话目录的领域类型(前端左侧那条会话列表要显示的东西)。

**为什么不并进 ``domain/auth.py``**:归属(这段会话是谁的)是**授权数据** —— 错了就是越权;
而这里讲的是「这段会话长什么样」—— 标题、轮数、时间。两者只是恰好都用 ``session_id`` 索引。
放在一起,改一个展示字段就得动授权模块,不划算。

**为什么标题的裁法在后端**:标题来自**首问那句话**,而只有后端手里有全部轮次。
让前端拿历史自己截,等于把「哪一轮是第一轮」这个判断复制到前端一份 —— 两份迟早不一致
(比如某天改成用第一句非寒暄的话当标题)。
"""

from __future__ import annotations

from dataclasses import dataclass

TITLE_CHARS = 40
"""标题最多几个字(超出加省略号)。**口径只有这一处** —— 前端不再截第二次。"""


def session_title(question: str, *, limit: int = TITLE_CHARS) -> str:
    """输入:第一轮的问题。行为:压成一行并裁到 ``limit`` 字。输出:标题。

    换行与连续空白先压成单个空格:会话标题是一行文本,带换行会把列表撑变形。
    裁剪在**字符**上而不是字节上 —— 中英混排按字节裁会切出半个汉字。
    """
    flat = " ".join(question.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """一段会话的**目录信息**(不含任何问答正文)。"""

    session_id: str
    title: str
    rounds: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        """输入:无。行为:校验字段。输出:无。

        轮数不许为负:那说明 SQL 聚合写错了(COUNT 不会是负的),而一个负的轮数
        会让前端显示成「-1 轮」还看不出是哪一层坏的。
        """
        if not self.session_id.strip():
            raise ValueError("session_id 不能为空")
        if self.rounds < 0:
            raise ValueError(f"轮数不能为负,收到 {self.rounds}")
