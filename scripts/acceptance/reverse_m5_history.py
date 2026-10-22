#!/usr/bin/env python
"""反向测试:把「会话历史 + 滑动窗口」改回坏样子,确认契约真的会红。

用法(会临时改 `src/` 下的文件,**跑完自动还原**)：

    uv run python scripts/acceptance/reverse_m5_history.py

**为什么补这一份**:`check_m5_history.py` 有 **36 条**检查,却一直**没有任何反向用例** ——
也就是说"它到底测不测得住"从来没有被验证过(同一件事在本仓库发生过:检查写得像模像样,
把产品改坏却照样全绿第 16/23 条)。这份文件补上这一环。

滑动窗口这一批的坏版本都长一个样:**对话看起来还在,只是模型"忘了"** ——
没有异常、没有报错、接口 200,而用户只会觉得"它怎么答非所问"。所以每条口径
(读几轮、写回几轮、按什么滑、FAQ 命中读不读历史、提示词里放不放)各有一个坏版本。

锚点是**逐字符照抄**源码里的那几行(含缩进);`main` 会先核对"锚点在文件里恰好出现 1 次"——
不核对的话,一个对不上的锚点会让什么都没改的源码跑出一片绿。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m5_history.py"

SRC = ROOT / "src" / "project_rag"
APP = SRC / "application"
ADAPTERS = SRC / "adapters"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)
    requires_mysql: bool = False  # 这条坏版本要真 MySQL 才验得了(CI 里没有 → 按用例跳过)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  字符预算不生效（贴一份文档进来问,5 轮历史就能把提示词撑到几十万字符）",
        target=APP / "answer_question.py",
        old="""    kept = window_history(loaded, max_chars=session.max_chars)""",
        new="""    kept = loaded""",
        needle="滑动窗口接线",
    ),
    Case(
        title="RT-2  「至少保留最新一轮」被去掉（最新一轮自己超预算时,窗口直接变空 = 静默失忆）",
        target=APP / "answer_question.py",
        old="""        if kept and used + size > max_chars:""",
        new="""        if used + size > max_chars:""",
        needle="滑动窗口：从最新往回装",
    ),
    Case(
        title="RT-3  裁剪从**最旧**的一端留（窗口里全是陈年上下文,最近几轮反而丢了）",
        target=APP / "answer_question.py",
        old="""    for turn in reversed(history):
        size = len(turn.question) + len(turn.answer)""",
        new="""    for turn in history:
        size = len(turn.question) + len(turn.answer)""",
        needle="滑动窗口接线",
    ),
    Case(
        title="RT-4  写回时用字面量 5 而不是 Session.rounds（读 3 轮、写回却留 5 轮）",
        target=APP / "answer_question.py",
        old="""        keep=session.rounds,""",
        new="""        keep=5,""",
        needle="轮数来自 Session.rounds",
    ),
    Case(
        title="RT-5  FAQ 命中也去读一遍历史（白付一次库查询,而标准答案根本用不上上文）",
        target=APP / "answer_question.py",
        old="""    match = match_faq(query, store=faq_store, matcher=matcher, threshold=faq_threshold)""",
        new="""    _history(session)
    match = match_faq(query, store=faq_store, matcher=matcher, threshold=faq_threshold)""",
        needle="FAQ 命中 + 有会话",
    ),
    Case(
        title="RT-6  Session 不再拦住 rounds<=0（半成品变成「永远读不到历史」）",
        target=APP / "answer_question.py",
        old="""        if self.rounds <= 0:
            raise ValueError(f"rounds 必须为正数,收到 {self.rounds}")""",
        new="""        if False:
            raise ValueError(f"rounds 必须为正数,收到 {self.rounds}")""",
        needle="Session：frozen+slots",
    ),
    Case(
        title="RT-7  提示词里不放历史（答案对指代的理解全靠猜）",
        target=APP / "answer_rag.py",
        old="""    prompt = rag_answer_prompt(citations, query, history)""",
        new="""    prompt = rag_answer_prompt(citations, query)""",
        # 这条要钉的是**接线**(answer_rag 真的把 history 递给了提示词构造器),
        # 不是那个纯函数 —— 纯函数那条检查在 history 被丢掉时照样绿(实测:第一版就是这个错)
        needle="answer_rag：历史只进提示词",
    ),
    Case(
        title="RT-8  recent 忽略 limit（「最近 N 轮」变成「全部轮」,窗口在存储层就破了一半）",
        target=ADAPTERS / "conversation_mysql.py",
        old="""                    f"SELECT question, answer, citations FROM {self._table} "
                    "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                    (sid, limit),""",
        new="""                    f"SELECT question, answer, citations FROM {self._table} "
                    "WHERE session_id = %s ORDER BY id DESC",
                    (sid,),""",
        needle="recent 的 limit 只取最近几轮",
    ),
    Case(
        title="RT-9  record 不裁剪（库里无限长,「最近 N 轮」只在读的时候装样子）",
        target=ADAPTERS / "conversation_mysql.py",
        old="""                cur.execute(
                    f"DELETE FROM {self._table} WHERE session_id = %s AND id NOT IN (\"""",
        new="""                cur.execute(
                    f"SELECT 1 FROM {self._table} WHERE session_id = %s AND id NOT IN (\"""",
        needle="record(keep=N) 把该会话裁到最近 N 轮",
    ),
    Case(
        title="RT-10 建表尾巴只写 CHARSET 不写 COLLATE（两族表 JOIN 报 1267,会话列表整页读不出来）",
        target=ADAPTERS / "mysql_schema.py",
        old=(
            'TABLE_OPTIONS = f"ENGINE=InnoDB DEFAULT CHARSET={CHARSET}'
            ' COLLATE={COLLATION}"'
        ),
        new='TABLE_OPTIONS = f"ENGINE=InnoDB DEFAULT CHARSET={CHARSET}"',
        needle="口径：所有 CREATE TABLE 的尾巴",
        requires_mysql=True,
    ),
    Case(
        title="RT-11 align_tables 不干活（老表还是两族,JOIN 照样 1267）",
        target=ADAPTERS / "mysql_schema.py",
        old="""    names = [name for name in tables if _SAFE_TABLE.match(name)]""",
        new="""    return ()
    names = [name for name in tables if _SAFE_TABLE.match(name)]""",
        needle="真实链路：老表(只写了 CHARSET)",
        requires_mysql=True,
    ),
    Case(
        title="RT-12 落库时不存引用（切回旧会话:正文里的 [1] 还在,卡片没了）",
        target=ADAPTERS / "conversation_mysql.py",
        old="""    if not citations:
        return None""",
        new="""    return None
    if not citations:
        return None""",
        needle="真实链路：引用快照随这一轮落库",
        requires_mysql=True,
    ),
    Case(
        title="RT-13 记历史时不拍快照（recent 读回来的引用永远是空的）",
        target=APP / "answer_question.py",
        old="""            citations=snapshot_citations(citations),""",
        new="""            citations=(),""",
        needle="记历史时带上引用",
    ),
)


