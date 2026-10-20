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

**删除只删"上传目录里的原件"**:语料目录(``data/corpus/**``)是**源材料** ——
删库不等于删源,清掉块与登记行之后重新灌一遍就能回来;把源文件一起删了就真没了(不可逆)。

这个模块还负责清单那一侧:**把登记表、磁盘、向量库三处摆到一起**
(``document_views`` / ``summarize``)。为什么必须有这一步:登记表是一份**没人失效的缓存** ——
``project-rag ingest`` 灌完不写它、``rm`` 一个语料文件不写它、drop 一个集合更不写它,
于是"清单"曾经只是把登记表念了一遍:集合清空了卡片还写"已入库 · 677 块",
语料目录里新放的文件前端**根本看不到**。三处对账之后,``state`` 说的才是"现在到底怎么样"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from dataclasses import dataclass
from pathlib import Path

from project_rag.domain.documents import (
    STATE_IN_FLIGHT,
    STATE_MISMATCHES,
    STATE_OK,
    UNFILED,
    DocRecord,
    DocState,
    DocStatus,
)
from project_rag.observability import get_logger
from project_rag.ports import DiskFile, DocumentStore, SparseEncoder, VectorStore

_logger = get_logger("application.documents")


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """一次删除的结果(**把"做了什么"如实说清**,而不是只回一个 200)。

    ``kept_source`` 与 ``removed_file`` 是**互斥**的两种"文件还在":前者是"那是语料源材料,
    我们没动它"(故意的),后者是"本来就不在"(想删也没得删)。合成一个字段会让前端
    把"故意保留"说成"没找到",而这两件事对用户的意义正相反。
    """

    document_id: str
    deleted_chunks: int
    removed_file: bool
    removed_record: bool
    kept_source: bool = False


def list_documents(store: DocumentStore) -> tuple[DocRecord, ...]:
    """输入:登记表。行为:全部文档(新→旧)。输出:记录元组。"""
    return tuple(store.list_all())


def delete_document(
    store: DocumentStore,
    vector_store: VectorStore,
    document_id: str,
    *,
    uploads_dir: Path,
    source_file: Path | None = None,
) -> DeleteOutcome:
    """输入:登记表、向量库、文档 ID、上传目录、可选的「磁盘上那份原件在哪」。

    行为:按"标记→删向量→删行"清一遍;文件**只在上传目录里才删**(语料源材料不动)。输出:结果。

    文档不存在时**不做任何事**并返回 ``removed_record=False``:删除是幂等的,
    重复点删除按钮不该报错(前端拿这个字段决定提示语)。

    ``source_file`` 由调用方(接口层)用**同一份磁盘索引**解析出来 —— 清单上显示的那份文件
    与删除时动手的那份必须是同一个,否则"我删的是这个、它删的是那个"。
    解析不到时回落到 ``uploads_dir / filename``(上传的原件就在那儿,而且这个拼法
    不会跑出上传目录);那之外的一律不动。
    """
    record = store.get(document_id)
    if record is None:
        return DeleteOutcome(document_id, 0, False, False)

    store.set_status(document_id, "deleting")
    deleted_chunks = vector_store.delete_by_doc_id(document_id)

    target = source_file if source_file is not None else uploads_dir / record.filename
    removed_file = False
    kept_source = False
    if not _inside(target, uploads_dir):
        # 语料目录里的源材料:清库不等于删源(这是不可逆的那一步,宁可留着)
        kept_source = target.is_file()
        if kept_source:
            _logger.info("删文档时保留语料源材料(删库不删源):%s", target)
    elif target.is_file():
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
        "已删" if removed_file else ("源材料保留" if kept_source else "不在"),
        "已删" if removed_record else "不在",
    )
    return DeleteOutcome(document_id, deleted_chunks, removed_file, removed_record, kept_source)


def _inside(path: Path, parent: Path) -> bool:
    """输入:一个路径与一个目录。行为:判断它是否在目录里(不做符号链接解析)。输出:布尔。"""
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


# ══ 清单:三处对账 ═══════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class DocumentView:
    """清单上的一行:**登记表说的** + **磁盘上的** + **库里实际有的**,三处摆在一起。

    两个数各有各的名字,不许合成一个:``chunks`` 是登记表记的(那次入库写进去多少),
    ``indexed_chunks`` 是库里现在实际的。相等才是"对得上",不等就是"哪一步出过事" ——
    只给一个数,用户没法知道该信谁(实测:集合被 drop 之后,卡片照样显示 677 块)。
    """

    document_id: str
    source: str
    filename: str
    # 登记表说的状态;未登记的文件是 "unregistered"(它没有登记行,别拿 processed 顶)。
    status: DocStatus | Literal["unregistered"]
    state: DocState
    chunks: int
    size_bytes: int  # 登记时记下的字节数(0 = 当年没记过,判不了"文件改没改")
    created_at: str  # 登记时间;未登记的文件是空串(磁盘 mtime 不是登记时间,不编)
    error: str | None
    searchable: bool  # **按库内实际块数**算,不是按 status
    on_disk: bool
    disk_size_bytes: int
    indexed_chunks: int
    # 归在哪一级目录下(空串 = 未分类)。**未登记的文件必然是未分类** —— 它没有行可写,
    # 而"把它归到某处"这件事需要一次写库,读接口不做(与 ``unregistered`` 只读同一条口径)。
    folder: str = ""


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    """清单的一句话汇总(与逐行 ``state`` **同一个口径**:数一遍,不另算一套)。"""

    total: int
    ok: int
    changed: int
    not_indexed: int
    file_missing: int
    unregistered: int
    in_flight: int


def document_views(
    records: Sequence[DocRecord],
    *,
    files: Sequence[DiskFile] = (),
    indexed: Mapping[str, int] | None = None,
) -> tuple[DocumentView, ...]:
    """输入:登记表的行、磁盘上的文件、``doc_id -> 库内块数``。

    行为:三处按 ``doc_id`` 摆到一起,给每行一个 ``state``;磁盘上有、登记表没有的**也列出来**
         (``unregistered``,只读:读接口不写库)。输出:清单行(登记的行在前,未登记的按 doc_id 排在后)。

    ``state`` 的优先级是刻意的(**这个顺序本身就是口径**)：

        没有登记行            → unregistered
        登记行不是 processed  → 原样回那个状态(在途/失败的行,登记表就是真相)
        磁盘上没有            → file_missing   ← 比下面那条更根本:重灌也救不了
        库内 0 块             → not_indexed
        记过大小且与磁盘不符  → changed
        其余                  → ok
    """
    counts = {} if indexed is None else indexed
    by_id: dict[str, DiskFile] = {}
    seen: dict[Path, DiskFile] = {}
    for file in files:
        if file.path in seen:  # 调用方把同一个文件列了两遍时只认一份
            continue
        seen[file.path] = file
        for candidate in file.doc_ids:
            by_id.setdefault(candidate, file)

    views: list[DocumentView] = []
    used: set[Path] = set()
    for record in records:
        file = by_id.get(record.document_id)
        if file is not None:
            used.add(file.path)
        views.append(_view_of(record, file, counts.get(record.document_id, 0)))

    # 未登记的文件:后端直接放进语料目录的、CLI 灌了但没登记的 —— 以前前端根本看不到它们。
    for path, file in sorted(seen.items(), key=lambda item: (item[1].doc_id, item[1].source)):
        if path in used:
            continue
        views.append(
            DocumentView(
                document_id=file.doc_id,
                source=file.source,
                filename=Path(file.doc_id).name,
                status="unregistered",
                state="unregistered",
                chunks=0,
                size_bytes=0,
                created_at="",
                error=None,
                searchable=counts.get(file.doc_id, 0) > 0,
                on_disk=True,
                disk_size_bytes=file.size_bytes,
                indexed_chunks=counts.get(file.doc_id, 0),
                folder=UNFILED,
            )
        )
    return tuple(views)


def _view_of(record: DocRecord, file: DiskFile | None, indexed_chunks: int) -> DocumentView:
    """输入:一条登记行 + 它在磁盘上的那份文件(没有给 None)+ 库内块数。行为:算 state。输出:清单行。"""
    return DocumentView(
        document_id=record.document_id,
        source=record.source,
        filename=record.filename,
        status=record.status,
        state=_state_of(record, file, indexed_chunks),
        chunks=record.chunks,
        size_bytes=record.size_bytes,
        created_at=record.created_at,
        error=record.error,
        searchable=indexed_chunks > 0,
        on_disk=file is not None,
        disk_size_bytes=0 if file is None else file.size_bytes,
        indexed_chunks=indexed_chunks,
        folder=record.folder,
    )


def _state_of(record: DocRecord, file: DiskFile | None, indexed_chunks: int) -> DocState:
    """输入:登记行 + 磁盘文件(可 None)+ 库内块数。行为:按上面那张优先级表判。输出:``DocState``。"""
    if record.status != "processed":
        return record.status
    if file is None:
        return "file_missing"
    if indexed_chunks == 0:
        return "not_indexed"
    # size_bytes=0 = 当年没记过大小(回填的老语料就是这种)——判不了就别猜成 changed。
    if record.size_bytes > 0 and record.size_bytes != file.size_bytes:
        return "changed"
    return STATE_OK


def summarize(views: Sequence[DocumentView]) -> DocumentSummary:
    """输入:清单行。行为:数一遍各状态。输出:汇总。

    **在途与失败合成一档**(``in_flight``):它们的处理方式都是"等一下 / 去看错误",
    而不是"三处对不上"。四个不一致态各占一档 —— 因为修法各不相同(重灌 / 删行 / 入库 / 移文件)。
    """
    counts: dict[str, int] = {}
    for view in views:
        counts[view.state] = counts.get(view.state, 0) + 1
    return DocumentSummary(
        total=len(views),
        ok=counts.get("ok", 0),
        changed=counts.get("changed", 0),
        not_indexed=counts.get("not_indexed", 0),
        file_missing=counts.get("file_missing", 0),
        unregistered=counts.get("unregistered", 0),
        in_flight=sum(counts.get(state, 0) for state in STATE_IN_FLIGHT),
    )


def mismatches(summary: DocumentSummary) -> int:
    """输入:汇总。行为:数出"三处对不上"的总数(在途与失败不算)。输出:份数。"""
    return sum(getattr(summary, state) for state in STATE_MISMATCHES)


def register_ingested(
    store: DocumentStore,
    *,
    documents: Sequence[Any],
    chunks: Sequence[Any],
    sizes: Mapping[str, int],
    created_at: str,
    folder: str = UNFILED,
    folders: Mapping[str, str] | None = None,
    on_write: Callable[[DocRecord], None] | None = None,
) -> tuple[DocRecord, ...]:
    """输入:登记表、这次入库的文档、切出来的块、``doc_id -> 磁盘字节数``、登记时间。

    行为:把**这次真的入库了**的结果写进登记表(有行就覆盖,没有就新增)。输出:写进去的记录。

    **为什么写入方必须登记**:登记表是清单的骨架,而 ``project-rag ingest`` 以前根本不碰它 ——
    于是"后端灌进去的语料"在知识库页上永远是「未登记」,用户只能看到上传的那些。
    上传那条路本来就登记(``register_document``),这里补的是 CLI 那条路,两边从此同口径。

    ``sizes`` 是**磁盘上的真实字节数**,不是正文长度:登记表要靠它判"文件后来改没改"
    (见 ``_state_of`` 的 ``changed``)。给 0 就等于放弃这个判据 —— 老的回填行就是这么来的。

    ``folders`` 是**逐份**的目录(``doc_id -> folder_id``),给了就按它来、``folder`` 只当兜底:
    一次灌整个语料根时,里面的文档分属不同的一级目录 —— 用同一个 ``folder`` 会把它们
    全塞进一格(而那一格的名字会是语料根本身,那是"所有格的父亲",不该出现)。
    """
    per_doc: dict[str, int] = {}
    for chunk in chunks:
        per_doc[chunk.doc_id] = per_doc.get(chunk.doc_id, 0) + 1

    written: list[DocRecord] = []
    for document in documents:
        target = folder if folders is None else folders.get(document.doc_id, folder)
        # 同名文档已经在**另一个目录**里:登记行会被这次覆盖(``document_id`` 就是文件名)。
        # 这是数据层面的限制,不是这次可以静默过去的事 —— 记一条 WARNING 点名两个目录。
        existing = store.get(document.doc_id)
        if existing is not None and existing.folder != target:
            _logger.warning(
                "文档 %s 原来在目录 %r,这次入到 %r —— 两个目录不能有同名文件,登记行跟着这次走",
                document.doc_id,
                existing.folder or "未分类",
                target or "未分类",
            )
        record = DocRecord(
            document_id=document.doc_id,
            source=document.source,
            filename=Path(document.doc_id).name,
            status="processed",
            chunks=per_doc.get(document.doc_id, 0),
            size_bytes=int(sizes.get(document.doc_id, 0)),
            created_at=created_at,
            folder=target,
        )
        store.upsert(record)
        if on_write is not None:
            on_write(record)
        written.append(record)
    return tuple(written)


def register_document(
    store: DocumentStore,
    *,
    source: str,
    filename: str,
    size_bytes: int,
    folder: str = UNFILED,
) -> DocRecord:
    """输入:登记表、来源、文件名、字节数。行为:登记一份 ``pending`` 文档。输出:记录。

    **``document_id`` 必须等于 ``load_documents`` 会给这个文件的 ``doc_id``**(对单文件就是
    相对路径 = 文件名),``source`` 也必须等于它会用的来源标签(顶层目录名)。
    **这是一条硬不变式**:向量库里的块是以 ``doc_id`` 为键的,删文档那一步正是
    ``delete_by_doc_id(登记表的 document_id)`` —— 两套 ID 一旦不一致,删除会**静默地
    什么都不删**(返回 0 块),留下永远清不掉的孤儿块。就是这么踩的:
    登记用哈希、装载用文件名,入库成功但删除报 ``deleted_chunks=0``。

    **不用内容哈希**:内容一改 ID 就该变(那是 ``chunk_id`` 的口径),而登记行要跟着
    "这个文件"走 —— 同名文件重传即覆盖登记(走 ``upsert``:状态回到 ``pending``、大小按这一次的、
    ``created_at`` 保留 —— 那是它第一次出现的时间)。

    **同名但换目录不允许**(接口层回 409):``document_id`` 就是文件名,而删块、挪目录、
    幂等清理都按这一个键 —— 两个目录各有一份同名文件时,它们会互相删对方的块。
    这是"文档 ID = 文件名"这条老口径带来的限制,目录这一层没能消除它,只把它变成一次**当场报错**。
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
        folder=folder,
    )
    store.upsert(record)
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
    sparse_encoder: SparseEncoder | None = None,
) -> int:
    """输入:登记表、文档记录、文件路径、切分器、嵌入器、向量库、批大小、可选的稀疏编码器。

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
        # 稀疏权重的统计口径是**全库**(BM25 的 idf 本来就是全库属性):上传的是某一篇,
        # 所以把已有语料的正文一起喂进去。这一步只在接了稀疏编码器时发生 ——
        # keyword.backend=memory 那档不需要它(那份索引自己会读全量语料)。
        corpus_texts = (
            [chunk.text for chunk in vector_store.load_all()]
            if sparse_encoder is not None
            else None
        )
        report = ingest_documents(
            loaded.documents,
            chunker=chunker,
            embedder=embedder,
            store=vector_store,
            batch_size=batch_size,
            sparse_encoder=sparse_encoder,
            corpus_texts=corpus_texts,
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
    (要改 schema 并整库重灌;目前不做)。

    **为什么用 ``load_all`` 再过滤**:与 ``GET /api/v1/sources`` 同一个取舍 —— 语料几千块时
    一次全读约 250KB,可接受;上万块时给端口加 ``chunks_of(doc_id, limit)``。
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
