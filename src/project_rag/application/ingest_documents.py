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

from project_rag.domain import Chunk, Document
from project_rag.observability import get_logger
from project_rag.ports import Embedder, TextChunker, VectorStore

_logger = get_logger("application.ingest_documents")

# BGE-M3 在 CPU 上实测:8 块 1.5 块/秒、40 块 2.2 块/秒 —— 批量越大越划算
# (padding 摊销 + 矩阵乘吃满)。所以默认按批走,而不是逐块调用。
DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True, slots=True)
class IngestReport:
    """一次 ingest 的统计。"""

    documents: int  # 读进来几份文档
    chunks: int  # 切出几个子块
    removed: int  # 写之前清掉的旧块数(重新 ingest 时不为 0)
    batches: int  # 发了几次嵌入调用
    in_store: int  # 最后一次 upsert 后集合里的子块总数(0 = 什么都没写)


def chunk_documents(documents: Sequence[Document], chunker: TextChunker) -> list[Chunk]:
    """输入:文档序列 + 切分器。行为:按父子分层切块并生成稳定 id。输出:子块列表。

    ``chunk_id`` 与 ``parent_id`` 都是 ``md5(来源|文档|位置|内容)``,成分各有理由:

    - **带内容** → 同一位置换了内容就是新块,不会被旧块"覆盖成没更新";
    - **带位置** → 不同位置出现相同文本不会互相顶掉(讲义用纯内容 md5,
      两条相同的块会合并成一条,父块归属就乱了);
    - **带文档路径** → 两份文件里恰好有同样的句子也不会撞;
    - **不带时间戳** → 同一份输入跑两次得到同一批 id,这就是 upsert 幂等的来源。

    子块 id 里**含父块序号**:同一段文本落在两个父块下时它们是两个块,
    各自记着自己的父块 —— 否则父块归属会张冠李戴。
    """
    chunks: list[Chunk] = []
    for document in documents:
        for parent_index, group in enumerate(chunker.split(document.text)):
            prefix = f"{document.source}|{document.doc_id}|p{parent_index}"
            parent_id = hashlib.md5(
                f"{prefix}|{group.parent_text}".encode("utf-8")
            ).hexdigest()
            for child_index, text in enumerate(group.children):
                chunk_id = hashlib.md5(
                    f"{prefix}|c{child_index}|{text}".encode("utf-8")
                ).hexdigest()
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        text=text,
                        source=document.source,
                        doc_id=document.doc_id,
                        parent_id=parent_id,
                        parent_content=group.parent_text,
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
) -> IngestReport:
    """输入:文档 + 切分器 + 嵌入器 + 向量库 + 批大小。

    行为:切块 → **清掉这批文档的旧块** → 分批嵌入 → upsert(按 chunk_id 幂等)。
    那一步清理不是可有可无:chunk_id 里含内容哈希,改了内容就是新 id,
    upsert 覆盖不到旧块,不清就会新旧并存,而过期内容往往还排在前面。
    输出:统计报告。往库里写之前先校验「向量条数 == 本批块数」——
    顺序对不上就宁可报错,也不要让向量和文本错位落库(那种错查起来极贵)。
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size 必须为正,收到 {batch_size}")

    chunks = chunk_documents(documents, chunker)

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
        embeddings = embedder.embed([chunk.text for chunk in batch])
        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"嵌入器返回 {len(embeddings)} 条向量,但这一批有 {len(batch)} 块 —— "
                "顺序对不上就不能写库"
            )
        in_store = store.upsert(batch, embeddings)
        batches += 1

    report = IngestReport(
        documents=len(documents),
        chunks=len(chunks),
        removed=removed,
        batches=batches,
        in_store=in_store,
    )
    _logger.info(
        "ingest 完成:文档 %d 份 → %d 块(清旧 %d 块)→ %d 批 → 库内 %d 块",
        report.documents,
        report.chunks,
        report.removed,
        report.batches,
        report.in_store,
    )
    return report
