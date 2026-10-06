"""
账本落表:把带 ``extra={"ledger": …}`` 的日志记录转交给一个 sink(通常是 MySQL 适配器)。

**为什么挂在 logging 上,而不是在每个调用点各调一次 store**:账本的出口只有一处
(``StreamTiming.emit``),但它会被 CLI / HTTP / WebSocket 三条路径走到 —— 在调用点各接一次,
迟早漏一个,而且**漏了不报错**,只是那张表里少一类请求(那正是最难发现的错误)。
挂成 handler 之后规矩只剩一条:**记了账就落表**。

**为什么 handler 只认 ``extra`` 里的字典、不去解析日志文本**:给人看的文本会被格式化、
加前缀、甚至被别的 handler 改写;结构化数据走 ``extra`` 是 logging 自己的机制,
handler 因此不需要懂账本的口径(账本加字段时这里一行都不用改)。
副作用:任何 logger 只要带这个键就会被落表 —— 这正是我们要的。

**失败只警告**:观测是旁路,MySQL 写不进去不该让用户的请求失败。这里交给
``logging.Handler.handleError``:它尊重 ``logging.raiseExceptions``,既不吞掉问题,
也不在请求路径上抛异常。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

# 账本记录在 LogRecord 上的属性名。**这是产品与落表之间的接口**,改它要同步改两边。
LEDGER_KEY = "ledger"


class LedgerHandler(logging.Handler):
    """把带 ``extra={"ledger": {…}}`` 的记录交给 ``sink``;别的记录一律不管。"""

    def __init__(
        self, sink: Callable[[Mapping[str, str]], None], level: int = logging.INFO
    ) -> None:
        """输入:sink(接一行字段字典)、级别。行为:建 handler。输出:无。"""
        super().__init__(level=level)
        self._sink = sink
        # 验收要数这两个:转交了几条、失败了几条。**不是调试字段** ——
        # 「落表静默失效」只能靠它们发现(日志里看不到异常时才最危险)。
        self.rows = 0
        self.failures = 0

    def emit(self, record: logging.LogRecord) -> None:
        """输入:一条日志记录。行为:有 ``ledger`` 就转交,异常只走 handleError。输出:无。"""
        row = getattr(record, LEDGER_KEY, None)
        if not isinstance(row, dict):
            return
        try:
            self._sink(row)
            self.rows += 1
        except Exception:  # noqa: BLE001 - 观测是旁路:落表失败不许影响请求
            self.failures += 1
            self.handleError(record)


def attach_ledger_sink(
    sink: Callable[[Mapping[str, str]], None],
    *,
    logger_name: str = "project_rag",
    level: int = logging.INFO,
) -> LedgerHandler:
    """输入:sink、挂到哪个 logger、级别。行为:**幂等**挂载(先摘旧的同类 handler)。输出:handler。

    为什么挂 ``project_rag`` 而不是 root:root 上还流着 uvicorn 等别人的记录,
    挂在那里等于"谁都能落表";挂在自己这棵子树上,「哪些记录会进表」才是确定的。

    幂等很重要:启动预热、测试夹具、重复 ``configure_logging()`` 都可能走到这里,
    挂两次就会把同一行账写两遍(表里出现重复行,而且很难看出是重复)。
    """
    logger = logging.getLogger(logger_name)
    for existing in list(logger.handlers):
        if isinstance(existing, LedgerHandler):
            logger.removeHandler(existing)
    handler = LedgerHandler(sink, level=level)
    logger.addHandler(handler)
    return handler
