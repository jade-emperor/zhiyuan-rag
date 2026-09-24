"""qa 子命令:合流问答 —— FAQ 通道优先,未命中转知识库通道(讲义 ⑥-6.1)。

跟 faq / rag 的关系(三条通道各司其职):

- ``faq`` = 只走 FAQ 通道:命中直返人工维护的标准答案,未命中让模型自由回答;
- ``rag`` = 只走知识库通道:检索 + 生成,答案带可核对的 [n] 引用;
- ``qa``  = **合流**:FAQ 命中就用标准答案,否则转知识库通道。生产入口是它
  (M5.4 的 FastAPI 调同一个用例),faq / rag 退化成两条通道各自的诊断入口。

``--stream`` 逐帧打印(答案边生成边出):走的是 ``stream_question`` 那条流;
不加就是收拢后的 ``answer_question``。**两条路打印出来的答案逐字相同** ——
非流式本来就是流式的收拢器(见 ``application/streaming.py``)。

一条规矩:命令层不做业务判断 —— 路由在 ``application/answer_question.py`` 里,
这里只做四件事:解析参数 | 调用例 | 打印结果 | 处理错误。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.cli.render import print_citations
from project_rag.domain.models import RetrievalOptions
from project_rag.observability import get_logger

_logger = get_logger("cli.qa")

NAME = "qa"

# 合流要两条通道的端口:FAQ 侧三个(库/匹配/模型) + 知识库侧三个(嵌入/向量库/关键字)
_BASE_FACTORIES = (
    "llm_factory",
    "faq_store_factory",
    "matcher_factory",
    "embedder_factory",
    "vector_store_factory",
    "keyword_index_factory",
)


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 qa,声明 -q / --k / --source(与 rag 同款)。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="合流问答:FAQ 命中直答,否则走知识库(检索 + 生成 + [n] 引用)"
    )
    parser.add_argument("--question", "-q", required=True, help="要问的问题")
    parser.add_argument(
        "--k", type=int, default=None, help="检索条数;默认取配置的 retrieval_k"
    )
    parser.add_argument("--source", default=None, help="按来源过滤(如 bagu_ai / bagu_java)")
    parser.add_argument(
        "--session",
        default=None,
        help="会话 ID;给了就读最近 N 轮历史进提示词,并把这一轮记下来",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="逐字流式打印(答案边生成边出);不加就是等答案完整再打",
    )
    return parser


def _print_origin(*, citations, faq_match, refused, truncated) -> None:
    """输入:答案的来历。行为:打通道/命中分数、引用清单与截断提示。输出:无。

    stdout 只放答案与引用(便于管道消费),**提示走 stderr** —— 两条路(流式/非流式)共用这一处。
    """
    if faq_match is not None:
        # 让人一眼看出这是人工维护的标准答案、以及像到什么程度 ——
        # 否则用户分不清「标准答案」和「模型生成的答案」
        print(f"\nFAQ 直答(标准答案):{faq_match.score:.4f}  「{faq_match.entry.question}」")
    print_citations(citations)
    if not citations and refused:
        print("\n(没有检索到相关资料,未调用大模型)")
    if truncated:
        print("[注意] 回答被截断（模型没说完），不要当成完整答案", file=sys.stderr)


def _stream(query: str, deps, knowledge, session) -> int:
    """输入:问题 + 依赖 + 知识库通道闭包 + 会话。行为:逐帧打印,收尾帧打来历。输出:退出码 0/1。"""
    from project_rag.application import stream_question

    done = None
    try:
        for event in stream_question(
            query,
            faq_store=deps.faq_store_factory(),
            matcher=deps.matcher_factory(),
            knowledge=knowledge,
            faq_threshold=deps.faq_threshold,
            session=session,
        ):
            if event.kind == "token":
                # flush=True:否则攒在缓冲区,看着像卡死(而且用户要的就是「边生成边出」)
                print(event.text, end="", flush=True)
            elif event.kind == "done":
                done = event
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("合流问答失败:%s", exc)
        _logger.debug("合流问答失败详情:", exc_info=True)
        print(f"\n合流问答失败:{exc}", file=sys.stderr)
        return 1

    print()
    if done is None:
        # 帧协议要求恰好一帧 done;走到这里说明生产方违约,别假装答完了
        print("合流问答失败:流里没有收尾帧", file=sys.stderr)
        return 1
    _print_origin(
        citations=done.citations,
        faq_match=done.faq_match,
        refused=done.refused,
        truncated=done.truncated,
    )
    return 0


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:查必需工厂 -> 调 answer_question(或流式版) -> 打印答案与来历。输出:退出码 0/1。"""
    from project_rag.application import Session, answer_question
    from project_rag.cli.deps import knowledge_callable
    from project_rag.config import get_settings

    deps = args.deps
    settings = get_settings()
    # 与 rag 同一条规矩:配置说要重排、组合根却没装重排器,那不是「降级」而是
    # 「静默换了个系统在答」—— 当场报错,别让人以为重排生效了
    required: list[str] = list(_BASE_FACTORIES)
    if settings.rag.rerank:
        required.append("reranker_factory")
    if args.session:
        required.append("conversation_store_factory")
    missing = [name for name in required if getattr(deps, name) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:合流问答要 FAQ 与知识库两条通道的端口"
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
    # 带了 --session 才有会话:没给就完全不碰历史存储(不读也不写)
    session = (
        Session(
            store=deps.conversation_store_factory(),
            session_id=args.session,
            rounds=settings.conversation.history_rounds,
        )
        if args.session
        else None
    )
    knowledge = knowledge_callable(
        deps, args.question, options, rerank=settings.rag.rerank
    )
    if args.stream:
        return _stream(args.question, deps, knowledge, session)

    try:
        result = answer_question(
            args.question,
            faq_store=deps.faq_store_factory(),
            matcher=deps.matcher_factory(),
            # 知识库那半边包成一次调用:路由在用例里,构造推迟到真正要走这条路时。
            # 这样 FAQ 命中的问题不会去加载嵌入/重排模型(秒级 + 上 G 内存,
            # 没 GPU 的机器上还会直接抛错)。
            knowledge=knowledge,
            faq_threshold=deps.faq_threshold,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("合流问答失败:%s", exc)
        _logger.debug("合流问答失败详情:", exc_info=True)
        print(f"合流问答失败:{exc}", file=sys.stderr)
        return 1

    # 首行永远是答案:便于管道消费,「这个答案的来历」一律排在后面
    print(result.answer)
    _print_origin(
        citations=result.citations,
        faq_match=result.faq_match,
        refused=result.refused,
        truncated=result.truncated,
    )
    return 0
