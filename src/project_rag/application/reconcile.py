"""
对账:把「登记表说的」和「向量库里实际有的」摆到一起。

**为什么必须有这一步**:一份文档在三个地方留痕(登记表 / 向量库 / 磁盘文件),
而三处的一致性靠**约定**维持 —— 约定就会破。就破过一次:登记表的 ``document_id``
与装载器给的 ``doc_id`` 是两套 ID,入库成功但删除一块都删不掉,**而所有接口都返回 200**。
那次是靠一条用真装载器的契约抓住的;对账是第二道网 —— 它不依赖任何断言写得好不好,
只回答一个事实问题:**库里那些块,登记表认不认?**

两类问题(名字是刻意选的,免得看的人要想):

* ``orphans`` **孤儿块**:向量库里有、登记表里没有。成因:ID 口径不一致 / 登记行被手工删了 /
  删除时只删了行没删向量。**它们永远不会被任何一次删除清掉**,而且照样会被检索到
  (用户会看到"库里有这个内容,但清单里没有这份文件")。
* ``missing`` **缺块**:登记表说是 ``processed``,库里却一块都没有。成因:入库失败后状态没改 /
  有人手工清了库。**前端会显示成"已处理"但搜不到**。

只读:``reconcile`` 不改任何东西。修不修、怎么修由人决定(自动"修复"孤儿块 = 自动删数据)。
``prune_orphans`` / ``forget_missing`` 是**给人用的那两个动作**(P0-3b-5-②②):它们只做
「把已经列出来的这些清掉」,判断谁该清仍然只发生在 ``reconcile`` 里 —— 两处各判一次,
迟早会判得不一样,而这条路的代价是删数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from project_rag.domain.documents import DocRecord
from project_rag.ports import DocumentStore, VectorStore


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """一次对账的结果。``healthy`` 只看那两份清单,不看计数。"""

    registered: int
    indexed_doc_ids: int
    ok: int
    orphans: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        """输入:无。行为:判断要不要报警。输出:两份清单都空为真。"""
        return not self.orphans and not self.missing


def reconcile(
    records: Sequence[DocRecord], chunk_counts: Mapping[str, int]
) -> ReconcileReport:
    """输入:登记表全部记录、``doc_id -> 库内块数``。

    行为:按 ``doc_id`` 集合求差。**只有 ``processed`` 的登记行才要求库里有块** ——
    ``pending`` / ``processing`` 本来就不该有,``failed`` 与 ``deleting`` 也不该有
    (后者是删到一半,下一次对账会把它报成 missing,那正是我们要的信号)。
    输出:对账报告。
    """
    registered_ids = {record.document_id for record in records}
    indexed_ids = {doc_id for doc_id, count in chunk_counts.items() if count > 0}

    orphans = tuple(sorted(indexed_ids - registered_ids))
    missing = tuple(
        sorted(
            record.document_id
            for record in records
            if record.status == "processed" and chunk_counts.get(record.document_id, 0) == 0
        )
    )
    return ReconcileReport(
        registered=len(records),
        indexed_doc_ids=len(indexed_ids),
        ok=len(registered_ids & indexed_ids),
        orphans=orphans,
        missing=missing,
    )


def backfill_records(
    grouped: Mapping[str, tuple[str, int]], *, created_at: str
) -> tuple[DocRecord, ...]:
    """输入:``doc_id -> (source, 库内块数)``、登记时间。

    行为:为**登记表出现之前**就入库的语料造登记行 —— 状态直接是 ``processed``
         (它们本来就在库里、可检索),``size_bytes=0``(当年没记大小,不编一个数)。
    输出:待写入的记录元组。

    **为什么需要它**:``documents`` 表是 P0-3b 才有的,而 ``bagu_ai / bagu_java / doc / pdf``
    那批语料在它之前就入库了 —— 没有这步,``doctor`` 首跑会把它们全报成孤儿块
    (真机实测 10 个 doc_id),而"天天误报"的对账等于没有对账。

    **为什么状态是 processed 而不是 pending**:``pending`` 的含义是"等入库",
    而这些块**已经在库里了** —— 标成 pending 会让前端显示"处理中",
    还会被对账报成缺块(自己造出一批假问题)。
    """
    return tuple(
        DocRecord(
            document_id=doc_id,
            source=source,
            filename=doc_id,
            status="processed",
            chunks=chunks,
            size_bytes=0,
            created_at=created_at,
        )
        for doc_id, (source, chunks) in sorted(grouped.items())
    )


def prune_orphans(
    store: VectorStore, doc_ids: Sequence[str]
) -> tuple[tuple[str, int], ...]:
    """输入:向量库 + 孤儿 ``doc_id`` 序列。行为:逐个删掉它们在库里的**全部**子块。输出:``(doc_id, 删了几块)``。

    **只删传进来的这些**:「谁是孤儿」由 ``reconcile`` 判完再交进来,这里不自己再判一次。
    「孤儿」的定义一旦有两处实现,迟早会不一致 —— 而这条路的代价是删掉**别人还有用的块**。

    返回**逐条计数**而不是总数:人事后核对时唯一看得见的东西是「哪一份删了多少」;
    合成一个数字,删错了也看不出来。
    """
    return tuple((doc_id, store.delete_by_doc_id(doc_id)) for doc_id in doc_ids)


def forget_missing(
    store: DocumentStore, doc_ids: Sequence[str]
) -> tuple[tuple[str, int], ...]:
    """输入:登记表 + 缺块 ``doc_id`` 序列。行为:删掉这些登记行。输出:``(doc_id, 删了几行)``。

    缺块(登记为 ``processed``、库里一块没有)的两种修法**不在这里选**:

    * 重灌 —— 语料还在磁盘上时选它(内容能回来);
    * 删行 —— 语料已经不在了(上传的临时文件被清掉、测试残留)时选它,否则对接账
      **天天误报**,而"天天误报的对账等于没有对账"(与 ``backfill_records`` 那条同一个道理)。

    这里只做第二件:删行。**别自动做** —— 自动删行会让「为什么会有缺块」这个真问题
    跟着一起消失(比如某次 ingest 一直失败,行被自动清掉就再也没人发现)。
    """
    return tuple((doc_id, store.remove(doc_id)) for doc_id in doc_ids)

