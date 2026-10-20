"""DeepSeek 适配器:全项目唯一 import langchain 的地方。

两件事都只在这里做:领域类型 <-> SDK 类型的转换、发请求。
异常不在这里吞 —— 翻译成人话是 cli 层的事。

**思考模式**:配置里只有「意图」(`llm.thinking`:auto/off/low/high/max),
翻成 provider 字段是**适配器的事** —— 字段名(`reasoning_effort`)、取值(`none`)
都是厂商/SDK 知识,漏进 config 或用例就等于把 provider 焊在了上层。
官方文档:<https://api-docs.deepseek.com/guides/thinking_mode/>
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek

from project_rag.config import LLMConfig
from project_rag.domain import ChatMessage, LlmDelta, LlmUsage
from project_rag.ports import LLMClient

# 配置里的思考模式 -> provider 的 reasoning_effort。
# OpenAI 格式下它**同时管开关与强度**:none 关思考,low/high/max 开启并指定强度;
# 不传 = 服务端默认(开启、high)。实测（deepseek-flash,同一个问题):
#   不传     -> 64 token 预算全被思考吃掉,一个内容字都没出
#   off      -> **0 个思考帧**,首个内容字 504ms
#   low/high -> 仍然先思考(思考帧 64/64,首个思考帧 645/723ms)
_EFFORT_BY_THINKING = {"off": "none", "low": "low", "high": "high", "max": "max"}


class ThinkingKwargs(TypedDict, total=False):
    """``thinking_kwargs()`` 的返回形状。

    用 TypedDict 而不是 ``dict[str, str]``:后者一展开(``**kwargs``)就会被类型检查器
    逐个去对 ``ChatDeepSeek.__init__`` 的几十个参数,报一屏「str 不能赋给 BaseCache」。
    TypedDict 只声明我们真的会传的那一个键,检查照样生效。
    """

    reasoning_effort: str


def thinking_kwargs(thinking: str) -> ThinkingKwargs:
    """输入:配置里的思考模式(auto/off/low/high/max)。行为:翻成构造客户端用的参数。输出:关键字参数字典。

    ``auto`` 给**空字典** = 什么都不发,沿用服务端默认(开启、high)——
    这样「不动配置」的行为与加这个开关之前逐字节一致,不会凭空换掉默认行为。

    **为什么不用文档里那个 ``thinking={"type": "disabled"}``**:官方说明它得走
    ``extra_body``(langchain 的 ``model_config`` 是 ``extra="ignore"``——字段名写错会被
    **静默吞掉**,连个警告都没有)。``reasoning_effort`` 是 langchain 的真字段
    (``str | None``),拼错会立刻构造失败,而且它一个参数就同时管开关与强度。
    """
    effort = _EFFORT_BY_THINKING.get(thinking)
    return {} if effort is None else {"reasoning_effort": effort}


# 角色 -> langchain 消息类,字典分派省掉 if/elif 链
_LANGCHAIN_MESSAGE = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}


def _tokens(value: Any) -> int | None:
    """输入:provider 给的 token 数(可能是 int/str/None)。行为:转成整数。输出:int;拿不到给 None。

    **拿不到给 None 而不是 0**:0 会被算成"这次不要钱",None 才会让报表说"没量到"。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    """输入:一串候选值。行为:取第一个**不是 None** 的。输出:那个值(全 None 给 None)。

    用它而不是 ``a or b or c``:缓存命中数``0`` 是个**有效值**(这一轮一个都没命中),
    而 ``0 or …`` 会把它跳过去,读成"provider 没回报"。
    """
    for value in values:
        if value is not None:
            return value
    return None


