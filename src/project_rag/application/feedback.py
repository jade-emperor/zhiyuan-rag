"""
用例:答案反馈(写)与运维台看板(读)。

三条口径:

1. **``turn_key`` 由这一轮的 (会话, 问题, 答案) 算出来**:同一个人对同一轮重复点
   (或刷新页面再点),是一次覆盖写而不是两条记录 —— 计数不该被重复点击放大;
2. **快照是用户点按钮那一刻的那一轮**,不是事后去翻他的全部历史;
3. **负反馈率的分母是"有反馈的轮次",不是全部提问**:没点按钮的人默认"没意见",
   把他们算进分母会得到一个永远很好看、也永远不动的数。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from project_rag.domain import (
    FEEDBACK_RATINGS,
    FeedbackCounts,
    FeedbackOverview,
    FeedbackRecord,
)
from project_rag.ports import FeedbackStore

MAX_COMMENT_CHARS = 200
MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 12000

#: 撤销反馈的哨兵值(只出现在请求里,不落库)。
WITHDRAW = "none"


class FeedbackError(ValueError):
    """请求本身不合法(评级不在闭集、备注超长、快照为空)。"""


def check_rating(value: str) -> str:
    """输入:评级。行为:必须是 ``up`` / ``down`` / ``none``。输出:原值。"""
    if value not in (*FEEDBACK_RATINGS, WITHDRAW):
        raise FeedbackError(f"评级只能是 {'/'.join((*FEEDBACK_RATINGS, WITHDRAW))}")
    return value


def check_comment(text: str) -> str:
    """输入:一句话备注(可空)。行为:去首尾空白并限长。输出:要入库的备注。"""
    value = text.strip()
    if len(value) > MAX_COMMENT_CHARS:
        raise FeedbackError(f"备注最多 {MAX_COMMENT_CHARS} 字,当前 {len(value)} 字")
    return value


def check_snapshot(question: str, answer: str) -> tuple[str, str]:
    """输入:被反馈的问题与答案。行为:非空 + 限长。输出:(问题, 答案)。"""
    clean_question = question.strip()
    clean_answer = answer.strip()
    if not clean_question or not clean_answer:
        raise FeedbackError("反馈要带上这一轮的问题与答案(否则看板上只剩一个孤零零的数字)")
    if len(clean_question) > MAX_QUESTION_CHARS or len(clean_answer) > MAX_ANSWER_CHARS:
        raise FeedbackError("这一轮太长了,不适合整段留痕")
    return clean_question, clean_answer


def turn_key(session_id: str, question: str, answer: str) -> str:
    """输入:会话 ID、问题、答案。行为:拼出稳定键。输出:64 位十六进制。

    **只有一处算这个键**:前端不算(它拿不到与后端逐字一致的字符串),存储层也不算
    (换存储时键会变,同一条反馈就会多出一行)。
    """
    digest = hashlib.sha256()
    digest.update(session_id.strip().encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(question.strip().encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(answer.strip().encode("utf-8"))
    return digest.hexdigest()


def submit(
    store: FeedbackStore,
    *,
    session_id: str,
    user_id: str,
    rating: str,
    question: str,
    answer: str,
    channel: str = "",
    comment: str = "",
) -> FeedbackRecord:
    """输入:存储、会话、用户、评级、快照。行为:校验 → 覆盖写。输出:落库后的记录。

    评级合法性与快照校验都在这里:接口层的 422 只能挡住形状,挡不住"评级是 upx"。
    """
    check_rating(rating)
    if rating == WITHDRAW:
        raise FeedbackError("撤销用 withdraw(),不是 submit()")
    clean_question, clean_answer = check_snapshot(question, answer)
    clean_comment = check_comment(comment)
    if not session_id.strip():
        raise FeedbackError("缺少 session_id")
    return store.upsert(
        turn_key=turn_key(session_id, clean_question, clean_answer),
        session_id=session_id.strip(),
        user_id=user_id,
        rating=rating,
        question=clean_question,
        answer=clean_answer,
        channel=channel.strip()[:16],
        comment=clean_comment,
    )


def withdraw(
    store: FeedbackStore, *, session_id: str, user_id: str, question: str, answer: str
) -> int:
    """输入:存储、会话、用户、快照。行为:删掉**自己**对这一轮的那条反馈。输出:删掉的条数。"""
    check_snapshot(question, answer)
    return store.remove(turn_key(session_id, question, answer), user_id)


def overview(
    store: FeedbackStore,
    *,
    limit: int = 50,
    rating: str | None = None,
    days: int = 30,
) -> FeedbackOverview:
    """输入:存储、条数、只看哪一档(可空)、窗口天数(0 = 全部)。行为:计数 + 最近条目。输出:看板。

    ``days=0`` 是"全部历史"的合法表达 —— 不拿 0 当"今天"。
    """
    if rating is not None:
        check_rating(rating)
        if rating == WITHDRAW:
            raise FeedbackError("看板只按 up / down 过滤")
    since = None if days <= 0 else datetime.now() - timedelta(days=days)
    up, down = store.counts(since=since)
    entries = store.list_recent(limit=limit, rating=rating, since=since)
    return FeedbackOverview(counts=FeedbackCounts(up=up, down=down), entries=tuple(entries))
