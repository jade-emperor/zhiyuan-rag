"""
用例:合流问答 —— FAQ 通道优先,未命中走知识库通道(讲义 ⑥-6.1 的 IntegratedQASystem.query);
带会话时顺带维护对话历史(讲义 ⑥-6.2)。

为什么要有这一层:两条通道各自都答得对,但「谁来答」是个**业务决定** ——
FAQ 表里是人工维护的标准答案,它命中时就是最可信的答案;命不中才值得付一次
检索 + 生成的钱。这个决定放在用例层而不是命令行/接口层,是因为 CLI(M5.1)、
HTTP 接口(M5.4)、评估脚本都要走同一条路由,谁都不该自己再抄一遍。

五条刻意的设计:

1. **命中判定借 FAQ 通道的 match_faq**,不自己写比较运算符:
   「≥ 还是 >」「边界算不算命中」只能有一个真值来源,否则两条通道迟早各长一套。
2. **不让裸模型兜底**:FAQ 未命中一律转知识库通道。``answer_faq`` 自带一条
   「未命中 → 问模型」的兜底,那是 M2 那条通道自己的能力;合流的语义是
   「答案要么来自人写的标准答案、要么来自语料」—— 让裸模型接第三棒等于重新打开
   M3 特意堵掉的幻觉口子(检索为空时 answer_rag 直接拒答,不调 LLM)。
3. **知识库那半边是「要用时才造」的**:它以一个零参函数(``knowledge``)交进来。
   Python 的函数参数是**先求值**的 —— 若把嵌入端口直接传进来,组合根在进入本函数
   之前就已经把 BGE-M3 装起来了(2G 内存 + 秒级加载),FAQ 命中的问题白付这份钱;
   配了 cuda 却没有 GPU 的机器上更是**直接抛错**,连一条标准答案都答不出来。
   交给一个闭包,「谁来答」的决定留在本用例,「怎么造、什么时候造」留在组合根
   (``cli/deps.py`` 的 knowledge_callable)。
4. **FAQ 存储的错误不吞**:MySQL 是必需资源,``load_all()`` 抛错就往上抛。
   吞掉它会让「库挂了」表现成「知识库答的」—— 那是最难查的一类故障。
5. **历史只影响知识库通道的提示词,而且读、写各只有一处**(M5.2):
   命中 FAQ 时**不读历史**(标准答案不需要知道上文,读了也是一次白跑的库查询),
   但这一轮仍然要**记下来** —— 会话是完整的;命中知识库时才读最近 ``rounds`` 轮。
   写历史收口在 ``_record`` 一处:讲义 6.3 的 ``POST /api/query`` 正是因为绕开了
   这个收口,MySQL 命中的答案压根没进历史 —— 这种「某条入口忘了写」只能靠
   「只有一个地方写」来防。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

# 数据类
from project_rag.domain import ConversationTurn, QaAnswer, QaEvent, RagAnswer

# 创建日志的方法
from project_rag.observability import get_logger

# 端口中的各种抽象类(接口)
from project_rag.ports import ConversationStore, FaqStore, TextMatcher

# 同包用相对导入:项目的分层扫描规则把「绝对导入 project_rag.application.*」
# 当成跨层依赖拦下来(它只看模块前缀),相对导入才表达得出「同一个包内」
from .answer_faq import match_faq
from .streaming import done_of

_logger = get_logger("application.answer_question")


@dataclass(frozen=True, slots=True)
class Session:
    """一次带会话的提问:历史存哪、是哪一段、读写几轮。

    为什么绑成一个对象:光有一个 ``session_id``、没有 store 就没法读写;有 store
    却没有 id 则是半成品。绑起来之后,那种半成品在类型上就构造不出来,用例里也就
    不用再写「这两个参数必须同时给」的运行时检查。
    """

    store: ConversationStore
    session_id: str
    rounds: int = 5

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id 不能为空 —— 否则所有匿名提问会挤进同一段历史")
        if self.rounds <= 0:
            raise ValueError(f"rounds 必须为正数,收到 {self.rounds}")


def _history(session: Session | None) -> tuple[ConversationTurn, ...]:
    """输入:会话(可为 None)。行为:读最近 rounds 轮。输出:时间正序的历史;无会话给空元组。

    只有**要走知识库通道**时才调它 —— 命中 FAQ 那条分支不读历史。
    """
    if session is None:
        return ()
    return tuple(session.store.recent(session.session_id, limit=session.rounds))


def _record(session: Session | None, question: str, answer: str) -> None:
    """输入:会话(可为 None)、问题、答案。行为:把这一轮记下来(拒答也记)。输出:无。

    **写历史只有这一个收口**:两条通道都从这里过。放在用例层而不是命令层,
    是因为多一个入口(M5.4 的 FastAPI)就多一次「忘了写」的机会。
    """
    if session is None:
        return
    session.store.record(
        session.session_id,
        ConversationTurn(question=question, answer=answer),
        keep=session.rounds,
    )


def stream_question(
    query: str,
    *,
    faq_store: FaqStore,
    matcher: TextMatcher,
    knowledge: Callable[[Sequence[ConversationTurn]], Iterator[QaEvent]],
    faq_threshold: float = 0.85,
    session: Session | None = None,
) -> Iterator[QaEvent]:
    """输入:问题 + FAQ 侧的端口与阈值 + **知识库通道的一次调用**(收历史、吐帧)+ 可选会话。

    行为:先问 FAQ 命中没有(不碰缓存、不调 LLM);
          命中 → 把标准答案当一帧 token 发出去(**不是流出来的,但以同样的方式交付**),
                 ``knowledge`` 一次都不会被调用,也不读历史;
          未命中 → 读最近几轮历史 → 转发知识库通道的帧(token 边到边发)→
                   自己补一帧 done(知识库不知道自己是不是「兜底」,channel 由本用例填);
          两条路都会把这一轮记进会话(带了会话的话)。
    输出:帧序列,恰好一帧 done 收尾。

    **历史落在 done 之后才写**:客户端中途断开时生成器被关闭,这一轮就不入库 ——
    半截答案进历史比没有更糟(下一轮会拿它当「上文」)。
    """
    match = match_faq(query, store=faq_store, matcher=matcher, threshold=faq_threshold)
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
    for event in knowledge(_history(session)):
        if event.kind == "done":
            done = event  # 换成自己那一帧再发,不直接转发
            continue
        yield event
    if done is None:
        raise RuntimeError("知识库通道的流没有收尾帧 —— 帧协议要求恰好一帧 done")

    if done.refused:
        _logger.info("知识库通道拒答(没有检索到相关资料): %s", query)
    _record(session, query, done.answer)
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
    knowledge: Callable[[Sequence[ConversationTurn]], Iterator[QaEvent]],
    faq_threshold: float = 0.85,
    session: Session | None = None,
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
