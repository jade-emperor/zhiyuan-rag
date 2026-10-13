#!/usr/bin/env python
"""FAQ 通道的「抢答」体检：**数据换一批/加一批，能不能治住误命中**。

用法::

    .venv/bin/python scripts/acceptance/faq_hijack_report.py

**它测的是什么**（作者报障）：FAQ 表里有一条 `协程`（**两个字**），于是任何带「协程」的提问
——「线程、进程、协程的对比」——都被它答掉，阈值调到 0.98 也没用。这一份不改任何算法规则，
只把**几种数据方案**摆到同一把尺子下量：现状 / 随机换 150 条 / 再加 150 条。

三个指标（都是"错的次数"，不是"看起来对不对"）：

* **自答**：每一条拿自己的问法去问，top1 该是它自己。错一条 = 那条问题在库里有更强的对手；
* **抢答**：拿**不在 FAQ 表里**的 50 条八股题（`data/eval/qa_bagu_pdf_v1.json` 的 eval 桶）去问，
  比值 ≥ 阈值 = 本该走知识库的问题被 FAQ 用一个**错答案**答掉（这就是作者遇到的形状）；
* **噪声**：与库完全不相干的问法（复用 `calibrate_threshold.py` 的探针），量下界。

**为什么分数会顶到 1.000（不是"很像"，是算法口径）**：`rank_bm25` 的 `BM25Plus` 对**每一个查询词**
都加 `idf(q) · delta`，**不管这个词在这条文档里有没有出现**：

    score(d) = Σ_{q ∈ 提问} idf(q) · ( delta + tf(q,d)·(k1+1) / (k1·(1-b+b·|d|/avgdl) + tf(q,d)) )

提问里那些"文档里没有"的词，贡献的是 `idf(q)·delta`（一个**底分**，对所有文档都一样）。
而 FAQ 的置信度 = `top1 原始分 ÷ 该条问自己的原始分` —— 分子里混进了底分，分母没有，
于是**提问越长，比值越大**，最后被夹到 1.000。条目越短（自身分越低）越容易被顶穿。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 为了 import calibrate_threshold

from project_rag.adapters import BM25TextMatcher, MySQLFaqStore  # noqa: E402
from project_rag.adapters.chinese_tokens import tokens  # noqa: E402
from project_rag.adapters.matcher_bm25 import entry_coverage, query_coverage  # noqa: E402
from project_rag.config import get_settings  # noqa: E402
from project_rag.domain import FaqEntry  # noqa: E402

QA_PAIRS = ROOT / "data" / "eval" / "qa_bagu_pdf_v1.json"
EXTRA_POOL = ROOT / "data" / "faq" / "pool-faq.csv"
HIJACK_QUERY = "线程、进程、协程的对比"
NOISE_PROBES = ["公司年会在哪天", "今天天气怎么样", "午饭吃什么", "帮我写一首诗", "怎么养猫"]


def load_extra_pool(path: Path) -> list[tuple[str, str]]:
    """输入:CSV 路径。行为:读 问题/答案 两列。输出:[(问题, 答案)]（文件不在给空表）。"""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return [
        (str(row.get("问题", "")).strip(), str(row.get("答案", "")).strip())
        for row in rows
        if str(row.get("问题", "")).strip()
    ]


def as_entries(rows: list[tuple[str, str]], *, start: int = 10_000) -> list[FaqEntry]:
    """输入:问答对。行为:包成 FaqEntry（id 从 start 起，避开真库的 id 段）。输出:领域对象列表。"""
    return [
        FaqEntry(id=start + index, question=question, answer=answer)
        for index, (question, answer) in enumerate(rows)
    ]


def measure(entries: list[FaqEntry], held_out: list[str], threshold: float) -> dict:
    """输入:语料 + 留出问题 + 阈值。行为:量自答/抢答/噪声。输出:一行读数。"""
    matcher = BM25TextMatcher()
    index = matcher._index_for(entries)

    def ratio(query: str) -> tuple[int, float, float, float]:
        """输入:提问。行为:按线上口径算 top1 与它的原始分/自身分/比值。输出:(下标, 原始分, 自身分, 比值)。"""
        top, raw, normalized = matcher._rank(query, entries)
        self_raw = float(
            index.get_scores(tokens(entries[top].question))[top]
        )
        return top, raw[top], self_raw, normalized[top]

    def coverage(query: str, entry: FaqEntry) -> float:
        """输入:提问 + top1 条目。输出:提问的实词有多大比例出现在这条条目里。

        **为什么量它**:BM25 的比值回答"这条像不像在答自己"，覆盖率回答"提问里有没有这条答不了的东西"。
        作者那个案子里，提问 5 个词只有 1 个在条目里（覆盖率 0.2）—— 比值却仍是 1.000。
        """
        asked = tokens(query)
        mine = set(tokens(entry.question))
        if not asked:
            return 1.0
        return sum(1 for term in asked if term in mine) / len(asked)

    missed = [
        (entry.question, entries[ratio(entry.question)[0]].question)
        for entry in entries
        if ratio(entry.question)[0] != entries.index(entry)
    ]
    hijacked = []
    for question in held_out:
        top, _raw, _self, value = ratio(question)
        if value >= threshold:
            hijacked.append((question, entries[top].question, value, coverage(question, entries[top])))
    noise = max((ratio(probe)[3] for probe in NOISE_PROBES), default=0.0)
    top, raw, self_raw, value = ratio(HIJACK_QUERY)
    return {
        "n": len(entries),
        "self_miss": len(missed),
        "self_miss_sample": missed[:3],
        "hijack": len(hijacked),
        "hijack_all": hijacked,
        "hijack_low_coverage": sum(1 for item in hijacked if item[3] < 0.6),
        "hijack_sample": sorted(hijacked, key=lambda item: -item[2])[:3],
        "query_coverage": coverage(HIJACK_QUERY, entries[top]),
        "noise_max": noise,
        "query_top": entries[top].question,
        "query_raw": raw,
        "query_self": self_raw,
        "query_ratio": value,
    }


def _unclipped(matcher, query: str, entries: list[FaqEntry]) -> float:
    """输入:闸门全关的匹配器 + 提问 + 语料。输出:top1 的未剪辑比值（拿不到匹配给 0）。"""
    hit = matcher.best_match(query, entries)
    return 0.0 if hit is None else hit.score


def ratio_of(entries: list[FaqEntry], query: str) -> tuple[int, float, float, float]:
    """输入:语料 + 提问。行为:按线上口径算 top1。输出:(下标, 原始分, 自身分, 比值)。"""
    matcher = BM25TextMatcher()
    index = matcher._index_for(entries)
    top, raw, normalized = matcher._rank(query, entries)
    return top, raw[top], float(index.get_scores(tokens(entries[top].question))[top]), normalized[top]


def coverage_of(query: str, entry: FaqEntry) -> float:
    """输入:提问 + 条目。输出:提问覆盖率（提问的实词有多大比例出现在这条里）。

    **委派给适配器**（``adapters.matcher_bm25.query_coverage``）—— 口径只有一处:
    这份报告量的是线上跑的那把尺子,不是另写一把"差不多的"。
    """
    return query_coverage(tokens(query), frozenset(tokens(entry.question)))


def entry_coverage_of(query: str, entry: FaqEntry) -> float:
    """输入:提问 + 条目。输出:条目覆盖率（这条条目的实词有多大比例出现在提问里）。

    **两个方向都要看**:提问覆盖率管"用户问的这条答得了吗",条目覆盖率管"这条问的是用户在问的吗"。
    """
    return entry_coverage(frozenset(tokens(query)), tokens(entry.question))


def main() -> int:
    parser = argparse.ArgumentParser(description="FAQ 抢答体检（只读，不改库）")
    parser.add_argument("--threshold", type=float, default=None, help="不给就用 config 里的值")
    parser.add_argument("--seed", type=int, default=0, help="随机抽题的种子（固定它才能复现）")
    parser.add_argument("--sample", type=int, default=150, help="另外抽多少条（默认 150）")
    parser.add_argument(
        "--short-max", type=int, default=6, help="D 组：把不超过几个字的条目补成问句（默认 6）"
    )
    args = parser.parse_args()

    settings = get_settings()
    threshold = settings.faq.threshold if args.threshold is None else args.threshold

    corpus = json.loads(QA_PAIRS.read_text(encoding="utf-8"))
    rows = corpus["rows"]
    faq_rows = [(r["question"], r["answer"]) for r in rows if r["bucket"] == "faq"]
    held_out = [r["question"] for r in rows if r["bucket"] == "eval"]
    extra_pool = load_extra_pool(EXTRA_POOL)

    live = list(MySQLFaqStore(settings.mysql, settings.faq).load_all())
    rng = random.Random(args.seed)
    shuffled = rng.sample(faq_rows, min(args.sample, len(faq_rows)))
    extra = rng.sample(extra_pool, min(args.sample, len(extra_pool))) if extra_pool else []

    # D 组：**只改数据**（一条算法都不动）—— 把"不是问句"的短条目补成完整问句。
    # 机理：置信度分母是"这条问自己"的分，短条目的自身分极低（一个词、|d|=1），
    # 于是任何**包含**这个词的提问都能把比值顶到 1.0。补成问句后自身分抬起来，
    # 光"含这个词"的提问就落回阈值之下（代价：用户只打一个词时 FAQ 不再答它 ——
    # 那是设计口径"FAQ 只答近乎一字不差的问法"，改写本来就归知识库通道）。
    short = [entry for entry in live if len(entry.question) <= args.short_max]
    rewritten = [
        entry
        if len(entry.question) > args.short_max
        else FaqEntry(id=entry.id, question=f"{entry.question}是什么？", answer=entry.answer)
        for entry in live
    ]

    scenarios = [
        (f"A 现状（MySQL 里的 {len(live)} 条）", live),
        (f"B 随机换 {len(shuffled)} 条（从八股 {len(faq_rows)} 条里抽，seed={args.seed}）",
         as_entries(shuffled)),
        (f"C 现状 + 另抽 {len(extra)} 条（{EXTRA_POOL.name}）",
         live + as_entries(extra)),
        (f"D 现状 + 把 {len(short)} 条短条目补成问句（≤{args.short_max} 字，只改数据）",
         rewritten),
        (f"E D + 另抽 {len(extra)} 条（改数据 + 加数据一起）", rewritten + as_entries(extra)),
        (f"F 现状 **去掉**那 {len(short)} 条短条目（FAQ 只收完整问句）",
         [entry for entry in live if len(entry.question) > args.short_max]),
    ]
    print(f"阈值 = {threshold}（{'命令行' if args.threshold is not None else 'config/config.yaml'}）"
          f" ｜ 留出题 {len(held_out)} 条（不在 FAQ 表里的八股题）")
    print(f"挑事的那条：问 {HIJACK_QUERY!r}\n")

    header = (f"{'方案':<46} {'自答错':>6} {'抢答':>5} {'低覆盖':>6} {'噪声峰':>7} "
              f"{'该问题的 top1':<16} {'比值':>6} {'覆盖':>5}")
    print(header)
    print("-" * 96)
    results = []
    for title, entries in scenarios:
        if not entries:
            continue
        row = measure(entries, held_out, threshold)
        results.append((title, row))
        print(f"{title:<46} {row['self_miss']:>6} {row['hijack']:>5} {row['hijack_low_coverage']:>6} "
              f"{row['noise_max']:>7.3f} {row['query_top'][:14]:<16} {row['query_ratio']:>6.3f} "
              f"{row['query_coverage']:>5.2f}")

    # ── 第二条尺子：**覆盖率门槛**能不能把"抢答"和"该答"分开 ────────────────────
    # 比值分不开（两边都是 1.000），覆盖率分得开：该答的提问，实词几乎都在条目里；
    # 抢答的提问，一半以上的词那条条目根本没有（A 组 6 次抢答里 5 次覆盖率 < 0.6）。
    # 这一节把几档覆盖率门槛摆出来，看"该答的放行 / 不该答的拦下"各自丢多少。
    print("\n▸ 覆盖率门槛扫描（现状 150 条；规则 = 比值 ≥ 阈值 **且** 覆盖率 ≥ 门槛）")
    group_a = [entry.question for entry in live]           # 一字不差：必须放行
    group_b = [f"请问，{entry.question}" for entry in live]  # 加礼貌语：必须放行
    group_d = [f"{entry.question}，另外帮我写一首诗" for entry in live]  # 句尾附加：口径待定
    noise = NOISE_PROBES + held_out                        # 噪声 + 留出题：必须拦下
    hijacks = results[0][1]["hijack_all"]
    header = f"{'门槛':>6} {'A 放行':>8} {'B 放行':>8} {'D 放行':>8} {'噪声放行':>9} {'抢答放过':>9}"
    print(header)
    print("-" * len(header))
    for gate in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        def passes(query: str) -> bool:
            top, _raw, _self, value = ratio_of(live, query)
            return value >= threshold and coverage_of(query, live[top]) >= gate

        survived = sum(
            1 for question, _answered, value, _cover in hijacks if value >= threshold and passes(question)
        )
        print(f"{gate:>6.2f} {sum(passes(q) for q in group_a):>8} {sum(passes(q) for q in group_b):>8} "
              f"{sum(passes(q) for q in group_d):>8} {sum(passes(q) for q in noise):>9} "
              f"{survived:>9}")

    # ── 上界带扫描（**结论：默认不用它**）────────────────────────────────────────
    # 不夹取之后，比值在一字不差时恒为 1.000、"夹带别的话题词"时明显 > 1，于是一个上界看起来
    # 能分开它们。**但上界比的是比值的绝对大小，而比值随语料规模漂**（idf=ln((N+1)/df)）：
    # 库一大，"该放行"的提问（比如「请问，<原问题>」）也会顶到 1.3+，上界就开始误伤。
    # 所以那一版最终**被删掉**了，命中口径只留下面两张表里那两道覆盖率 —— 它们只数词、不看分，
    # 与规模无关。这一节留着是为了**改配置前先看代价**（也是"别再把它加回来"的证据）。
    print("\n▸ 上界带扫描（不剪辑比值；规则 = 下界 ≤ 比值 ≤ 上界）—— 单看上界会成立,但它随语料漂")
    header = f"{'上界':>6} {'A 放行':>8} {'B 放行':>8} {'D 放行':>8} {'噪声放行':>9} {'抢答放过':>9}"
    print(header)
    print("-" * len(header))
    for ceiling in (1.02, 1.05, 1.10, 1.20, 1.50):
        def in_band(query: str) -> bool:
            _top, raw, self_raw, _clipped = ratio_of(live, query)
            if self_raw <= 0:
                return False
            unclipped = raw / self_raw
            return threshold <= unclipped <= ceiling

        survived = sum(1 for question, *_rest in hijacks if in_band(question))
        print(f"{ceiling:>6.2f} {sum(in_band(q) for q in group_a):>8} {sum(in_band(q) for q in group_b):>8} "
              f"{sum(in_band(q) for q in group_d):>8} {sum(in_band(q) for q in noise):>9} "
              f"{survived:>9}")
    _top, _raw, _self_raw, _clipped = ratio_of(live, HIJACK_QUERY)
    print(f"  参考：问「{HIJACK_QUERY}」的不剪辑比值 = {_raw / _self_raw:.3f}"
          f"（原始 {_raw:.3f} / 自身 {_self_raw:.3f}）")

    # 两条一起用：下界（原阈值）+ 上界带（不剪辑）+ 覆盖率门槛
    print("\n▸ 组合规则（下界 ≥ 阈值 且 不剪辑比值 ≤ 上界 且 提问覆盖率 ≥ 前 且 条目覆盖率 ≥ 后）")
    header = (f"{'上界/提问/条目':>16} {'A 放行':>7} {'B 放行':>7} {'D 放行':>7} "
              f"{'噪声放行':>8} {'抢答放过':>8}")
    print(header)
    print("-" * len(header))
    for ceiling, gate, entry_gate in (
        (1.10, 0.5, 0.0), (1.10, 0.5, 0.8), (1.10, 0.5, 0.9),
        (1.10, 0.6, 0.8), (1.10, 0.6, 0.9),
        (1.05, 0.5, 0.8), (1.05, 0.6, 0.8),
        (1.15, 0.5, 0.8), (1.20, 0.5, 0.8),
    ):
        def kept(query: str) -> bool:
            top, raw, self_raw, _clipped = ratio_of(live, query)
            if self_raw <= 0:
                return False
            unclipped = raw / self_raw
            return (
                threshold <= unclipped <= ceiling
                and coverage_of(query, live[top]) >= gate
                and entry_coverage_of(query, live[top]) >= entry_gate
            )

        survived = sum(1 for question, *_rest in hijacks if kept(question))
        print(f"{ceiling:>6.2f}/{gate:<4.2f}/{entry_gate:<4.2f} {sum(kept(q) for q in group_a):>7} "
              f"{sum(kept(q) for q in group_b):>7} {sum(kept(q) for q in group_d):>7} "
              f"{sum(kept(q) for q in noise):>8} {survived:>8}")

    # ── 分子是怎么被抬起来的：逐个查询词给出 df / idf / 在不在 top1 里 / 贡献多少 ──────
    print(f"\n▸ 「{HIJACK_QUERY}」的分子拆解（top1 = 「{live[_top].question}」）")
    from rank_bm25 import BM25Plus  # 只为读 df/idf，口径与适配器同一份实现

    index = BM25Plus([tokens(entry.question) for entry in live])
    mine = set(tokens(live[_top].question))
    print(f"  {'词':<8} {'库内 df':>8} {'idf':>7} {'在 top1 里':>10} {'贡献':>8}")
    for term in tokens(HIJACK_QUERY):
        df = sum(1 for doc in index.doc_freqs if term in doc)
        idf = index.idf.get(term, 0.0)
        present = term in mine
        tf_part = (1 * (index.k1 + 1)) / (
            index.k1 * (1 - index.b + index.b * len(mine) / index.avgdl) + 1
        )
        contribution = idf * ((1 + tf_part) if present else index.delta)
        print(f"  {term:<8} {df:>8} {idf:>7.3f} {'是' if present else '否':>10} {contribution:>8.3f}")
    print("  （不在 top1 里的词，每个照样贡献 idf×delta —— 那是 BM25Plus 的底分，"
          "提问里这类词越多，分子被抬得越高，而分母（自身分）纹丝不动）")

    # ── 语料规模漂移：同一批提问在不同规模的库上还判得一样吗 ────────────────────
    # 作者的追问："分数不再是 0~1 之后,随着语料漂移怎么办?" 这一节就是答案：
    # 比值会漂（分子分母都含 idf），覆盖率不漂（纯 token 计数）。把库撑大再量一遍。
    print("\n▸ 语料规模漂移（库从 %d 条撑到 %d 条：把 FAQ 表与 demo/JP 池拼起来）" % (len(live), len(live) + len(extra)))
    corpora: list[tuple[str, list[FaqEntry]]] = [(f"{len(live)}（现状）", live)]
    for take in (args.sample, len(extra_pool)):  # 用**整份**池子,不是抽样后的那一份
        if take and len(extra_pool) >= take:
            corpora.append(
                (f"{len(live) + take}（+{take}）", live + as_entries(extra_pool[:take], start=20_000))
            )
    probes = [entry.question for entry in live] + [f"请问，{entry.question}" for entry in live]
    probes += NOISE_PROBES + [HIJACK_QUERY]
    header = f"{'语料':<18}{'该放行（问自己/礼貌语）':>24}{'该拦（噪声+夹带）':>20}{'A 最高分':>10}{'B 最高分':>10}"
    print(header)
    print("-" * len(header))
    baseline: list[bool] | None = None
    for name, entries in corpora:
        matcher = BM25TextMatcher(
            min_query_coverage=settings.faq.min_query_coverage,
            min_entry_coverage=settings.faq.min_entry_coverage,
        )
        keep = [
            (hit := matcher.best_match(query, entries)) is not None
            and hit.score >= threshold
            for query in probes[: 2 * len(live)]
        ]
        block = [
            (hit := matcher.best_match(query, entries)) is not None
            and hit.score >= threshold
            for query in probes[2 * len(live) :]
        ]
        open_matcher = BM25TextMatcher(min_query_coverage=0, min_entry_coverage=0)
        a_max = max(_unclipped(open_matcher, e.question, entries) for e in entries)
        b_max = max(_unclipped(open_matcher, "请问，" + e.question, entries) for e in entries)
        if baseline is None:
            baseline = keep + block
        same = "✔ 判定与现状逐条相同" if keep + block == baseline else "✘ 判定变了（漂移）"
        print(f"{name:<18}{f'{sum(keep)}/{len(keep)}':>24}{f'{sum(block)}/{len(block)}':>20}"
              f"{a_max:>10.3f}{b_max:>10.3f}   {same}")
    print("  （上界打开时 B 最高分会顶到 1.3+ ⇒ 那一档开始误伤合法提问；覆盖率不看分数,所以不漂）")

    for title, row in results:
        print(f"\n▸ {title}")
        print(f"  问「{HIJACK_QUERY}」→ top1「{row['query_top']}」"
              f"（原始 {row['query_raw']:.3f} / 自身 {row['query_self']:.3f} = {row['query_ratio']:.3f}）")
        if row["self_miss_sample"]:
            print(f"  自答错的例子：{row['self_miss_sample']}")
        if row["hijack_sample"]:
            for question, answered, value, cover in row["hijack_sample"]:
                print(f"  抢答：问「{question[:34]}」→ 答「{answered[:24]}」"
                      f"(比值 {value:.3f} / 覆盖率 {cover:.2f})")
    if short:
        print(f"\n▸ D/E 改写了这 {len(short)} 条（≤{args.short_max} 字）：")
        for entry in short[:10]:
            print(f"    「{entry.question}」 → 「{entry.question}是什么？」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
