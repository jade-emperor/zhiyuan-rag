"""chat 子命令:向模型发一句话,流式打印回答。"""

from __future__ import annotations

import argparse
import sys

from project_rag.domain import ChatMessage
from project_rag.observability import get_logger

_log = get_logger("cli.chat")

NAME = "chat"


# 这里写添加子命令的具体规则
def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    # 具体添加一个子命令解析器,参数1表示子命令的名字,参数2表示子命令的帮助信息
    parser = sub.add_parser(NAME, help="向模型发一句话,流式打印回答")
    # 为这个子命令创建一个参数
    parser.add_argument("--prompt", "-p", required=True, help="要发送给模型的内容")
    return parser


def run(args) -> int:
    # 延迟 import:--help 时不必把 langchain 拖进来

    deps = args.deps

    if deps.llm_factory is None:
        print(
            "缺少依赖 llm_factory；用 Deps(...) 注入,或让 build_real_deps() 装配",
            file=sys.stderr,
        )
        return 1

    try:
        client = deps.llm_factory()
        # 通过domain领域类创建一个领域值对象的列表
        messages = [ChatMessage(role="user", content=args.prompt)]
        # print(f"{messages[0].role},{messages[0].content}")
        for text in client.stream(messages):
            print(text, end="", flush=True)  # flush=True:否则攒在缓冲区,看着像卡死
        print()
    except Exception as exc:  # 这一层才翻译人话:堆栈进日志,短消息给用户
        _log.exception("调用模型失败")  # exception方法会记录错误的完整堆栈给日志
        print(f"调用 DeepSeek 失败：{exc}", file=sys.stderr)  # 短消息给用户
        return 1
    return 0
