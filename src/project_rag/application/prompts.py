"""
知识库通道的提示词。

**为什么放在 application/ 而不是 adapters/**:提示词是业务规则 —— 该怎么答、
答不出来怎么办、引用怎么标 —— 换一个 LLM 供应商不该动它。
适配器只管「怎么把消息发出去」,不管「发什么」。
"""

from __future__ import annotations

from collections.abc import Sequence

from project_rag.domain import Citation

# 每条上下文长这样:编号 + 来源 + 正文。编号必须和 citations 的 index 对齐,
# 否则答案里的 [1] 指不到东西。
_CONTEXT_BLOCK = "[{index}] 来源:{source}\n{text}"

RAG_ANSWER_PROMPT = """你是一个知识库问答助手。请**只依据下面的资料**回答用户问题。

要求:
1. 用到资料的地方,在句末标出来源编号,例如 [1] 或 [1][2];
2. 资料里找不到答案时,直接回答「信息不足,无法回答,请联系人工客服」,不要用自己的知识补充;
3. 只输出回答本身,不要复述这些要求,也不要罗列资料原文。

资料:
{context}

问题:{question}

回答:"""


def render_context(citations: Sequence[Citation]) -> str:
    """输入:引用列表。行为:编号拼成上下文块。输出:上下文字符串(空列表给空串)。"""
    return "\n\n".join(
        _CONTEXT_BLOCK.format(
            index=citation.index,
            source=citation.chunk.source,
            # 用 context_text 而不是 text:有父块就给父块(上下文中更全),
            # 没有父块的旧数据自动退回子块本身
            text=citation.chunk.context_text,
        )
        for citation in citations
    )


def rag_answer_prompt(citations: Sequence[Citation], question: str) -> str:
    """输入:引用列表 + 用户问题。行为:渲染上下文与提问。输出:完整提示词。"""
    return RAG_ANSWER_PROMPT.format(
        context=render_context(citations), question=question
    )
