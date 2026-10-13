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

实测(一份 13601 字的课程大纲语料,2026-10-13 已随培训课程数据清除;
父 1200 / 子 300 / 重叠 50):**15 个父块、70 个子块**,子块 41~299 字,无空块。
(不分层时是 68 块 —— 先切父块会改变子块的边界,所以数量不完全等于单级切分。)

**偏移(P0-5)**:``add_start_index=True`` 让 langchain 把每块的起始偏移一起给我们,
页码就是拿它去查 ``Document.pages``(见 ``domain/pages.py``)。
⚠️ 这个开关在 langchain-text-splitters **1.x 是构造参数**,0.x 才是 ``create_documents`` 的参数 ——
写成 0.x 那种"1.1.2 直接 TypeError";而写在构造函数上却能一直用。
**langchain 找不到块的位置时给的是 ``-1``(不报错)**,所以 ``_start_of`` 必须当场验:
-1 会被当成"第 0 个字符之前",二分出来的页码是**另一个看着合法的页号**。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from project_rag.domain import ChunkGroup
from project_rag.ports import TextChunker

if TYPE_CHECKING:  # 只为类型注解;运行期在 _build 里延迟 import
    from typing import Any, Protocol

    from langchain_text_splitters import RecursiveCharacterTextSplitter

    class SplitPiece(Protocol):
        """切分器吐出来的一块(只声明我们真正用到的那两个属性)。

        **为什么不用 langchain 的 ``Document`` 做注解**:1.x 的 ``langchain_text_splitters``
        **不再导出** ``Document``(它搬去了 ``langchain_core``),写成注解会被 pyright 判成
        ``"Document" is unknown import symbol`` —— 而为了一个纯注解去 import ``langchain_core``,
        等于把一个**传递依赖**写进依赖表(法律 6)。结构类型反而更准确:我们只依赖
        ``page_content`` 与 ``metadata["start_index"]`` 这两样。
        """
        page_content: str
        metadata: dict[str, Any]

# 语义强度从强到弱。最后那个空串是兜底:实在断不开就逐字切,保证块长有上界。
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]


def _build(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    """输入:块大小与重叠字数。行为:按中文分隔符优先级建一个切分器。输出:切分器。

    **langchain 在函数里 import**(不在模块级):`langchain_text_splitters` 会连带拉起
    `transformers`(1.1s)、`sentence_transformers`(1.0s)、`nltk`(0.7s),实测这一个 import
    就是 **3.08s** —— 而 `adapters/__init__.py` 里 re-export 了本模块,于是每条
    `from project_rag.adapters import …`(含 `--help`)都要替它付这份钱。
    移到函数里之后,只有真正要切分(ingest)的那些进程才付。
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        # 句末标点留在块尾:否则读起来像断头句,人也难核对切分质量
        keep_separator=True,
        # 中文按「字数」而不是 token 算长度 —— 300 字对 BGE-M3 是合适的输入规模
        length_function=len,
        # P0-5:把每块的起始偏移写进 metadata["start_index"](页码靠它查)
        add_start_index=True,
    )


def _start_of(text: str, piece: SplitPiece, what: str) -> int:
    """输入:被切的正文、langchain 给的一块、报错用的名字。行为:取出起始偏移并验它对得上。输出:偏移。

    **两件都必须验,两件都是静默的**:

    * 没带 ``start_index``(有人把 ``add_start_index`` 去掉/写错了地方)→ 页码全错;
    * langchain **找不到**这一块时给 ``-1`` 且不报错 → 二分查表查出来的是**别的页**。
    """
    start = piece.metadata.get("start_index")
    if not isinstance(start, int) or start < 0:
        raise ValueError(
            f"切分器没给出{what}的起始偏移(langchain 回的是 {start!r})—— "
            "页码会整体算错,先别入库;检查 _build 里的 add_start_index=True"
        )
    body = piece.page_content
    if text[start : start + len(body)] != body:
        raise ValueError(
            f"{what}的起始偏移 {start} 与正文对不上 —— 切分器给的位置不可信;"
            "页码宁可不给,也不能给一个看着合法的错页号"
        )
    return start


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
        """输入:一段文本。行为:先切父块,再把每个父块切子块,并记下每个子块的起始偏移。输出:父块分组序列。

        用 ``create_documents`` 而不是 ``split_text``:两者切出来的块**逐字相同**
        (契约盯着),但前者带 ``start_index`` —— 这就是页码的锚点。
        子块的偏移是**相对父块正文**的,加上父块在整段正文里的起点才是绝对位置。
        """
        groups: list[ChunkGroup] = []
        for parent in self._parent_splitter.create_documents([text]):
            parent_text = parent.page_content
            if not parent_text.strip():
                continue
            parent_start = _start_of(text, parent, "父块")
            children = tuple(
                piece
                for piece in self._child_splitter.create_documents([parent_text])
                if piece.page_content.strip()
            )
            # 父块切不出子块就整块丢掉:留一个空父块只会让检索命中一条什么都喂不了的引用
            if children:
                groups.append(
                    ChunkGroup(
                        parent_text=parent_text,
                        children=tuple(piece.page_content for piece in children),
                        child_starts=tuple(
                            parent_start + _start_of(parent_text, piece, "子块")
                            for piece in children
                        ),
                    )
                )
        return groups
