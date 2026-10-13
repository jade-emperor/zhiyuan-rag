"""
sparse 子命令:按当前语料**重算全库的 BM25 稀疏向量**(关键字那一路)。

**什么时候需要它**(``keyword.backend=milvus`` 那一档):

1. **老集合补列**?不行 —— 稀疏列是建集合那一刻定格的,老集合只能重灌(见下);
2. **调过 BM25 常数**(``keyword.bm25_k1`` / ``bm25_b``)之后:已有文档的权重是按旧常数烘进去的;
3. **上传/增量入库之后想让权重回到"整库统一口径"**:上传那份只有一个文档能算的近似统计
   (见 ``application/ingest_documents.py`` 的 ``corpus_texts``),重算能把全库拉回同一口径;
4. **换了分词/哈希实现之后**(那种改动本来也该重灌,这里至少能把稀疏那半边修对)。

**它不重建稠密向量**:稠密向量是从文本重新嵌入算出来的,这里只读回已有的稠密向量、
重算稀疏列,再整行 upsert 回去 —— Milvus 的 upsert 是**整行替换**(2.4.4 没有部分更新),
所以读回稠密向量这一步不是优化,是正确性的前提(漏了它会把稠密列清空,而且不报错)。

**默认只说不做**(与 ``drop`` 同一个口径):打印会重算多少行、词表多大,``--yes`` 才真写。
"""

from __future__ import annotations

import argparse
import sys

NAME = "sparse"

# 批大小:一次 upsert 的行数。Milvus 对单次请求有大小限制,一批几百行足够稳。
_BATCH = 200


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 sparse(--yes / --batch-size)。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="按当前语料重算全库的 BM25 稀疏向量(keyword.backend=milvus 那一档)"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="真的重算并写回(不带它只打印会做什么,然后退出码 1)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH,
        help=f"每次 upsert 的行数(默认 {_BATCH})",
    )
    return parser


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:读全库 → 重算稀疏 → 整行写回。输出:退出码。"""
    deps = args.deps
    if deps.vector_store_factory is None or deps.sparse_encoder_factory is None:
        print(
            "sparse 需要向量库与稀疏编码器 —— 后者只在 `keyword.backend=milvus` 那一档装配"
            "(见 cli/deps.py);memory 那档的 BM25 索引是进程内自己建的,不需要这一步。",
            file=sys.stderr,
        )
        return 1

    store = deps.vector_store_factory()
    encoder = deps.sparse_encoder_factory()

    if not store.has_sparse_column():
        print(
            f"集合 {getattr(store, '_collection', '?')} 没有稀疏列 —— 稀疏列是**建集合那一刻**"
            "定格的,老集合补不上。两条路:① 换一个新集合名,按 `keyword.backend=milvus` 重灌;"
            "② 原地重建:`project-rag drop --collection <名字> --yes` 之后再 ingest。",
            file=sys.stderr,
        )
        return 1

    rows = store.load_with_vectors()
    if not rows:
        print("库里一块都没有 —— 先 ingest 语料")
        return 1

    texts = [chunk.text for chunk, _ in rows]
    stats = encoder.fit(texts)
    vectors = encoder.encode_documents(texts)
    print(
        f"将重算 {len(rows)} 块的稀疏向量:全库 {stats.n_docs} 块 / 平均 {stats.avg_len:.1f} 词 / "
        f"词表 {stats.vocabulary} 维\n"
        "（稠密向量从库里读回原样写回，不动嵌入结果；这一路 upsert 是整行替换）"
    )
    if not args.yes:
        print("\n确认无误后加 --yes 真写。")
        return 1

    batch_size = args.batch_size
    if batch_size <= 0:
        print(f"--batch-size 必须为正,收到 {batch_size}", file=sys.stderr)
        return 1

    written = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        chunks = [chunk for chunk, _ in batch]
        embeddings = [_embedding_of(vector) for _, vector in batch]
        store.upsert(
            chunks,
            embeddings,
            sparse=list(vectors[start : start + batch_size]),
        )
        written += len(batch)
        print(f"  已写回 {written}/{len(rows)} 块", flush=True)
    print(f"\n完成:重算并写回 {written} 块的稀疏向量")
    return 0


def _embedding_of(vector: tuple[float, ...]):
    """输入:稠密向量元组。行为:包成领域类型。输出:Embedding。

    延迟 import:``domain`` 是轻量的,但这条命令的顶层只该有 argparse 与 sys
    (与其它命令模块同一条规矩:``--help`` 不该拖任何东西)。
    """
    from project_rag.domain import Embedding

    return Embedding(dense=vector)


if __name__ == "__main__":  # pragma: no cover - 只为直接跑本文件时可用
    from project_rag.cli.main import cli

    raise SystemExit(cli([NAME]))
