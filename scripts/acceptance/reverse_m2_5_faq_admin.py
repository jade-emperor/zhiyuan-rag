#!/usr/bin/env python
"""反向测试：把 FAQ 管理台的每条守卫逐个改回「坏的样子」，确认对应检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m2_5_faq_admin.py          # 全部
    uv run python scripts/acceptance/reverse_m2_5_faq_admin.py RT-3     # 只跑某几条

改的是 ``application/faq_admin.py``、``api/app.py`` 与前端导航（**原地改、`finally` 还原**）。
九个用例都是真实会有人那么写的坏版本：

* RT-1 清单**不查角色**（员工也能看整张 FAQ 表）；
* RT-2 新建**不查重**（同一条问题录两遍，命中时随机答其中一条）；
* RT-3 问题**不去首尾空白**（库里存进 " 怎么报销 "，与用户原话永远差两个空格）；
* RT-4 改不存在的 ID **假装成功**（界面显示已保存，实际库里没这条）；
* RT-5 删除**不幂等**（重复点报 404 —— 用户以为没删掉）；
* RT-6 存储**不检查写入能力**（只读实现直接 AttributeError，500 里没有一句人话）；
* RT-7 查重**不排除自己**（改答案时被自己拦住，409 说"已经有这条问题了"）；
* RT-8 前端导航**不给管理员判断**（员工点进去必然 403）；
* RT-9 重复问题**回 400 而不是 409**（前端无法区分"填错了"与"已经有了"）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m2_5_faq_admin.py"

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  清单不查角色（员工也能看整张 FAQ 表）",
        "src/project_rag/api/app.py",
        """        auth.require_admin(user)
        return await _call(_faq_list, deps, query)""",
        """        return await _call(_faq_list, deps, query)""",
        "接口 清单",
    ),
    (
        "RT-2  新建不查重（同一条问题录两遍）",
        "src/project_rag/application/faq_admin.py",
        """    _ensure_unique(store.load_all(), clean_question)
    return store.add(clean_question, clean_answer)""",
        """    return store.add(clean_question, clean_answer)""",
        "用例 新建",
    ),
    (
        "RT-3  问题不去首尾空白（库里存进 ' 怎么报销 '）",
        "src/project_rag/application/faq_admin.py",
        """    value = text.strip()
    if not value:
        raise ValueError("问题不能为空")""",
        """    value = text
    if not value:
        raise ValueError("问题不能为空")""",
        "用例 新建",
    ),
    (
        "RT-4  改不存在的 ID 假装成功",
        "src/project_rag/application/faq_admin.py",
        """    if updated is None:
        raise LookupError(f"没有 ID 为 {entry_id} 的问答对")
    return updated""",
        """    if updated is None:
        return FaqEntry(id=entry_id, question=clean_question or "", answer=clean_answer or "")
    return updated""",
        "接口 修改",
    ),
    (
        "RT-5  删除不幂等（重复点报错）",
        "src/project_rag/application/faq_admin.py",
        """    return store.remove(entry_id)""",
        """    removed = store.remove(entry_id)
    if removed == 0:
        raise LookupError(f"没有 ID 为 {entry_id} 的问答对")
    return removed""",
        "用例 删除",
    ),
    (
        "RT-6  存储不检查写入能力（只读实现直接 AttributeError）",
        "src/project_rag/api/app.py",
        """    missing = [
        name
        for name in ("load_all", "add", "update", "remove")
        if not callable(getattr(store, name, None))
    ]""",
        """    missing: list[str] = []""",
        "接口 降级",
    ),
    (
        "RT-7  查重不排除自己（改答案时被自己拦住）",
        "src/project_rag/application/faq_admin.py",
        """        _ensure_unique(store.load_all(), clean_question, exclude_id=entry_id)""",
        """        _ensure_unique(store.load_all(), clean_question)""",
        "用例 修改",
    ),
    (
        "RT-8  前端导航不给管理员判断（员工点进去必然 403）",
        "web/src/components/AppHeader.vue",
        """      <RouterLink v-if="isAdmin" class="tab" :to="{ name: 'faq' }">FAQ</RouterLink>""",
        """      <RouterLink class="tab" :to="{ name: 'faq' }">FAQ</RouterLink>""",
        "前端：/faq 路由要 manage 能力",
    ),
    (
        "RT-9  重复问题回 400 而不是 409",
        "src/project_rag/api/app.py",
        """    if isinstance(exc, FaqExists):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))""",
        """    if isinstance(exc, FaqExists):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))""",
        "接口 新建",
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
