"""
中文分词:jieba + 丢掉纯标点/空白。

**为什么要单独一个模块**:有两处要用同一个分词口径 —— FAQ 匹配器(问题级)
和 BM25 关键字索引(块级)。分词口径一旦分叉,「同一条查询在两处切出不同的词」
这种 bug 极难查,所以宁可抽出来共用。

顺带一句:``jieba`` 只准出现在 ``adapters/`` —— 这个模块在 adapters 里,
所以 ``ports/`` / ``application/`` 都不会碰到它。
"""

from __future__ import annotations

import re

import jieba

# 只保留含「词字符」的 token,过滤掉标点和空白(中文标点不在 \w 里)
_WORDISH = re.compile(r"\w")


def tokens(text: str) -> list[str]:
    """输入:一段文本。行为:jieba 分词,丢掉标点和空白。输出:词列表。"""
    return [
        token
        for token in (piece.strip() for piece in jieba.lcut(text))
        if token and _WORDISH.search(token)
    ]
