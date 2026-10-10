"""
文档治理的用例:**删除一份文档时把它的三处痕迹一起清掉**。

一份文档在三个地方留痕:登记表一行、向量库里它的子块、磁盘上的原文件。
**顺序是刻意的**:

1. 先标记 ``deleting`` —— 立刻把它从"可检索"里摘出去(前端不会再显示成 Processed),
   而这一步是**幂等**的:重复删同一份不会出错;
2. 再删向量(``delete_by_doc_id``:按 ``doc_id`` 清子块)。**失败就抛,不动后面的** ——
   库里留着孤儿块比"表里没了但库里有"更难查(后者至少还查得到);
3. 再删磁盘文件(**文件不在不算错**:可能是手工删过,或者上传到一半);
4. 最后删登记行。

**为什么不用"先删行再删向量"**:那样中途失败就只剩一堆查不到主人的块 ——
``doc_id`` 是内容哈希,没有登记表就再也对不上是哪个文件了。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from project_rag.domain.documents import DocRecord
from project_rag.observability import get_logger
from project_rag.ports import DocumentStore, VectorStore

_logger = get_logger("application.documents")


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """一次删除的结果(**把"做了什么"如实说清**,而不是只回一个 200)。"""

    document_id: str
    deleted_chunks: int
    removed_file: bool
    removed_record: bool


def list_documents(store: DocumentStore) -> tuple[DocRecord, ...]:
    """输入:登记表。行为:全部文档(新→旧)。输出:记录元组。"""
    return tuple(store.list_all())


def delete_document(
    store: DocumentStore,
    vector_store: VectorStore,
    document_id: str,
    *,
    uploads_dir: Path,
) -> DeleteOutcome:
    """输入:登记表、向量库、文档 ID、上传目录。行为:按"标记→删向量→删文件→删行"清一遍。输出:结果。

    文档不存在时**不做任何事**并返回 ``removed_record=False``:删除是幂等的,
    重复点删除按钮不该报错(前端拿这个字段决定提示语)。
    """
    record = store.get(document_id)
    if record is None:
        return DeleteOutcome(document_id, 0, False, False)

    store.set_status(document_id, "deleting")
    deleted_chunks = vector_store.delete_by_doc_id(document_id)

    removed_file = False
    target = uploads_dir / record.filename
    if target.is_file():
        target.unlink()
        removed_file = True
    else:
        _logger.warning("删文档时原文件不在(可能手工删过):%s", target)

    removed_record = store.remove(document_id) > 0
    _logger.info(
        "已删除文档 %s(%s):向量 %d 块 / 文件 %s / 登记行 %s",
        record.filename,
        document_id,
        deleted_chunks,
        "已删" if removed_file else "不在",
        "已删" if removed_record else "不在",
    )
    return DeleteOutcome(document_id, deleted_chunks, removed_file, removed_record)


def register_document(
    store: DocumentStore, *, source: str, filename: str, size_bytes: int
) -> DocRecord:
    """输入:登记表、来源、文件名、字节数。行为:登记一份 ``pending`` 文档。输出:记录。

    **``document_id`` 必须等于 ``load_documents`` 会给这个文件的 ``doc_id``**(对单文件就是
    相对路径 = 文件名),``source`` 也必须等于它会用的来源标签(顶层目录名)。
    **这是一条硬不变式**:向量库里的块是以 ``doc_id`` 为键的,删文档那一步正是
    ``delete_by_doc_id(登记表的 document_id)`` —— 两套 ID 一旦不一致,删除会**静默地
    什么都不删**(返回 0 块),留下永远清不掉的孤儿块。就是这么踩的:
    登记用哈希、装载用文件名,入库成功但删除报 ``deleted_chunks=0``。

    **不用内容哈希**:内容一改 ID 就该变(那是 ``chunk_id`` 的口径),而登记行要跟着
    "这个文件"走 —— 同名文件重传即覆盖登记。
    """
    from datetime import datetime

    document_id = Path(filename).as_posix()
    record = DocRecord(
        document_id=document_id,
        source=source,
        filename=filename,
        status="pending",
        chunks=0,
        size_bytes=size_bytes,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    store.add(record)
    return record


def ingest_uploaded(
    store: DocumentStore,
    record: DocRecord,
    path: Path,
    *,
    chunker,
    embedder,
    vector_store: VectorStore,
    batch_size: int,
    load: Callable[[Path], Any],
) -> int:
    """输入:登记表、文档记录、文件路径、切分器、嵌入器、向量库、批大小。

    行为:状态推到 ``processing`` → 装载 → 切块 → 入库 → 推到 ``processed``(带块数);
          **任何异常都把状态记成 ``failed`` 并写上原因,然后原样抛出**。
    输出:入库的子块数。

    **为什么状态必须先写库再干活**:入库要几分钟,前端在那几分钟里要能看到"在处理";
    只在成功/失败时写,中间那段时间卡片会一直显示 pending —— 用户不知道系统是不是卡住了。

    **失败了也要写状态**:静默失败会留下一份"永远 pending"的文档,
    前端只能显示一个转不完的圈;写清楚原因,用户至少知道该重传。
    """
    from .ingest_documents import ingest_documents

    store.set_status(record.document_id, "processing")
    try:
        loaded = load(path)
        report = ingest_documents(
            loaded.documents,
            chunker=chunker,
            embedder=embedder,
            store=vector_store,
            batch_size=batch_size,
        )
    except Exception as exc:  # noqa: BLE001 - 状态要如实记下来,再抛给调用方
        store.set_status(record.document_id, "failed", error=f"{type(exc).__name__}: {exc}")
        _logger.error("入库失败 %s:%s", record.filename, exc)
        raise
    store.set_status(record.document_id, "processed", chunks=report.chunks)
    _logger.info("已入库 %s:%d 块", record.filename, report.chunks)
    return report.chunks


PREVIEW_CHARS = 400
"""预览里每段正文最多显示几个字。**口径只在这一处** —— 接口不另给默认值。"""

PREVIEW_LIMIT = 20
"""预览最多返回几段(接口的 limit 上界也用它)。"""


@dataclass(frozen=True, slots=True)
class PreviewBlock:
    """预览里的一段 —— **父块**(模型真正读到的那段),不是检索命中的子块。"""

    index: int  # 从 1 开始,给前端显示
    page: int | None  # 这一段里**第一个有页码的子块**的页
    last_page: int | None  # 最后一个有页码的子块的页(与 page 相同时前端只显示一个)
    chars: int  # 这一段正文的完整字数(不是截断后的)
    text: str  # 截断后的正文


@dataclass(frozen=True, slots=True)
class DocumentPreview:
    """一份文档在向量库里的样子(前端那个「预览」按钮要看的东西)。

    **``parent_blocks`` 与 ``child_chunks`` 是两个数,不能混**:入库写进去的是**子块**
    (``registry.chunks`` 与 ``child_chunks`` 同口径),而预览显示的是折叠后的**父块** ——
    两者差着切分比例(实测约 1 父 : 5 子)。前端把"入库 42 块"和"预览 15 段"并排显示时,
    用户才不会以为丢数据了。
    """

    document_id: str
    parent_blocks: int
    child_chunks: int
    truncated: bool
    blocks: tuple[PreviewBlock, ...]


def preview_document(
    vector_store: VectorStore,
    document_id: str,
    *,
    limit: int = PREVIEW_LIMIT,
    chars: int = PREVIEW_CHARS,
) -> DocumentPreview:
    """输入:向量库、文档 ID、最多几段、每段最多几个字。行为:取该文档的块 → 折叠成父块。输出:预览。

    **为什么在这里折叠**:向量库里存的是**子块**,父块正文挂在每个子块的 ``parent_content``
    上(见 ``ingest_documents``)。预览要给人看的是"模型会读到的那段",所以按 ``parent_id``
    把子块折叠回去;老数据没有父块(``parent_id`` 为空串)时那一段就是它自己 ——
    与检索时「有父块用父块,没有就用自己」是**同一条口径**(``Chunk.context_text``)。

    **顺序只保证稳定,不保证与原文一致**:块里没有偏移列(``chunk_id`` 是摘要),
    所以只能按「先有页码的、页码升序、再按块 ID」排。对 PDF 来说这基本就是原文顺序;
    对 md/txt 只能保证两次预览看到的是一样的。要做到严格原文顺序,得给向量库加一列偏移
    (schema 变更 + 整库重灌,见 ``docs/deferred.md``)。

    **为什么用 ``load_all`` 再过滤**:与 ``GET /api/v1/sources`` 同一个取舍 —— 语料几千块时
    一次全读约 250KB,可接受;上万块时给端口加 ``chunks_of(doc_id, limit)``(已记 deferred)。
    """
    if limit <= 0:
        raise ValueError(f"limit 必须为正,收到 {limit}")
    if chars <= 0:
        raise ValueError(f"chars 必须为正,收到 {chars}")

    mine = [chunk for chunk in vector_store.load_all() if chunk.doc_id == document_id]

    # 按父块折叠。键取 ``parent_id or chunk_id``:老数据没有父块,那它自己就是一段。
    grouped: dict[str, list] = {}
    for chunk in mine:
        grouped.setdefault(chunk.parent_id or chunk.chunk_id, []).append(chunk)

    blocks: list[PreviewBlock] = []
    for children in grouped.values():
        pages = [child.page for child in children if child.page is not None]
        body = max((child.context_text for child in children), key=len)
        blocks.append(
            PreviewBlock(
                index=0,  # 排完序再编号
                page=min(pages) if pages else None,
                last_page=max(pages) if pages else None,
                chars=len(body),
                text=body if len(body) <= chars else body[:chars] + "…",
            )
        )

    blocks.sort(key=lambda block: (block.page is None, block.page or 0))
    ordered = tuple(
        PreviewBlock(
            index=position,
            page=block.page,
            last_page=block.last_page,
            chars=block.chars,
            text=block.text,
        )
        for position, block in enumerate(blocks[:limit], start=1)
    )
    return DocumentPreview(
        document_id=document_id,
        parent_blocks=len(blocks),
        child_chunks=len(mine),
        truncated=len(blocks) > limit,
        blocks=ordered,
    )
