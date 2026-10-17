#!/usr/bin/env python
"""M4.6 验收:查询改写 A/B(评估集 mix_v1 + 两臂数据集 + 两臂读数 + 对比报告)。

用法::

    .venv/bin/python scripts/acceptance/check_m4_rewrite_ab.py

分组:**LOCAL_ONLY**(见 ``run_all.py``)—— 它盯的全是 ``data/`` 下的产物,
而 ``data/`` 不随仓库发布;``scripts/tools/`` 同样整目录不公开。所以这份检查在
公开检出上会 SKIP 并点名 owner,**不是故障**。

这一组要回答的问题只有一个:**报告里那些数字,是不是产物算出来的**。

四类断言:

1. **评估集**(``eval_set_mix_v1.json``):4 类 × 30、id 口径、四类互不相交、
   重建两次逐字一致。评估集本身不可复现的话,两臂的差就说不清是谁造成的。
2. **两臂只有改写这一个变量**:两臂数据集的 ``user_input`` 逐条相同、raw 的 ``rewrite``
   标志一真一假、基线里**不许**出现改写字段。
3. **读数对得上输入**:两份 summary 的指纹 == 当前数据集文件;行数 == 可评估条数。
4. **报告不能手写**:把 ``rewrite_ab_report.md`` 里那张四指标表的每个数**重算一遍**,
   对不上就红 —— 手抄一个好看的数进来是最容易发生、也最难发现的一种错。
"""

from __future__ import annotations

import csv
import hashlib
import json

import statistics
import sys
from pathlib import Path

from _harness import check, report, SkipCheck

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "scripts" / "tools"
EVAL_DIR = ROOT / "data" / "eval"
EVAL_SET = EVAL_DIR / "eval_set_mix_v1.json"
VAGUE_CACHE = EVAL_DIR / "mix_v1_vague.json"
DATASET_DIR = EVAL_DIR / "datasets"
RUNS_DIR = EVAL_DIR / "ragas_runs"
REPORT = EVAL_DIR / "rewrite_ab_report.md"
MIX_TOOL = TOOLS / "build_eval_set_mix.py"

BASELINE, REWRITE = "mix_v1_norw", "mix_v1_rw"
KINDS = ("direct", "vague", "subquery", "denoise")
PER_KIND = 30
MIN_GROUND_TRUTH_CHARS = 20
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
# 报告里那张表允许的误差:报告是 4 位小数渲染的,重算与它的差只可能来自四舍五入
REPORT_TOLERANCE = 5e-5


def _need(path: Path, owner: str) -> Path:
    """输入:路径 + 该找谁。行为:不在就 SKIP(点名 owner)。输出:路径。"""
    if not path.exists():
        raise SkipCheck(f"{path.relative_to(ROOT)} 不在本地;owner:{owner}")
    return path


