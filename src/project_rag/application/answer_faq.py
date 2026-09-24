"""
用例:回答一个 FAQ 问题(旁路缓存 + 相似匹配 + LLM 兜底)。

用例层只认识端口,不认识 MySQL/Redis/jieba —— 换任何一个适配器都不用改这个文件。
"""

from __future__ import annotations

from typing import Literal

# 数据类
from project_rag.domain import ChatMessage, FaqAnswer, FaqMatch

# 创建日志的方法
from project_rag.observability import get_logger

# 端口中的各种抽象类(接口)
from project_rag.ports import Cache, FaqStore, LLMClient, TextMatcher

_logger = get_logger("application.answer_faq")

# key带命名空间和版本号,方便管理
CACHE_KEY_PREFIX = "faq:v1:"
DEFAULT_CACHE_TTL_S = 300
_CACHE_SEP = "\x1f"  # ASCII 单元分隔符,正常提问里不会出现


def _normalize(query: str) -> str:
    """
    输入:原始提问
    行为:折叠空白,让「怎么退款」和「怎么  退款」共用一条缓存(穿透防御的一半)
    输出:归一化后的字符串
    """
    return " ".join(query.split())


# 将问题拼上前缀
def _cache_key(query: str) -> str:
    """输入:原始提问。行为:归一化后拼上带版本号的命名空间前缀。输出:缓存键。"""
    return f"{CACHE_KEY_PREFIX}{_normalize(query)}"


# 缓存时,要声明来源于faq还是llm
def _encode(source: str, answer: str) -> str:
    """输入:来源和答案。行为:用分隔符拼成一条缓存值。输出:缓存 value。"""
    return f"{source}{_CACHE_SEP}{answer}"


# 返回时,要将缓存键解码成来源和答案
def _decode(value: str) -> tuple[Literal["faq", "llm"], str]:
    """输入:缓存 value
    行为:按分隔符切开,没有分隔符的按裸答案处理
    输出:(来源, 答案);分开写两个 if 是为了让 pyright 收窄成 Literal
    """
    source, sep, answer = value.partition(_CACHE_SEP)
    if sep and source == "llm":
        return "llm", answer
    if sep and source == "faq":
        return "faq", answer
    return "faq", value


def _safe_get(cache: Cache | None, key: str) -> str | None:
    """输入:缓存(可为 None)和键。行为:读缓存,没缓存或抛异常都按未命中并记 warning。输出:值或 None。"""
    if cache is None:
        return None
    try:
        return cache.get(key)
    except Exception as e:
        _logger.warning("缓存读取失败,按未命中处理: %s", e)
        return None


def _safe_set(cache: Cache | None, key: str, value: str, ttl: int) -> None:
    """输入:缓存(可为 None)、键、值、过期秒数。行为:写缓存,抛异常只记 warning 不影响答案。输出:无。"""
    if cache is None:
        return
    try:
        cache.set(key, value, ttl)
    except Exception as e:
        _logger.warning("缓存写入失败,按未命中处理: %s", e)


def match_faq(
    query: str,
    *,
    store: FaqStore,
    matcher: TextMatcher,
    threshold: float = 0.85,
) -> FaqMatch | None:
    """输入:问题 + FAQ 存储 + 匹配器 + 阈值。

    行为:取全量问答对、取最像的一条,``score >= threshold`` 才算命中(取等号)。
    输出:命中的匹配(带 entry 与 score);没命中给 None。

    **命中判定只有这一处**:FAQ 单通道(``answer_faq``)与合流通道(``answer_question``)
    都从它拿结论 —— 两个地方各写一遍比较运算符,「≥ 还是 >」「边界算不算命中」
    迟早各长一套。刻意**不碰缓存、不调 LLM**:缓存与兜底是通道自己的事,
    合流只借它一句「命中还是没命中」。
    """
    match = matcher.best_match(query, store.load_all())
    if match is not None and match.score >= threshold:
        return match
    return None


def answer_faq(
    query: str,
    *,  # 后面的参数只能关键字传参,不能位置传参
    store: FaqStore,  # 端口类型,适配器实现,静态鸭子类型(结构化子类型)
    matcher: TextMatcher,
    llm: LLMClient,
    cache: Cache | None = None,
    threshold: float = 0.85,
    cache_ttl: int = DEFAULT_CACHE_TTL_S,
) -> FaqAnswer:
    """ "
    输入:问题,各种适配器
    行为(找到返回,找不到继续往后执行):redis->mysql->milvus和大模型
    输出:答案
    """
    # 拼缓存键
    key = _cache_key(query)
    # 直接获取缓存,命中之后返回
    cached = _safe_get(cache, key)
    if cached is not None:
        source, answer = _decode(cached)  # source 跟着一起解出来,不能一律当 faq
        return FaqAnswer(answer=answer, source=source)

    # 命中判定借 match_faq:阈值语义只准有一处实现
    match = match_faq(query, store=store, matcher=matcher, threshold=threshold)
    # 匹配到了,然后拼接缓存键,进行缓存
    if match is not None:
        _safe_set(cache, key, _encode("faq", match.entry.answer), cache_ttl)
        return FaqAnswer(answer=match.entry.answer, source="faq", score=match.score)

    # 调用大模型的流式方法,输入stream.[ChatMessage(角色,内容),ChatMessage()....]
    # 返回生成器,将生成器通过.join()转为字符串
    deltas = list(llm.stream([ChatMessage(role="user", content=query)]))
    text = "".join(delta.text for delta in deltas)
    # 结束原因在最后一帧(适配器的约定);provider 没说就是 None,按「没截断」处理
    truncated = deltas[-1].truncated if deltas else False
    if truncated:
        _logger.warning(
            "模型兜底回答被截断(finish_reason=%s),只拿到 %d 字:%s",
            deltas[-1].finish_reason,
            len(text),
            query,
        )
    # 穿透防御:兜底答案也缓存。否则同一个超纲问题每问一次就烧一次 token。
    # 空答案不缓存 —— 否则空串会被后续查询当成有效命中,用户看到一片空白。
    if text:
        _safe_set(cache, key, _encode("llm", text), cache_ttl)
    return FaqAnswer(answer=text, source="llm", truncated=truncated)
