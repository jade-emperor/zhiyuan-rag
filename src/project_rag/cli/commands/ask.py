"""ask 子命令:拿一个问题去 FAQ 库检索,命中直答,未命中交给 LLM 兜底。"""

from __future__ import annotations

import argparse
import sys

from project_rag.observability import get_logger
from project_rag.ports import Cache

_logger = get_logger("cli.ask")

# 用于主命令查找
NAME = "ask"

# 3个必须要的依赖工厂
_REQUIRED_FACTORIES = ("llm_factory", "faq_store_factory", "matcher_factory")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """添加 ask 子命令的解析器。

    输入:子命令注册器
    行为:注册 ask 命令,声明必填的 -q/--question
    输出:该子命令的解析器
    """
    parser = sub.add_parser("ask", help="问答子命令,输入问题,输出答案")
    parser.add_argument("--question", "-q", help="具体检索的问题", required=True)
    return parser


def _build_cache(deps) -> Cache | None:
    """输入:依赖集合。行为:调 cache_factory 建缓存,没给或建失败就降级为无缓存。输出:Cache 或 None。"""
    if deps.cache_factory is None:
        return None
    try:
        return deps.cache_factory()
    except Exception as e:
        _logger.warning("缓存初始化失败,降级为无缓存:%s", e)
        return None


def run(args) -> int:
    """输入:解析好的命令行参数(含 deps)。行为:查必需工厂 -> 调 answer_faq -> 打印答案。输出:退出码 0/1。"""
    from project_rag.application import answer_faq

    deps = args.deps

    # getattr的作用是判断一个对象是否包含某个属性
    missing = [name for name in _REQUIRED_FACTORIES if getattr(deps, name) is None]

    if missing:
        print(
            f"缺少依赖 {missing}请检查cli/deps文件中是否配置了依赖项",
            file=sys.stderr,  # 错误输出
        )
        return 1

    try:
        result = answer_faq(
            args.question,
            store=deps.faq_store_factory(),
            matcher=deps.matcher_factory(),
            llm=deps.llm_factory(),
            cache=_build_cache(deps),
            threshold=deps.faq_threshold,
        )
    except Exception as e:
        _logger.error("查询失败:%s", e)
        # exc_info=True表示打印错误详细堆栈,debug级别进文件,不打印到控制台
        _logger.debug("查询失败详情:", exc_info=True)
        print(f"查询失败:{e}", file=sys.stderr)
        return 1

    print(result.answer)
    return 0
