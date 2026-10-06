"""
页码:把「这段正文在第几页」做成一个**纯函数**,放在 domain 里。

**为什么在 domain 而不是 PDF 适配器**:算页码的是用例层(``chunk_documents`` 拿切分器
给的偏移去查表),而 application 不许 import adapters(分层扫描拦着)。把它留在
``adapters/pdf_files.py`` 里,用例层就只剩两条路:把 PDF 适配器 import 进来(破层),
或者自己再写一遍二分(两处口径迟早会漂 —— 而漂的后果是页码整体错位,静默的)。
所以这里只留「区间表 + 二分」这一件与格式无关的事;**区间表怎么来**还是 PDF 适配器的事。

这一层不认识 PyMuPDF,也不认识 PDF:任何能给出「(起始, 结束, 页码),左闭右开、
首尾相接」的来源都能用(将来接 EPUB / PPT 分页也是这张表)。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

# (起始偏移, 结束偏移, 页码)。左闭右开、按起始偏移递增、首尾相接且覆盖全文。
# 页码从 **1** 开始(与书签目录、与用户看到的页码同一套);没有页码概念的格式给**空元组**,
# 不是"第 0 页" —— 空表让 page_of 一律返回 None,而 0 会被读成一个真实页号。
PageSpans = Sequence[tuple[int, int, int]]


def page_of(spans: PageSpans, offset: int) -> int | None:
    """输入:页码区间表、字符偏移。行为:二分找出它落在哪一段。输出:页码;不知道给 None。

    **越界给 None 而不是夹到首/末页**:偏移越界说明调用方算错了(比如拿父块偏移去查子块的表),
    夹过去会给出一个**看着合法**的页号 —— 那种错查起来最贵。宁可给 None(引用上显示"页码未知")。

    **偏移落在两段之间的 ``\\n\\n`` 上也算 None**:那两个字符是拼页时插进去的分隔符,
    不属于任何一页;它们没有被算进去,恰恰说明这张表是"逐字符对齐"的那一种。
    """
    if offset < 0 or not spans:
        return None
    index = bisect_right([start for start, _end, _page in spans], offset) - 1
    if index < 0:
        return None
    start, end, page = spans[index]
    return page if start <= offset < end else None
