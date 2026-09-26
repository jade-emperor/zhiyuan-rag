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
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek

from project_rag.config import LLMConfig
from project_rag.domain import ChatMessage, LlmDelta
from project_rag.ports import LLMClient

# 配置里的思考模式 -> provider 的 reasoning_effort。
# OpenAI 格式下它**同时管开关与强度**:none 关思考,low/high/max 开启并指定强度;
# 不传 = 服务端默认(开启、high)。实测(2026-09-26,deepseek-flash,同一个问题):
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


def to_sdk_messages(messages: Sequence[ChatMessage]) -> list:
    """
    将领域消息 -> langchain 需要的消息。

    转换只属于适配器:domain 里绝不写 to_langchain(),否则依赖方向就反了。

    输入:领域消息序列[领域值对象中的role,content]
    行为:按 role 分派,转成对应的 langchain 消息类。
    输出:langchain 消息列表[SystemMessage/HumanMessage/AIMessage(content)]
    """
    # 举个直观的例子,这里将[领域值对象中的role,content]->[role,content转为SystemMessage\HumanMessage\AIMessage(content)]
    return [_LANGCHAIN_MESSAGE[m.role](content=m.content) for m in messages]


class DeepSeekLLM(LLMClient):
    """满足 LLMClient 端口(结构化子类型,不需要显式继承)。"""

    def __init__(self, config: LLMConfig) -> None:
        """输入:LLM 配置。行为:建 ChatDeepSeek 客户端,开启流式。输出:无。"""
        self._client = ChatDeepSeek(
            model=config.model,
            # 密钥只从 settings 来并显式传入,不让 SDK 自己去读环境变量,否则又多一个配置源
            api_key=config.deepseek_api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout_s,  # timeout 是 request_timeout 的 alias
            streaming=True,
            **thinking_kwargs(config.thinking),  # auto 时是空字典 = 不发参数
        )  # 这里获得deepseek的策略类

    # 调用大模型,输入
    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[LlmDelta]:
        """输入:领域消息序列。行为:转换格式后调模型,逐帧取 content / reasoning_content / finish_reason。输出:帧迭代器。

        **不再丢 finish_reason**(M4 实测到过半截答案,而它就是这么丢的):
        收尾时补一帧 ``last=True``,把「这次是怎么结束的」明确交出去。

        **也不再丢 reasoning_content**(M5.8):思考模型在正文之前先吐一串思考,langchain 把它
        放在 ``chunk.additional_kwargs["reasoning_content"]``(**不在** ``chunk.content`` 里 ——
        实测 2026-09-26)。它作为 ``reasoning_text`` 交出去,与 ``text`` 分开:上层要靠它数
        「这一轮想没想、想了多久」,但答案里一个字都不能有它(见 ``domain.LlmDelta``)。
        一帧同时带思考和正文时按「先思考后正文」发两帧 —— provider 真这么做时顺序也是对的。

        四个实测细节(2026-09-23 / 2026-09-26,deepseek-flash):
        ① finish_reason 在 ``chunk.response_metadata`` 里,不在 ``chunk.finish_reason``
           (langchain 的 AIMessageChunk 根本没那个属性);
        ② provider 在带 finish_reason 的那帧**之后**还会再发一个空帧(无内容、无元数据)——
           那种帧没有信息,直接丢掉;否则消费方会以为「最后一帧没有结束原因」;
        ③ ``chunk.content`` 可能是 None(收尾帧),``str(None)`` 会把字符串 "None" 混进答案;
        ④ 思考帧的 ``content`` 是空串,正文帧的 ``additional_kwargs`` 里没有 reasoning ——
           两个字段不会同时有值,但也不该假设它一定不会(见上)。
        """
        reason: str | None = None
        for chunk in self._client.stream(to_sdk_messages(messages)):
            text = str(chunk.content or "")
            thought = str((chunk.additional_kwargs or {}).get("reasoning_content") or "")
            got = (chunk.response_metadata or {}).get("finish_reason")
            if got is not None:
                reason = str(got)
            if thought:
                yield LlmDelta(reasoning_text=thought)
            if text:
                yield LlmDelta(text=text)
        # 恰好一帧 last:只说「流结束了、原因是这个」(provider 没说就给 None)
        yield LlmDelta(finish_reason=reason, last=True)
