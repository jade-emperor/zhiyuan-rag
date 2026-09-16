"""领域模型:纯数据,不依赖任何第三方 SDK。

domain 是依赖图的叶子 —— 这个包不认识 ports / adapters / cli 里的任何东西。
判断一个类型该不该放这里,问一句:如果明天把所有 LLM 端口都删掉,它还在不在?
ChatMessage 在(会话历史、上下文拼装、评估用例都要用它),所以它属于 domain。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]

Source = Literal["faq", "llm"]


# 这个装饰器,frozen=Tru参数表示这个类构建的对象不可变,不可动态添加属性.slots=True表示优化缓存
@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息。

    只用 dataclass,不用 pydantic:domain 不该依赖校验/序列化框架,
    这样它可以在任何地方被实例化、比较、当字典键,不需要框架参与。
    M2 要给 FastAPI 返回时,在 cli 层做 domain -> schema 的转换。

    也不要在这里加 to_langchain() 之类的方法 —— 那会把 langchain 拉进 domain。
    转换是适配器的职责(见 adapters/llm_deepseek.py 的 to_sdk_messages)。
    """

    # 声明Role只能是system、user、assistant三种字面量
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class FaqEntry:
    """一条问答对,对应mysql中的行数据"""

    id: int
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class FaqMatch:
    """问答对和和对应的匹配分数"""

    entry: FaqEntry
    score: float


@dataclass(frozen=True, slots=True)
class FaqAnswer:
    """最终答案以及来源"""

    answer: str
    source: Source
    score: float | None = None
