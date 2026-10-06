"""
纯文本来源:``.md`` / ``.txt``。

**为什么不走 unstructured 的 markdown 分区器**:markdown 的标题**本来就在正文里**(``#`` 开头的行),
直接读原文就是最完整的结构;过一遍解析器只会把 ``#`` 变成元素属性、再由我们拼回 ``#``,
白白多一轮「拆开再装回去」的风险。这条路径同时是既有的:换语料重灌时 ``chunk_id``
必须逐字不变,读原文是唯一能担保这一点的做法。

``read`` 只做一件事:读文本。命名(``doc_id`` / ``source``)、排序、格式分派都在
``adapters/document_loader.py`` —— 那些规则对所有格式一样,只该有一处。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar


class TextFilesSource:
    """``.md`` / ``.txt`` → 原文。"""

    suffixes: ClassVar[frozenset[str]] = frozenset({".md", ".txt"})

    def read(self, path: Path) -> str:
        """输入:一份 .md/.txt 的路径。行为:按 utf-8-sig 读。输出:原文。

        编码用 utf-8-sig:有 BOM 就吃掉,没 BOM 也无害 —— 课程资料里 BOM 很常见
        (FAQ CSV 就带 BOM,读成 utf-8 时列名会多出 \\ufeff)。
        """
        return path.read_text(encoding="utf-8-sig")
