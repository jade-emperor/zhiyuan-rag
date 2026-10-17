#!/usr/bin/env python
"""新评估集（八股 + PDF）的反向测试：把数据改回「坏的样子」，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m4_eval.py        # 全部
    uv run python scripts/acceptance/reverse_m4_eval.py RT-1   # 只跑某几条

**为什么这份 runner 改的是 `data/` 下的数据文件，而不是源码**：这一组检查要证明的是
「评估题不会被 FAQ 直答截胡」「问答对两个桶按行互斥」这类**数据性质** —— 它们只在数据坏掉时才会红，
把源码改坏根本触发不到。所以用 `_harness.patched_source`（它会把原文件备份到
`.cache/reverse_backup/<同样的相对路径>`，退出时还原、崩溃也能靠 `restore_stale_patches()` 自愈）
去改那三份产物，跑检查，断言对应那条红了。

**跳过不算通过**：`data/` 不公开（`docs/` 与 `data/` 都在 .gitignore 里），新克隆的机器上
这三份文件不存在 —— 那时这份 runner 会明确打印「数据不在本地，跳过」并以 0 退出，
**这不是"9 条反向全红"**，别把它的 0 当成证据。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import patched_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/acceptance/check_m4_eval.py"
EVAL_SET = ROOT / "data/eval/eval_set_bagu_pdf_v1.json"
QA_DATASET = ROOT / "data/eval/qa_bagu_pdf_v1.json"
FAQ_CSV = ROOT / "data/faq/qa_bagu_pdf_faq.csv"
DEMO_FAQ = ROOT / "demo/faq.csv"

REQUIRED = (EVAL_SET, QA_DATASET, FAQ_CSV, DEMO_FAQ)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑检查、只看带裁决标记的行。输出:(是否真红了, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, str(CHECK)], capture_output=True, text=True, timeout=600
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    if proc.returncode == 0:
        return False, f"⚠️ 那条红了但整份检查仍然退出 0（报告口径坏了）：{hits[0]}"
    return "[FAIL]" in hits[0], hits[0]


def _first_eval_question() -> str:
    """从新评估集里取一条**真实**的评估题当"污染源"。"""
    rows = json.loads(EVAL_SET.read_text(encoding="utf-8"))["rows"]
    return str(rows[0]["question"])


def _first_question_in_csv() -> str:
    import csv

    with FAQ_CSV.open(encoding="utf-8-sig", newline="") as handle:
        return str(next(iter(csv.DictReader(handle)))["问题"])


def _first_pdf_chunk_id() -> str:
    rows = json.loads(EVAL_SET.read_text(encoding="utf-8"))["rows"]
    return str(next(row["chunk_id"] for row in rows if row["source"] == "pdf"))


def main() -> int:
    missing = [path for path in REQUIRED if not path.exists()]
    if missing:
        print(
            "数据不在本地（`data/` 与演示产物不随仓库发布），跳过：\n  "
            + "\n  ".join(str(path.relative_to(ROOT)) for path in missing)
            + "\nowner：数据装载 —— 造这三份产物的命令见 scripts/tools/build_qa_dataset.py 的头部。"
        )
        return 0

    eval_question = _first_eval_question()
    csv_question = _first_question_in_csv()
    pdf_chunk_id = _first_pdf_chunk_id()
    _, rag_demo_question = ("", "")

    cases: list[tuple[str, Path, str, str, str]] = [
        (
            "RT-1 把 FAQ CSV 里一条问题换成评估题（评估时会被 FAQ 直答截胡）",
            FAQ_CSV,
            f",{csv_question},",
            f",{eval_question},",
            "评估题不得与 faq 表那份 CSV 近似",
        ),
        (
            "RT-2 评估集里一条 PDF 题的 chunk_id 清空（换不了页/块，溯源断了）",
            EVAL_SET,
            f'"chunk_id": "{pdf_chunk_id}"',
            '"chunk_id": ""',
            "新评估集（八股+PDF）：结构 + 溯源齐全",
        ),
        (
            "RT-3 把知识库演示那句问题也放进 demo/faq.csv（演示会被 FAQ 直答截胡，实测踩过）",
            DEMO_FAQ,
            "学科名称,问题,答案",
            f"学科名称,问题,答案\n演示,据 README 那句知识库演示问题,占位答案",
            "演示语料是自写的八股题",
        ),
    ]

    # RT-3 的污染源要**真的**是 README 里那句：从 demo/README.md 现取，避免写死漂移
    readme = (ROOT / "demo" / "README.md").read_text(encoding="utf-8")
    rag_line = next(
        (ln for ln in readme.splitlines() if "知识库" in ln and "|" in ln and "`" in ln), ""
    )
    cells = [cell.strip() for cell in rag_line.strip("|").split("|")]
    quoted = next((cell for cell in cells if cell.startswith("`") and cell.endswith("`")), "")
    rag_demo_question = quoted.strip("`")
    if not rag_demo_question:
        print("⚠️ demo/README.md 里找不到知识库演示问题那一行，RT-3 无法判定")
        return 1
    cases[2] = (
        cases[2][0],
        cases[2][1],
        cases[2][2],
        f"学科名称,问题,答案\n演示,{rag_demo_question},占位答案",
        cases[2][4],
    )

    bad = 0
    for title, path, old, new, needle in cases:
        text = path.read_text(encoding="utf-8")
        hits = text.count(old)
        if hits != 1:
            print(
                f"⚠️  {title}\n      锚点在 {path.relative_to(ROOT)} 里出现 {hits} 次（必须恰好 1 次）"
                f"—— 锚点过时了，这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(path, old, new):
            red, line = run(needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")

    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏数据")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
