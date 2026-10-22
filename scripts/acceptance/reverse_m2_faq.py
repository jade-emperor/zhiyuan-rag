#!/usr/bin/env python
"""FAQ 通道的反向测试：把命中口径逐个改回「坏的样子」，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m2_faq.py            # 全部
    uv run python scripts/acceptance/reverse_m2_faq.py RT-3       # 只跑某几条

**为什么现在才有这一份**：`check_m2_faq.py` 一直是**唯一没有反向用例**的契约套件
（它进了 `local` 档，作者机器上跑；而这轮之前没人问过"那些检查改坏了会不会红"）。
（任何带「协程」的提问都被两个字的那条答掉）之后，命中口径多了三道闸门，
正好把这一格补上 —— 顺带把老的几条也钉住。

九条各自的坏法，共同点是**都不报错**（只是把提问答错、或者把该答的拦掉）：

* RT-1 **不夹取**（分数跑出 0~1）;
* RT-2 提问覆盖率不看;
* RT-3 条目覆盖率不看;
* RT-4 门槛写成严格大于;
* RT-5 排序改回用**归一化分** argmax（那次 13.8% 答错条目的原形状）;
* RT-6 类默认值与适配器常量分家;
* RT-7 数据侧最少词数放宽（单实词短条目又能灌进去）;
* RT-8 两道闸门没法关（回滚路径没了）;
* RT-9 引入"比绝对大小"的判据（原始分门槛）—— 判定随语料规模漂。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
MATCHER = ROOT / "src" / "project_rag" / "adapters" / "matcher_bm25.py"
CONFIG = ROOT / "src" / "project_rag" / "config.py"
LOADER = ROOT / "scripts" / "tools" / "load_faq_csv.py"
CONFIG_YAML = ROOT / "config" / "config.yaml"
SCRIPT = "check_m2_faq.py"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  不夹取（比值能 > 1）—— 分数不再落在 0~1",
        target=MATCHER,
        old="        confidences = [max(0.0, min(1.0, s / baseline)) for s in raw]",
        new="        confidences = [max(0.0, s / baseline) for s in raw]",
        needle="分数回到 0~1",
    ),
    Case(
        title="RT-2  提问覆盖率不看（只沾一个词的提问又被答掉）",
        target=MATCHER,
        old="""        if (
            self._min_query_coverage
            and query_coverage(asked, self._doc_token_sets[position]) < self._min_query_coverage
        ):""",
        new="        if False:",
        needle="命中口径②提问覆盖率",
    ),
    Case(
        title="RT-3  条目覆盖率不看（问答两边不是同一件事也照答）",
        target=MATCHER,
        old="""        return not (
            self._min_entry_coverage
            and entry_coverage(asked_set, self._doc_tokens[position]) < self._min_entry_coverage
        )""",
        new="        return True",
        needle="命中口径③条目覆盖率",
    ),
    Case(
        title="RT-4  覆盖率门槛写成「严格大于」（卡在门槛上的那条被误拦）",
        target=MATCHER,
        old="            and query_coverage(asked, self._doc_token_sets[position]) < self._min_query_coverage",
        new="            and query_coverage(asked, self._doc_token_sets[position]) <= self._min_query_coverage",
        needle="命中口径②提问覆盖率",
    ),
    Case(
        title="RT-5  排序改回用归一化分 argmax（那次 13.8% 答错条目的原形状）",
        target=MATCHER,
        old="        order = sorted(range(len(raw)), key=lambda i: (-raw[i], i))",
        new=(
            "        # 反向测试:排序用**各条自身分归一后再夹到 1.0**的置信度（13.8% 事故的原形状）\n"
            "        order = sorted(range(len(raw)), key=lambda i: (\n"
            "            -min(1.0, raw[i] / float(index.get_scores(self._doc_tokens[i])[i])), i\n"
            "        ))"
        ),
        needle="真实语料：给原问题接一句无关请求后仍命中自己",
    ),
    Case(
        title="RT-6  类默认值与适配器常量分家（配置说的与线上跑的不是一份）",
        target=CONFIG,
        old="    min_query_coverage: float = 0.5",
        new="    min_query_coverage: float = 0.4",
        needle="命中口径：类默认值与适配器里的常量一致",
    ),
    Case(
        title="RT-7  把配置里的最少词数放宽到 1（单实词的短条目又能灌进去）",
        # 下限住在 `faq.min_question_tokens` —— 改配置就该立刻反映到装载判定上，
        # 这条同时证明「旋钮真的接线了」（脚本里已经不存第二处默认值）。
        target=CONFIG_YAML,
        old="  min_question_tokens: 2",
        new="  min_question_tokens: 1",
        needle="数据侧：入库校验拒绝",
    ),
    Case(
        title="RT-8  两道闸门没法关（回滚路径没了）",
        target=MATCHER,
        old="""        for name, value in (
            ("min_query_coverage", min_query_coverage),
            ("min_entry_coverage", min_entry_coverage),
        ):
            if value < 0 or value > 1:
                raise ValueError(f"faq.{name} 是比例,必须落在 [0, 1]，收到 {value}")""",
        new="""        min_query_coverage = min_query_coverage or DEFAULT_MIN_QUERY_COVERAGE
        min_entry_coverage = min_entry_coverage or DEFAULT_MIN_ENTRY_COVERAGE""",
        needle="命中口径：两道覆盖率可以用 0 关掉",
    ),
    Case(
        title="RT-9  引入「比绝对大小」的判据（原始分门槛）—— 判定随语料规模漂",
        target=MATCHER,
        old="""        kept = [
            confidences[index_] if self._passes(asked, asked_set, index_) else 0.0
            for index_ in range(len(entries))
        ]""",
        new="""        kept = [
            confidences[index_] if (self._passes(asked, asked_set, index_) and raw[index_] >= 20)
            else 0.0
            for index_ in range(len(entries))
        ]""",
        needle="命中口径对语料规模不敏感",
    ),
)


# 目标检查因为「前置数据不在本地」而 SKIP 时的判据。这些前置文件**不随仓库发布**
# (扩充 FAQ 池等),所以在这台机器上量不出那一条 —— 那就如实记成"跳过",不能算成
# "检查是摆设"(那是把"没测"说成"测了没用",与 reverse_p0_4 同一条规矩)。
_MISSING_PREREQ = ("不在本地", "RAG_EXTRA_FAQ_POOL")


def run(needle: str) -> tuple[bool | None, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。

    ``None`` = 目标检查**跳过**了(前置数据不在本地),既不算红也不算绿。
    """
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        skips = [line for line in body if "[SKIP]" in line]
        if skips:
            return None, skips[0]
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    if "[SKIP]" in hits[0]:
        return None, hits[0]
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    skipped: list[str] = []
    for case in CASES:
        original = case.target.read_text(encoding="utf-8")
        hits = original.count(case.old)
        if hits != 1:
            print(
                f"⚠️  {case.title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(case.target, case.old, case.new):
            red, line = run(case.needle)
        mark = "○ 跳过(前置数据不在本地)" if red is None else ("✔ 红了(检查有效)" if red else "✘ 还是绿的(检查是摆设)")
        if red is False:
            bad += 1
        elif red is None:
            skipped.append(case.title)
        print(f"{mark}  {case.title}\n      {line}")
    print("─" * 68)
    ran = len(CASES) - len(skipped)
    summary = f"{ran - bad} 条全红" if ran else "一条都没跑成"
    if skipped:
        summary += f";{len(skipped)} 条跳过(前置数据不随仓库发布,本机要跑就 export RAG_EXTRA_FAQ_POOL=<扩充 CSV 的路径>)"
    print(summary if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码({summary})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
