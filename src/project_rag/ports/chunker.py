"""
端口:文本切分策略(**父子分层**)。

只依赖 domain + 标准库 —— 换策略(中文递归 / Markdown 标题 / 语义切分)只换适配器。

**为什么返回的是「父块 + 它的子块」而不是一串文本**:父子分层是这个系统的检索基础 ——
子块小、匹配准;父块大、上下文全。命中子块之后要能还原出父块,所以「谁是谁的父块」
这层关系必须在切分阶段就定下来,不能靠事后按文本猜。

``parent_id`` 由用例层用「来源+文档+位置+内容」哈希生成,不用纯位置编号 ——
位置编号在文档一改就会整体错位,而哈希既稳定又能反映内容变化。

约定:``split`` 只负责「切」;空块由适配器丢掉 —— 用例层不该关心会不会切出空串。

**偏移**:``ChunkGroup.child_starts`` 是每个子块在传进来的那段正文里的起始偏移
(与 ``children`` 同序等长;空元组 = 这个切分器不报偏移)。要它是因为页码:
``page_of(document.pages, child_starts[i])``。**偏移只能由切分器给** —— 用例层拿子块文本
回正文里 ``find`` 会在重复句子上给出看着合法的错位置(见 ``domain/models.py`` 的 ChunkGroup)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import ChunkGroup


class TextChunker(Protocol):
    """把长文本切成「父块 + 子块」。"""

    def split(self, text: str) -> Sequence[ChunkGroup]:
        """输入:一段文本。行为:先切父块,再把每个父块切成子块。

        输出:父块分组序列(可为空);报偏移时 ``child_starts`` 必须与 ``children`` 同序等长。
        """
        ...
