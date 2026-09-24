"""
HTTP / WebSocket 的请求与响应模型(pydantic)。

**为什么模型放 api 而不是 domain**:domain 是纯 dataclass、不依赖校验框架(它的 docstring
就是这么写的);而这一层的职责恰好是「把外面的 JSON 变成合法的调用参数」—— 那是 pydantic 的活。
转换只发生在这一层,和 cli 只做 domain -> 文本是同一个分工。

``answer`` 不给默认值、也不允许 None:讲义 6.3 的 ``POST /api/query`` 在答不出来时返回
``null``,前端要为此写一支分支;这里拒答也是**一句人话**(拒答话术),前端永远拿到字符串。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """问答请求(HTTP body 与 WebSocket 的一条消息共用)。"""

    query: str = Field(min_length=1, description="用户问题")
    session_id: str | None = Field(default=None, description="会话 ID;给了就带对话历史")
    source_filter: str | None = Field(
        default=None, description="按来源过滤(如 bagu_ai / bagu_java)"
    )
    k: int | None = Field(default=None, ge=1, description="检索条数;默认取配置")


class CitationOut(BaseModel):
    """引用清单里的一条(给人核对答案出处)。"""

    index: int
    source: str
    doc_id: str
    preview: str
    score: float
    rerank_score: float | None = None


class QueryResponse(BaseModel):
    """非流式回答。

    ``channel`` 比 ``domain.Channel`` 多一个 ``"greeting"``:寒暄是这一层的前置拦截,
    既不是 FAQ 通道也不是知识库通道 —— 混进那两种会让前端没法区分。
    """

    answer: str
    channel: Literal["faq", "rag", "greeting"]
    refused: bool = False
    truncated: bool = False
    citations: list[CitationOut] = []
    faq_score: float | None = None
    session_id: str | None = None


class SessionOut(BaseModel):
    session_id: str


class HistoryTurnOut(BaseModel):
    question: str
    answer: str


class HistoryOut(BaseModel):
    session_id: str
    rounds: list[HistoryTurnOut]


class DeletedOut(BaseModel):
    session_id: str
    deleted: int


class HealthOut(BaseModel):
    status: str


class SourcesOut(BaseModel):
    sources: list[str]
