#!/usr/bin/env python
"""契约（**services 档**）：索引类型与 BM25 稀疏向量在**真 Milvus** 上的往返。

用法：

    uv run python scripts/acceptance/check_m3_sparse_milvus.py     # 需要真中间件

`check_m3_sparse.py` 那 14 条用的是桩客户端（零容器可跑）；这一份补上"服务端真的接受吗"：

* 建出来的集合带 ``sparse_vector`` 列与 ``SPARSE_INVERTED_INDEX``；
* 稀疏向量写进去、检索能按 BM25 权重（内积）把它捞回来；
* ``milvus.index.type`` 换成 IVF_FLAT 之后**重建索引**真的换了类型（改配置本身不会自动生效）；
* 页哨兵、来源过滤这些老口径在带稀疏列的集合上照样成立。

Milvus 连不上就 SKIP（点名 owner）—— 这一档属于 ``--set services``，CI 里没有容器。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from _harness import SkipCheck, check, report

ROOT = Path(__file__).resolve().parents[2]

# 验收用的集合名。**带 accept 前缀**:一眼看得出是验收产物,万一没清掉也不会被当成真语料。
COLLECTION = "rag_accept_sparse_m3"


def _config(index_type: Literal["FLAT", "IVF_FLAT"], *, nlist: int = 4, nprobe: int = 2):
    """输入:索引类型与参数。行为:造指向验收集合的 Milvus 配置。输出:MilvusConfig。"""
    from project_rag.config import MilvusConfig, MilvusIndexConfig

    return MilvusConfig(
        collection=COLLECTION,
        index=MilvusIndexConfig(type=index_type, nlist=nlist, nprobe=nprobe),
    )


def _store(index_type: Literal["FLAT", "IVF_FLAT"], *, nlist: int = 4, nprobe: int = 2):
    """输入:索引类型。行为:造一个指着验收集合的 Milvus 适配器。输出:store。"""
    from project_rag.adapters.milvus_store import MilvusVectorStore

    return MilvusVectorStore(_config(index_type, nlist=nlist, nprobe=nprobe), sparse=True)


def _require_milvus() -> None:
    """输入:无。行为:Milvus 连不上就 SKIP(点名 owner)。输出:无。"""
    from project_rag.config import get_settings

    from pymilvus import MilvusClient

    config = get_settings().milvus
    try:
        client = MilvusClient(uri=f"http://{config.host}:{config.port}", db_name=config.database)
        client.list_collections()
    except Exception as exc:  # noqa: BLE001 - 环境问题按 SKIP 处理
        raise SkipCheck(
            f"Milvus({config.host}:{config.port}) 连不上:{type(exc).__name__} —— "
            "owner:想跑这一条就先 `docker compose up -d standalone mysql redis`"
        ) from exc


def _fixture():
    """输入:无。行为:造 3 个短块 + 对应的稀疏向量(用手算得清的语料)。输出:(chunks, embeddings, sparse)。"""
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder
    from project_rag.domain import Chunk, Embedding

    texts = [
        "装饰器 是 给 函数 加 行为 的 语法",
        "向量检索 用 稠密 向量 算 余弦",
        "BM25 是 词面 匹配 的 经典 算法",
    ]
    chunks = [
        Chunk(chunk_id=f"accept-{i}", text=text, source="accept", doc_id="accept", page=i + 1)
        for i, text in enumerate(texts)
    ]
    # 稠密向量用"一维独热"就够了:这里验的是稀疏那一路与索引类型,不是嵌入质量
    embeddings = [
        Embedding(dense=tuple(1.0 if j == i else 0.0 for j in range(3)))
        for i in range(len(texts))
    ]
    encoder = BM25SparseEncoder()
    encoder.fit(texts)
    return chunks, embeddings, encoder.encode_documents(texts)


@check("真 Milvus：集合带稀疏列，稀疏检索能按 BM25 权重把它捞回来")
def _() -> str:
    _require_milvus()
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, MilvusSparseKeywordIndex

    store = _store("FLAT")
    store.drop()
    try:
        chunks, embeddings, sparse = _fixture()
        store.upsert(chunks, embeddings, sparse=sparse)
        if not store.has_sparse_column():
            raise AssertionError(f"集合 {COLLECTION} 建出来没有稀疏列")

        encoder = BM25SparseEncoder()
        index = MilvusSparseKeywordIndex(store=store, encoder=encoder)
        hits = index.search("装饰器 语法", k=3)
        if not hits:
            raise AssertionError("稀疏检索一条都没回来 —— 要么没写进去,要么维度对不上")
        if hits[0].chunk.chunk_id != "accept-0":
            raise AssertionError(f"最该命中的是 accept-0,实际 {hits[0].chunk.chunk_id}")
        others = index.search("BM25 算法", k=3)
        if others[0].chunk.chunk_id != "accept-2":
            raise AssertionError(f"「BM25 算法」该命中 accept-2,实际 {others[0].chunk.chunk_id}")
        if index.search("？？？", k=3):
            raise AssertionError("纯标点查询该直接判没命中")
    finally:
        store.drop()
    return "写入 → 稀疏检索:两条查询命中各自的块,纯标点查询不命中(集合已清理)"


@check("真 Milvus：改配置后 rebuild_index 真的换了索引类型（含 nlist）")
def _() -> str:
    _require_milvus()
    from project_rag.adapters.milvus_store import list_collections

    flat = _store("FLAT")
    flat.drop()
    try:
        chunks, embeddings, sparse = _fixture()
        flat.upsert(chunks, embeddings, sparse=sparse)
        info = [i for i in list_collections(_config('FLAT')) if i.name == COLLECTION][0]
        if info.index_type != "FLAT":
            raise AssertionError(f"刚建出来该是 FLAT,实际 {info.index_type}")
        if not info.has_sparse:
            raise AssertionError("集合该带稀疏列(collections 打印的 has_sparse 是 False)")

        ivf = _store("IVF_FLAT", nlist=4, nprobe=2)
        steps = ivf.rebuild_index()
        after = [i for i in list_collections(_config('IVF_FLAT', nlist=4, nprobe=2)) if i.name == COLLECTION][0]
        if after.index_type != "IVF_FLAT":
            raise AssertionError(f"重建之后该是 IVF_FLAT,实际 {after.index_type}({steps})")

        # IVF_FLAT 下两条路都还能检索(重建索引把集合 release 过,load 要跟上)
        hits = ivf.search(_fixture()[1][0], k=3)
        if not hits:
            raise AssertionError("重建索引之后稠密检索空了 —— load_collection 没做?")
        keyword = ivf.search_sparse(_fixture()[2][0], k=3)
        if not keyword:
            raise AssertionError("重建索引之后稀疏检索空了")
    finally:
        flat.drop()
    return "FLAT → rebuild → IVF_FLAT(collections 读到新类型);两条检索路都还通"


@check("真 Milvus：带稀疏列的集合上，页码哨兵与来源过滤照旧")
def _() -> str:
    _require_milvus()
    from project_rag.domain import Chunk, Embedding

    store = _store("FLAT")
    store.drop()
    try:
        chunks, embeddings, sparse = _fixture()
        chunks = [
            *chunks,
            Chunk(chunk_id="accept-nopage", text="没有页码 的 块", source="other", doc_id="other"),
        ]
        embeddings = [*embeddings, Embedding(dense=(0.0, 0.0, 1.0))]
        sparse = [*sparse, {1: 1.0}]
        store.upsert(chunks, embeddings, sparse=sparse)

        loaded = {chunk.chunk_id: chunk for chunk in store.load_all()}
        if loaded["accept-1"].page != 2:
            raise AssertionError(f"页码没往返回来:{loaded['accept-1'].page}")
        if loaded["accept-nopage"].page is not None:
            raise AssertionError("没有页码的块该读回 None(哨兵 -1 只活在适配器里)")

        filtered = store.search_sparse({1: 1.0}, k=5, source="accept")
        if any(hit.chunk.source != "accept" for hit in filtered):
            raise AssertionError(f"来源过滤没生效:{[(h.chunk.chunk_id, h.chunk.source) for h in filtered]}")
    finally:
        store.drop()
    return "page 往返(2 / None)+ 稀疏检索的来源过滤,在带稀疏列的集合上都成立"


def main() -> int:
    return report(
        "索引类型 + BM25 稀疏向量（真 Milvus 往返）",
        hint="零容器的那 14 条在 check_m3_sparse.py;这一份属于 --set services",
    )


if __name__ == "__main__":
    sys.exit(main())
