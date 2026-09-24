"""
chat 子命令:向模型发一句话,流式打印回答。
子命令不应该写具体逻辑,只做如下操作:
解析参数 | 调用例 | 打印结果 | 处理错误。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.domain import ChatMessage
from project_rag.observability import get_logger

_log = get_logger("cli.chat")

NAME = "chat"


# 这里写添加子命令的具体规则
def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 chat 命令,声明必填的 -p/--prompt。输出:该子命令的解析器。"""
    # 具体添加一个子命令解析器,参数1表示子命令的名字,参数2表示子命令的帮助信息
    parser = sub.add_parser(NAME, help="向模型发一句话,流式打印回答")
    # 为这个子命令创建一个参数
    parser.add_argument("--prompt", "-p", required=True, help="要发送给模型的内容")
    return parser


def run(args) -> int:
    """输入:解析好的命令行参数(含 deps)。行为:把 prompt 包成领域消息,流式打印模型回答;错误只给短消息。输出:退出码 0/1。"""
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
        truncated = False
        for delta in client.stream(messages):
            if delta.last:
                truncated = delta.truncated
            print(delta.text, end="", flush=True)  # flush=True:否则攒在缓冲区,看着像卡死
        print()
        if truncated:
            # 半截答案比「答不出来」更容易误导 —— 必须让人看见
            print("[注意] 回答被截断（模型没说完），不要当成完整答案", file=sys.stderr)
    except Exception as exc:  # 这一层才翻译人话:堆栈进日志,短消息给用户
        _log.error("调用模型失败：%s", exc)  # 控制台/文件都收,不带堆栈
        _log.debug("调用模型失败（堆栈）", exc_info=True)  # 堆栈只进文件
        print(f"调用 DeepSeek 失败：{exc}", file=sys.stderr)  # 短消息给用户
        return 1
    return 0
