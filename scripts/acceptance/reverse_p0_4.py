#!/usr/bin/env python
"""P0-4 反向测试:把 A/B 的**裁判**改回坏样子,确认契约真的会红。

用法::

    uv run python scripts/acceptance/reverse_p0_4.py          # 全部

改的是 ``scripts/tools/ab_retrieval.py`` 与 ``config/config.yaml``
(原地改、`finally` 还原,并带崩溃自愈 —— 见 ``_harness.patched_source``)。
五个坏版本都是"看起来没问题"的那种 —— 它们不会报错,只会让读数变好看或者变没意义:

* RT-1 空 reference 判成命中(整份评估集可能满分);
* RT-2 覆盖率换成 Jaccard(长 context 被稀释,命中判成不中);
* RT-3 阈值判定写成 ``>``(正好卡在阈值上的那条漏掉,读数静默偏移);
* RT-4 **把 FAQ 阈值调到无关问法的分数之下**(看着像"让 FAQ 多命中一些",实际是让无关问题误答;
  锚点从 config 里**读当前值**,不抄一份 —— 抄了就会在作者调旋钮时静默失效);
* RT-5 **重排旋钮没接上**(`rerank_input` 收了却永远喂父块 —— 两种喂法读出来一模一样，A/B 直接变成平局，而且不会报错)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
TOOL = "scripts/tools/ab_retrieval.py"
CONFIG = "config/config.yaml"


def _configured_threshold() -> str:
    """输入:无。行为:从 `config/config.yaml` 读**当前**的 `faq.threshold` 字面量。输出:文本。

    **锚点不许抄一份期望值**（§5 第 26/50 条）：这里原来写死 `threshold: 0.85`，作者
 把部署旋钮改成 0.95 之后，这条反向测试的锚点当场过时 —— 它自己变成了摆设
    （"锚点在 config 里出现 0 次"），而 `--set ci` 会因此报一条不合格。读当前值就不会再漂。
    """
    for line in (ROOT / CONFIG).read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("threshold:"):
            return line.strip().split(":", 1)[1].strip()
    raise RuntimeError(f"{CONFIG} 里读不到 faq.threshold —— 反向测试的锚点没了依托")

# 每条用例自带目标文件:改的是工具还是配置,只有用例自己知道
CASES: list[tuple[str, str, str, str, str, str]] = [
    (
        "RT-1  空 reference 判成命中（整份评估集可能满分）",
        TOOL,
        "    if not ref:\n        return False",
        "    if not ref:\n        return True",
        "空 reference 一律不命中",
        "check_p0_4_ab.py",
    ),
    (
        "RT-2  覆盖率换成 Jaccard（长 context 被稀释，命中判成不中）",
        TOOL,
        "    return len(ref & _ngrams(context)) / len(ref) >= threshold",
        "    return len(ref & _ngrams(context)) / len(ref | _ngrams(context)) >= threshold",
        "用「覆盖率」而不是 Jaccard",
        "check_p0_4_ab.py",
    ),
    (
        "RT-3  阈值判定写成 `>`（卡在阈值上的那条漏掉）",
        TOOL,
        "    return len(ref & _ngrams(context)) / len(ref) >= threshold",
        "    return len(ref & _ngrams(context)) / len(ref) > threshold",
        "阈值是 >= 不是 >",
        "check_p0_4_ab.py",
    ),
    (
        "RT-4  把 FAQ 阈值调到无关问法的分数之下（看着像「让 FAQ 多命中一些」，实际是让无关问题误答）",
        CONFIG,
        f"threshold: {_configured_threshold()}",
        "threshold: 0.0",
        "FAQ 阈值：实测可用区间",
        "check_p0_4_ab.py",
    ),
    (
        "RT-5  重排旋钮没接上（两种喂法读出来一模一样，A/B 变成平局）",
        "src/project_rag/application/answer_rag.py",
        '        item.chunk.text if rerank_input == "child" else item.chunk.context_text',
        '        item.chunk.context_text',
        "喂进去的是父块正文",
        "check_m3_rag.py",
    ),
    (
        "RT-6  把演示用的哈希嵌入设成默认（没模型的机器会静默用字面匹配冒充语义检索）",
        "src/project_rag/config.py",
        '    backend: Literal["bge_m3", "hashing"] = "bge_m3"',
        '    backend: Literal["bge_m3", "hashing"] = "hashing"',
        "演示后端必须显式开启",
        "check_m3_rag.py",
    ),
]


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:检查脚本名 + 期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{script}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    skipped: list[str] = []
    for title, rel, old, new, needle, script in CASES:
        target = ROOT / rel
        if not target.is_file():
            # 这条用例要改的文件**不随仓库发布**（`scripts/tools/` 整目录，见 .gitignore）。
            # 跳过它并**单独计数** —— 既不算红、也不算绿：混进任何一边都是在骗人
            # （「反向测试要能单独打红」是本项目最贵的那条纪律，见 §5 法律 67③）。
            # 与 `reverse_m4_eval` 对「data/ 不发布」的处理是同一个约定。
            skipped.append(f"{title}（缺 {rel}）")
            print(f"○ 跳过  {title}\n      缺 {rel} —— 该文件不随仓库发布（见 .gitignore）")
            continue
        original = target.read_text(encoding="utf-8")
        hits = original.count(old)
        if hits != 1:
            print(f"⚠️  {title}\n      锚点在 {rel} 里出现 {hits} 次(必须恰好 1 次)")
            bad += 1
            continue
        with patched_source(target, old, new):
            red, line = run(script, needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    print("─" * 68)
    ran = len(CASES) - len(skipped)
    if bad:
        print(f"{bad} 条没红 —— 那些检查抓不到坏代码")
    elif skipped:
        print(f"{ran} 条全红；{len(skipped)} 条**跳过**（前置文件不随仓库发布，见上）")
    else:
        print("全部反向测试都红了")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
