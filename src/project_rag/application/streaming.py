"""
协议(所有流式入口统一,帧类型是 ``domain.QaEvent``)：

    sources  0..1 帧   知识库通道的引用清单(前端可以先渲染来源)
    token    0..n 帧   答案文本 —— **答案一律走 token 帧**(标准答案、拒答话术都在内),
                       所以消费方只要「打印 token」,不用为「这句话从哪来」写分支
    done     恰好 1 帧 收尾:answer(拼接结果)/ channel / citations / faq_match /
                       refused / truncated

"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from project_rag.domain import QaEvent
from project_rag.observability import StreamTiming


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


def _close(events: Iterator[QaEvent]) -> None:
    """输入:上游帧流。行为:它有 ``close`` 就调一下。输出:无。

    为什么必须显式做:``for`` 循环不会替我们关上游,而「客户端断开」这件事**上游要知道** ——
    合流入口正是靠生成器被关闭来判断「这一轮不该入库」(半截答案进历史比没有更糟)。
    """
    close: Callable[[], None] | None = getattr(events, "close", None)
    if close is not None:
        close()


def timed(events: Iterator[QaEvent], timing: StreamTiming) -> Iterator[QaEvent]:
    """输入:帧流 + 计时账本。行为:逐帧透传,同时记账;收尾/中断/出错时记一条 latency 日志。输出:同一个帧流。

    三条实现上的注意:

    1. **只观察、不改写**:帧的内容/顺序/条数一个字节都不动(验收拿未包装的流逐帧比对);
    2. **逐帧透传,不许预取**:上游被取走第一帧时只该产出第一帧 —— 写成 ``list(events)``
       就等于「先全部生成完再交出来」,「边生成边出」和 TTFT 一起没了;
    3. **中断也要留账**:客户端断开时生成器被 ``close()``,走 ``GeneratorExit`` 分支 ——
       「慢到用户跑掉」的那些请求恰恰最该看,半截的 TTFT/tokens 比没有有用。
       这时顺手把 ``close()`` 传给上游(见 ``_close``),让它照旧做「不入库」的清理。
    """
    try:
        for event in events:
            if event.kind == "token":
                timing.token(event.text)  # 空帧不算 token,规则在 StreamTiming 里
            elif event.kind == "sources":
                timing.mark("sources")  # 检索那一段结束,前端此刻就能渲染来源
            elif event.kind == "done":
                timing.finish(
                    channel=event.channel,
                    refused=event.refused,
                    truncated=event.truncated,
                )
            yield event
    except GeneratorExit:
        _close(events)
        raise
    finally:
        timing.emit()
