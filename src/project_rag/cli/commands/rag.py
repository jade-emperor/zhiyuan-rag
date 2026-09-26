"""rag 子命令:知识库问答 —— 检索 + 生成,答案带 [1][2] 引用。

跟 faq(FAQ 通道)的区别:faq 只答"近乎一字不差"的常见问题、直返人写的标准答案;
rag 面向语料里的长尾问题,靠检索 + 生成回答,并给出可核对的引用。
两条通道怎么合流是 qa 的事(FAQ 优先、未命中转到这里),这个命令只走知识库那一条。

**引用清单里两个分都打**:重排分是排序依据,融合分是「它原来排第几」。
只打一个的话,「重排把它从第 3 提到了第 1」这件事就看不见了 ——
而那正是判断重排有没有帮忙的唯一线索。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.cli.render import print_citations
from project_rag.domain.models import RetrievalOptions
from project_rag.observability import get_logger

_logger = get_logger("cli.rag")

NAME = "rag"

_BASE_FACTORIES = ("embedder_factory", "vector_store_factory", "llm_factory")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 rag,声明 -q / --k / --source。输出:该子命令的解析器。"""
    parser = sub.add_parser(NAME, help="知识库问答:检索 + 生成,答案带 [1][2] 引用")
    parser.add_argument("--question", "-q", required=True, help="要问的问题")
    parser.add_argument(
        "--k", type=int, default=None, help="检索条数;默认取配置的 retrieval_k"
    )
    parser.add_argument("--source", default=None, help="按来源过滤(如 ai / java)")
    return parser


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:检索 → 生成 → 打印答案与引用清单。输出:退出码 0/1。"""
    from project_rag.application import answer_rag
    from project_rag.config import get_settings

    deps = args.deps
    settings = get_settings()
    # 重排开着就必须有重排器:配置说要精排、组合根却没装,那不是「降级」而是
    # 「静默换了个系统在答」—— 当场报错,别让人以为重排生效了
    required: list[str] = list(_BASE_FACTORIES)
    if settings.rag.rerank:
        required.append("reranker_factory")
    missing = [name for name in required if getattr(deps, name) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:知识库问答需要嵌入模型、向量库与大模型"
            "(重排开着时还要重排模型),检查 build_real_deps",
            file=sys.stderr,
        )
        return 1

    # 旋钮全部来自配置:融合方式、RRF 常数、两路权重都不在命令行里 ——
    # 它们是要用评估集调的参数,不该每次手输,否则结果没法复现
    options = RetrievalOptions(
        k=args.k if args.k is not None else settings.rag.retrieval_k,
        max_context=settings.rag.candidate_m,
        source=args.source,
        fusion=settings.rag.fusion,
        rrf_k=settings.rag.rrf_k,
        bm25_weight=settings.rag.bm25_weight,
        vector_weight=settings.rag.vector_weight,
    )
    try:
        result = answer_rag(
            args.question,
            embedder=deps.embedder_factory(),
            store=deps.vector_store_factory(),
            llm=deps.llm_factory(),
            keyword_index=deps.keyword_index_factory(),
            reranker=deps.reranker_factory() if settings.rag.rerank else None,
            options=options,
            thinking=deps.thinking,  # 只进延迟账(M5.8),不影响答案
        )
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("知识库问答失败:%s", exc)
        _logger.debug("知识库问答失败详情:", exc_info=True)
        print(f"知识库问答失败:{exc}", file=sys.stderr)
        return 1

    print(result.answer)
    # 渲染只有一处实现(cli/render.py):qa 命令打印的是同一份清单
    print_citations(result.citations)
    if not result.citations and result.refused:
        print("\n(没有检索到相关资料,未调用大模型)")
    if result.truncated:
        # stdout 只放答案(便于管道消费),提示走 stderr
        print("[注意] 回答被截断（模型没说完），不要当成完整答案", file=sys.stderr)
    return 0
