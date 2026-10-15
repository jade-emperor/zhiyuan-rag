"""用例:把用户问法规划成「拿哪几句话去检索」。

**位置**:FAQ 未命中之后、向量检索之前 —— 合流通道里就是这一段。FAQ 命中时压根
走不到这里(人工维护的标准答案不需要检索,也就不需要改写)。

三步,每一步失败都退回「原话直接检索」:

1. 判策略(``StrategyClassifier``:微调的 BERT + LoRA,见 ``tools/lora/``);
2. 按策略生成检索语句(``LLMClient`` + ``prompts.rewrite_prompt``);
3. 收口:剥 json、去重、限长、限条数,``queries`` 永不为空。

**为什么失败一律退回原话,而不是往上报**:改写是锦上添花。让它能把一次问答搞挂,
等于给系统新增一个「答不出来」的原因 —— 而它争到的那点召回率远不值这个价。
反过来,**降级必须留痕**:每次都记一条日志带上原因。否则「改写其实一直没生效」
在线上表现成「效果一般」,谁也不会想到去查它。

**为什么分类器与改写器分开注入**:分类器是那个微调模型(重、可选、会加载失败),
改写器只是一次带提示词的 LLM 调用。把它们焊成一个类,「模型没装但还是想改写」
与「模型装了但不想花钱改写」这两种情况就都表达不出来了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from project_rag.domain import (
    ChatMessage,
    QueryRewrite,
    RewriteStrategy,
    StrategyPrediction,
    direct_rewrite,
    strategy_of,
)
from project_rag.observability import get_logger
from project_rag.ports import LLMClient, StrategyClassifier

from .prompts import rewrite_prompt

_logger = get_logger("application.query_rewrite")

# 模型输出里 json 外面的 ``` 围栏。它不是错(网页版爱这么吐),剥掉就好。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _collect(llm: LLMClient, prompt: str) -> str:
    """输入:LLM 端口 + 提示词。行为:调一次流式生成并拼出全文。输出:模型输出的正文。

    只拼 ``delta.text``:**思考帧不算答案**(``LlmDelta.reasoning_text`` 是另一个字段,
    理由见 domain/models.py 里那段)。这里要是把思考也拼进来,解析出的 json 会多一堆草稿。
    """
    parts: list[str] = []
    for delta in llm.stream([ChatMessage(role="user", content=prompt)]):
        if delta.text:
            parts.append(delta.text)
    return "".join(parts).strip()


def _strip_fence(raw: str) -> str:
    """输入:模型原始输出。行为:有 ``` 就取围栏里的内容。输出:待解析文本。"""
    match = _FENCE.search(raw)
    return (match.group(1) if match else raw).strip()


def _load_payload(text: str) -> object | None:
    """输入:待解析文本。行为:抠出第一个 json 对象或数组并解析。输出:解析结果;解不出给 None。

    **只做"抠出来"这一件事,不做修复**:截断的 json、缺引号的 json、中文全角引号,
    一律判为解不出 → 退回原话。花式修复的代价是"修出半个对象"这类更难查的错,
    而这里的收益上限只是"省一次退回原话"。
    """
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start < 0 or end <= start:
            continue
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
    return None


def _items_of(payload: object) -> list[object]:
    """输入:解析结果。行为:取出句子列表。输出:列表(取不到给空表)。

    认三种形状:``{"queries": [...]}``(提示词要求的)、``{"queries": "..."}``(模型图省事)、
    裸数组 ``[...]``。**不认别的键名** —— 放宽键名等于让模型自己发明接口。
    """
    if isinstance(payload, dict):
        payload = payload.get("queries")
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


def _clean(
    items: list[object], *, limit: int, max_chars: int
) -> tuple[str, ...]:
    """输入:句子列表 + 条数上限 + 单句长度上限。行为:去空白、去重、截断、限量。输出:元组。

    **去重按"去空白之后的逐字相等"**:同一句话模型很容易吐两遍(尤其子问题),
    留着它等于同一路检索白跑一次、还把融合分算重。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        # 换行/连续空格在检索里没有意义,统一压成单空格(提示词里也要求了不写序号)
        text = " ".join(item.split())
        if len(text) > max_chars:
            text = text[:max_chars].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def _limit_for(strategy: RewriteStrategy, max_subqueries: int) -> int:
    """输入:策略 + 子问题上限。行为:定这次最多要几句。输出:条数上限。

    只有子查询分解是"多句"的:假设答案与去噪改写的产物就是**一句**替换语句,
    多给几句只会把检索摊薄(每句各召回一路,融合后谁都排不上)。
    """
    return max(1, max_subqueries) if strategy == "subquery" else 1


def plan_rewrite(
    query: str,
    *,
    classifier: StrategyClassifier,
    llm: LLMClient | None = None,
    max_subqueries: int = 3,
    max_query_chars: int = 400,
    confidence_threshold: float = 0.0,
) -> QueryRewrite:
    """输入:用户原话 + 分类器 + 可选的 LLM + 三个旋钮。

    行为:判策略 → (需要时)调模型生成检索语句 → 收口;任何一步失败都退回原话。
    输出:``QueryRewrite``,``queries`` 至少有一条。

    ``confidence_threshold`` **默认 0 = 不做低置信兜底**,这是有实测依据的:
    在 1000 条测试集上,把低置信样本一刀切回退到「直接检索」会让准确率从 0.9700 掉到
    0.9590(阈值 0.7)甚至 0.9460(0.9)—— 因为判错的那些样本,真值大多落在第二位,
    回退并没有利用这个信息。留着这个旋钮是因为换一个分类器时可能需要它,
    **不是因为它现在有用**(读数见 ``tools/lora/finetune_report.md`` §6)。
    """
    text = " ".join((query or "").split())
    if not text:
        # 空问题轮不到改写:检索那侧本来就会召空 → 走拒答话术,那是它该管的事。
        # 给的是**压缩之后的空串**,不是原样的空白 —— 同一个函数里两套口径迟早出错。
        return direct_rewrite(text, note="空问题", decided_by="fallback")

    # ① 判策略 -------------------------------------------------------------
    try:
        prediction: StrategyPrediction = classifier.classify(text)
        strategy = strategy_of(prediction.label)
    except Exception as exc:  # noqa: BLE001 - 分类器是可选件,它挂了不该让问答挂
        # 越界标签也会落到这里(strategy_of 抛 ValueError):它和"模型没加载"是同一类事
        # —— 判定结果不可用,退回原话。
        _logger.warning("策略分类不可用(%s: %s),退回直接检索:%s", type(exc).__name__, exc, text)
        return direct_rewrite(
            text, note=f"分类失败:{type(exc).__name__}", decided_by="fallback"
        )

    if confidence_threshold > 0 and prediction.confidence < confidence_threshold:
        _logger.info(
            "分类置信度 %.3f < %.3f,按直接检索:%s",
            prediction.confidence,
            confidence_threshold,
            text,
        )
        return direct_rewrite(
            text, note="低置信", decided_by="fallback", confidence=prediction.confidence
        )

    if strategy == "direct":
        # **一次模型都不调**:这一类的定义就是"一个字都不改"。多花一次调用既不加信息,
        # 又会把原话改脏 —— 而"改脏"在检索上表现为召回变差,没人会怀疑到这一步。
        _logger.info("查询改写[直接检索 %.3f]:原样检索 %s", prediction.confidence, text)
        return QueryRewrite(
            original=text,
            strategy=strategy,
            queries=(text,),
            confidence=prediction.confidence,
            decided_by=prediction.decided_by,
        )

    if llm is None:
        _logger.warning("策略是 %s 但组合根没配 LLM,退回直接检索:%s", strategy, text)
        return direct_rewrite(
            text, note="没配 LLM", decided_by="fallback", confidence=prediction.confidence
        )

    # ② 生成检索语句 -------------------------------------------------------
    limit = _limit_for(strategy, max_subqueries)
    prompt = rewrite_prompt(strategy, text, max_subqueries=max_subqueries)
    try:
        raw = _collect(llm, prompt)
    except Exception as exc:  # noqa: BLE001 - 同上:改写失败不该让问答失败
        _logger.warning("改写调用失败(%s: %s),退回直接检索:%s", type(exc).__name__, exc, text)
        return direct_rewrite(
            text,
            note=f"改写调用失败:{type(exc).__name__}",
            decided_by="fallback",
            confidence=prediction.confidence,
        )

    queries = _clean(
        _items_of(_load_payload(_strip_fence(raw))), limit=limit, max_chars=max_query_chars
    )
    if not queries:
        # 把原始输出截一段打进日志:排查时"模型到底吐了什么"是第一个要看的,
        # 而这一步之后 raw 就没了
        _logger.warning("改写结果不可用,退回直接检索(模型输出 %.160s):%s", raw, text)
        return direct_rewrite(
            text,
            note="改写结果不可用",
            decided_by="fallback",
            confidence=prediction.confidence,
        )

    if queries == (text,):
        # 模型把原话原样吐了回来(或只差空白)。结论是"没有可改的",那就**归成直接检索** ——
        # 这样 domain 那条不变式(`direct` ⟺ `queries == (original,)`)是**无条件**成立的。
        # 不归的话账上会写着"去噪改写"、检索用的却是原话,而"账与行为不一致"是最难查的一类。
        # decided_by 仍留着 model:它确实判过、也确实调过模型,只是产出等于原话。
        _logger.info("改写结果与原话逐字相同,按直接检索:%s", text)
        return QueryRewrite(
            original=text,
            strategy="direct",
            queries=(text,),
            confidence=prediction.confidence,
            decided_by=prediction.decided_by,
        )

    _logger.info(
        "查询改写[%s %.3f %d 句 %s]%s → %s",
        strategy,
        prediction.confidence,
        len(queries),
        prediction.decided_by,
        text,
        " | ".join(queries),
    )
    return QueryRewrite(
        original=text,
        strategy=strategy,
        queries=queries,
        confidence=prediction.confidence,
        decided_by=prediction.decided_by,
    )


@dataclass(frozen=True, slots=True)
class PlannedRewriter:
    """把 ``plan_rewrite`` 包成**端口形状**的对象:``rewrite(query) -> QueryRewrite``。

    **为什么要有这个类,而不是让组合根返回一个偏函数**:``QueryRewriter`` 端口声明的
    是一个**方法**,不是一个可调用对象。这两种形状在 Python 里都"能调一下试试",
    所以混用不会当场报错 —— 只会在某个调用点写成 ``rewriter.rewrite(q)`` 时炸,
    而且炸在请求路径上(写这条时就是这样踩了一次)。形状只定义一处,就让它真的是那个形状。
    """

    classifier: StrategyClassifier
    llm: LLMClient | None = None
    max_subqueries: int = 3
    max_query_chars: int = 400
    confidence_threshold: float = 0.0

    def rewrite(self, query: str) -> QueryRewrite:
        """输入:用户原话。行为:走 ``plan_rewrite`` 那三步。输出:``QueryRewrite``。"""
        return plan_rewrite(
            query,
            classifier=self.classifier,
            llm=self.llm,
            max_subqueries=self.max_subqueries,
            max_query_chars=self.max_query_chars,
            confidence_threshold=self.confidence_threshold,
        )


@dataclass(frozen=True, slots=True)
class PassthroughRewriter:
    """装配失败时的替身:**原样检索**,并把原因一路带在 ``note`` 里。

    它不实现"什么都不做"—— 它实现的是「退回原话」这条降级路径,所以
    ``strategy="direct"``、``decided_by="fallback"``,日志里一眼能看出改写没生效。
    静默地返回 ``None`` 之类才是真正的坑:调用方得为它写分支,漏一处就是 AttributeError。
    """

    reason: str

    def rewrite(self, query: str) -> QueryRewrite:
        """输入:用户原话。行为:原样返回 + 记下降级原因。输出:``QueryRewrite``。"""
        return direct_rewrite(query, note=self.reason, decided_by="fallback")
