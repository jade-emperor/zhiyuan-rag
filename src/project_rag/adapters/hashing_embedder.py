"""
确定性哈希嵌入:字符 bigram 投影到固定维度,零依赖、零模型。

**它不是语义模型**:只保证「文本越像、向量越近」,不保证「意思相同、向量相近」。
它存在的理由是让 ingest→search 的管线在没有 2.2G BGE-M3 的机器上也能跑通与验收;
真实语义检索由 M3.3 的 bge_m3 适配器提供(那才是生产实现)。

**绝不能进 build_real_deps**:装真实依赖时悄悄退回哈希向量,等于让检索结果看起来正常、
实际全是噪声 —— 比直接报错危险得多。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from project_rag.domain import Embedding
from project_rag.ports import Embedder

_DEFAULT_DIM = 256


class HashingEmbedder(Embedder):
    """字符 bigram + 带符号哈希 → L2 归一化向量。同样的输入永远给同样的输出。"""

    def __init__(self, dim: int = _DEFAULT_DIM) -> None:
        """输入:向量维度。行为:校验并记下维度。输出:无。"""
        if dim <= 0:
            raise ValueError(f"dim 必须为正,收到 {dim}")
        self._dim = dim

    @property
    def dim(self) -> int:
        """输入:无。行为:返回维度。输出:维度。"""
        return self._dim

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """输入:一批文本。行为:逐条转成 bigram 哈希向量。输出:与输入同序的向量。"""
        return [Embedding(dense=self._vector(text)) for text in texts]

    def _vector(self, text: str) -> tuple[float, ...]:
        """输入:一段文本。行为:去空白 → 取字符 bigram → 带符号哈希 → L2 归一化。输出:稠密向量。"""
        # 去掉所有空白:中文里的空格和换行基本都是排版噪声,留着只会稀释相似度
        cleaned = "".join(char for char in text if not char.isspace())
        if not cleaned:
            return tuple([0.0] * self._dim)
        if len(cleaned) == 1:
            grams = [cleaned]
        else:
            grams = [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]

        vector = [0.0] * self._dim
        for gram in grams:
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            # 第 5 个字节决定符号:带符号哈希能抵消一部分碰撞造成的虚高相似度
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return tuple(vector)
        return tuple(value / norm for value in vector)
