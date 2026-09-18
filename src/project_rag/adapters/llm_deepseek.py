"""DeepSeek 适配器:全项目唯一 import langchain 的地方。

两件事都只在这里做:领域类型 <-> SDK 类型的转换、发请求。
异常不在这里吞 —— 翻译成人话是 cli 层的事。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek

from project_rag.config import LLMConfig
from project_rag.domain import ChatMessage
from project_rag.ports import LLMClient

# 角色 -> langchain 消息类,字典分派省掉 if/elif 链
_LANGCHAIN_MESSAGE = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}


def to_sdk_messages(messages: Sequence[ChatMessage]) -> list:
    """
    将领域消息 -> langchain 需要的消息。

    转换只属于适配器:domain 里绝不写 to_langchain(),否则依赖方向就反了。

    输入:领域消息序列[领域值对象中的role,content]
    行为:按 role 分派,转成对应的 langchain 消息类。
    输出:langchain 消息列表[SystemMessage/HumanMessage/AIMessage(content)]
    """
    # 举个直观的例子,这里将[领域值对象中的role,content]->[role,content转为SystemMessage\HumanMessage\AIMessage(content)]
    return [_LANGCHAIN_MESSAGE[m.role](content=m.content) for m in messages]


class DeepSeekLLM(LLMClient):
    """满足 LLMClient 端口(结构化子类型,不需要显式继承)。"""

    def __init__(self, config: LLMConfig) -> None:
        """输入:LLM 配置。行为:建 ChatDeepSeek 客户端,开启流式。输出:无。"""
        self._client = ChatDeepSeek(
            model=config.model,
            # 密钥只从 settings 来并显式传入,不让 SDK 自己去读环境变量,否则又多一个配置源
            api_key=config.deepseek_api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout_s,  # timeout 是 request_timeout 的 alias
            streaming=True,
        )  # 这里获得deepseek的策略类

    # 调用大模型,输入
    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        """输入:领域消息序列。行为:转换格式后调模型,逐块取 content。输出:文本片段迭代器。"""
        for chunk in self._client.stream(to_sdk_messages(messages)):
            yield str(chunk.content)
