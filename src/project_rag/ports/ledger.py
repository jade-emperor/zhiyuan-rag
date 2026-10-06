"""
端口:延迟账本的持久化(**只读/写行,不做聚合**)。

行是 ``{字段名: 文本值}`` —— 与日志那一行同源(``StreamTiming.values()``),所以这个端口
**不需要跟着账本字段一起改**:加指标是账本的事,存储只管"把这行留下来"。

**为什么聚合不在这里**:分位数、冷热分层、"``-`` 不参与统计"这些都是**口径**,口径要能
在一台没有 MySQL 的机器上测(``check_m5_9_metrics.py`` 就是这么测的)。存储只回答
「这段时间有哪些行」,口径留给用例层。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol


class LedgerStore(Protocol):
    """延迟账本的存与取。"""

    def init_schema(self) -> None:
        """输入:无。行为:建表/迁移(**幂等**,可重复调用)。输出:无。

        放在端口上而不是让调用方 import 具体的 DDL:建表是存储层自己的事,
        组合根只需要知道"用之前先确保它是好的"。
        """
        ...

    def record(self, row: Mapping[str, str]) -> None:
        """输入:一行账本(字段名 → 文本值)。行为:追加写入。输出:无。

        写失败**必须抛出去**:降级是调用方的事(``LedgerHandler`` 会吞掉并计数),
        在这里偷偷吞掉会让"表里少了一半数据"变得无法发现。
        """
        ...

    def load_since(self, since: datetime) -> Sequence[Mapping[str, str]]:
        """输入:起始时刻(含)。行为:取该时刻之后写入的行,按时间升序。输出:行序列。

        返回的是**原始文本值**(``-`` 原样保留):把它当 0 或者丢掉,都是口径决策,
        不该由存储层替用例层做。
        """
        ...
