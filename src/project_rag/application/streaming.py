"""
流式回答的**帧协议**与收拢器。

协议(所有流式入口统一,帧类型是 ``domain.QaEvent``)::

    sources  0..1 帧   知识库通道的引用清单(前端可以先渲染来源)
    token    0..n 帧   答案文本 —— **答案一律走 token 帧**(标准答案、拒答话术都在内),
                       所以消费方只要「打印 token」,不用为「这句话从哪来」写分支
    done     恰好 1 帧 收尾:answer(拼接结果)/ channel / citations / faq_match /
                       refused / truncated

恒等式:``"".join(t.text for t in token 帧).strip() == done.answer``。

**为什么把协议单独放一处**:两个生产方(``stream_rag`` / ``stream_question``)与两个消费方
(CLI、M5.4 的 WebSocket)必须对同一套帧达成一致;而 ``done_of`` 是非流式入口复用的桥 ——
``answer_rag`` / ``answer_question`` 就是「把流收拢成一句答案」,所以**流式与非流式
逐字一致这件事是结构上成立的**,不是靠两边小心地写两遍。
"""

from __future__ import annotations

from collections.abc import Iterable

from project_rag.domain import QaEvent


def done_of(events: Iterable[QaEvent]) -> QaEvent:
    """输入:帧序列。行为:消费整个流,取出收尾帧。输出:``kind == "done"`` 的那一帧。

    没有收尾帧 = 生产方违约(中途抛错会走异常,不会走到这里)。这里直接报错,
    别让调用方拿到一个「看起来正常」的半截答案。
    """
    done: QaEvent | None = None
    for event in events:
        if event.kind == "done":
            done = event
    if done is None:
        raise RuntimeError("流里没有收尾帧(kind='done')—— 帧协议要求恰好一帧")
    return done
