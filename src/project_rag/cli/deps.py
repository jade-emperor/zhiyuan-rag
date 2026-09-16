"""
组合根:把「怎么造依赖」收在一个可注入的数据结构里。

装配完就不该再被改,而每个字段默认 None 即「不注入就不造」。

惰性:字段全是工厂,不是实例。所以 chat 不会因为 MySQL 挂了就跑不起来,
--help 更不会去连任何东西。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from project_rag.ports import Cache, FaqStore, LLMClient, TextMatcher

DEFAULT_FAQ_THRESHOLD = 0.85


# 这个类实例创建完成以后,就不再需要修改了,所以用 frozen=True,slots=True
@dataclass(frozen=True, slots=True)
class Deps:
    """一次进程运行需要的全部外部依赖,全部以工厂形式持有。"""

    # 类型注解,一个可调用的对象(如函数):Callable[[参数类型列表], 返回类型]
    llm_factory: Callable[[], LLMClient] | None = None
    faq_store_factory: Callable[[], FaqStore] | None = None
    matcher_factory: Callable[[], TextMatcher] | None = None
    cache_factory: Callable[[], Cache | None] | None = None  # 返回 None = 无缓存
    faq_threshold: float = DEFAULT_FAQ_THRESHOLD


def build_real_deps() -> Deps:
    """真实装配:读一次 Settings,把四个工厂装好。

    工厂里只捕获 settings、不建连接 —— 真正的连接发生在命令用到它的时候。
    适配器延迟 import:否则 --help 会把 langchain / pymysql / redis / jieba 全拖进来。

    核心:这些依赖会统一交给args.Deps并且传给所有子命令,子命令再根据自身需要创建策略类

    """
    from project_rag.adapters import (
        DeepSeekLLM,
        MySQLFaqStore,
        # RedisCache,
        # SimpleTextMatcher,
    )
    from project_rag.config import get_settings

    settings = get_settings()
    # lambda是为了构建闭包形式,也可以在前面def定义,但是不够优雅
    # 这些变量都指向函数和类而不是直接实现,Deos.属性()才真正执行的,惰性加载策略类(懒汉模式)
    return Deps(
        llm_factory=lambda: DeepSeekLLM(settings.llm),
        faq_store_factory=lambda: MySQLFaqStore(settings.mysql, settings.faq),
        # matcher_factory=SimpleTextMatcher,  # 类本身就是无参工厂
        # cache_factory=lambda: RedisCache(settings.redis),
        # faq_threshold=settings.faq.threshold,
    )
