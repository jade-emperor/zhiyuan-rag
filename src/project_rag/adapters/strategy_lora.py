"""微调模型版策略分类器:``chinese-roberta-wwm-ext`` + LoRA,4 分类。

**重量级适配器,故意不进 ``adapters`` 包入口**:torch + transformers + peft 是秒级
import、上 G 内存,与 ``embedder_bge_m3`` / ``reranker_bge`` 同一档 —— 按完整路径
import,免得 ``--help`` 也替它付这份钱(那条纪律见 ``adapters/__init__.py``)。

模型从哪来:``tools/lora/train.py`` 产出 ``<run>/lora_adapter/``(默认 ``logs/lora/run1``),
基座是 ``models/chinese-roberta-wwm-ext``。**两份都不在版本库里**(``data/``/``models/``/
``logs/`` 全在 ``.gitignore`` 里),所以"文件不在"是最常见的失败形状 —— 构造时当场抛,
由组合根降级成原样检索并记一条 WARNING,而不是让整个问答起不来。

**构造即加载**(与 ``BGEM3Embedder`` / ``BGEReranker`` 一致):组合根用 ``lru_cache``
把实例钉住,所以"加载一次"这件事是组合根保证的,这里不必再做惰性加载 + 双重检查。
``classify`` 因此必须是纯推理、零构造。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from project_rag.domain import StrategyPrediction
from project_rag.observability import get_logger

from .batching import Batcher

_logger = get_logger("adapters.strategy_lora")

# 与 tools/lora/infer.py 同一套默认值:训练时 max_length=64,推理必须一致
# (不一致不会报错,只会让长短句的判定悄悄变样 —— 那种漂移最难发现)。
DEFAULT_MAX_LENGTH = 64
_DEFAULT_BASE = "models/chinese-roberta-wwm-ext"
# run_meta.json 记的是**训练时**用的基座路径(相对仓库根);适配器目录的父目录就是它。
_META_NAME = "run_meta.json"


def resolve_base_model(adapter_dir: Path, explicit: str | None) -> str:
    """输入:适配器目录 + 显式基座路径(可空)。行为:定基座。输出:基座路径。

    优先级:**显式配置 > 训练记录 > 内置默认**。中间那一档是必要的 ——
    ``run_meta.json`` 是训练时自己写下的,比人记得准;而显式配置让"换了基座目录"
    这件事不必回去改训练记录。
    """
    if explicit:
        return explicit
    meta = adapter_dir.parent / _META_NAME
    if meta.is_file():
        try:
            recorded = json.loads(meta.read_text(encoding="utf-8")).get("model")
        except (OSError, json.JSONDecodeError):  # 记录坏了不该拦住推理,退回默认
            recorded = None
        if recorded:
            return str(recorded)
    return _DEFAULT_BASE


class LoraStrategyClassifier:
    """把微调好的 LoRA 适配器包成 ``StrategyClassifier``。"""

    def __init__(
        self,
        *,
        adapter_dir: str,
        model_dir: str | None = None,
        device: str = "cpu",
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = 32,
        batch_max_texts: int = 64,
    ) -> None:
        """输入:适配器目录 + 可选基座 + 设备 + 最大长度 + 批处理上限。行为:加载权重(重)。输出:无。

        路径不存在时**抛 FileNotFoundError**,消息里带上"先跑 tools/lora/train.py"——
        这条路径上最常见的失败就是"忘了训练/换了机器没同步 logs 目录"。
        """
        directory = Path(adapter_dir)
        if not directory.is_dir():
            raise FileNotFoundError(
                f"找不到 LoRA 适配器目录 {directory} —— 先跑 tools/lora/train.py 产出它"
                "(或把 rewrite.adapter_dir 指到已有的那一份)"
            )
        self._adapter_dir = directory
        self._max_length = max_length
        self._batch_size = batch_size

        import torch  # 重量级 import 只在这里发生(构造即加载)
        from peft import PeftModel
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        # 设备:配置来的字符串 → torch.device。写 "cuda" 但机器上没有 GPU 时,
        # 这里会当场抛而不是等到第一次前向 —— fail-fast,别让服务带着一个坏配置跑起来。
        resolved = resolve_base_model(directory, model_dir)
        self._device = torch.device(device)

        tokenizer = AutoTokenizer.from_pretrained(str(directory))
        base = AutoModelForSequenceClassification.from_pretrained(
            resolved, num_labels=4, problem_type="single_label_classification"
        )
        model = PeftModel.from_pretrained(base, str(directory))
        model.to(self._device)
        model.eval()
        self._tokenizer = tokenizer
        self._model = model
        # 并发下不再用互斥锁把调用串起来:批处理器把同刻排队的判定合成一批前向,
        # 只有 worker 线程碰模型 —— 既没有并发重入,也不会一条一条地等锁。
        self._batcher: Batcher[str, StrategyPrediction] = Batcher(
            self._classify_batch, max_items=batch_max_texts, name="strategy"
        )
        _logger.info(
            "策略分类器就绪:%s(基座 %s,设备 %s)", directory, resolved, self._device
        )

    def classify(self, query: str) -> StrategyPrediction:
        """输入:用户原话。行为:交给批处理器(同刻排队的判定合成一批)取 argmax。输出:``StrategyPrediction``。

        置信度是 **softmax 后的最大概率**。注意它**不是校准过的概率**
        (4 分类的 BERT 普遍过自信),所以只看它做"很确信 / 不太确信"的粗分档,
        别拿它当"这句话有 93% 属于第 1 类"来读。
        """
        return self._batcher.submit([query])[0]

    def _classify_batch(self, texts: Sequence[str]) -> list[StrategyPrediction]:
        """输入:一批用户原话(批处理器合批后的)。行为:一次前向批量取 argmax。输出:同序的预测。

        **只该由批处理器调用**:这是真正碰模型的地方,单线程执行,不必考虑并发重入。
        批量与单条的唯一差别是 padding 到本批最长;注意力 mask 会盖掉 padding,
        所以每条的结果与单条推理一致(数值上只差 fp16 的舍入)。
        """
        torch = self._torch
        with torch.no_grad():
            encoded = self._tokenizer(
                list(texts),
                truncation=True,
                max_length=self._max_length,
                padding=True,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            logits = self._model(**encoded).logits
            probs = torch.softmax(logits.float(), dim=-1)
        labels = probs.argmax(dim=-1).tolist()
        confidences = probs.max(dim=-1).values.tolist()
        return [
            StrategyPrediction(label=int(label), confidence=float(conf), decided_by="model")
            for label, conf in zip(labels, confidences)
        ]
