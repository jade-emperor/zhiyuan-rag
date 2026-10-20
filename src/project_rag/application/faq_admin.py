"""
用例:维护 FAQ 问答对(管理台)。查重、长度、空值都在这一层判,存储层只管存。

**为什么查重要读全表而不是建唯一索引**:``question`` 是 TEXT/VARCHAR(512)、
历史数据里已有近似重复问法,建唯一约束会当场失败;而 150 条读一遍是毫秒级。
口径是「压空白 + 忽略大小写后完全相同」—— 这是"同一条问题被录了两遍",
与「两个问法很像」不是一回事(后者该并存,靠匹配器打分)。
"""

from __future__ import annotations

from collections.abc import Sequence

from project_rag.domain import FaqEntry
from project_rag.ports import FaqAdminStore

MAX_QUESTION_CHARS = 512
MAX_ANSWER_CHARS = 8000


class FaqExists(Exception):
    """同一个问题已经有一条(改这条时排除自己)。"""


def _normalize(text: str) -> str:
    """输入:任意文本。行为:压空白 + 折叠大小写。输出:查重用的键。"""
    return " ".join(text.split()).casefold()


def check_question(text: str) -> str:
    """输入:问题原文。行为:去首尾空白并校验长度。输出:要入库的问题。"""
    value = text.strip()
    if not value:
        raise ValueError("问题不能为空")
    if len(value) > MAX_QUESTION_CHARS:
        raise ValueError(f"问题最多 {MAX_QUESTION_CHARS} 字,当前 {len(value)} 字")
    return value


def check_answer(text: str) -> str:
    """输入:答案原文。行为:去首尾空白并校验长度。输出:要入库的答案。"""
    value = text.strip()
    if not value:
        raise ValueError("答案不能为空")
    if len(value) > MAX_ANSWER_CHARS:
        raise ValueError(f"答案最多 {MAX_ANSWER_CHARS} 字,当前 {len(value)} 字")
    return value


def _ensure_unique(
    entries: Sequence[FaqEntry], question: str, *, exclude_id: int | None = None
) -> None:
    """输入:现有条目、待查问题、要排除的 ID。行为:发现同问就抛。输出:无。"""
    key = _normalize(question)
    for entry in entries:
        if exclude_id is not None and entry.id == exclude_id:
            continue
        if _normalize(entry.question) == key:
            raise FaqExists(f"已经有这条问题了(ID {entry.id})")


def create_entry(store: FaqAdminStore, *, question: str, answer: str) -> FaqEntry:
    """输入:存储、问题、答案。行为:校验 → 查重 → 写入。输出:新问答对。

    重名 → ``FaqExists``;字段不合法 → ``ValueError``。
    """
    clean_question = check_question(question)
    clean_answer = check_answer(answer)
    _ensure_unique(store.load_all(), clean_question)
    return store.add(clean_question, clean_answer)


def update_entry(
    store: FaqAdminStore,
    entry_id: int,
    *,
    question: str | None = None,
    answer: str | None = None,
) -> FaqEntry:
    """输入:存储、ID、要改的字段。行为:校验 → 查重(排除自己)→ 写入。输出:改后的行。

    两个字段都没给 → ``ValueError``(空更新多半是调用方拼错了);ID 不存在 → ``LookupError``。
    """
    if question is None and answer is None:
        raise ValueError("没有要改的字段(问题和答案至少给一个)")
    clean_question = None if question is None else check_question(question)
    clean_answer = None if answer is None else check_answer(answer)
    if clean_question is not None:
        _ensure_unique(store.load_all(), clean_question, exclude_id=entry_id)
    updated = store.update(entry_id, question=clean_question, answer=clean_answer)
    if updated is None:
        raise LookupError(f"没有 ID 为 {entry_id} 的问答对")
    return updated


def delete_entry(store: FaqAdminStore, entry_id: int) -> int:
    """输入:存储、ID。行为:删掉这一行(**幂等**:删不存在的给 0,不报错)。输出:删掉的行数。"""
    if entry_id <= 0:
        raise ValueError("ID 必须是正整数")
    return store.remove(entry_id)


def filter_entries(entries: Sequence[FaqEntry], query: str = "") -> tuple[FaqEntry, ...]:
    """输入:全部条目、搜索词(可空)。行为:按问题/答案做包含匹配(忽略大小写)。输出:条目序列。

    在内存里过滤比让 SQL 做 ``LIKE`` 简单,而且**搜得到答案正文** ——
    "我记得那条回答里提过 StringBuilder"也是维护时的常见线索。
    """
    needle = query.strip().casefold()
    if not needle:
        return tuple(entries)
    return tuple(
        entry
        for entry in entries
        if needle in entry.question.casefold() or needle in entry.answer.casefold()
    )
