"""
把目录下的纯文本文件读成 ``Document``。

现在只认 ``.md`` / ``.txt``。PDF / Word / PPT / OCR 的工具其实都装好了,缺的是抽
``DocumentSource`` 端口把它们接进来 —— 见 ``docs/deferred.md`` §1.4。

``source`` 取父目录名去掉 ``_data`` 后缀:``ai_data`` → ``ai``,
可以直接当 Milvus 的 source 过滤字段用。``doc_id`` 用相对路径 —— 日志里能一眼看出是哪份文件。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from project_rag.domain import Document

_SUFFIXES = (".md", ".txt")


def load_text_files(path: Path) -> Sequence[Document]:
    """输入:文件或目录路径。行为:递归收集 .md/.txt,按路径排序后逐个读入。输出:文档序列。

    按路径排序是为了**确定性**:同一目录跑两次必须给出同样的顺序,
    否则「同一份输入 → 同一批 chunk_id」这件事无从谈起(幂等就建立在它上面)。

    编码用 utf-8-sig:有 BOM 就吃掉,没 BOM 也无害 —— 课程资料里 BOM 很常见
    (FAQ CSV 就带 BOM,读成 utf-8 时列名会多出 \\ufeff)。
    """
    if path.is_file():
        root = path.parent
        candidates = [path]
    else:
        root = path
        candidates = [p for p in path.rglob("*") if p.is_file()]

    files = sorted(p for p in candidates if p.suffix.lower() in _SUFFIXES)

    documents: list[Document] = []
    for file in files:
        documents.append(
            Document(
                doc_id=file.relative_to(root).as_posix(),
                text=file.read_text(encoding="utf-8-sig"),
                source=root.name.removesuffix("_data") or root.name,
            )
        )
    return documents
