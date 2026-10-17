"""端口:把用户的问法变成「拿哪几句话去检索」。

**为什么是两个端口,不是一个**:这两件事换掉的理由完全不同 ——

* ``StrategyClassifier``:判「该用哪种改写策略」。换它 = 换判定器
  (微调的 BERT → 规则兜底 → 以后换个更小的模型);
* ``QueryRewriter``:真的给出检索语句。换它 = 换改写实现
  (LoRA 分类 + LLM 改写 → 纯 LLM → 纯规则)。

合成它们的是用例 ``application.query_rewrite.plan_rewrite``,**不是适配器** ——
「先判策略、再按策略改写、失败就原样检索」是业务顺序,不该长在某个供应商的细节里。

只 import domain,不 import 任何第三方 —— 这是它能当"端口"的前提。
"""

from __future__ import annotations

from typing import Protocol

from project_rag.domain import QueryRewrite, StrategyPrediction


class StrategyClassifier(Protocol):
    """我需要的能力:给一句用户原话,判它该走哪种改写策略。"""

    def classify(self, query: str) -> StrategyPrediction:
        """输入:用户原话。行为:判策略。输出:标签 + 置信度 + 谁判的。

        **适配器该抛就抛**(模型没加载、权重缺文件、输出越界),不要在这里偷偷吞掉 ——
        兜底是**用例**的事(``plan_rewrite`` 会退回「原样检索」并记一条 WARNING)。
        在适配器里吞掉的话,「模型其实一直没在跑」这件事就没人看得见了。

        单条调用要够快(线上是每请求一次):实现应当**一次构造、多次复用**,
        不要在 ``classify`` 里加载权重。
        """
        ...


class QueryRewriter(Protocol):
    """我需要的能力:给一句用户原话,给出「拿哪几句话去检索」。"""

    def rewrite(self, query: str) -> QueryRewrite:
        """输入:用户原话。行为:判策略 → 生成检索语句。输出:``QueryRewrite``。

        两条约定:

        - ``queries`` **永不为空**;失败也退回 ``(query,)``;
        - ``strategy == "direct"`` 时 ``queries`` 必须**逐字**等于原话。

        **实现不许抛异常**:这条路的失败只该让检索退回原话,不该让一次问答失败。
        (``StrategyClassifier`` 允许抛,是因为它上面还有 ``plan_rewrite`` 兜着;
        走到这个端口就已经是兜底之后了。)
        """
        ...
