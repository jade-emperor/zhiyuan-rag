"""查询改写的领域类型:4 种改写策略的闭集 + 一次改写的结果。

**策略为什么是闭集而不是自由字符串**:策略决定「拿改写后的哪几句话去检索」,
下游(检索、日志、验收)都要按它分支。写成任意字符串的话,模型多吐一个字就是一条
没人认识的新策略,而且**不报错** —— 只会在检索那头静默退回原句,谁也发现不了。

4 个标签与训练数据(`data/intent/strategy_v1/`)是**同一套编号**:

===  ============  ================================================
编号  策略          什么时候用
===  ============  ================================================
0     direct        问题标准、术语明确,直接拿去检索
1     hypothetical  问题空泛、缺可检索的实体,先编一段假设答案再检索(HyDE)
2     subquery      一句话里塞了两个以上各自独立的问题,拆开分别检索
3     denoise       话里混了不需要被回答的内容,去掉噪声再检索
===  ============  ================================================

编号是**模型输出层的口径**(logits 的列序),策略名是**代码里的口径**。两边只在
``STRATEGIES`` 这一处对齐 —— 改这个元组的顺序等于换一套标签,必须重训模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RewriteStrategy = Literal["direct", "hypothetical", "subquery", "denoise"]

# **索引即标签**：STRATEGIES[0] 就是标签 0。训练数据、模型输出、这里三者同序。
STRATEGIES: tuple[RewriteStrategy, ...] = (
    "direct",
    "hypothetical",
    "subquery",
    "denoise",
)

NUM_STRATEGIES = len(STRATEGIES)

# 给人看的名字。日志 / CLI / 报表一律从这里取 —— 三处各译一遍迟早译出三种叫法。
STRATEGY_NAMES: dict[RewriteStrategy, str] = {
    "direct": "直接检索",
    "hypothetical": "假设答案",
    "subquery": "子查询分解",
    "denoise": "去噪改写",
}


def strategy_of(label: int) -> RewriteStrategy:
    """输入:标签编号。行为:查表。输出:策略名;越界当场抛。

    **越界必须抛,不许夹到 0**:夹一下会让「模型输出了个 7」表现成「直接检索」,
    而那是最像正常的一种表现 —— 排障时永远不会怀疑到这里。
    """
    if not 0 <= label < NUM_STRATEGIES:
        raise ValueError(f"策略标签只能是 0~{NUM_STRATEGIES - 1},收到 {label}")
    return STRATEGIES[label]


def label_of(strategy: str) -> int:
    """输入:策略名。行为:反查编号。输出:编号;不认识当场抛。"""
    try:
        return STRATEGIES.index(strategy)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"不认识的改写策略 {strategy!r};只有 {STRATEGIES}") from exc


@dataclass(frozen=True, slots=True)
class StrategyPrediction:
    """分类器的一次判定:该走哪种改写策略,以及它有多确信。

    ``decided_by`` 说清**是谁判的**。这不是装饰:微调模型与规则兜底(见
    ``adapters/strategy_rule.py``)的准确率差着一个量级,日志里不写清来源,
    「线上分类怎么变差了」会查成玄学。
    """

    label: int
    confidence: float = 1.0
    decided_by: str = "model"  # model = 微调模型 / rule = 规则兜底

    @property
    def strategy(self) -> RewriteStrategy:
        """输入:无。行为:把编号翻成策略名。输出:策略名;编号越界时抛。"""
        return strategy_of(self.label)


@dataclass(frozen=True, slots=True)
class QueryRewrite:
    """一次改写的结论:**拿哪几句话去检索**。

    两条不变式,检索侧与验收都按它们写(``plan_rewrite`` 负责保证):

    - ``queries`` **永不为空**。改写失败一律退回 ``(original,)`` ——
      空元组会让检索直接返回空结果,伪装成「知识库里没有」,那是最坏的一种失败;
    - ``strategy == "direct"`` ⟺ ``queries == (original,)``。直接检索的定义就是
      「一个字都不改」,所以这两件事是同一条;出现 ``direct`` 却换了句子,
      说明有地方在偷偷改写而账上写着「没改」。

    ``confidence`` / ``decided_by`` / ``note`` 只用于日志与排查,**不参与任何判定** ——
    低置信怎么处理是策略问题(实测过:一刀切回退反而更差),不该散落在检索路径里。
    """

    original: str
    strategy: RewriteStrategy
    queries: tuple[str, ...]
    confidence: float | None = None  # None = 没跑分类器(直接退回)
    decided_by: str = "passthrough"  # model / rule / passthrough / fallback
    note: str = ""  # 降级原因,只进日志

    @property
    def label(self) -> int:
        """输入:无。行为:策略名翻回编号。输出:编号。"""
        return label_of(self.strategy)

    @property
    def changed(self) -> bool:
        """输入:无。行为:检索语句是否与用户原话不同。输出:布尔。"""
        return self.queries != (self.original,)


def direct_rewrite(
    query: str,
    *,
    note: str = "",
    decided_by: str = "passthrough",
    confidence: float | None = None,
) -> QueryRewrite:
    """输入:原话 + 备注 + 谁判的 + 可选的置信度。行为:造一个「原样检索」的结论。输出:``QueryRewrite``。

    **降级只有这一个出口**:改写失败、模型没装、置信度太低、模型回了个越界标签 ——
    全部走这里。散着写 ``QueryRewrite(..., strategy="direct", ...)`` 的话,
    迟早有一处忘了带 ``queries=(query,)``,而那种对象一进检索就是空召回。

    ``confidence`` 单独留一个口子:降级往往发生在**已经判出策略之后**(没配 LLM、
    改写调用失败),那时置信度是有的,丢掉它等于把"模型其实判得挺准、只是改写那步挂了"
    这条线索一起扔掉。
    """
    return QueryRewrite(
        original=query,
        strategy="direct",
        queries=(query,),
        confidence=confidence,
        decided_by=decided_by,
        note=note,
    )
