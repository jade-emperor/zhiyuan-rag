"""
BGE-M3 稠密嵌入适配器(**只出稠密向量**)。

用 ``sentence_transformers.SentenceTransformer`` 而不是 ``milvus_model.BGEM3EmbeddingFunction``:

- 后者**总是**连稀疏一起算,``return_sparse=False`` 它不认(实测 TypeError)。纯稠密实测
  **2.69 块/秒 vs 1.91 块/秒**(40 块 × 300 字,CPU),快约 40%;
- 两条路出的稠密向量**逐元素一致**(cos = 1.000000,最大元素差 1.5e-08),
  换实现不影响已经算好的向量;
- 模型目录自带 ``1_Pooling`` + ``2_Normalize`` 模块,池化与 L2 归一化由模型自己做,
  所以出库向量 norm = 1.0 —— **Milvus 用 IP 度量就等于余弦**;
- M3.9 的重排用同一套库的 ``CrossEncoder``,少一个 SDK 少一份版本约束。

**为什么关键字检索不用嵌入模型的稀疏向量**:学习型稀疏与 BM25 都是词面匹配,两路重叠度高,
留着只会让融合多一路、评估多一维、还要多维护一个稀疏索引。少一路、每一路都说得清,
是这个项目选的取舍;而且 BM25 的 IDF 来自**自己的语料**,对罕见错误码/符号天然更强。

**必须在 import 之前关掉 HuggingFace 联网**(见下面那行):ST 5.x 会去 hub 查模型元数据,
本环境 huggingface.co 不通,每次调用都要等超时 —— 实测模型加载 **135s → 1.2s**。
模型是本地路径,本来就不需要联网。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

# 这行必须在 import sentence_transformers 之前 —— 它读的是 import 期的环境变量。
# 用 setdefault 而不是直接赋值:用户显式设过就尊重用户的。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sentence_transformers import SentenceTransformer

from project_rag.config import EmbeddingConfig
from project_rag.domain import Embedding
from project_rag.ports import Embedder


class BGEM3Embedder(Embedder):
    """BGE-M3 稠密嵌入。"""

    def __init__(self, config: EmbeddingConfig) -> None:
        """输入:嵌入配置(模型目录 / 设备 / fp16)。

        行为:加载模型并读出维度。
        输出:无。**加载是重操作**(实测 1.2s 起),所以它该发生在工厂被调用时,
        而不是 import 时 —— 否则 `--help` 都要交这份钱。
        """
        self._model = SentenceTransformer(str(config.model_path), device=config.device)
        if config.use_f16 and config.device.startswith("cuda"):
            # CPU 上 fp16 反而更慢,所以只在 CUDA 上开
            self._model.half()
        # ST 5.x 把方法改名成 get_embedding_dimension(),4.x 只有旧名 —— 两个都认
        self._dim = self._embedding_dimension()

    def _embedding_dimension(self) -> int:
        """输入:无。行为:读出稠密维度。输出:维度。

        ST 5.x 叫 ``get_embedding_dimension()``,4.x 只有 ``get_sentence_embedding_dimension()``。
        写成独立方法而不是一行 ``getattr`` 兜底:后者会把类型推成 ``int | None``,检查器过不去。
        """
        new_api = getattr(self._model, "get_embedding_dimension", None)
        if new_api is not None:
            return int(new_api())
        legacy = self._model.get_sentence_embedding_dimension()
        if legacy is None:
            raise RuntimeError(
                "模型没有声明嵌入维度（1_Pooling/config.json 缺 word_embedding_dimension）—— "
                "这种目录是残缺的模型,换一个完整的,别让它带着猜出来的维度进库"
            )
        return int(legacy)

    @property
    def dim(self) -> int:
        """输入:无。行为:返回稠密维度(BGE-M3 是 1024)。输出:维度。"""
        return self._dim

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """输入:一批文本。行为:批量编码 + L2 归一化。输出:与输入同序的向量。"""
        vectors = self._model.encode(
            [str(text) for text in texts],
            # 模型自带 2_Normalize,这里仍然显式要求一次:
            # 「IP = 余弦」是这个项目的前提,不能靠模型的默认行为兜着
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [
            Embedding(dense=tuple(float(value) for value in vector))
            for vector in vectors
        ]
