"""引用清单的渲染:一份实现,``rag`` 与 ``qa`` 两个命令共用。

为什么抽出来:同一份引用清单在两个命令里各渲染一遍,同一列分数迟早显示成两种样子
(一个 4 位小数、一个 2 位;「重排 vs 融合」的标注也会各自漂移)。人和评估脚本看到的
口径必须是同一个 —— 所以渲染也只留一处。

只依赖 domain 与标准库:命令模块顶层不许拖重依赖(见 cli/commands/__init__.py)。
"""

from __future__ import annotations

from collections.abc import Sequence

from project_rag.domain import Citation


def print_citations(citations: Sequence[Citation]) -> None:
    """输入:引用序列。行为:打印「编号 + 分数 + 来源 + 文档 + 正文预览」。输出:无(空序列什么都不打)。"""
    if not citations:
        return
    # 引用清单是给人核对的:编号要和答案里的 [n] 对得上
    print("\n引用:")
    for citation in citations:
        chunk = citation.chunk
        # 预览用 context_text:那才是模型真正读到的正文(有父块就是父块)。
        # 用子块预览会让人对着一段模型没看过的文字核对答案。
        preview = chunk.context_text[:40].replace("\n", "⏎")
        which = (
            f"父块 {len(chunk.parent_content)} 字" if chunk.parent_content else "无父块"
        )
        # 重排分和融合分是两个量纲(RRF≈0.016 / 重排是 0~1),所以分开放、分别标名,
        # 绝不相加也不会被误读成同一个尺子
        if citation.rerank_score is None:
            score = f"{citation.score:.4f}"
        else:
            score = f"重排 {citation.rerank_score:.4f} (融合 {citation.score:.4f})"
        print(f"  [{citation.index}] {score}  {chunk.source}  {chunk.doc_id}  {which}")
        print(f"      {preview}…")
