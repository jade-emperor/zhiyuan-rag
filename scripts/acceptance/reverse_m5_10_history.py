#!/usr/bin/env python
"""反向测试：把「会话历史」的每道守卫逐个改回「坏的样子」，确认对应检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m5_10_history.py          # 全部
    uv run python scripts/acceptance/reverse_m5_10_history.py RT-2     # 只跑某几条

改的是 ``api/app.py``、``api/auth.py``、``adapters/sessions_mysql.py`` 与前端路由
（**原地改、`finally` 还原**）。八个用例都是真实会有人那么写的坏版本：

* RT-1 跨账号目录**不查角色**（员工也能列出全公司的会话目录）；
* RT-2 归属判断**不给管理员开门**（运维台看不到真实问答，只能看元数据）；
* RT-3 `mine` **一律为真**（管理员看到的每一段都标成"我的"）；
* RT-4 适配器用 **INNER JOIN**（删一个账号，他的历史从管理员视野里凭空消失）；
* RT-5 目录查询**把 0 轮的会话也列出来**（刚建没问过的占满列表）；
* RT-6 **去掉 LIMIT**（会话表一长，这条查询就是个全表扫描 + 大响应）；
* RT-7 `scope` **回显写死 mine**（前端切了"全部"，拿回来的到底是谁的不清楚）；
* RT-8 前端路由**要 manage 而不是 knowledge**（员工读自己的历史也被拦在门外）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m5_10_history.py"

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  跨账号目录不查角色（员工能列出全公司的会话）",
        "src/project_rag/api/app.py",
        """        if scope == "all":
            auth.require_admin(user)""",
        """        if scope == "all":
            pass""",
        "接口 目录",
    ),
    (
        "RT-2  归属判断不给管理员开门（运维台看不到真实问答）",
        "src/project_rag/api/auth.py",
        """        if owner != user.user_id and not user.is_admin:""",
        """        if owner != user.user_id:""",
        "接口 正文",
    ),
    (
        "RT-3  mine 一律为真（管理员看到的每一段都是「我的」）",
        "src/project_rag/api/app.py",
        """                    mine=(scope == "mine")
                    or (user is not None and row.owner == user.username),""",
        """                    mine=True,""",
        "接口 目录",
    ),
    (
        "RT-4  适配器用 INNER JOIN（删号顺手吞掉他的历史）",
        "src/project_rag/adapters/sessions_mysql.py",
        """                    f" LEFT JOIN {self._users} u ON u.user_id = o.user_id\"""",
        """                    f" JOIN {self._users} u ON u.user_id = o.user_id\"""",
        "适配器",
    ),
    (
        "RT-5  目录把 0 轮的会话也列出来（刚建没问过的占满列表）",
        "src/project_rag/adapters/sessions_mysql.py",
        """                    f"SELECT o.session_id, o.created_at, COUNT(c.id), MAX(c.created_at), u.username"
                    f" FROM {self._owners} o JOIN {self._table} c\"""",
        """                    f"SELECT o.session_id, o.created_at, COUNT(c.id), MAX(c.created_at), u.username"
                    f" FROM {self._owners} o LEFT JOIN {self._table} c\"""",
        "适配器",
    ),
    (
        "RT-6  去掉 LIMIT（会话表一长就是全表扫描 + 大响应）",
        "src/project_rag/adapters/sessions_mysql.py",
        """                    " ORDER BY MAX(c.created_at) DESC, o.created_at DESC"
                    " LIMIT %s",
                    (limit,),""",
        """                    " ORDER BY MAX(c.created_at) DESC, o.created_at DESC",
                    (),""",
        "适配器",
    ),
    (
        "RT-7  scope 回显写死 mine（切了「全部」也不知道拿的是谁的）",
        "src/project_rag/api/app.py",
        """        return SessionsOut(
            scope=scope,""",
        """        return SessionsOut(
            scope="mine",""",
        "接口 目录",
    ),
    (
        "RT-8  前端路由要 manage 而不是 knowledge（员工读自己的历史也进不去）",
        "web/src/router.ts",
        """      component: HistoryView,
      meta: { needs: 'knowledge' },""",
        """      component: HistoryView,
      meta: { needs: 'manage' },""",
        "前端：/history 要 knowledge 能力",
    ),
]


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    originals: dict[str, str] = {}
    for _, rel, _, _, _ in CASES:
        originals.setdefault(rel, (ROOT / rel).read_text(encoding="utf-8"))
    print(f"改动目标：{len(originals)} 个文件（跑完在 finally 里还原）\n")
    bad = 0
    try:
        for title, rel, old, new, needle in CASES:
            if only and not any(title.startswith(tag) for tag in only):
                continue
            path = ROOT / rel
            original = originals[rel]
            hits = original.count(old)
            if hits != 1:
                print(
                    f"⚠️  {title}\n      锚点在 {rel} 里出现 {hits} 次（必须恰好 1 次）"
                    f"—— 锚点过时了，这条反向测试等于没跑"
                )
                bad += 1
                continue
            with patched_source(path, old, new):
                red, line = run(needle)
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    finally:
        for rel, original in originals.items():
            (ROOT / rel).write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