def _rows() -> list[dict]:
    """输入:无。行为:读评估集 rows。输出:行列表。"""
    doc = json.loads(_need(EVAL_SET, "M4.6 评估集构造").read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "rows" not in doc:
        raise AssertionError(f"{EVAL_SET.name} 顶层要是 {{meta, rows}}")
    return doc["rows"]


def _raw(name: str) -> dict:
    """输入:数据集名。行为:读 builder 的 raw.json。输出:整个文档。"""
    path = _need(DATASET_DIR / f"{name}_raw.json", "M4.6 A/B 两臂的 builder")
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_csv(tag: str) -> list[dict]:
    """输入:tag。行为:读最新一次该 tag 的逐条结果。输出:行列表。"""
    found = sorted(RUNS_DIR.glob(f"run_*_{tag}.rows.csv"))
    if not found:
        raise SkipCheck(f"还没有 tag={tag} 的 RAGAS 结果;owner:M4.6 两臂评估")
    with found[-1].open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _mix_tool():
    """输入:无。行为:把 scripts/tools 挂进 sys.path 后导入生成器(脚本不是包,没有 __init__.py)。输出:模块。

    走 ``__import__`` 而不是 ``import x``:静态类型检查器不认运行期才拼出来的路径,
    而这条路径本来就只在本地检出上存在(``scripts/tools/`` 整目录不公开)。
    """
    tools = str(TOOLS)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return __import__("build_eval_set_mix")


def _sha12(path: Path) -> str:
    """输入:文件。行为:sha256 前 12 位。输出:摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _mean(values: list[float]) -> float:
    """输入:数值列表。行为:均值。输出:浮点(空列表给 nan)。"""
    return statistics.fmean(values) if values else float("nan")


def _number(value: object) -> float | None:
    """输入:CSV 里的格子。行为:能变 float 就变。输出:浮点或 None。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number


# ---------------------------------------------------------------------------
# A. 评估集
# ---------------------------------------------------------------------------


@check(f"mix 评估集:{len(KINDS)} 类 × {PER_KIND} 条,question 唯一,参考答案都够长")
def _() -> str:
    rows = _rows()
    counts = {kind: sum(1 for row in rows if row["kind"] == kind) for kind in KINDS}
    if counts != dict.fromkeys(KINDS, PER_KIND):
        raise AssertionError(f"四类分布不对:{counts},契约是每类 {PER_KIND} 条")
    questions = [row["question"] for row in rows]
    if len(set(questions)) != len(questions):
        raise AssertionError("有重复的问题:两臂会对同一条打分两次,均值被它拉偏")
    for row in rows:
        if len(str(row["ground_truth"]).strip()) < MIN_GROUND_TRUTH_CHARS:
            raise AssertionError(f"ground_truth 太短:{row['question']!r}")
    return f"{len(rows)} 条,{counts}"


@check("mix 评估集:id 是 sha256(来源|章|标题|类) 前 12 位")
def _() -> str:
    for row in _rows():
        title = row["title"] if not row.get("partner_title") else f"{row['title']}+{row['partner_title']}"
        raw = f"{row['source']}|{row['section']}|{title}|{row['kind']}"
        want = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        if row["id"] != want:
            raise AssertionError(f"id 对不上:{row['id']} != sha256({raw})[:12] = {want}")
    return f"{len(_rows())} 条口径一致(builder 靠它断点续跑)"


@check("mix 评估集:四类互不相交(同一小节不测两遍)")
def _() -> str:
    seen: dict[tuple[str, str, str], str] = {}
    for row in _rows():
        keys = [(row["source"], row["section"], row["title"])]
        if row.get("partner_title"):
            keys.append((row["source"], row["section"], str(row["partner_title"])))
        for key in keys:
            if key in seen:
                raise AssertionError(f"{key} 同时出现在 {seen[key]} 与 {row['kind']} 两类里")
            seen[key] = row["kind"]
    by_kind: dict[str, int] = {}
    for kind in seen.values():
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return f"{len(seen)} 个小节四类不重叠:{by_kind}"


@check("mix 评估集:vague 是空泛问法(不是拿标题冒充),且与冻结的问题表一致")
def _() -> str:
    cache = json.loads(_need(VAGUE_CACHE, "M4.6 vague 问题生成").read_text(encoding="utf-8"))
    frozen = {str(key): str(value) for key, value in (cache.get("questions") or {}).items()}
    for row in _rows():
        if row["kind"] != "vague":
            continue
        if frozen.get(row["id"]) != row["question"]:
            raise AssertionError(
                f"「{row['question'][:24]}」与冻结的问题表对不上 —— "
                "评估集必须由那份表组装出来,不能就地改题(改了指标就不可复现)"
            )
        if row["title"] in row["question"]:
            raise AssertionError(f"vague 的问题里出现了标题术语「{row['title']}」,它就不是假设答案那一类了")
    meta = cache.get("meta") or {}
    if not meta.get("model") or not meta.get("prompt_sha12"):
        raise AssertionError("冻结表里没记模型与提示词摘要:复现不出这批题是怎么来的")
    return f"{PER_KIND} 条与冻结表一致,生成模型 {meta['model']},提示词 {meta['prompt_sha12']}"


@check("mix 评估集:重建两次逐字相同,且与磁盘一致")
def _() -> str:
    if not MIX_TOOL.exists():
        raise SkipCheck(f"{MIX_TOOL.relative_to(ROOT)} 不在这份检出里(scripts/tools/ 不公开);owner:数据装载")
    mix = _mix_tool()
    vague = mix._load_vague()
    first = mix.build_rows(vague)
    second = mix.build_rows(vague)
    if first != second:
        raise AssertionError("build_rows() 两次结果不同:评估集不可复现,两臂的差就说不清是谁造成的")
    if json.loads(json.dumps(first, ensure_ascii=False)) != json.loads(json.dumps(_rows(), ensure_ascii=False)):
        raise AssertionError("磁盘上的评估集与现在重建的结果不一致:改过规则就要重跑 build_eval_set_mix.py")
    return f"{len(first)} 条可复现且与磁盘一致"


@check("mix 评估集:denoise 只多了一层噪声前缀,subquery 是两句拼的")
def _() -> str:
    mix = _mix_tool()
    slots = {mix.row_id(slot): slot for slot in mix.plan_slots(mix.build_pool())}
    checked = 0
    for row in _rows():
        slot = slots.get(row["id"])
        if slot is None:
            raise AssertionError(f"评估集里的 {row['id']} 重建不出来:抽样规则变过")
        if row["kind"] == "denoise":
            bare = mix._base_question(slot)
            if not row["question"].endswith(bare):
                raise AssertionError(f"denoise 的问题不是「噪声前缀 + 原标题」:{row['question'][:30]!r}")
            prefix = row["question"][: len(row["question"]) - len(bare)]
            if prefix not in mix._NOISE_PREFIXES:
                raise AssertionError(f"用了噪声池以外的前缀:{prefix!r}")
            checked += 1
        elif row["kind"] == "subquery":
            assert slot.partner is not None
            if row["question"] != f"{slot.item.title}，另外{slot.partner.title}？":
                raise AssertionError(f"subquery 的问法不对:{row['question'][:40]!r}")
            if slot.item.section == slot.partner.section:
                raise AssertionError("子查询的两个小节落在同一章:拆开检索与不拆差别太小,测不出东西")
            checked += 1
    return f"{checked} 条(denoise {PER_KIND} + subquery {PER_KIND})的构造规则逐条对得上"


# ---------------------------------------------------------------------------
# B. 两臂:只有改写这一个变量
# ---------------------------------------------------------------------------


@check("两臂数据集:问题逐条相同(除了改写,没有第二个变量)")
def _() -> str:
    base = _raw(BASELINE)
    new = _raw(REWRITE)
    base_q = [row["question"] for row in base["rows"]]
    new_q = [row["question"] for row in new["rows"]]
    if base_q != new_q:
        only_base = [q for q in base_q if q not in set(new_q)][:2]
        raise AssertionError(f"两臂问的不是同一批题(基线独有 {only_base})—— 这样比出来的差不是改写带来的")
    if base["meta"]["source_file"] != new["meta"]["source_file"]:
        raise AssertionError("两臂用的不是同一份评估集")
    return f"{len(base_q)} 条问题逐条相同,评估集 {Path(base['meta']['source_file']).name}"


@check("两臂数据集:rewrite 标志一真一假,基线里不许出现改写字段")
def _() -> str:
    base = _raw(BASELINE)
    new = _raw(REWRITE)
    if base["meta"].get("rewrite") is not False:
        raise AssertionError(f"{BASELINE} 的 meta.rewrite={base['meta'].get('rewrite')!r},基线必须是 False")
    if new["meta"].get("rewrite") is not True:
        raise AssertionError(f"{REWRITE} 的 meta.rewrite={new['meta'].get('rewrite')!r},改写臂必须是 True")
    if not new["meta"].get("rewrite_adapter_dir"):
        raise AssertionError("改写臂没记下适配器目录:换了权重就说不清这批读数是谁跑的")
    leaked = sorted({key for row in base["rows"] for key in row if key.startswith("rewrite") or key == "strategy"})
    if leaked:
        raise AssertionError(f"基线里出现了改写字段 {leaked}:基线必须是 rewriter=None 那条路")
    for row in new["rows"]:
        for key in ("strategy", "queries", "rewrite_ms", "decided_by"):
            if key not in row:
                raise AssertionError(f"改写臂第 {row['id']} 条缺 {key}:归因时看不到改写做了什么")
        if not row.get("rewrite_ms"):
            raise AssertionError(f"改写臂第 {row['id']} 条的 rewrite_ms 是 0:改写没被计时")
    return f"{len(new['rows'])} 条都带策略/检索语句/耗时,基线一条都没有"


@check("两臂数据集:判成 direct 的行不多花 LLM 调用(它一个字都没改)")
def _() -> str:
    direct = [row for row in _raw(REWRITE)["rows"] if row.get("strategy") == "direct"]
    if not direct:
        raise SkipCheck("这一轮改写臂里没有 direct 的行:分类器把每条都判成要改写了;owner:M4.6")
    wasted = [row["id"] for row in direct if int(row.get("rewrite_calls") or 0) != 0]
    if wasted:
        raise AssertionError(
            f"{len(wasted)} 条判成 direct 却调了模型({wasted[:3]}):"
            "direct 的定义就是「一个字都不改」,多调一次既不加信息又会把原话改脏"
        )
    for row in direct:
        if tuple(row.get("queries") or ()) != (row["question"],):
            raise AssertionError(f"{row['id']} 判成 direct,检索语句却不是原话:{row.get('queries')}")
    return f"{len(direct)} 条 direct 全部零调用、且检索语句就是原话"


# ---------------------------------------------------------------------------
# C. 读数与报告
# ---------------------------------------------------------------------------


@check("读数:两份 summary 的指纹 == 当前数据集文件")
def _() -> str:
    detail = []
    for tag in (BASELINE, REWRITE):
        found = sorted(RUNS_DIR.glob(f"run_*_{tag}.summary.csv"))
        if not found:
            raise SkipCheck(f"还没有 tag={tag} 的 summary;owner:M4.6 两臂评估")
        with found[-1].open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        path = DATASET_DIR / row["dataset_file"]
        if not path.exists():
            raise AssertionError(f"{row['dataset_file']} 不在 datasets/ 下,指纹无从比对")
        if _sha12(path) != row["dataset_sha256_12"]:
            raise AssertionError(
                f"{tag} 的指纹对不上:数据集重建过就得重跑评估,别拿旧分数解释新数据集"
            )
        detail.append(f"{tag}@{row['dataset_sha256_12']}({row['rows']} 条)")
    return "、".join(detail)


@check("读数:两臂行数相同,且等于评估集里那条能评的条数")
def _() -> str:
    base = _rows_csv(BASELINE)
    new = _rows_csv(REWRITE)
    if len(base) != len(new):
        raise AssertionError(f"两臂行数不同({len(base)} vs {len(new)}):配对比较会掉样本,而报告里看不出来")
    evaluable = int(_raw(BASELINE)["meta"]["evaluable"])
    if len(base) != evaluable:
        raise AssertionError(f"逐条结果 {len(base)} 行,但数据集只有 {evaluable} 条可评估(拒答的不进 RAGAS)")
    return f"两臂各 {len(base)} 行,与可评估条数一致"


@check("报告:四指标表里的每个数都能从两份 rows.csv 重算出来")
def _() -> str:
    path = _need(REPORT, "M4.6 A/B 报告")
    base = _rows_csv(BASELINE)
    new = _rows_csv(REWRITE)
    index = {row["user_input"]: row for row in new}
    pairs = [(row, index[row["user_input"]]) for row in base if row["user_input"] in index]
    if not pairs:
        raise AssertionError("两臂一条都对不上:两次评估用的可能不是同一份评估集")

    want: dict[str, tuple[float, float, float, int]] = {}
    for name in METRICS:
        # **配对子集**:只留两臂都算出了这个分的行 —— 只有一边有分的行配不成对,
        # 拿它们去比等于用两条不同的样本比大小(报告里那句"n 比 120 少"说的就是这个)
        both = [
            (one, two)
            for row, other in pairs
            if (one := _number(row.get(name))) is not None and (two := _number(other.get(name))) is not None
        ]
        values_base = [one for one, _ in both]
        values_new = [two for _, two in both]
        want[name] = (
            _mean(values_base),
            _mean(values_new),
            _mean(values_new) - _mean(values_base),
            len(both),
        )
    # 四项均值:逐条先算一行自己的均值,再对这些行求均值(与报告同一口径)
    base_four: list[float] = []
    new_four: list[float] = []
    for row, other in pairs:
        one = [_number(row.get(name)) for name in METRICS]
        two = [_number(other.get(name)) for name in METRICS]
        if any(value is None for value in (*one, *two)):
            continue
        base_four.append(_mean([value for value in one if value is not None]))
        new_four.append(_mean([value for value in two if value is not None]))
    want["mean_of_four"] = (
        _mean(base_four),
        _mean(new_four),
        _mean(new_four) - _mean(base_four),
        len(base_four),
    )

    text = path.read_text(encoding="utf-8")
    checked = 0
    for name, (base_value, new_value, delta, count) in want.items():
        line = next((ln for ln in text.splitlines() if ln.startswith(f"| {name} |")), None)
        if line is None:
            raise AssertionError(f"报告里没有 {name} 那一行")
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        got_n = int(cells[1])
        if got_n != count:
            raise AssertionError(
                f"报告的 {name} 配对条数是 {got_n},重算是 {count} —— "
                "配对子集写错会让「两边都有的行」看起来更多,而差值就是在这批行上算的"
            )
        # 差值那一格可能带一个 ✅(自助区间不跨 0),只取数字那一段
        got_base, got_new = float(cells[2]), float(cells[3])
        got_delta = float(cells[4].split()[0])
        for label, got, expected in (("基线", got_base, base_value), ("改写", got_new, new_value), ("差值", got_delta, delta)):
            if abs(got - expected) > REPORT_TOLERANCE:
                raise AssertionError(
                    f"报告的 {name} {label} 是 {got},重算是 {expected:.4f} —— "
                    "报告必须由产物算出来,手抄一个数进来是最难发现的一种错"
                )
        checked += 1
    return f"{checked} 个指标 × (n + 3 列)全部与 rows.csv 重算一致(两臂配对 {len(pairs)} 条)"


@check("报告:代价与噪声底那两节在(延迟 / 钱 / 改写行为 / 噪声底),不能只报好指标")
def _() -> str:
    text = _need(REPORT, "M4.6 A/B 报告").read_text(encoding="utf-8")
    for needle in ("## 三、代价", "### 延迟", "### 钱", "## 四、改写到底做了什么", "95% 自助区间", "噪声底"):
        if needle not in text:
            raise AssertionError(f"报告里缺「{needle}」:只报四指标会把代价与噪声藏起来")
    return "四指标 / 分层 / 噪声底对照 / 延迟 / 钱 / 改写行为 / 自助区间 都在"


def main() -> int:
    return report("M4.6 查询改写 A/B 验收")


if __name__ == "__main__":
    sys.exit(main())
