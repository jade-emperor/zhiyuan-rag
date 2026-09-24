"""
端口:对话历史的存取。

只依赖 ``domain`` + 标准库。``pymysql`` / SQL 都不许出现在这里 ——
换 MySQL / 换 Redis / 换内存实现,用例层一行都不用改。

**为什么端口这么窄**:会话历史在产品里只干一件事 ——「把最近几轮喂给提示词」。
所以它只需要「记一轮 / 读最近几轮 / 清掉一段」三个动作,不需要 UPDATE、不需要分页、
更不需要把表结构暴露出来。窄端口换来的是「实现可以随存储换」。

**为什么裁剪轮数是调用方传进来的**:会话表必须有上限,否则一个会话能无限长下去
(表一直涨,提示词也一直涨)。把 ``keep`` 做成参数而不是适配器自己的默认值,
是为了让「保留几轮」只有一个真值来源 —— 那个数字在配置里(``conversation.history_rounds``),
由用例层读出来、传下来。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import ConversationTurn


class ConversationStore(Protocol):
    """一段会话的问答历史。"""

    def record(self, session_id: str, turn: ConversationTurn, *, keep: int) -> None:
        """输入:会话 ID、这一轮的问答、保留轮数。

        行为:写入这一轮,并把该会话裁剪到**最近 keep 轮**(写入与裁剪同一次事务)。
        输出:无。
        """
        ...

    def recent(self, session_id: str, *, limit: int) -> Sequence[ConversationTurn]:
        """输入:会话 ID、最多取几轮。

        行为:取该会话最近的 limit 轮,按**时间正序**返回(最旧的在前)。
        输出:问答轮序列;没有历史给空序列(不抛错 —— 用例层要能走「第一轮」这条分支)。
        """
        ...

    def clear(self, session_id: str) -> int:
        """输入:会话 ID。行为:删掉该会话的全部历史。输出:删掉的轮数。"""
        ...
