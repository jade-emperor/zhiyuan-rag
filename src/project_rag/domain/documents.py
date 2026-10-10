"""
文档治理的领域类型:一份**已入库文档**的登记信息。

**为什么需要它**:向量库里只有 chunk 级的 ``doc_id``(内容哈希),回答不了
"库里有哪些文件、各多少块、什么时候传的、处理成功没有" —— 而那正是前端要显示的东西
(参考图里的卡片:文件名 / 15 chunks / Processed / &lt;时间&gt;)。

**为什么时间用字符串**:``created_at`` 是给人看的登记信息,不是用来算间隔的
(算间隔的地方用 ``time.perf_counter()``,见 observability/latency.py)。
存 ISO-8601 文本省掉一堆时区问题,也让 JSON 序列化零转换。

**状态是闭集**:``pending``(已登记未处理) / ``processing``(正在切块入库) /
``processed``(可检索) / ``failed``(处理失败,带错误原因) / ``deleting``(正在删)。
``deleting`` 是**中间态**:它让"删到一半崩了"留下痕迹(而不是看起来一切正常)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DocStatus = Literal["pending", "processing", "processed", "failed", "deleting"]

DOC_STATUSES: tuple[DocStatus, ...] = ("pending", "processing", "processed", "failed", "deleting")

# 前端只该看到"能检索"或"在处理/失败"两类;`deleting` 归到不可检索那一类
SEARCHABLE_STATUSES: tuple[DocStatus, ...] = ("processed",)


@dataclass(frozen=True, slots=True)
class DocRecord:
    """一份文档的登记信息。``chunks`` 是入库成功后的子块数(未处理时是 0)。"""

    document_id: str
    source: str
    filename: str
    status: DocStatus
    chunks: int
    size_bytes: int
    created_at: str
    error: str | None = None

    @property
    def searchable(self) -> bool:
        """输入:无。行为:判断这份文档现在能不能被检索到。输出:布尔。"""
        return self.status in SEARCHABLE_STATUSES
