"""search 子命令:只做检索,把每一路捞回了什么、以及融合后长什么样摊开给你看。

**刻意不调用大模型**:这一层的价值就是让你看见"检索到底捞回了什么"。
带了生成反而看不清 —— 答案不对时,你分不清是检索没捞到还是模型没答好。
排障顺序永远是:先 search,再 rag。

**为什么要能分路看**(``--route``):两路的分数不是一个量纲(BM25 原始分无上界、
余弦在 [0,1]),混在一起打印会让人误以为能比大小。分开看还有一个用处:
调融合权重之前,先确认每一路**单独**是能用的 —— 一路本身就是烂的,融合救不回来。

``--route both`` 会打三段:向量 / BM25 / 融合。**融合那段才是线上真正用的排序**,
前两段是给你对照用的 —— 融合把某条从第 5 拉到了第 1,你要能一眼看出来。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.observability import get_logger

_logger = get_logger("cli.search")

NAME = "search"

_ROUTES = ("both", "vector", "bm25")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 search,声明 -q / --k / --source / --route。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="只做检索:打印每一路与融合后的命中(不调用大模型)"
    )
    parser.add_argument("--question", "-q", required=True, help="要检索的问题")
    parser.add_argument(
        "--k", type=int, default=None, help="每路返回条数;默认取配置的 retrieval_k"
    )
    parser.add_argument("--source", default=None, help="按来源过滤(如 ai / java)")
    parser.add_argument(
        "--route",
        choices=_ROUTES,
        default="both",
        help="看哪一路:vector=向量 / bm25=关键字 / both=两路 + 融合(默认)",
    )
    return parser


def _print_hits(title: str, hits) -> None:
    """输入:段落标题与命中列表。行为:打印这一路的命中。输出:无。"""
    print(f"\n[{title}] 命中 {len(hits)} 条")
    for index, hit in enumerate(hits, start=1):
        chunk = hit.chunk
        preview = chunk.text[:50].replace("\n", "⏎")
        # 标出父块:命中的是子块,但喂给模型的是父块 —— 两者不一样时人要能一眼看出来
        parent = (
            f"  父块 {len(chunk.parent_content)} 字"
            if chunk.parent_content
            else "  无父块"
        )
        print(
            f"  [{index}] {hit.score:.4f}  {chunk.source}  {chunk.doc_id}  "
            f"{chunk.chunk_id[:12]}{parent}"
        )
        print(f"      {preview}…")
    if not hits:
        # 空结果不是错误(库里可能真没有),但必须说出来 —— 静默的空输出最容易被当成 bug
        print("  (没有命中。先确认 ingest 跑过,或换个问法)")


def _print_fused(options, fused) -> None:
    """输入:检索旋钮与融合结果。行为:打印融合后的排序以及每条来自哪几路。输出:无。"""
    print(
        f"\n[融合 {options.fusion} rrf_k={options.rrf_k} "
        f"权重 向量{options.vector_weight}/BM25{options.bm25_weight}] "
        f"命中 {len(fused)} 条"
    )
    for index, item in enumerate(fused, start=1):
        chunk = item.chunk
        preview = chunk.text[:50].replace("\n", "⏎")
        routes = "+".join(item.routes) if item.routes else "不分路"
        print(
            f"  [{index}] {item.score:.4f}  ({routes})  {chunk.source}  "
            f"{chunk.doc_id}  {chunk.chunk_id[:12]}"
        )
        print(f"      {preview}…")


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:按 --route 跑对应的一路或多路,并在两路时打融合结果。输出:退出码 0/1。"""
    from project_rag.config import get_settings
    from project_rag.domain import RetrievalOptions

    deps = args.deps
    # 按路径要依赖:只跑 BM25 就不该强制要求嵌入模型 —— 那条路上模型纯属白加载
    required = []
    if args.route in ("both", "vector"):
        required += ["embedder_factory", "vector_store_factory"]
    if args.route in ("both", "bm25"):
        required += ["keyword_index_factory"]
    missing = [name for name in required if getattr(deps, name, None) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:检查 build_real_deps 有没有装配这条路径",
            file=sys.stderr,
        )
        return 1

    settings = get_settings()
    options = RetrievalOptions(
        k=args.k if args.k is not None else settings.rag.retrieval_k,
        source=args.source,
        fusion=settings.rag.fusion,
        rrf_k=settings.rag.rrf_k,
        bm25_weight=settings.rag.bm25_weight,
        vector_weight=settings.rag.vector_weight,
    )
    where = f"source={args.source}" if args.source else "全库"
    print(f"检索「{args.question}」  路径={args.route}  k={options.k}  {where}")

    try:
        # 三个依赖各造一次:embedder_factory() 每次调用都会重新加载 2G 模型,
        # 在下面反复调它等于把一次查询变成三次模型加载
        embedder = deps.embedder_factory() if args.route in ("both", "vector") else None
        store = (
            deps.vector_store_factory() if args.route in ("both", "vector") else None
        )
        index = deps.keyword_index_factory() if args.route in ("both", "bm25") else None

        if (
            args.route in ("both", "vector")
            and embedder is not None
            and store is not None
        ):
            query_vector = embedder.embed([args.question])[0]
            _print_hits(
                "向量", store.search(query_vector, k=options.k, source=args.source)
            )
        if args.route in ("both", "bm25") and index is not None:
            _print_hits(
                "BM25", index.search(args.question, k=options.k, source=args.source)
            )

        # 显式收窄而不是 assert:pyright 需要看到 None 被排除,人也能一眼看出前提
        if (
            args.route == "both"
            and embedder is not None
            and store is not None
            and index is not None
        ):
            from project_rag.application import retrieve

            # 融合那段走与 rag 完全相同的入口(retrieve):「看到什么」就是「模型拿到什么」。
            # 检索会因此重跑一遍 —— 融合前的两次检索是给人对照用的,可接受
            _print_fused(
                options,
                retrieve(
                    args.question,
                    embedder=embedder,
                    store=store,
                    keyword_index=index,
                    options=options,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("检索失败:%s", exc)
        _logger.debug("检索失败详情:", exc_info=True)
        print(f"检索失败:{exc}", file=sys.stderr)
        return 1
    return 0
