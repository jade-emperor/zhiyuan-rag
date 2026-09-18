#!/usr/bin/env python
"""在真实 FAQ 语料上标定命中阈值。

分数口径见 ``adapters/matcher_bm25.py``：``top1 原始分 ÷ 该条问自己的原始分``。
本脚本用三组探针量出可用区间：

  A 一字不差  每条问题问自己        —— 理论天花板，恒为 1.000
  B 加礼貌语  每条问题加「请问，」   —— FAQ 通道**应该**命中的最强改写
  C 无关问法  与库不相干的固定问法   —— 应当低于任何可用阈值

可用阈值区间 = ``(C 的最大值, B 的最小值]``。阈值取区间内任何值都能「放行 B、拦住 C」，
B 与 C 之间的地带就是余量；余量越窄越要谨慎（阈值一抖动就误判）。

另有两条只报告、不参与区间的边界：

  D 句尾附加  原问题 + 「，另外帮我写一首诗」 —— 量分数的**夹取上限**。
    这类问法同样得 1.000（FAQ 答前半句、忽略后半句），证明分数被夹在 1.0；
    多意图拆分归 M5 路由层，不在这里解决。
  E 库内近邻  每条问题问自己时，**第二名**拿到自身分的几成 —— 量语料自身的可混淆度。
    白盒（``score_all``），口径与 ``best_match`` 同源。近邻分越高，说明语料里
    「换个措辞就答错那条」的风险越集中在这些条目上；它不参与区间，但会跟着语料增长而升高。

**覆盖范围**：换词/口语化级别的改写不在本脚本范围内 —— 那个不归 FAQ 通道管，
由 M3 的向量检索通道负责，评估集放 ``data/eval/``。

用法::

    .venv/bin/python scripts/acceptance/calibrate_threshold.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

# 本文件在 scripts/acceptance/,所以 parents[0]=acceptance [1]=scripts [2]=仓库根
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from project_rag.adapters import BM25TextMatcher, MySQLFaqStore
from project_rag.config import get_settings
from project_rag.domain import FaqEntry

# C 组：与 FAQ 库不相干的问法。刻意选**会共享常见词**的问法（怎么/怎么办），
# 否则测出来的下界虚低，掩盖误命中风险。
NOISE_PROBES = [
    "公司年会在哪天",
    "今天天气怎么样",
    "午饭吃什么",
    "帮我写一首诗",
    "你叫什么名字",
    "股票怎么开户",
    "怎么养猫",
]

# D 组：在真问题后面接一段无关请求。用来量「分数被夹到 1.0」这个边界，
# 顺便证明「FAQ 会只答前半句」不是猜测 —— 多意图拆分是 M5 路由层的活。
ATTACH_SUFFIX = "，另外帮我写一首诗"


def _probe(matcher, entries, queries):
    """跑一批探针。

    输入:matcher + 语料 + [(问题, 期望命中的 entry.id 或 None)]
    行为:逐条匹配,记录分数;期望条目对不上就记进 missed
    输出:(分数列表, 没命中预期条目的 [(问题, 原因)])
    """
    scores: list[float] = []
    missed: list[tuple[str, str]] = []
    for query, want_id in queries:
        hit = matcher.best_match(query, entries)
        if hit is None:
            missed.append((query, "无候选"))
            continue
        if want_id is not None and hit.entry.id != want_id:
            missed.append((query, f"命中了「{hit.entry.question[:20]}」"))
        scores.append(hit.score)
    return scores, missed


def _report(label: str, scores: list[float]) -> None:
    if not scores:
        print(f"{label:<16} n=0（没有样本）")
        return
    print(
        f"{label:<16} n={len(scores):<4} "
        f"min={min(scores):.3f}  中位数={statistics.median(scores):.3f}  "
        f"max={max(scores):.3f}"
    )


def _neighbours(matcher: BM25TextMatcher, entries: list[FaqEntry]) -> list[tuple[float, str, str]]:
    """量语料自身的可混淆度：每条问题问自己时,第二名拿到自身分的几成。

    输入:matcher + 语料。
    行为:逐条用 score_all 取全量分数,挑出除 top1 外的最高分并除以 top1 的分。
         score_all 与 best_match 同口径,所以这里量的就是线上会发生的分数。
    输出:按分数降序的 [(第二名占比, 该条问题, 抢它的邻居问题)]。
    """
    rows: list[tuple[float, str, str]] = []
    for entry in entries:
        scores = matcher.score_all(entry.question, entries)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        top_i, runner_i = ranked[0], ranked[1]
        base = scores[top_i]
        if base <= 0.0:
            continue
        rows.append(
            (
                scores[runner_i] / base,
                entry.question,
                entries[runner_i].question,
            )
        )
    rows.sort(reverse=True)
    return rows


def main() -> int:
    settings = get_settings()
    entries = list(MySQLFaqStore(settings.mysql, settings.faq).load_all())
    if not entries:
        print("faq 表是空的,先灌种子数据")
        return 1

    matcher = BM25TextMatcher()
    by_id = [(e.question, e.id) for e in entries]

    exact_scores, exact_missed = _probe(matcher, entries, by_id)
    polite_scores, polite_missed = _probe(
        matcher, entries, [("请问，" + q, i) for q, i in by_id]
    )
    noise_scores, _ = _probe(matcher, entries, [(q, None) for q in NOISE_PROBES])
    attach_scores, attach_missed = _probe(
        matcher, entries, [(q + ATTACH_SUFFIX, i) for q, i in by_id]
    )

    print(f"语料规模：{len(entries)} 条    口径：top1 原始分 ÷ 该条问自己的分\n")
    _report("A 一字不差", exact_scores)
    _report("B 加礼貌语", polite_scores)
    _report("C 无关问法", noise_scores)
    _report("D 句尾附加", attach_scores)

    for label, missed in (
        ("一字不差", exact_missed),
        ("加礼貌语", polite_missed),
        ("句尾附加", attach_missed),
    ):
        if missed:
            print(f"\n⚠️ {label}组有 {len(missed)} 条没命中自己预期的那条：")
            for q, why in missed[:5]:
                print(f"   - {q}  →  {why}")

    if not (polite_scores and noise_scores):
        print("\n样本不足，无法给出可用区间")
        return 0

    low, high = max(noise_scores), min(polite_scores)
    configured = settings.faq.threshold
    print()
    if low < high:
        print(f"可用阈值区间：({low:.3f}, {high:.3f}]    余量 {high - low:.3f}")
        print(f"建议起点：{(low + high) / 2:.2f}")
        verdict = "✓ 在区间内" if low < configured <= high else "✗ 落在区间外"
        print(f"契约阈值 RAG_FAQ__THRESHOLD = {configured}  {verdict}")
    else:
        print(
            f"⚠️ B、C 两组分数重叠（noise_max={low:.3f} ≥ polite_min={high:.3f}）："
            "不存在能同时「放行礼貌语改写、拦住无关问法」的阈值。"
            "先查 matcher 的分母是不是该条问自己的分。"
        )
        return 1

    # E 组只报告,不参与区间:它量的是语料内部的可混淆度,随语料增长而升高。
    neighbours = _neighbours(matcher, entries)
    if neighbours:
        ratios = [r for r, _, _ in neighbours]
        risky = [row for row in neighbours if row[0] >= configured]
        print(
            f"\n库内近邻（第二名 ÷ 自身分）n={len(ratios)}  "
            f"中位数={statistics.median(ratios):.3f}  max={ratios[0]:.3f}"
        )
        if risky:
            print(f"  其中 {len(risky)} 条问题的近邻分 ≥ 阈值 {configured}，换个措辞就可能答错那条：")
            for ratio, question, neighbour in risky[:5]:
                print(f"    {ratio:.3f}  {question[:30]}  ←  {neighbour[:30]}")
        else:
            print(f"  没有一条问题的近邻分达到阈值 {configured}，语料内部无混淆风险")

    print(
        f"\n说明：D 组恒为 1.000（分数被夹在 1.0，句尾附加内容不扣分），"
        "它只说明「FAQ 会答前半句」，多意图拆分归 M5 路由层。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
