"""规则版策略分类器:**没有微调模型时的兜底**,不假装自己是那个模型。

它和 ``adapters/hashing_embedder.py`` 是同一个角色:让「这台机器上没有几百兆的权重」
不至于变成「整个问答起不来」。差别在于那个是演示嵌入,这个是演示**判定** ——
它的准确率比微调模型差得远(判定规则本来就是造训练数据时写给人看的那三条),
所以:

* 它在日志与账上永远标 ``decided_by="rule"``,和模型的 ``"model"`` 分得开;
* 构造时打一条 **WARNING**,让人一眼看出"线上跑的不是模型";
* 判不准时**一律给 0(直接检索)** —— 0 的后果是"原样检索"(安全),
  而误判成 1/2/3 会白调一次模型、还可能把检索语句改坏。**偏向 0 是刻意的**。

三条判定规则与造训练数据时用的那套提示词同源(`data/intent/` 下,不随仓库发布),
按顺序判断、命中即停:

1. 句中有不需要被回答的内容 → 3(去噪);
2. 剩下的话能拆成两个都要回答的独立问题 → 2(子查询分解);
3. 剩下的话里没有可检索的实体或术语 → 1(假设答案);
4. 都不命中 → 0(直接检索)。
"""

from __future__ import annotations

import re

from project_rag.domain import StrategyPrediction
from project_rag.observability import get_logger

_logger = get_logger("adapters.strategy_rule")

# 规则 1:噪声。只收**高置信**的那几类 —— 寒暄、情绪、身份铺垫、元指令。
# 宁可漏(退回 0,安全)也不要误伤:漏掉的噪声只是让检索差一点,
# 而把"用户真正想问的东西"当成噪声删掉,是直接答错。
_NOISE_WORDS = (
    "请详细",
    "详细讲",
    "详细说",
    "展开讲",
    "展开说",
    "麻烦你",
    "谢谢",
    "多谢",
    "辛苦了",
    "在线等",
    "我是新手",
    "我是小白",
    "我刚转行",
    "我刚学",
    "我最近在学",
    "我有个",
    "我有一个",
    "不要用专业术语",
    "说人话",
    "通俗易懂",
)

# 规则 2:两个及以上问号 —— 一句话里问了两件事的最可靠信号。
_QUESTION_MARKS = re.compile(r"[?？]")
# 连接词 + 疑问词:没打两个问号但确实是两问("A怎么样,另外B是什么")
_LINK_WORDS = ("另外", "还有", "以及", "顺便", "再问")
_INTERROGATIVES = ("什么", "怎么", "如何", "哪些", "哪个", "多少", "为什么", "吗", "呢")

# 规则 3:没有可检索的实体。两个信号,都要求**短**且**无 ASCII 实词**
# (ASCII 字母/数字几乎总是术语:Python / JVM / GIL / Redis / 100亿)。
_LEADING_PRONOUN = re.compile(r"^(它|他|她|这个|那个|这些|那些|这|那)")
_VAGUE_TAIL = ("有哪些", "有什么", "怎么样", "怎么办", "是什么", "有什么用", "值得", "如何")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
_SHORT_CHARS = 20


class RuleStrategyClassifier:
    """规则兜底分类器。**无状态、零依赖、构造即用**(没有权重可加载)。"""

    def __init__(self) -> None:
        _logger.warning(
            "查询改写用的是**规则兜底**分类器(rewrite.classifier=rule):"
            "只有噪声/多问/空泛三条粗规则,准确率远低于微调模型。"
            "要真效果请配 rewrite.classifier=lora 并准备好适配器目录。"
        )

    def classify(self, query: str) -> StrategyPrediction:
        """输入:用户原话。行为:按三条规则判一次。输出:``StrategyPrediction``(``decided_by="rule"``)。

        规则命中就给**固定的 0.6 置信度** —— 它不是概率,只是"这条是规则判的"标记。
        编一个像模型输出的高置信度,会让账本上的 ``confidence`` 失去意义。
        """
        text = (query or "").strip()
        if not text:
            return StrategyPrediction(label=0, confidence=0.6, decided_by="rule")

        # 规则 1:噪声 → 去噪改写
        if any(word in text for word in _NOISE_WORDS):
            return StrategyPrediction(label=3, confidence=0.6, decided_by="rule")

        # 规则 2:两个独立问题 → 子查询分解
        if len(_QUESTION_MARKS.findall(text)) >= 2:
            return StrategyPrediction(label=2, confidence=0.6, decided_by="rule")
        if any(word in text for word in _LINK_WORDS) and any(
            word in text for word in _INTERROGATIVES
        ):
            return StrategyPrediction(label=2, confidence=0.6, decided_by="rule")

        # 规则 3:没有可检索的实体 → 假设答案
        if _LEADING_PRONOUN.match(text):
            return StrategyPrediction(label=1, confidence=0.6, decided_by="rule")
        if (
            len(text) <= _SHORT_CHARS
            and not _ASCII_WORD.search(text)
            and any(word in text for word in _VAGUE_TAIL)
        ):
            return StrategyPrediction(label=1, confidence=0.6, decided_by="rule")

        # 都不命中 → 直接检索(宁可漏判,不能误判)
        return StrategyPrediction(label=0, confidence=0.6, decided_by="rule")
