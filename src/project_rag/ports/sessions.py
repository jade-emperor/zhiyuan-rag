"""
端口:会话目录(「我有哪些会话」)。

**为什么单独一个端口而不是塞进 ``ConversationStore``**:那个端口讲的是「一段会话的
最近几轮怎么读写」,它的每个方法都带 ``session_id``;而这里问的是**跨会话**的问题
(我名下有哪几段),而且要**按人过滤** —— 人这件事属于授权,不属于对话内容。
混进去会让 ``ConversationStore`` 的实现(内存实现、验收里的假实现)凭空多出一个
它们不关心的用户维度。

**为什么只依赖 domain + 标准库**:与其它端口同一条规矩 —— 换 MySQL / 换 Redis /
换内存实现,用例层与 api 层一行都不用改。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain.sessions import SessionSummary


class SessionCatalog(Protocol):
    """按账号列出会话目录。"""

    def list_for(self, user_id: str, *, limit: int) -> Sequence[SessionSummary]:
        """输入:用户 ID、最多几条。

        行为:取**这个用户名下**的会话,按最近活动倒序 —— 只列出**至少有一轮问答**的
        (刚建但没问过的会话不占地方:前端是"首次提问时才建会话")。

        输出:会话摘要序列;没有给空序列(不抛错 —— 新账号本来就该是空的)。
        """
        ...

    def list_all(self, *, limit: int) -> Sequence[SessionSummary]:
        """输入:最多几条。行为:跨账号列会话(管理员视角),只列**至少有一轮问答**的。

        输出:会话摘要序列(最近活动在前),每条的 ``owner`` 是归属人的登录名
        (账号已删除或没记录时给空串);没有给空序列。
        """
        ...
