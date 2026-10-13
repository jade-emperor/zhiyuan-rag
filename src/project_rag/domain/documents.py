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

**``DocState`` 是另一根轴**:上面那个 ``DocStatus`` 是**登记表说它怎么样**,
而 ``DocState`` 是**它现在到底怎么样** —— 清单接口把登记表、磁盘、向量库三处摆在一起后
给的那一个字段(见 ``application/documents.py`` 的 ``document_views``)。
两者只在 ``processed`` 上分岔:非 ``processed`` 的行 ``state`` 原样等于 ``status``
(在途/失败的行,登记表就是真相);``processed`` 的行才可能被现实推翻成下面四种之一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DocStatus = Literal["pending", "processing", "processed", "failed", "deleting"]

DOC_STATUSES: tuple[DocStatus, ...] = ("pending", "processing", "processed", "failed", "deleting")

# 前端只该看到"能检索"或"在处理/失败"两类;`deleting` 归到不可检索那一类
SEARCHABLE_STATUSES: tuple[DocStatus, ...] = ("processed",)

# 「这份文档现在到底怎么样」—— 清单要显示的那一个字段。**闭集,而且只有这一处定义**:
# 前端的话术表、验收的断言、接口的枚举全跟着它走。
#
# 四个"不一致态"的名字是刻意选的,每个都直接说清**哪一处不对**:
#   not_indexed  登记说 processed,向量库里一块都没有(集合被清过 / 那次入库其实没成);
#   file_missing 磁盘上找不到这个文件(被手工删了 / 上传的原件被清了);
#   unregistered 磁盘上有、登记表没有(后端直接放进去的语料,还没入库);
#   changed      磁盘上的文件变了(与登记时记下的大小不符)—— 库里的内容是旧的。
# 剩下四个就是那四种在途/失败状态**原样**搬过来(pending/processing/failed/deleting):
# 那种行的真相就是登记表 —— 拿"库里没有块"去报 pending,等于自己造一批假问题。
DocState = Literal[
    "ok",
    "changed",
    "not_indexed",
    "file_missing",
    "unregistered",
    "pending",
    "processing",
    "failed",
    "deleting",
]

DOC_STATES: tuple[DocState, ...] = (
    "ok",
    "changed",
    "not_indexed",
    "file_missing",
    "unregistered",
    "pending",
    "processing",
    "failed",
    "deleting",
)

# 「三处一致」的那一个值。别处要判"这份文档没问题"就比它,不要自己再数一遍条件。
STATE_OK: DocState = "ok"

# 「不一致」的那四个 —— 清单的汇总、前端的话术都按这一组走。**在途/失败不算不一致**:
# 它们是"还没干完"或"干砸了",不是"三处对不上"。
STATE_MISMATCHES: tuple[DocState, ...] = ("changed", "not_indexed", "file_missing", "unregistered")

# 「在途与失败」那一组(汇总里合成一档:它们的处理方式都是"等一下 / 看错误")。
STATE_IN_FLIGHT: tuple[DocState, ...] = ("pending", "processing", "failed", "deleting")


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
        """输入:无。行为:判断**登记表**说这份文档能不能被检索。输出:布尔。

        **这是登记表那一侧的说法,不是现实**:登记表会撒谎(集合被清过、入库其实没成),
        而"现在搜不搜得到"的唯一判据是**向量库里有没有它的块**。清单接口回的就是后者
        (见 ``application/documents.py`` 的 ``document_views``)—— 这里保留原义,
        是因为对账(``reconcile``)要的正是"登记表怎么说"。
        """
        return self.status in SEARCHABLE_STATUSES
