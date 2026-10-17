"""
用例:合流问答 —— FAQ 通道优先,未命中走知识库通道;带会话时顺带维护对话历史。

为什么要有这一层:两条通道各自都答得对,但「谁来答」是个**业务决定** ——
FAQ 表里是人工维护的标准答案,它命中时就是最可信的答案;命不中才值得付一次
检索 + 生成的钱。这个决定放在用例层而不是命令行/接口层,是因为 CLI、
HTTP 接口、评估脚本都要走同一条路由,谁都不该自己再抄一遍。

"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

# 数据类
from project_rag.domain import (
    Citation,
    ConversationTurn,
    QaAnswer,
    QaEvent,
    RagAnswer,
    snapshot_citations,
)

# 创建日志的方法
from project_rag.observability import StreamTiming, get_logger

# 端口中的各种抽象类(接口)
from project_rag.ports import ConversationStore, FaqStore, TextMatcher

# 同包用相对导入:项目的分层扫描规则把「绝对导入 project_rag.application.*」
# 当成跨层依赖拦下来(它只看模块前缀),相对导入才表达得出「同一个包内」
from .answer_faq import match_faq
from .streaming import done_of, timed

_logger = get_logger("application.answer_question")

# 知识库通道的形状:(历史轮次, 这一轮的账本) -> 帧序列。
#
# **账本为什么是参数**:账本由合流入口开(它才是"一次请求"的那一层),知识库通道把
# 检索/生成的打点记在**同一行**上。两个各开一条的话,一次请求会落两行账 ——
# 报表的请求量、拒答率、通道计数全部翻倍,而"翻倍"在报表上看着只是"量比想象的大"。
KnowledgeChannel = Callable[[Sequence[ConversationTurn], StreamTiming], Iterator[QaEvent]]


@dataclass(frozen=True, slots=True)
class Session:
    """一次带会话的提问:历史存哪、是哪一段、读写几轮、最多带多少字符。

    为什么绑成一个对象:光有一个 ``session_id``、没有 store 就没法读写;有 store
    却没有 id 则是半成品。绑起来之后,那种半成品在类型上就构造不出来,用例里也就
    不用再写「这两个参数必须同时给」的运行时检查。

    ``rounds`` 与 ``max_chars`` 是滑动窗口的两半:前者限轮数(读与写共用),
    后者限**整个历史块的大小**(只影响读进提示词的那一份,不影响库里留几轮)。
    """

    store: ConversationStore
    session_id: str
    rounds: int = 5
    max_chars: int = 0

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id 不能为空 —— 否则所有匿名提问会挤进同一段历史")
        if self.rounds <= 0:
            raise ValueError(f"rounds 必须为正数,收到 {self.rounds}")
        if self.max_chars < 0:
            raise ValueError(f"max_chars 不能为负(0 = 不限),收到 {self.max_chars}")


def window_history(
    history: Sequence[ConversationTurn], *, max_chars: int = 0
) -> tuple[ConversationTurn, ...]:
    """输入:历史轮次(时间正序)、整个历史块的字符预算(0 = 不限)。

    行为:从**最新**一轮往回装,装不下的**整轮丢掉**。输出:仍然时间正序的那一段。

    三条刻意的口径:

    * **整轮丢,不截断半轮** —— 半个答案进了提示词,模型会把断句当结论(上下文里读不出
      "这里被我剪了");而且提示词块是按轮渲染的,截断还会破坏 ``Q: … / A: …`` 的形状;
    * **至少保留最新一轮**,哪怕它自己就超预算:窗口的作用是"给模型上一句上下文",
      把它减成空等于静默失忆 —— 那种错在对话里表现为"答非所问",查起来极难;
    * **预算按字符算**(问题 + 答案的字符数),不是 token:精确、可复核、换模型不变。
    """
    if max_chars <= 0 or not history:
        return tuple(history)
    kept: list[ConversationTurn] = []
    used = 0
    for turn in reversed(history):
        size = len(turn.question) + len(turn.answer)
        if kept and used + size > max_chars:
            break
        kept.append(turn)
        used += size
    kept.reverse()
    return tuple(kept)


def _history(session: Session | None) -> tuple[ConversationTurn, ...]:
    """输入:会话(可为 None)。行为:读最近 rounds 轮,再按字符预算滑一次窗口。输出:时间正序的历史。

    只有**要走知识库通道**时才调它 —— 命中 FAQ 那条分支不读历史。

    **读是两段**:先让存储按 ``rounds`` 取(那是 SQL 那一层的窗口,也是库里保留的轮数),
    再在用例层按 ``max_chars`` 从最新往回裁(那一层存储不该管:它是提示词的成本口径,
    换个存储也成立)。裁掉了几轮要记一条日志 —— 否则"模型怎么忘了"只能靠猜。
    """
    if session is None:
        return ()
    loaded = tuple(session.store.recent(session.session_id, limit=session.rounds))
    kept = window_history(loaded, max_chars=session.max_chars)
    if len(kept) < len(loaded):
        _logger.info(
            "历史按字符预算裁剪:%d 轮 → %d 轮(预算 %d 字符;丢掉最旧的 %d 轮)",
            len(loaded),
            len(kept),
            session.max_chars,
            len(loaded) - len(kept),
        )
    return kept


def _record(
    session: Session | None,
    question: str,
    answer: str,
    citations: Sequence[Citation] = (),
) -> None:
    """输入:会话(可为 None)、问题、答案、这一轮的引用。行为:把这一轮记下来(拒答也记)。输出:无。

    **写历史只有这一个收口**:两条通道都从这里过。放在用例层而不是命令层,
    是因为多一个入口(FastAPI)就多一次「忘了写」的机会。

    **引用一起存**(``snapshot_citations``):答案正文里的 ``[1]`` 要是没有卡片可点,
    等于没有出处 —— 而切回旧会话正是"想回看上次的出处"最常见的入口。
    快照在这里拍,是因为**只有这一层同时握着答案和它的证据链**。
    """
    if session is None:
        return
    session.store.record(
        session.session_id,
        ConversationTurn(
            question=question,
            answer=answer,
            citations=snapshot_citations(citations),
        ),
        keep=session.rounds,
    )


def stream_question(
    query: str,
    *,
    faq_store: FaqStore,
    matcher: TextMatcher,
    knowledge: KnowledgeChannel,
    faq_threshold: float = 0.85,
    session: Session | None = None,
    thinking: str | None = None,
) -> Iterator[QaEvent]:
    """输入/输出同 ``_question_frames``,额外做一件事:**计时并记一条 latency 日志**。

    **账本在路由之前就开**,所以 ``ttft_ms`` 里含 FAQ 匹配那一段 —— 那正是用户在等的时间。
    FAQ 命中时 ``knowledge`` 一次都不会被调,这条账照样有:它证明「便宜通道有多便宜」。

    **一次请求一条账**:知识库通道拿到的是**同一个** ``timing``,它的检索/生成打点
    记在同一行上(见 ``KnowledgeChannel``)。

    ``thinking`` 同 ``stream_rag``:给日志用的静态标签,由组合根从 config 读出来交进来。
    FAQ 命中时账里只有这一条,不带的话「这一轮按哪一档跑的」就查不到了。
    """
    timing = StreamTiming(label="qa", thinking=thinking)
    yield from timed(
        _question_frames(
            query,
            faq_store=faq_store,
            matcher=matcher,
            knowledge=knowledge,
            faq_threshold=faq_threshold,
            session=session,
            timing=timing,
        ),
        timing,
    )


def _question_frames(
    query: str,
    *,
    faq_store: FaqStore,
    matcher: TextMatcher,
    knowledge: KnowledgeChannel,
    faq_threshold: float = 0.85,
    session: Session | None = None,
    timing: StreamTiming,
) -> Iterator[QaEvent]:
    """输入:问题 + FAQ 侧的端口与阈值 + **知识库通道的一次调用**(收历史、吐帧)+ 可选会话 + 计时账本。

    行为:先问 FAQ 命中没有(不碰缓存、不调 LLM);
          命中 → 把标准答案当一帧 token 发出去(**不是流出来的,但以同样的方式交付**),
                 ``knowledge`` 一次都不会被调用,也不读历史;
          未命中 → 读最近几轮历史 → 转发知识库通道的帧(token 边到边发)→
                   自己补一帧 done(知识库不知道自己是不是「兜底」,channel 由本用例填);
          两条路都会把这一轮记进会话(带了会话的话)。
    输出:帧序列,恰好一帧 done 收尾。

    **历史落在 done 之后才写**:客户端中途断开时生成器被关闭,这一轮就不入库 ——
    半截答案进历史比没有更糟(下一轮会拿它当「上文」)。

    **账本交下去、不另开**:``knowledge`` 收到的就是这一轮的 ``timing``(见 ``KnowledgeChannel``)。

    **这是内部的帧生产者**:公开入口是 ``stream_question``(它包了一层计时/日志);
    这里只多一件事 —— 路由一有结论就给 ``timing`` 打 ``faq`` 点。
    """
    match = match_faq(query, store=faq_store, matcher=matcher, threshold=faq_threshold)
    timing.mark("faq")
    if match is not None:
        # 命中就不做检索:标准答案不需要模型参与,也不该为它付一次嵌入 + 检索的钱
        _logger.info("FAQ 通道命中(score=%.4f),不做检索: %s", match.score, query)
        _record(session, query, match.entry.answer)
        yield QaEvent(kind="token", text=match.entry.answer)
        yield QaEvent(
            kind="done", answer=match.entry.answer, channel="faq", faq_match=match
        )
        return

    _logger.info("FAQ 通道未命中,转知识库通道: %s", query)
    done: QaEvent | None = None
    for event in knowledge(_history(session), timing):
        if event.kind == "done":
            done = event  # 换成自己那一帧再发,不直接转发
            continue
        yield event
    if done is None:
        raise RuntimeError("知识库通道的流没有收尾帧 —— 帧协议要求恰好一帧 done")

    if done.refused:
        _logger.info("知识库通道拒答(没有检索到相关资料): %s", query)
    _record(session, query, done.answer, done.citations)
    yield QaEvent(
        kind="done",
        answer=done.answer,
        channel="rag",
        citations=done.citations,
        refused=done.refused,
        truncated=done.truncated,
    )


def answer_question(
    query: str,
    *,
    faq_store: FaqStore,
    matcher: TextMatcher,
    knowledge: KnowledgeChannel,
    faq_threshold: float = 0.85,
    session: Session | None = None,
    thinking: str | None = None,
) -> QaAnswer:
    """输入/输出同 ``stream_question``,只是把流**收拢**成一句答案。

    和 ``answer_rag`` 一样:非流式实现是流式的收拢器,所以两者**逐字一致**是结构上成立的。
    """
    done = done_of(
        stream_question(
            query,
            faq_store=faq_store,
            matcher=matcher,
            knowledge=knowledge,
            faq_threshold=faq_threshold,
            session=session,
            thinking=thinking,
        )
    )
    if done.channel is None:
        raise RuntimeError("收尾帧必须带 channel —— 帧协议要求 done 说明是谁答的")
    return QaAnswer(
        answer=done.answer,
        channel=done.channel,
        citations=done.citations,
        faq_match=done.faq_match,
        refused=done.refused,
        truncated=done.truncated,
    )
