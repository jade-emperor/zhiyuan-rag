"""
BGE 重排适配器(CrossEncoder)。

**出库分数是 sigmoid 后的相关性分,恒在 [0,1]**:模型是单标签序列分类,
``predict`` 默认就接 Sigmoid(已验证 num_labels=1 / activation=Sigmoid)。
实测相关句 **0.99**、无关句 **0.000** —— 落差极大,所以引用清单里可以直接把
这个分数摆给人看。这和嵌入的余弦完全不是一回事:余弦全挤在 0.5~0.8,
**排序 ≠ 置信度**,那边不能当置信度用。

**max_seq_length 保持默认(8192),不截**:父块最长 1200 字,本来就进得去;
截短也省不了多少(实测 444 字的父块截到 512 几乎没变化 —— 它压根没超),
而真超了就是拿半截正文去判断相关性。这条以后要不要改,先让评估集说话。

**必须在 import 之前关掉 HuggingFace 联网**(见下面那行):和 embedder_bge_m3 同一个
原因 —— 本环境 huggingface.co 不通,ST 5.x 会去 hub 查模型元数据,每次加载都等超时。
两个适配器各写一遍,是因为**谁先被 import 谁负责**:import 顺序由调用方决定,
不能指望另一个文件已经替它设过了。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

# 这行必须在 import sentence_transformers 之前 —— 它读的是 import 期的环境变量
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sentence_transformers import CrossEncoder

from project_rag.config import EmbeddingConfig
from project_rag.ports import Reranker


class BGEReranker(Reranker):
    """BGE 系列的重排模型(默认 bge-reranker-v2-m3)。"""

    def __init__(self, config: EmbeddingConfig) -> None:
        """输入:嵌入配置(复用 rerank_model_dir / device;精度用**自己的** rerank_use_f16)。

        行为:加载 CrossEncoder 模型。
        输出:无。**加载是重操作**(实测 1.6s + 上 G 内存),所以它该发生在工厂被调用时,
        而不是 import 时 —— 否则连 `--help` 都要交这份钱。
        """
        self._model = CrossEncoder(str(config.rerank_model_path), device=config.device)
        if config.rerank_use_f16 and config.device.startswith("cuda"):
            # 用 rerank_use_f16 而不是 use_f16:重排与嵌入的收益/精度风险不同,要能分开拧
            # (实测 9 条 1104 字父块 fp32 398ms → fp16 101ms,3.9×)。
            # CPU 上 fp16 反而更慢,所以只在 CUDA 上开(和嵌入适配器一个口径)。
            # CrossEncoder 没有自己的 half(),要落到里面那个 transformers 模型上
            self._model.half()

    def rerank(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        """输入:一个问题 + 一批候选正文。行为:拼成 (问题, 正文) 对批量前向。输出:同序等长的相关性分。"""
        if not texts:
            # 空批直接返回,不去问模型:predict([]) 的行为随版本变,
            # 而「没有候选」本来就该是空结果
            return []
        scores = self._model.predict(
            [(query, str(text)) for text in texts],
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(score) for score in scores]
