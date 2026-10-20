"""
用例:把文档切块、编码、写进向量库(幂等)。

只认识端口 —— 换切分策略 / 换嵌入模型 / 换向量库,这个文件一行都不改。

两个函数分开是为了**能只跑前半段**:``chunk_documents`` 是纯计算(不联网、不加载模型),
``ingest_documents`` 才需要嵌入器和向量库。CLI 的 ``--dry-run`` 只调前者。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from project_rag.domain import Chunk, ChunkGroup, Document, page_of
from project_rag.observability import get_logger
from project_rag.ports import Embedder, SparseEncoder, SparseStats, TextChunker, VectorStore

_logger = get_logger("application.ingest_documents")

# 批量越大越划算(padding 摊销 + 矩阵乘吃满),所以默认按批走,而不是逐块调用。
DEFAULT_BATCH_SIZE = 64

# 主键摘要算法。**只在这一处定义**:换算法只改这一个字符串,
# 十六进制长度必须 ≤ Milvus schema 里 chunk_id / parent_id 的 max_length(64)。
_DIGEST = "sha256"

# 摘要各字段之间的分隔符。用单元分隔符而不是竖线:
# 竖线是合法文件名字符、markdown 表格里也常见,而 f"{a}|{b}" 对
# ("a|b", "c") 和 ("a", "b|c") 会给出同一个串。
# 分隔符本来就该挑一个正文里不会出现的字符;
# 这也是缓存 value 契约用的那个分隔符,口径一致。
_FIELD_SEP = "\x1f"


def _digest(*parts: str) -> str:
    """输入:若干文本片段。行为:按 ``_FIELD_SEP`` 连接后取摘要。输出:十六进制摘要字符串。

    **为什么不是 md5**:md5 已有实用的碰撞构造,而 ``chunk_id`` 是本项目**唯一**的
    幂等依据 —— 一旦碰撞,两个不同的块会被当成同一个,``upsert`` 会**静默覆盖**掉一个,
    表现成「语料里少了一块」而没有任何报错。这种静默的数据丢失,比多花几微秒糟得多。

    sha256 的十六进制是 64 字符,正好等于 Milvus schema 里 ``chunk_id`` / ``parent_id``
    的 ``max_length=64``,所以换算法**不需要改集合结构**。
    """
    return hashlib.new(_DIGEST, _FIELD_SEP.join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IngestReport:
    """一次 ingest 的统计。"""

    documents: int  # 读进来几份文档
    chunks: int  # 切出几个子块
    removed: int  # 写之前清掉的旧块数(重新 ingest 时不为 0)
    batches: int  # 发了几次嵌入调用
    in_store: int  # 最后一次 upsert 后集合里的子块总数(0 = 什么都没写)
    # 稀疏向量那半边的读数(没接编码器时是 0):词表大小与平均块长 ——
    # 它们决定"要不要调 chunk_size / 换 k1,b",所以跟着报告一起出来,而不是只进日志。
    sparse_vocabulary: int = 0
    sparse_avg_len: float = 0.0


def _page_of_child(
    document: Document, group: ChunkGroup, child_index: int
) -> int | None:
    """输入:文档 + 父块分组 + 子块序号。行为:拿这个子块的起始偏移查页码区间表。输出:页码;不知道给 None。

    **跨页的块取"起始页"**:一个 300 字的子块压在页边界上时,它确实横跨两页;
    引用只能给一个页号,给的是它**开始**的那一页(和"翻到这一页就能看到这段"一致)。
    另一半在下一页这种事不写进页码 —— 页码不是区间,写成区间前端也没法显示。

    三种「不知道」都归到 None,一个都不许猜:格式没有页码概念(``pages`` 为空)、
    切分器不报偏移(``child_starts`` 为空)、偏移落进拼页用的 ``\\n\\n`` 里(``page_of`` 给 None)。
    """
    if not document.pages or child_index >= len(group.child_starts):
        return None
    return page_of(document.pages, group.child_starts[child_index])


def _image_keys_of_parent(
    document: Document, groups: Sequence[ChunkGroup], index: int
) -> tuple[str, ...]:
    """输入:文档 + 全部父块分组 + 当前父块序号。行为:取落在该父块区间内的图片 key。输出:去重后的 key 元组。

    区间是 ``[parent_start, 下一个 parent_start)``;最后一个父块到正文末尾。
    为什么结束位置取**下一个父块的起点**而不是 ``start + len(parent_text)``:
    切分器可能在父块之间留下 ``\\n\\n`` 或 overlap,图片的锚点落在缝隙里时
    也得有归属 —— 归给前一个父块,而不是静默丢掉。

    ``parent_start`` 为 None(旧切分器/验收夹具)时**不挂图**:没有可信位置就不猜,
    与页码那句「宁可不给,也不能给一个看着合法的错页号」是同一条口径。
    """
    group = groups[index]
    start = group.parent_start
    if start is None or not document.images:
        return ()

    if index + 1 < len(groups):
        next_start = groups[index + 1].parent_start
        end = (
            next_start
            if next_start is not None and next_start > start
            else start + len(group.parent_text)
        )
    else:
        # +1 让正好落在正文末尾的图也归最后一个父块
        end = len(document.text) + 1

    keys = [
        image.key
        for image in document.images
        if start <= image.offset < end
    ]
    # 同一张图在父块里出现两次(或两个 DocumentImage 指向同一 key)只留一份;
    # dict.fromkeys 保序去重,前端/模型看到的顺序与文档一致。
    return tuple(dict.fromkeys(keys))


def chunk_documents(documents: Sequence[Document], chunker: TextChunker) -> list[Chunk]:
    """输入:文档序列 + 切分器。行为:按父子分层切块并生成稳定 id。输出:子块列表。

    ``chunk_id`` 与 ``parent_id`` 都是 ``sha256(来源 / 文档 / 位置 / 内容)``,成分各有理由:

    - **带内容** → 同一位置换了内容就是新块,不会被旧块"覆盖成没更新";
    - **带位置** → 不同位置出现相同文本不会互相顶掉(只按**内容**哈希的话,两条相同的块会合并成一条,父块归属就乱了);
    - **带文档路径** → 两份文件里恰好有同样的句子也不会撞;
    - **不带时间戳** → 同一份输入跑两次得到同一批 id,这就是 upsert 幂等的来源。

    子块 id 里**含父块序号**:同一段文本落在两个父块下时它们是两个块,
    各自记着自己的父块 —— 否则父块归属会张冠李戴。

    **页码也在这一步定下来,但它不进任何 id**:身份是「内容 + 结构位置」,
    页码是源文件的排版属性 —— 混进 id 就意味着"PDF 重新导出一次,整库 id 全变、
    历史引用全部失效"。所以 ``Chunk.page`` 只当载荷,重灌即可修正。
    """
    chunks: list[Chunk] = []
    for document in documents:
        groups = list(chunker.split(document.text))
        for parent_index, group in enumerate(groups):
            starts = group.child_starts
            # 偏移与子块必须一一对应:少一个就会让后面所有子块的页码**整体错位**(静默),
            # 多一个说明切分器数错了块。这是端口契约(见 ports/chunker.py),当场驳回。
            if starts and len(starts) != len(group.children):
                raise ValueError(
                    f"{document.doc_id} 第 {parent_index} 个父块:切分器报了 {len(starts)} 个偏移,"
                    f"却有 {len(group.children)} 个子块 —— 页码会张冠李戴,先修切分器"
                )
            # 图片按父块在整篇正文里的区间归属。**不拿图注去正文 find**:重复句子
            # 会给出看着合法的错位置;位置只认切分器报的 parent_start。
            image_keys = _image_keys_of_parent(
                document, groups, parent_index
            )
            # 位置部分先在前面拼好,父块和子块共用同一份
            where = (document.source, document.doc_id, f"p{parent_index}")
            parent_id = _digest(*where, group.parent_text)
            for child_index, text in enumerate(group.children):
                chunk_id = _digest(*where, f"c{child_index}", text)
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        text=text,
                        source=document.source,
                        doc_id=document.doc_id,
                        parent_id=parent_id,
                        parent_content=group.parent_text,
                        page=_page_of_child(document, group, child_index),
                        image_keys=image_keys,
                    )
                )
    return chunks


def ingest_documents(
    documents: Sequence[Document],
    *,
    chunker: TextChunker,
    embedder: Embedder,
    store: VectorStore,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sparse_encoder: SparseEncoder | None = None,
    corpus_texts: Sequence[str] | None = None,
) -> IngestReport:
    """输入:文档 + 切分器 + 嵌入器 + 向量库 + 批大小 + 可选的稀疏编码器与语料口径。

    行为:切块 → **清掉这批文档的旧块** → 分批嵌入（有编码器时同时编码稀疏向量）
    → upsert（按 chunk_id 幂等）。那一步清理不是可有可无:chunk_id 里含内容哈希,
    改了内容就是新 id,upsert 覆盖不到旧块,不清就会新旧并存,而过期内容往往还排在前面。

    输出:统计报告。往库里写之前先校验「向量条数 == 本批块数」——
    顺序对不上就宁可报错,也不要让向量和文本错位落库(那种错查起来极贵)。

    ``sparse_encoder`` 给不给,取决于集合有没有稀疏列(``keyword.backend=milvus`` 那一档);
    给了就**先 fit 再编码** —— BM25 的 idf 是**全库**属性,而 ``corpus_texts`` 就是"全库"
    的口径:``None`` = 本次这批文档就是全库(CLI 整份重灌);上传单份文档时传已有语料的正文
    (见 ``application/documents.py``),否则新文档的权重会按"全世界只有这一篇"来算,
    在检索里被系统性压低(静默的排序错误,不报错)。
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size 必须为正,收到 {batch_size}")

    chunks = chunk_documents(documents, chunker)

    stats: SparseStats | None = None
    if sparse_encoder is not None:
        # **预检放在最贵的两步之前**(全库统计 + 嵌入):这个库接不了稀疏向量时,
        # 现在就说清楚 —— 否则要等第一批 upsert 才报错,而那时嵌入的钱已经花了。
        if not store.has_sparse_column():
            raise RuntimeError(
                "这个集合接不了稀疏向量(没有 sparse_vector 列)—— 现在写进去,稀疏权重会被"
                "静默丢掉。多半是**配置与集合不是同一档**:集合是 `keyword.backend=memory` 那档建的"
                "(schema 建集合那一刻定格,删了重建也一样)。三条路:"
                "① 想用稀疏向量 → 把 config 的 `keyword.backend` 改成 `milvus`,"
                "再换一个新集合名(或 drop 掉这个)重新 ingest;"
                "② 想保持 memory → 别带稀疏编码器(这一条正常由 cli/deps.py 保证);"
                "③ 先 `project-rag collections` 看一眼那个集合有没有稀疏列。"
            )
        fit_texts = [chunk.text for chunk in chunks] if corpus_texts is None else list(corpus_texts)
        stats = sparse_encoder.fit(fit_texts)

    # 先清这批文档的历史块。按 doc_id 而不是按 source:同一个 source 下
    # 通常有多份文档,按 source 清会把没在这次 ingest 名单里的文档一起误删。
    removed = sum(
        store.delete_by_doc_id(doc_id)
        for doc_id in sorted({document.doc_id for document in documents})
    )

    in_store = 0
    batches = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        texts = [chunk.text for chunk in batch]
        embeddings = embedder.embed(texts)
        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"嵌入器返回 {len(embeddings)} 条向量,但这一批有 {len(batch)} 块 —— "
                "顺序对不上就不能写库"
            )
        sparse = sparse_encoder.encode_documents(texts) if sparse_encoder is not None else None
        in_store = store.upsert(batch, embeddings, sparse=sparse)
        batches += 1

    report = IngestReport(
        documents=len(documents),
        chunks=len(chunks),
        removed=removed,
        batches=batches,
        in_store=in_store,
        sparse_vocabulary=0 if stats is None else stats.vocabulary,
        sparse_avg_len=0.0 if stats is None else stats.avg_len,
    )
    _logger.info(
        "ingest 完成:文档 %d 份 → %d 块(清旧 %d 块)→ %d 批 → 库内 %d 块%s",
        report.documents,
        report.chunks,
        report.removed,
        report.batches,
        report.in_store,
        (
            ""
            if stats is None
            else f";稀疏向量按全库 {stats.n_docs} 块统计(平均 {stats.avg_len:.1f} 词)"
        ),
    )
    return report
