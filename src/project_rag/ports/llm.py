"""端口:只声明「业务需要什么能力」。

只 import domain,不 import 任何第三方 —— 这是它能当"端口"的前提。
换 Ollama / 换厂商 = 新增一个 adapters/ 下的文件,interfaces 一行都不用改。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from project_rag.domain import ChatMessage


class LLMClient(Protocol):
    """我需要的能力:给一串消息,流式吐回文本片段。

    stream 只吐 str。如果它吐 langchain 的 chunk 对象,
    interfaces 就不得不认识 langchain —— 那端口就白设了。
    """

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]: ...