def _cached_tokens(raw: dict, response: dict) -> int | None:
    """输入:标准用量字典、原始 ``response_metadata``。行为:三种形状里找"命中缓存的输入 token"。

    三个来源都认(换 provider / 换 SDK 版本时只会有其中一个):

    * ``input_token_details.cache_read`` —— langchain 的标准形状(OpenAI 系走这条);
    * ``prompt_tokens_details.cached_tokens`` —— OpenAI 协议的原始嵌套;
    * ``prompt_cache_hit_tokens`` —— **DeepSeek 自己的字段**(在 ``token_usage`` 那一层)。

    拿不到给 ``None``(而不是 0):这一档的单价与未命中差 50 倍,编一个 0 等于按 1 元/百万
    记账 —— 报表必须能说"这一段我不知道"。
    """
    details = _first(
        raw.get("input_token_details"),
        raw.get("prompt_tokens_details"),
        response.get("token_usage", {}).get("prompt_tokens_details")
        if isinstance(response.get("token_usage"), dict)
        else None,
    )
    if not isinstance(details, dict):
        details = {}
    token_usage = response.get("token_usage")
    raw_usage = token_usage if isinstance(token_usage, dict) else {}
    return _tokens(
        _first(
            details.get("cache_read"),
            details.get("cached_tokens"),
            raw_usage.get("prompt_cache_hit_tokens"),
            raw.get("prompt_cache_hit_tokens"),
        )
    )


def _usage_of(chunk: object, fallback_model: str) -> LlmUsage | None:
    """输入:一个 SDK chunk + 配置里的模型名。行为:从它身上取 provider 回报的用量。输出:``LlmUsage``;没有给 None。

    两个来源都认(实测:langchain 把它规整到 ``usage_metadata``,但换 provider/换 SDK 版本时
    可能只剩原始 ``token_usage``):``chunk.usage_metadata``(标准形状)与
    ``chunk.response_metadata["token_usage"]``(OpenAI 协议的原始形状)。

    模型名取 provider 回报的那个(``model_name``):**单价是按模型配的**,而"我们要求它用哪个"
    与"它实际用了哪个"不一定一样(代理转发时尤其如此)。两处都没有才回落到配置里的名字。

    缓存命中数走 ``_cached_tokens``:标准形状里它藏在 ``input_token_details`` 里,
    而 DeepSeek 把它放在原始 ``token_usage.prompt_cache_hit_tokens`` —— 后者 langchain 不一定往上搬,
    所以两个地方都得看。
    """
    metadata = getattr(chunk, "usage_metadata", None) or {}
    response = getattr(chunk, "response_metadata", None) or {}
    raw: dict = dict(metadata) if metadata else dict(response.get("token_usage") or {})
    if not raw:
        return None
    input_tokens = _tokens(_first(raw.get("input_tokens"), raw.get("prompt_tokens")))
    output_tokens = _tokens(_first(raw.get("output_tokens"), raw.get("completion_tokens")))
    if input_tokens is None and output_tokens is None:
        # 有 usage 这个键、但里面什么都没有 —— 当作"没回报",别编一个 0 出来
        return None
    model = str(response.get("model_name") or response.get("model") or fallback_model)
    return LlmUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=_cached_tokens(raw, response),
    )


def _content_of(message: ChatMessage) -> str | list[dict[str, Any]]:
    """输入:领域消息。行为:有图时转成 OpenAI 风格的多模态 content 列表。输出:字符串或内容块列表。

    **没有图时必须原样返回字符串**:纯文本路径的提示词与加图片之前逐字节一致
    (提示词契约按字符串比)。有图时文字放第一块,图片按顺序跟在后面 ——
    模型看到的顺序就是调用方拼好的引用顺序。
    """
    if not message.images:
        return message.content
    return [
        {"type": "text", "text": message.content},
        *[
            {"type": "image_url", "image_url": {"url": url}}
            for url in message.images
        ],
    ]


def to_sdk_messages(messages: Sequence[ChatMessage]) -> list:
    """
    将领域消息 -> langchain 需要的消息。

    转换只属于适配器:domain 里绝不写 to_langchain(),否则依赖方向就反了。

    输入:领域消息序列[领域值对象中的role,content,images]
    行为:按 role 分派,转成对应的 langchain 消息类;有图时 content 用多模态列表。
    输出:langchain 消息列表[SystemMessage/HumanMessage/AIMessage(content)]
    """
    return [
        _LANGCHAIN_MESSAGE[m.role](content=_content_of(m))
        for m in messages
    ]


