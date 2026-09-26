"""
中文递归切分(**两级:先父块,再子块**)。

每一级都按「段落 → 换行 → 句末标点 → 分号 → 逗号 → 顿号 → 空格 → 逐字」
的语义强度从强到弱递进,尽量在自然边界断开。

**为什么用 langchain 的 RecursiveCharacterTextSplitter 而不是自己写**:
它已经把「小片合并到 chunk_size」「overlap 从上一块尾部取多少」「超长且无分隔符时逐字降级」
这些边界处理干净了,自己写十有八九错在 overlap 上。
但**分隔符顺序是中文专属的**,英文默认那套(``\\n\\n`` 之后就是空格)对中文等于没有句级边界 ——
这就是本适配器存在的理由。

**为什么要两级**:子块小(300 字)才匹配得准,但只给模型 300 字上下文太窄;
所以检索用子块、喂模型用父块(1200 字)。两级的重叠都取 ``chunk_overlap``。

实测(``data/corpus/ai_data/course-outline.md``,13601 字,
父 1200 / 子 300 / 重叠 50):**15 个父块、70 个子块**,子块 41~299 字,无空块。
(不分层时是 68 块 —— 先切父块会改变子块的边界,所以数量不完全等于单级切分。)
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_text_splitters import RecursiveCharacterTextSplitter

from project_rag.domain import ChunkGroup
from project_rag.ports import TextChunker

# 语义强度从强到弱。最后那个空串是兜底:实在断不开就逐字切,保证块长有上界。
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]


def _build(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    """输入:块大小与重叠字数。行为:按中文分隔符优先级建一个切分器。输出:切分器。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        # 句末标点留在块尾:否则读起来像断头句,人也难核对切分质量
        keep_separator=True,
        # 中文按「字数」而不是 token 算长度 —— 300 字对 BGE-M3 是合适的输入规模
        length_function=len,
    )


class ChineseRecursiveChunker(TextChunker):
    """中文递归切分器(父块 + 子块)。"""

    def __init__(
        self, parent_chunk_size: int, child_chunk_size: int, chunk_overlap: int
    ) -> None:
        """输入:父块大小、子块大小、重叠字数。行为:建两级切分器。输出:无。

        尺寸自洽(child ≤ parent、overlap < child)由 config.RagConfig 在配置层保证,
        这里不重复校验 —— 同一个约束只在一处拦。
        """
        self._parent_splitter = _build(parent_chunk_size, chunk_overlap)
        self._child_splitter = _build(child_chunk_size, chunk_overlap)

    def split(self, text: str) -> Sequence[ChunkGroup]:
        """输入:一段文本。行为:先切父块,再把每个父块切子块。输出:父块分组序列。"""
        groups: list[ChunkGroup] = []
        for parent_text in self._parent_splitter.split_text(text):
            if not parent_text.strip():
                continue
            children = tuple(
                piece
                for piece in self._child_splitter.split_text(parent_text)
                if piece.strip()
            )
            # 父块切不出子块就整块丢掉:留一个空父块只会让检索命中一条什么都喂不了的引用
            if children:
                groups.append(ChunkGroup(parent_text=parent_text, children=children))
        return groups
