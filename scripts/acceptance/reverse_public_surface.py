#!/usr/bin/env python
"""公开面契约的反向测试：往仓库里塞回"只有内部看得懂"的东西，确认检查真的会红。

用法::

    uv run python scripts/acceptance/reverse_p0_8_public_surface.py        # 全部
    uv run python scripts/acceptance/reverse_p0_8_public_surface.py RT-1   # 只跑某几条

**为什么要有它**：这条契约守的是"读起来别扭"这一类坏法 —— 没有报错、测试全绿,
唯一症状是外人读注释时一头雾水。这种检查最容易变成摆设（写的时候对、之后没人复查）,
所以必须证明它**真的会因为一行注释而红**。

三条坏法都是真实会发生的写法：

* RT-1 在源码注释里补一句「见 docs/STATUS.md §0.3 第 39 条」（看着像"给读者留个出处"）;
* RT-2 把决策过程写进检查脚本（"作者要求…"—— 对内部人有用，对读者是噪音）;
* RT-3 在对外文档里丢一个 `docs/` 路径而不说它不发布（外人点进去是死链）。

改的是**真的仓库文件**（原地改、`finally` 还原），因为这条契约读的就是 `git ls-files`。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_public_surface.py"

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  源码注释里补一句台账指针（看着像给读者留出处）",
        "src/project_rag/config.py",
        "    read_consistency: Literal[\"bounded\", \"eventually\", \"strong\"] = \"strong\"",
        "    # 见 docs/STATUS.md §0.3 第 39 条\n"
        "    read_consistency: Literal[\"bounded\", \"eventually\", \"strong\"] = \"strong\"",
        "仓库文件里不出现",
    ),
    (
        "RT-2  把决策者口吻写进检查脚本（对内部有用，对读者是噪音）",
        "scripts/acceptance/check_deps_root.py",
        '@check("组合根：Deps 是 frozen+slots dataclass，字段名与默认值按契约")',
        '@check("组合根：Deps 是 frozen+slots dataclass（作者要求），字段名与默认值按契约")',
        "仓库文件里不出现",
    ),
    (
        "RT-4  源码注释里补一句里程碑票号（读者不知道 M7.4 是什么）",
        "scripts/acceptance/_fakes.py",
        '"""假重排器:按候选顺序给递减分。',
        '"""M7.4 的假重排器:按候选顺序给递减分。',
        "里程碑编号",
    ),
    (
        "RT-3  对外文档里丢一个 `docs/` 路径却不说它不发布（外人点进去是死链）",
        "API.md",
        "## 14. 版本与兼容",
        "## 14. 版本与兼容\n\n（设计理由与决策过程见 docs/STATUS.md。）",
        "提到 `docs/`",
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
            hits = originals[rel].count(old)
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
