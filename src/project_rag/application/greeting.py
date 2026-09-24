"""
问候拦截:日常寒暄不进检索、也不进历史(讲义 ⑥ 6.3 的 ``GREETING_PATTERNS``)。

放 application 而不是 api:这是业务规则(什么算寒暄、答什么),HTTP 与 WebSocket
两条入口都得用同一份 —— 写在 ``app.py`` 里,第二条入口就会各抄一遍。

**与讲义的一处刻意不同**:讲义用 ``re.search``,于是「你好，帮我看看这个报错」
会被当成寒暄拦掉 —— 而那正是最需要检索的一类提问。这里要求「整句只是寒暄」
(正则里的 ``^...$``),宁可漏拦,不可错拦。
"""

from __future__ import annotations

import re

# 每组是一个「寒暄词」
_GREETINGS = (
    r"你好|您好|哈喽|哈啰|嗨|hi|hello|hey",
    r"早上好|中午好|下午好|晚上好|早安|晚安",
    r"在吗|在不在|有人吗",
    r"谢谢|多谢|感谢|thanks|thank you",
    r"再见|拜拜|bye",
)
# 整句只能是寒暄,后面可以跟标点/空白,不能再有别的内容
_PUNCT = r"[\s，,。.!！?？~～]*"
_GREETING_RE = re.compile(rf"^(?:{'|'.join(_GREETINGS)}){_PUNCT}$", re.IGNORECASE)

GREETING_REPLY = (
    "你好，我是智能问答助手。"
    "基于内部操作手册为你解答流程,政策,或技术问题。"
    "请直接提问，我会附上来源；找不到时也会如实说明。"
)


def match_greeting(text: str) -> str | None:
    """输入:用户原话。行为:整句只是寒暄时给出模板回复。输出:回复话术;不是寒暄给 None。"""
    if _GREETING_RE.match(text.strip()):
        return GREETING_REPLY
    return None
