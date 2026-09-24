"""
端口:只声明业务需要什么能力。
只 import domain,不 import 任何第三方 —— 这是它能当"端口"的前提。
换 Ollama / 换厂商 = 新增一个 adapters/ 下的文件,cli 一行都不用改。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from project_rag.domain import ChatMessage, LlmDelta


class LLMClient(Protocol):
    """我需要的能力:给一串消息,流式吐回帧。

    ``stream`` 吐的是**领域类型** ``LlmDelta``,不是 SDK 的 chunk 对象 ——
    否则 cli 就得认识 langchain,那端口就白设了。每一帧带增量文本与(收尾帧才有的)
    结束原因:半截答案必须能被人看见,所以 ``finish_reason`` 是这个端口的一部分,
    不是适配器的内部细节。
    """

    # sequence的子类型包括list,tuple等
    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[LlmDelta]:
        """输入:领域消息列表。行为:调模型流式生成。输出:帧的迭代器;

        约定:**恰好一帧** ``last=True`` 且在最后,它带着 provider 给的结束原因
        (provider 没说就是 None)。
        """
        ...
