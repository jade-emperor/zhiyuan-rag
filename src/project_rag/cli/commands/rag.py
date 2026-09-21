"""rag 子命令:知识库问答 —— 检索 + 生成,答案带 [1][2] 引用。

跟 ask(FAQ 通道)的区别:ask 只答"近乎一字不差"的常见问题、直返人写的标准答案;
rag 面向语料里的长尾问题,靠检索 + 生成回答,并给出可核对的引用。
两条通道怎么合流是 M5 的事,现在各走各的。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.domain.models import RetrievalOptions
from project_rag.observability import get_logger

_logger = get_logger("cli.rag")

NAME = "rag"

_REQUIRED_FACTORIES = ("embedder_factory", "vector_store_factory", "llm_factory")


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
    missing = [name for name in _REQUIRED_FACTORIES if getattr(deps, name) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:知识库问答需要嵌入模型、向量库与大模型,检查 build_real_deps",
            file=sys.stderr,
        )
        return 1

    settings = get_settings()
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
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("知识库问答失败:%s", exc)
        _logger.debug("知识库问答失败详情:", exc_info=True)
        print(f"知识库问答失败:{exc}", file=sys.stderr)
        return 1

    print(result.answer)
    if result.citations:
        # 引用清单是给人核对的:编号要和答案里的 [n] 对得上
        print("\n引用:")
        for citation in result.citations:
            chunk = citation.chunk
            # 预览用 context_text:那才是模型真正读到的正文(有父块就是父块)。
            # 用子块预览会让人对着一段模型没看过的文字核对答案。
            preview = chunk.context_text[:40].replace("\n", "⏎")
            which = (
                f"父块 {len(chunk.parent_content)} 字"
                if chunk.parent_content
                else "无父块"
            )
            print(
                f"  [{citation.index}] {citation.score:.4f}  {chunk.source}  "
                f"{chunk.doc_id}  {which}"
            )
            print(f"      {preview}…")
    elif result.refused:
        print("\n(没有检索到相关资料,未调用大模型)")
    return 0