def run(needle: str) -> tuple[bool | None, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。

    输出:(是否 FAIL；``None`` = 目标那条自己 SKIP 了,这一条坏代码无从证明,附那一行)。

    ``None`` 与 ``False`` 不是一回事:``False`` 是「改坏了它还是绿的」= 检查是摆设;
    ``None`` 是「这条检查在本机跑不了」= 没测。后者在 CI 里是常态(RT-8/RT-9 要真 MySQL),
    当成 ``False`` 会把「没测」说成「抓不到坏代码」—— 那是误导性结论。
    """
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    if "[SKIP]" in hits[0]:
        return None, hits[0]
    return "[FAIL]" in hits[0], hits[0]


def _mysql_reachable() -> bool:
    """输入:无。行为:试连一次 MySQL(5 秒超时)。输出:连得上为真。

    需要真库的那几条坏版本在**没有库的环境里只能跳过** —— 让它们去跑会得到 SKIP,
    而 SKIP 在反向测试里读作"检查是摆设",那是**误导性结论**(真实情况是"没测")。
    """
    try:
        import pymysql

        from project_rag.config import get_settings

        cfg = get_settings().mysql
        conn = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password.get_secret_value(),
            database=cfg.database,
            connect_timeout=5,
        )
        conn.close()
        return True
    except Exception:  # noqa: BLE001 - 连不上就是"这台机器没有库",不是契约失败
        return False


def main() -> int:
    """输入:无。行为:逐个改坏→跑契约→还原,统计哪几条没红。输出:退出码。"""
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")

    originals = {case.target: case.target.read_text(encoding="utf-8") for case in CASES}
    has_mysql = _mysql_reachable()
    if not has_mysql:
        print("⚠️  连不上 MySQL:需要真库的那几条坏版本**按用例跳过**(CI 里本来就没有库)\n")
    bad = 0
    skipped = 0
    try:
        for case in CASES:
            if only and not any(case.title.startswith(tag) for tag in only):
                continue
            if case.requires_mysql and not has_mysql:
                skipped += 1
                print(f"○ 跳过(需要真 MySQL)  {case.title}")
                continue
            original = originals[case.target]
            hits = original.count(case.old)
            if hits != 1:
                print(
                    f"⚠️  {case.title}\n      锚点出现 {hits} 次(必须恰好 1 次) —— 源码变了,先更新锚点"
                )
                bad += 1
                continue
            with patched_source(case.target, case.old, case.new):
                red, line = run(case.needle)
            if red is None:  # 目标那条检查自己 SKIP(没库/没服务) —— 没测,不是摆设
                skipped += 1
                print(f"○ 跳过(这一条检查本身 SKIP)  {case.title}\n      {line}")
                continue
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    finally:
        for target, original in originals.items():
            if target.read_text(encoding="utf-8") != original:
                target.write_text(original, encoding="utf-8")
    print("─" * 68)
    tail = f";{skipped} 条跳过(需要真 MySQL 或目标检查自己 SKIP)" if skipped else ""
    print("全部反向测试都红了" + tail if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
