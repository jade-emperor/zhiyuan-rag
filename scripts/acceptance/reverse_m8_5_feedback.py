#!/usr/bin/env python
"""反向测试：把「答案反馈」的每道守卫逐个改回「坏的样子」，确认对应检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m8_5_feedback.py          # 全部
    uv run python scripts/acceptance/reverse_m8_5_feedback.py RT-3     # 只跑某几条

改的是 ``application/feedback.py``、``domain/feedback.py``、``api/app.py``、
``adapters/feedback_mysql.py`` 与前端 store（**原地改、`finally` 还原**）。
九个用例都是真实会有人那么写的坏版本：

* RT-1 提交**不查会话归属**（谁都能给别人的会话打分）；
* RT-2 **不校验评级**（"meh" 也能入库，看板按 up/down 分组时空掉）；
* RT-3 撤销**不删行**（用户点了撤销，计数却不变）；
* RT-4 ``turn_key`` **不含问题**（同一段会话里两轮被当成同一轮，后一条覆盖前一条）；
* RT-5 看板**不查报表权限**（员工也能看到全公司的差评与原文）；
* RT-6 适配器**没有 ON DUPLICATE KEY**（重复点直接撞唯一键，接口 500）；
* RT-7 前端**只改本地状态**（按钮亮了，库里没有记录）；
* RT-8 没有样本时**把比率算成 0**（"负反馈率 0%" 与"还没人反馈"看起来一样）；
* RT-9 撤销**不带 user_id**（谁都能删掉别人的反馈）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m8_5_feedback.py"

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  提交不查会话归属（谁都能给别人的会话打分）",
        "src/project_rag/api/app.py",
        """        auth.require_knowledge(user)
        await auth.check_owner(request.session_id, user)""",
        """        auth.require_knowledge(user)""",
        "接口 提交：up 200",
    ),
    (
        "RT-2  不校验评级（\"meh\" 也能入库）",
        "src/project_rag/application/feedback.py",
        """    check_rating(rating)
    if rating == WITHDRAW:""",
        """    if rating == WITHDRAW:""",
        "接口 提交：非法评级",
    ),
    (
        "RT-3  撤销不删行（计数不变）",
        "src/project_rag/application/feedback.py",
        """    return store.remove(turn_key(session_id, question, answer), user_id)""",
        """    store.remove(turn_key(session_id, question, answer), user_id)
    return 0""",
        "接口 提交：up 200",
    ),
    (
        "RT-4  turn_key 不含问题（同一段会话的两轮互相覆盖）",
        "src/project_rag/application/feedback.py",
        """    digest.update(question.strip().encode("utf-8"))""",
        """    digest.update(b"")""",
        "用例：turn_key 稳定",
    ),
    (
        "RT-5  看板不查报表权限（员工能看到全公司的差评原文）",
        "src/project_rag/api/app.py",
        """        auth.require_reports(user)
        return await _call(_feedback_report, deps, limit=limit, rating=rating, days=days)""",
        """        return await _call(_feedback_report, deps, limit=limit, rating=rating, days=days)""",
        "接口 看板",
    ),
    (
        "RT-6  没有 ON DUPLICATE KEY（重复点撞唯一键，接口 500）",
        "src/project_rag/adapters/feedback_mysql.py",
        """                    " ON DUPLICATE KEY UPDATE rating = VALUES(rating),"
                    " comment = VALUES(comment), channel = VALUES(channel),"
                    " updated_at = CURRENT_TIMESTAMP(3)",
""",
        """""",
        "适配器：turn_key 唯一",
    ),
    (
        "RT-7  前端只改本地状态（按钮亮了，库里没有记录）",
        "web/src/stores/chat.ts",
        """    await api.submitFeedback({
      session_id: sessionId,
      question: question.text,
      answer: message.text,
      rating: next ?? 'none',
      channel: message.channel ?? '',
    })""",
        """    void 0""",
        "前端：两个按钮落到后端",
    ),
    (
        "RT-8  没有样本时把比率算成 0（与「还没人反馈」看起来一样）",
        "src/project_rag/domain/feedback.py",
        """        return None if self.total == 0 else self.down / self.total""",
        """        return 0.0 if self.total == 0 else self.down / self.total""",
        "接口 看板",
    ),
    (
        "RT-9  撤销不带 user_id（谁都能删掉别人的反馈）",
        "src/project_rag/adapters/feedback_mysql.py",
        """                    f"DELETE FROM {self._table} WHERE turn_key = %s AND user_id = %s",
                    (turn_key, user_id),""",
        """                    f"DELETE FROM {self._table} WHERE turn_key = %s",
                    (turn_key,),""",
        "适配器：turn_key 唯一",
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
