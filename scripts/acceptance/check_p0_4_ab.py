#!/usr/bin/env python
"""P0-4 契约:检索层 A/B 的**裁判**本身要可靠。

用法::

    uv run python scripts/acceptance/check_p0_4_ab.py

`scripts/tools/ab_retrieval.py` 用一条离线判据回答"这次检索命中了吗":

    命中 = reference(H3 小节原文)的字符 3-gram 被某条 citation 覆盖的比例 ≥ 0.35

**这条尺子错了,整个 A/B 的读数全废**,而且不会报错 —— 它只会给出一个看着很正常的百分比。
所以它必须和别人一样有契约。这里钉**七件**(下面 ①–⑤ + 配置表 + FAQ 阈值):

1. **identical 必然命中**(尺子至少认得完全一样的东西);
2. **空 reference 一律不命中** —— 评估集里若有空的标准答案,那不是"全都命中";
3. **覆盖率而不是 Jaccard**:一条**短的** citation 落在长 reference 里,覆盖率低 → 不命中;
   反过来一条**长的** citation 包住整个 reference,覆盖率 100% → 命中
   (用 Jaccard 的话后者会被长 context 稀释,把命中判成不中);
4. **空白不敏感**(换行/空格是排版噪声,不该影响判定);
5. **阈值是 `>=` 不是 `>`** —— 正好卡在阈值上的那条要算命中(边界写错会静默改变读数)。

外加一条:工具里那张配置表(rrf / weighted-x-y)必须与 `RetrievalOptions` 的字段对得上,
否则 `--configs` 会拿着一组不存在的旋钮去跑,读出来的数没有意义。

**P0-4 第三组(FAQ 阈值)也在这里**:`faq.threshold` 是"FAQ 通道命中判定"的那个数,
调低会让无关问法误命中、调高会让礼貌语失效。契约 = **实测可用区间必须罩住它**
(探针口径直接复用 `calibrate_threshold.py`,只跑最快的那三组、0.8 秒)。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _harness import SkipCheck, check, report

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "tools" / "ab_retrieval.py"


def _tool():
    """输入:无。行为:按文件路径加载那个脚本(它不是包,import 不到)。输出:模块。"""
    spec = importlib.util.spec_from_file_location("ab_retrieval", TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError(f"加载不了 {TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@check("裁判：完全相同的文本必然命中（尺子至少认得它自己）")
def _identical_hits() -> str:
    overlaps = _tool().overlaps
    text = "编译型语言先编译成机器码,解释型语言边解释边执行。"
    if not overlaps(text, text):
        raise AssertionError("同一段文本自己和自己比,应当命中")
    if overlaps("完全无关的另一段话。", text):
        raise AssertionError("毫不相干的文本不该命中 —— 那样 A/B 会全都满分")
    return "identical → 命中;无关文本 → 不命中"


@check("裁判：空 reference 一律不命中（空标准答案不是「全都命中」）")
def _empty_reference_misses() -> str:
    overlaps = _tool().overlaps
    for empty in ("", "   ", "\n\n"):
        if overlaps("任意一段正文内容。", empty):
            raise AssertionError(f"空 reference {empty!r} 不该命中 —— 那会把整份评估集判成满分")
    return "空/纯空白 reference 都判不命中"


@check("裁判：用「覆盖率」而不是 Jaccard —— 短 citation 不中、长 context 命中")
def _coverage_not_jaccard() -> str:
    overlaps = _tool().overlaps
    # **别用重复文本**:3-gram 是集合,重复内容会被去重 —— `X*8` 的"覆盖率"高得离谱,
    # 短 citation 也能拿 0.7(这条检查第一版就是这么写错的,正向测试自己抓出来了)。
    reference = (
        "第一条:编译型语言在运行前整体翻译成机器码。"
        "第二条:解释型语言在运行时逐句翻译。"
        "第三条:两者在启动速度上差异明显。"
        "第四条:类型检查发生的时机也不同。"
    )
    short = "第一条:编译型语言在运行前整体翻译成机器码。"
    if overlaps(short, reference):
        raise AssertionError(
            "短得只覆盖 reference 一小部分的 citation 不该算命中 —— "
            "否则「捞到一句就去报命中」也能拿高分"
        )
    # **填充要够长,而且必须互不相同**:3-gram 是集合,`X * 8` 这种重复串的去重后
    # 只有 ~30 个 gram,根本稀释不了 Jaccard —— 这条检查因此两次没区分出两种口径
    # (`reverse_p0_4` 的 RT-2 一直绿)。用**逐条不同**的填充才真的撑出长度。
    filler = "".join(f"第{i:03d}段与标准答案完全无关的填充内容。" for i in range(120))
    if not overlaps(f"{filler}{reference}{filler}", reference):
        raise AssertionError(
            "包住整个 reference 的长 context 必须命中 —— 覆盖率口径不看 context 有多长;"
            "换成 Jaccard 会被这段填充稀释成不命中"
        )
    return "短 citation 不中;长 context 中(覆盖率口径,不是 Jaccard)"


@check("裁判：空白与换行不敏感；阈值是 >= 不是 >")
def _whitespace_and_threshold() -> str:
    overlaps = _tool().overlaps
    reference = "退款在订单页点申请退款,三个工作日到账。"
    spaced = "退款在订单页点申请退款,\n三个工作日到账。\n\n"
    if not overlaps(spaced, reference):
        raise AssertionError("只是多了换行/空格,不该判成不命中")
    # 边界:`>=` 而不是 `>`。**覆盖率由裁判自己算一遍再拿来当阈值** ——
    # 手算会被"接缝 gram"(A…A 与 B…B 交界处产生的 2 个新 gram)坑到,
    # 而这条检查要验的是**比较运算符**,不是 gram 的数学。
    tool = _tool()
    reference2, context2 = "甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊"
    ratio = len(tool._ngrams(reference2) & tool._ngrams(context2)) / len(tool._ngrams(reference2))
    if not overlaps(context2, reference2, threshold=ratio):
        raise AssertionError(f"覆盖率正好等于阈值({ratio:.4f})时该命中 —— 判定写 `>` 会漏掉这一档")
    if overlaps(context2, reference2, threshold=ratio + 1e-9):
        raise AssertionError("低于阈值一点点不该命中")
    return "空白不敏感;覆盖率 == 阈值 → 命中(>=)"


@check("工具：配置表里的旋钮与 RetrievalOptions 对得上（不然读出来的数没有意义）")
def _configs_match_options() -> str:
    from project_rag.domain import RetrievalOptions

    configs = _tool().CONFIGS
    if "rrf" not in configs:
        raise AssertionError("配置表里必须有现默认的 rrf —— 没有对照就没有 A/B")
    fields = set(RetrievalOptions.__dataclass_fields__)
    for name, (_fusion, bm25, vector) in configs.items():
        if {"fusion", "bm25_weight", "vector_weight"} - fields:
            raise AssertionError(f"RetrievalOptions 少了旋钮:{fields}")
        total = bm25 + vector
        if abs(total - 1.0) > 1e-9:
            raise AssertionError(f"{name} 的权重加起来是 {total},不是 1.0 —— 读数会不可比")
    return f"{len(configs)} 组配置:{'/'.join(configs)};权重各自归一"


@check("FAQ 阈值：实测可用区间必须罩住 faq.threshold（放行礼貌语、拦住无关问法）")
def _faq_threshold() -> str:
    """P0-4 第三组:FAQ 命中阈值（`faq.threshold`，读 `config.yaml` —— 本机现在是 0.95）。

    注意这里**只断言"配置值落在实测区间里"**，不钉具体数字：0.85 是**代码默认值**
    （`check_m2_faq` / `check_m5_qa` 钉着），而 `config.yaml` 那一行是**部署旋钮**，
    作者可以调（§5 第 26 条）—— 调完由这条检查负责说"还在不在可用区间内"。

    口径与工具**共用一份**:直接 import ``calibrate_threshold`` 的 ``probe`` 与 ``NOISE_PROBES``。
    各写一份探针的话,"工具量的是 A、检查量的是 B",两边对不上还没人知道。

    这里只跑 A/B/C 三组(实测 **0.8 秒**);E 组(库内近邻)是 463×463 次打分,留给人工跑工具。

    三条判据:
    * A(一字不差)恒为 1.000 —— 归一化口径(``top1 ÷ 该条问自己的分``)没坏;
    * B(加礼貌语)的下界 **>** C(无关问法)的上界 —— 否则**不存在**能同时放行与拦住的阈值,
      那是 matcher 的口径坏了,而不是"阈值该调";
    * **配置的阈值落在 ``(C 上界, B 下界]`` 里** —— 调低到噪声之上,无关问法会误命中 FAQ 通道
      (用户问"怎么养猫",系统回一段退款政策);调高到礼貌语之上,FAQ 通道直接失效。
    """
    from project_rag.adapters import BM25TextMatcher, MySQLFaqStore
    from project_rag.config import get_settings

    from calibrate_threshold import NOISE_PROBES, probe  # 同一目录的标定工具

    settings = get_settings()
    try:
        entries = list(MySQLFaqStore(settings.mysql, settings.faq).load_all())
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题,不是契约失败
        raise SkipCheck(
            f"连不上 FAQ 表（{type(exc).__name__}: {str(exc)[:80]}）—— 先在 Windows 侧 docker compose up -d"
        ) from exc
    if len(entries) < 30:
        raise SkipCheck(
            f"FAQ 表只有 {len(entries)} 条,标不出可用区间；先跑 scripts/tools/load_faq.py 灌种子数据"
        )

    matcher = BM25TextMatcher()
    by_id = [(entry.question, entry.id) for entry in entries]
    exact, _ = probe(matcher, entries, by_id)
    polite, _ = probe(matcher, entries, [("请问，" + q, i) for q, i in by_id])
    noise, _ = probe(matcher, entries, [(q, None) for q in NOISE_PROBES])

    if not exact or abs(min(exact) - 1.0) > 1e-6:
        raise AssertionError(
            f"A 组（一字不差）该恒为 1.000,实测最低 {min(exact):.3f} —— "
            "归一化口径（top1 ÷ 该条问自己的分）坏了,分数不再可比"
        )
    # **结构上分得开**才算过：一字不差全满分、礼貌语全部越过阈值、无关问法全部在阈值之下。
    # 只看"区间非空"这一条的话，三道闸门（上界 + 两个覆盖率）把噪声压成 0 之后就变得太平了 ——
    # 闸门本身坏掉（比如上界失效、覆盖率不看）时噪声会重新抬头，这里必须能红。
    configured = settings.faq.threshold
    bad_polite = [score for score in polite if score < configured]
    if bad_polite:
        raise AssertionError(
            f"{len(bad_polite)}/{len(polite)} 条礼貌语改写没越过阈值（最低 {min(bad_polite):.3f} < "
            f"{configured}）—— 三道闸门（上界/提问覆盖率/条目覆盖率）是不是拦多了？"
            "先跑 `uv run python scripts/acceptance/calibrate_threshold.py` 看是哪一组掉下来的"
        )
    bad_noise = [(probe, score) for probe, score in zip(NOISE_PROBES, noise) if score >= configured]
    if bad_noise:
        raise AssertionError(
            f"无关问法被 FAQ 通道答掉了：{bad_noise} —— 阈值调低了，或者三道闸门没生效"
        )
    low, high = max(noise), min(polite)
    return (
        f"{len(entries)} 条语料:一字不差全 1.000、礼貌语 {len(polite)}/{len(polite)} 全越过阈值 "
        f"{configured}、无关问法最高 {low:.3f}（全部拦下）；余量 {high - configured:.3f}"
    )


def main() -> int:
    return report(
        "P0-4 契约:检索层 A/B 的裁判（overlaps）+ 配置表 + FAQ 阈值",
        notes=(
            "说明:除「FAQ 阈值」那条要真 FAQ 表(0.8 秒)外,全内存、不需要容器/模型/网络。",
            "它不评检索质量,只保证尺子本身可靠。",
        ),
        hint=(
            "提示:跑读数用 `uv run python scripts/tools/ab_retrieval.py --limit 112 "
            "--configs rrf,weighted-5-5,weighted-3-7,weighted-7-3`。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