class DeepSeekLLM(LLMClient):
    """满足 LLMClient 端口(结构化子类型,不需要显式继承)。"""

    def __init__(self, config: LLMConfig) -> None:
        """输入:LLM 配置。行为:建 ChatDeepSeek 客户端,开启流式。输出:无。"""
        self._model = config.model
        self._client = ChatDeepSeek(
            model=config.model,
            # 密钥只从 settings 来并显式传入,不让 SDK 自己去读环境变量,否则又多一个配置源
            api_key=config.deepseek_api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout_s,  # timeout 是 request_timeout 的 alias
            streaming=True,
            # 让 provider 在流末尾**多回一帧用量**(OpenAI 协议的 ``stream_options.include_usage``):
            # 这是"这次花了多少钱"的唯一真源 —— 自己按字数估的 token 数永远和账单对不上。
            # 关掉它(``llm.stream_usage: false``)账本上就是 ``-``,报表会如实说"没量到",
            # 而不是编一个 0。有些代理不认这个参数(400),那时就是靠这个开关回退。
            stream_usage=config.stream_usage,
            **thinking_kwargs(config.thinking),  # auto 时是空字典 = 不发参数
        )  # 这里获得deepseek的策略类

    # 调用大模型,输入
    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[LlmDelta]:
        """输入:领域消息序列。行为:转换格式后调模型,逐帧取 content / reasoning_content / finish_reason。输出:帧迭代器。

        **不丢 finish_reason**(丢过,结果用户拿到半截答案却没有任何提示 —— 比明说答不出来更误导):
        收尾时补一帧 ``last=True``,把「这次是怎么结束的」明确交出去。

        **也不丢 reasoning_content**:思考模型在正文之前先吐一串思考,langchain 把它
        放在 ``chunk.additional_kwargs["reasoning_content"]``(**不在** ``chunk.content`` 里 ——
        实测）。它作为 ``reasoning_text`` 交出去,与 ``text`` 分开:上层要靠它数
        「这一轮想没想、想了多久」,但答案里一个字都不能有它(见 ``domain.LlmDelta``)。
        一帧同时带思考和正文时按「先思考后正文」发两帧 —— provider 真这么做时顺序也是对的。

        四个实测细节（deepseek-flash):
        ① finish_reason 在 ``chunk.response_metadata`` 里,不在 ``chunk.finish_reason``
           (langchain 的 AIMessageChunk 根本没那个属性);
        ② provider 在带 finish_reason 的那帧**之后**还会再发一个空帧(无内容、无元数据)——
           那种帧没有信息,直接丢掉;否则消费方会以为「最后一帧没有结束原因」;
        ③ ``chunk.content`` 可能是 None(收尾帧),``str(None)`` 会把字符串 "None" 混进答案;
        ④ 思考帧的 ``content`` 是空串,正文帧的 ``additional_kwargs`` 里没有 reasoning ——
           两个字段不会同时有值,但也不该假设它一定不会(见上)。

        **用量(含缓存命中数)挂在收尾帧上**:它可能在任意一帧(带 finish_reason 的那帧、
        或它后面那个只有用量的空帧),所以逐帧看一眼、后到的覆盖先到的 —— 最后那个才是完整的一次调用。
        """
        reason: str | None = None
        usage: LlmUsage | None = None
        for chunk in self._client.stream(to_sdk_messages(messages)):
            text = str(chunk.content or "")
            thought = str((chunk.additional_kwargs or {}).get("reasoning_content") or "")
            got = (chunk.response_metadata or {}).get("finish_reason")
            if got is not None:
                reason = str(got)
            usage = _usage_of(chunk, self._model) or usage
            if thought:
                yield LlmDelta(reasoning_text=thought)
            if text:
                yield LlmDelta(text=text)
        # 恰好一帧 last:只说「流结束了、原因是这个、这次用了多少」(provider 没说就给 None)
        yield LlmDelta(finish_reason=reason, last=True, usage=usage)
