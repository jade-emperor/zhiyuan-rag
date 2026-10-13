#!/usr/bin/env python
"""M4 验收：RAG 系统评估（四指标闭环）。

用法::

    uv run python scripts/acceptance/check_m4_eval.py

本脚本是 M4 契约的**唯一权威**（``docs/STATUS.md`` 只留设计理由，不重复规格）。
允许存在的 SKIP 只有两类：前置条件不满足（容器 / 缺 API key / 评估还没跑），
或**在理由里点名了 owner 的后继里程碑**。

M4 分步::

    M4.1 语料入库:data/corpus/bagu_ai_data + bagu_java_data → Milvus（两个新 source）
    M4.2 评估集构造:八股文 H3 小节 → question / ground_truth（含口语化改写组）
    M4.3 dataset builder:参考实现里缺的那一环 —— 跑本系统批量产出 answer + contexts
    M4.4 RAGAS 四指标 + 带元数据的 CSV 落盘
    M4.5 本脚本 + 文档收尾

**评估不进产品代码**:M4 的全部产物都在 ``scripts/`` 与 ``data/``（用户明确过
``src/**`` 由作者自己敲）。所以本脚本有一条检查钉住「``src/`` 里不出现 ragas」。

评估集契约（``data/eval/eval_set_bagu_v1.json``）::

    {"meta": {...}, "rows": [{
        "id": "<sha256(source|section|title|kind)[:12]>",   # 确定性,给 builder 断点续跑用
        "question": "...",          # 真正丢给检索的字符串
        "ground_truth": "...",      # 人工/半自动参考答案(八股文小节的正文)
        "source": "bagu_ai" | "bagu_java",
        "section": "Python",        # H2 章节名,只作元数据,不拼进 question
        "title": "装饰器",           # H3 标题去掉序号
        "kind": "verbatim" | "paraphrase",
    }]}

两条口径,都是刻意的:

1. **``question`` 不拼章节前缀**（不写成「Python：装饰器」）。拼了等于把答案的
   关键词白送给检索器,指标会虚高;章节名只存进 ``section`` 供分层排查。
2. **同一道题不测两遍**:``verbatim`` 与 ``paraphrase`` 取自**不相交**的 title 集合。

``paraphrase`` 是**确定性规则**改写（加口语前缀/疑问尾,原词一个不删）,
不调 LLM:评估集本身必须可复现,否则两次指标差异说不清是系统的还是评估集的。
LLM 造题（三个评估代理）留在后面,见 ``docs/deferred.md``。

dataset builder 契约（``data/eval/datasets/<name>_dataset.json``）::

    [{"user_input": "...", "retrieved_contexts": ["..."], "response": "...", "reference": "..."}]

**恰好这四个键** —— ragas 0.4.x 的 ``SingleTurnSample`` 是 pydantic 模型,
多一个键、少一个键都会当场报错。``retrieved_contexts`` 必须是 **list[str] 且非空**。字段名的来历:``question/answer/context/
ground_truth`` 是 ragas 0.4.x **之前**的叫法,现在换成了
``user_input/response/retrieved_contexts/reference``;两个名字的**映射只发生在
builder 写文件的那一刻**,评估集里仍旧用旧名字。

同目录另有一份 ``<name>_raw.json``,是**排障用**的:每条带 citations
（chunk_id / doc_id / source / routes / score / rerank_score）、是否拒答、耗时。
ragas 读不了它,但「检索到底给了什么」只能从这里看,而且 ``meta`` 里记着
**建数据集那一刻**的四个检索旋钮 —— 那是 summary 里 ``rag_*`` 列的**唯一合法来源**。

**数据集按旋钮命名、可以有多份**。评估集（题目）与配置无关,但数据集（答案 + contexts）
是「某个配置下的系统」跑出来的产物,所以调参会产出不同的数据集文件
（``bagu_v1`` = k5/m2；调参时另有一份 ``bagu_v1_k10m3``）。这些检查**遍历目录里所有**
``*_dataset.json``,不写死名字 —— 每一份都要同样合格,否则 A/B 的两条腿一条粗一条细。

的第一组 A/B（``retrieval_k`` 5→10、``candidate_m`` 2→3）**结论是回退到 5/2**
（均值 0.9347 vs 0.9313）,那份 10/3 的数据集已按结论删除;它那次的**结果 CSV 保留在
``ragas_runs/`` 里作为历史读数**（输入已删,不再可核 —— provenance 检查会显式报出来,
不报的话下一个人会以为它还核得动）。

⚠️ **summary 里的 ``rag_*`` 必须取自数据集的 provenance,不能读实时配置**:
踩过一次 —— 数据集在 k=5/m=2 下建,跑到一半把配置改成 10/3,summary 差点把 k 记成 10,
那份 CSV 就变成一个查不出错的谎。

RAGAS 运行契约（``data/eval/ragas_runs/run_<UTC时间戳>_<tag>.{summary,rows}.csv``）::

    summary: 一行,元数据 + 四个分数
             run_at tag dataset_file dataset_sha256_12 rows judge_provider judge_model
             embedding_model metrics rag_fusion rag_k rag_candidate_m rag_rerank
             faithfulness answer_relevancy context_precision context_recall mean_of_four errors
    rows:    每条样本一行,四字段 + 四个分数

- **绝不覆盖**:文件名带时间戳（原始写法是写死文件名 + 只写一行,
  而它自己又说「便于多次运行比较」—— 这两句互相矛盾,这里按后者）。
- **元数据是必填**:CSV 里不记 judge 模型 / 指标版本 / 数据集指纹的话,
  两次指标差 0.1 都没法解释。``dataset_sha256_12`` 还让检查能验
  「这份结果确实来自那份数据集」。
- **四个指标列是均值,``errors`` 列记有几个指标出现过空值**（单条判分失败的分数会是空,
  不当场炸整轮 —— 但也不许悄悄按 0 算）。``mean_of_four`` 是四个指标的均值。

**拒答样本不进 RAGAS,但必须留在报告里**:检索为空 → ``contexts`` 为空,
``context_recall`` 无从计算,所以 ``<name>_dataset.json`` 只放「检索到东西」的那些条。
但 ``<name>_raw.json`` **全量保留**并把拒答数写进 ``meta``,验收还钉住「拒答率 ≤ 10%」——
拒答一大片说明检索根本没接对,那时候四个分数再漂亮也没意义。

裁判模型的选择（**与常见写法不同,理由充分**）::

    常见写法:ChatOpenAI(model="gpt-4") + OpenAIEmbeddings,API key 还是硬编码占位符
    本项目:judge = DeepSeek（复用 settings.llm 的 model/base_url/key）
            embedding = 本地 BGE-M3（复用 Embedder 端口,零成本、零新增外部依赖）

理由:① 本项目**没有也不打算有** OpenAI key,RAG 链路走的就是 DeepSeek,
评估再引一套国外 API 等于凭空多一个计费项与失败点;② ``answer_relevancy``
需要一个 embedding 服务,而项目里已经有 BGE-M3 与它的端口,再引 OpenAI embeddings
属于重复建设。有一条检查静态盯着「评估脚本里不出现 OPENAI_API_KEY / openai.com」。

指标名的版本差异（**实测,照抄旧写法会崩**）::

    旧写法: faithfulness / answer_relevancy / context_relevancy / context_recall
    0.4.3: faithfulness / answer_relevancy / context_precision  / context_recall

``context_relevancy`` 在 0.4.x 已不存在（换成 ``context_precision``,
并且从「需要 ground_truth」变成同样要 reference）。参考实现留下的
``data/eval/ragas_evaluation_results.csv`` 里也是 ``context_precision`` ——
**两边独立印证**了这个改名,不是我们挑的。

两个依赖坑（都在 ``scripts/tools/`` 里绕开了,细节见那两个文件的 docstring）:

1. ``import ragas`` 在 0.4.3 + langchain-community 0.4.x 下**直接崩**
   （硬导入已拆走的 ``langchain_community.chat_models.vertexai``）→ ``ragas_compat.py``。
2. ``llm_factory(provider="deepseek")`` 在 0.4.3 里**把 OpenAI 客户端当成 Anthropic 客户端用**
   （``client.messages.create``）→ 自己 ``instructor.from_openai()`` + ``InstructorLLM``。

分层（静态扫描,不靠自觉）:``src/`` 里不得出现 ``ragas`` —— 评估是脚本层的活儿;
``scripts/tools/`` 下的评估脚本不得 ``import pymilvus``（向量库只经 VectorStore 端口进）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from _harness import check, report, SkipCheck

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"
TOOLS = ROOT / "scripts" / "tools"

EVAL_SET = ROOT / "data" / "eval" / "eval_set_bagu_v1.json"
LEGACY_EVAL = ROOT / "data" / "eval" / "rag_evaluate_data.json"
DATASET_DIR = ROOT / "data" / "eval" / "datasets"
RUNS_DIR = ROOT / "data" / "eval" / "ragas_runs"

# 契约默认值（改这里就得同时改 build_eval_set.py 的默认值）
BAGU_SOURCES = ("bagu_ai", "bagu_java")
KINDS = ("verbatim", "paraphrase")
PER_SOURCE_PER_KIND = 28
MIN_GROUND_TRUTH_CHARS = 20

# 四个指标在 ragas 0.4.3 里的名字（顺序 = 报告顺序）
METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
# ragas 0.4.x 的 SingleTurnSample 字段名（builder 必须原样写这四个键）
SAMPLE_KEYS = ("user_input", "retrieved_contexts", "response", "reference")
SUMMARY_META_COLUMNS = (
    "run_at",
    "tag",
    "dataset_file",
    "dataset_sha256_12",
    "raw_file",
    "raw_sha256_12",
    "rows",
    "judge_provider",
    "judge_model",
    "embedding_model",
    "metrics",
    "rag_fusion",
    "rag_k",
    "rag_candidate_m",
    "rag_rerank",
)
# 评估脚本里**不准出现**的字符串：裁判与嵌入都不许走 OpenAI 官方端点
FORBIDDEN_IN_EVAL_CODE = ("OPENAI_API_KEY", "openai.com", "text-embedding-ada-002")
# dataset builder 允许的拒答率上限（检索真的能工作的话，拒答应该是少数）
MAX_REFUSAL_RATE = 0.10


def _load_json(path: Path):
    if not path.exists():
        raise SkipCheck(f"{path.relative_to(ROOT)} 还不存在；owner：M4.2 评估集构造")
    return json.loads(path.read_text(encoding="utf-8"))


def _eval_rows() -> list[dict]:
    doc = _load_json(EVAL_SET)
    if not isinstance(doc, dict) or "rows" not in doc:
        raise AssertionError(f"{EVAL_SET.name} 顶层要是 {{meta, rows}}，实际是 {type(doc).__name__}")
    rows = doc["rows"]
    if not isinstance(rows, list) or not rows:
        raise AssertionError("rows 要是非空列表")
    return rows


def _dataset_path(name: str) -> Path:
    return DATASET_DIR / f"{name}_dataset.json"


def _datasets() -> list[Path]:
    """输入:无。行为:列出 datasets/ 下所有数据集。输出:按名排序的路径列表(空则 SKIP)。

    **按目录列举而不是写死一个名字**:M4 的产出之一就是「同一份评估集、不同检索旋钮」
    的对比（`bagu_v1` = k5/m2 基线,`bagu_v1_k10m3` = 调参后），每一份都要同样合格。
    """
    if not DATASET_DIR.exists():
        raise SkipCheck(f"{DATASET_DIR.relative_to(ROOT)} 还没有；owner：M4.3 dataset builder")
    paths = sorted(DATASET_DIR.glob("*_dataset.json"))
    if not paths:
        raise SkipCheck(f"{DATASET_DIR.relative_to(ROOT)} 里还没有 *_dataset.json；owner：M4.3 dataset builder")
    return paths


def _tool(name: str) -> Path:
    return TOOLS / name


def _read_tool(name: str) -> str:
    path = _tool(name)
    if not path.exists():
        raise SkipCheck(f"scripts/tools/{name} 还没落地；owner：M4")
    return path.read_text(encoding="utf-8")


def _import_tool(name: str, module: str):
    """把 scripts/tools 挂进 sys.path 后按模块名导入（脚本们不是包,没有 __init__.py）。"""
    tools = str(TOOLS)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    _read_tool(name)
    return __import__(module)


def _sha256_12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _help_ok(path: Path) -> str:
    """跑 `脚本 --help`；只要退出码 0 就算这个脚本的 CLI 立起来了。"""
    if not path.exists():
        raise SkipCheck(f"{path.name} 还没落地；owner：M4")
    proc = subprocess.run(
        [sys.executable, str(path), "--help"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(f"--help 退出码 {proc.returncode}：{proc.stderr.strip()[-300:]}")
    return "退出码 0"


def _m4_script_texts() -> dict[str, str]:
    """M4 相关、且**是代码**的脚本正文（ragas_compat 的 docstring 里会提到 OpenAI，只扫代码段）。"""
    texts: dict[str, str] = {}
    for name in (
        "build_eval_set.py",
        "build_eval_dataset.py",
        "run_ragas_eval.py",
        "ragas_judge.py",
    ):
        path = _tool(name)
        if path.exists():
            texts[name] = path.read_text(encoding="utf-8")
    if not texts:
        raise SkipCheck("M4 的脚本都还没落地；owner：M4")
    return texts


def _options_kwargs(path: Path) -> set[str]:
    """输入:一个 python 文件。行为:找出里面 `RetrievalOptions(...)` 调用的关键字名。输出:名字集合。

    用来钉「评估脚本与 CLI 用的是同一套检索旋钮」—— 两边任何一边多一个少一个,
    评估测的就不是线上跑的那个系统了。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "RetrievalOptions":
            found |= {keyword.arg for keyword in node.keywords if keyword.arg}
            found |= {arg.id for arg in node.args if isinstance(arg, ast.Name)}
    return found


def _code_only(text: str) -> str:
    """去掉 docstring / 注释后的代码正文（判断「有没有真的用 OpenAI」只看代码）。"""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# A. 依赖与裁判：ragas 能不能用、用的是谁
# ---------------------------------------------------------------------------


@check("依赖：ragas 能 import（靠 scripts/tools/ragas_compat.py 绕开上游坏导入）")
def _() -> str:
    _read_tool("ragas_compat.py")
    mod = _import_tool("ragas_compat.py", "ragas_compat")
    if mod.SHIMMED:
        return f"垫片生效：{mod.REASON}"
    return f"垫片已是 no-op（上游修好了）：{mod.REASON}"


@check("依赖：四个指标名在 0.4.3 里真的存在，且要的字段 builder 都写了")
def _() -> str:
    compat = _import_tool("ragas_compat.py", "ragas_compat")
    metrics = compat.legacy_metrics()
    if set(metrics) != set(METRIC_NAMES):
        raise AssertionError(
            f"ragas_compat.legacy_metrics() 给了 {sorted(metrics)}，契约写的是 {sorted(METRIC_NAMES)}"
        )
    provided = set(SAMPLE_KEYS)
    found: dict[str, set[str]] = {}
    for name, metric in metrics.items():
        cols = set(next(iter(metric.required_columns.values())))
        extra = cols - provided
        if extra:
            raise AssertionError(f"{name} 还要 {sorted(extra)}，builder 没写这些字段")
        found[name] = cols
    covered = set().union(*found.values())
    if covered != provided:
        raise AssertionError(f"这四列里有字段一个指标都没用到：{sorted(provided - covered)}")
    # 四个指标要的字段**不一样**（context 那两个不要 response），所以逐条打印而不是统一口径
    return "、".join(f"{name}={len(cols)}" for name, cols in found.items()) + " 字段，并集正好是四列"


@check("裁判：评估脚本里不出现 OpenAI 官方端点 / key（judge 与 embedding 都不走它）")
def _() -> str:
    hits: list[str] = []
    for name, text in _m4_script_texts().items():
        code = _code_only(text)
        for bad in FORBIDDEN_IN_EVAL_CODE:
            if bad in code:
                hits.append(f"{name}: {bad}")
    if hits:
        raise AssertionError("只准走 settings.llm（DeepSeek）与本地 BGE-M3：" + "、".join(hits))
    return f"{len(_m4_script_texts())} 个脚本的代码段干净"


@check("裁判：judge 的 base_url / model / key 来自 settings.llm，不写死")
def _() -> str:
    text = _read_tool("ragas_judge.py")
    code = _code_only(text)
    missing = [name for name in ("base_url", "deepseek_api_key", "model") if name not in code]
    if missing:
        raise AssertionError(f"ragas_judge.py 的代码段里没出现 {missing}：judge 可能被写死了")
    if "get_secret_value" not in code:
        raise AssertionError(
            "没调 SecretStr.get_secret_value()：OpenAI SDK 拿到 SecretStr 会把它 str 成 "
            "'**********' 发出去，实测报 401（api key is invalid），很容易误判成 key 过期"
        )
    return "base_url / model / key 都取自 settings.llm"


# ---------------------------------------------------------------------------
# B. 评估集
# ---------------------------------------------------------------------------


@check("评估集：文件存在、meta + rows 结构、每条字段齐全")
def _() -> str:
    doc = _load_json(EVAL_SET)
    if "meta" not in doc:
        raise AssertionError("缺 meta（评估集要能自证是哪份语料、什么时候、按什么规则造的）")
    rows = _eval_rows()
    need = {"id", "question", "ground_truth", "source", "section", "title", "kind"}
    for index, row in enumerate(rows):
        missing = need - set(row)
        if missing:
            raise AssertionError(f"第 {index} 条缺 {sorted(missing)}")
    return f"{len(rows)} 条"


@check(f"评估集：分层 = {len(BAGU_SOURCES)} 个 source × {len(KINDS)} 种问法 × {PER_SOURCE_PER_KIND} 条")
def _() -> str:
    rows = _eval_rows()
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        counts[(row["source"], row["kind"])] = counts.get((row["source"], row["kind"]), 0) + 1
    want = {(source, kind): PER_SOURCE_PER_KIND for source in BAGU_SOURCES for kind in KINDS}
    if counts != want:
        raise AssertionError(f"实际分层 {counts}，契约是 {want}")
    return " × ".join(f"{s}/{k}={counts[(s, k)]}" for s, k in sorted(counts))


@check("评估集：question 唯一、ground_truth 非空、来源都落在约定的 source 上")
def _() -> str:
    rows = _eval_rows()
    questions = [row["question"] for row in rows]
    if len(set(questions)) != len(questions):
        dup = [q for q in questions if questions.count(q) > 1]
        raise AssertionError(f"question 有重复：{sorted(set(dup))[:3]}")
    for row in rows:
        if row["source"] not in BAGU_SOURCES:
            raise AssertionError(f"未知 source：{row['source']}")
        if len(row["ground_truth"].strip()) < MIN_GROUND_TRUTH_CHARS:
            raise AssertionError(
                f"ground_truth 太短（<{MIN_GROUND_TRUTH_CHARS} 字）：{row['title']!r} —— "
                "空答案会让 context_recall 变成无意义的 0"
            )
    return f"{len(rows)} 条 question 唯一，最短 ground_truth {min(len(r['ground_truth']) for r in rows)} 字"


@check("评估集：verbatim 原样、paraphrase 只加口语前后缀（原词一个不删）")
def _() -> str:
    rows = _eval_rows()
    for row in rows:
        if row["kind"] == "verbatim":
            if row["question"] != row["title"]:
                raise AssertionError(f"verbatim 的 question 必须等于 title：{row['question']!r} != {row['title']!r}")
        elif row["kind"] == "paraphrase":
            if row["question"] == row["title"]:
                raise AssertionError(f"paraphrase 与原文一字不差：{row['question']!r}")
            if row["title"] not in row["question"]:
                raise AssertionError(
                    f"paraphrase 把原词改掉了：{row['question']!r} 里找不到 {row['title']!r} —— "
                    "改写的目的是测「加了口语噪声还能不能召回」，不是测同义替换"
                )
        else:
            raise AssertionError(f"未知 kind：{row['kind']}")
    return f"verbatim {sum(1 for r in rows if r['kind'] == 'verbatim')} / paraphrase {sum(1 for r in rows if r['kind'] == 'paraphrase')}"


@check("评估集：同一道题不测两遍（verbatim 与 paraphrase 的 title 集合不相交）")
def _() -> str:
    rows = _eval_rows()
    verbatim = {row["title"] for row in rows if row["kind"] == "verbatim"}
    paraphrase = {row["title"] for row in rows if row["kind"] == "paraphrase"}
    overlap = verbatim & paraphrase
    if overlap:
        raise AssertionError(f"这些 title 两组都抽到了：{sorted(overlap)[:3]}")
    return f"两组各 {len(verbatim)} / {len(paraphrase)} 个互不相交的 title"


@check("评估集：确定性 —— 重建两次，rows 逐字相同")
def _() -> str:
    mod = _import_tool("build_eval_set.py", "build_eval_set")
    first = mod.build_rows()
    second = mod.build_rows()
    if first != second:
        raise AssertionError("build_rows() 两次结果不同：评估集不可复现，指标差异就说不清了")
    on_disk = _eval_rows()
    if json.loads(json.dumps(first, ensure_ascii=False)) != json.loads(
        json.dumps([dict(row) for row in on_disk], ensure_ascii=False)
    ):
        raise AssertionError("磁盘上的评估集与现在重建的结果不一致：改过规则就要重跑 build_eval_set.py")
    return f"{len(first)} 条可复现，且与磁盘一致"


@check("评估集：id 是 sha256 前 12 位（builder 靠它断点续跑，必须确定性）")
def _() -> str:
    for row in _eval_rows():
        raw = f"{row['source']}|{row['section']}|{row['title']}|{row['kind']}"
        want = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        if row["id"] != want:
            raise AssertionError(f"id 对不上：{row['id']} != sha256({raw})[:12] = {want}")
    return "全部 12 位十六进制且口径一致"


# ---------------------------------------------------------------------------
# C. dataset builder（参考实现里缺的那一环）
# ---------------------------------------------------------------------------


@check("builder：脚本能 --help（CLI 立起来了）")
def _() -> str:
    return _help_ok(_tool("build_eval_dataset.py"))


@check("builder：检索旋钮与 cli/commands/rag.py 逐字对齐（评的必须是同一个系统）")
def _() -> str:
    want = _options_kwargs(SRC / "cli" / "commands" / "rag.py")
    got = _options_kwargs(_tool("build_eval_dataset.py"))
    if not want or not got:
        raise AssertionError(f"有一边没构造 RetrievalOptions：rag.py={sorted(want)} builder={sorted(got)}")
    if want != got:
        raise AssertionError(
            f"两边旋钮不一致：rag.py 独有 {sorted(want - got)}，builder 独有 {sorted(got - want)} —— "
            "评估脚本自己能改旋钮的话,「指标说的是哪个系统」就说不清了"
        )
    return f"{len(got)} 个旋钮一致：{'、'.join(sorted(got))}"


@check("builder：每个数据集都**恰好**是 ragas 认的那四个键")
def _() -> str:
    paths = _datasets()
    for path in paths:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            raise AssertionError(f"{path.name} 要是非空 list")
        for index, row in enumerate(rows):
            extra = set(row) - set(SAMPLE_KEYS)
            missing = set(SAMPLE_KEYS) - set(row)
            if extra or missing:
                raise AssertionError(
                    f"{path.name} 第 {index} 条键不对：多 {sorted(extra)} 少 {sorted(missing)} —— "
                    "ragas 的 SingleTurnSample 是 pydantic，多一个键当场报错"
                )
    return "、".join(f"{path.stem.replace('_dataset', '')}={len(json.loads(path.read_text(encoding='utf-8')))} 条" for path in paths)


@check("builder：contexts 是 list[str] 且非空")
def _() -> str:
    detail = []
    for path in _datasets():
        rows = json.loads(path.read_text(encoding="utf-8"))
        for index, row in enumerate(rows):
            contexts = row["retrieved_contexts"]
            if not isinstance(contexts, list) or not contexts:
                raise AssertionError(f"{path.name} 第 {index} 条的 contexts 不是非空 list")
            if not all(isinstance(c, str) and c.strip() for c in contexts):
                raise AssertionError(f"{path.name} 第 {index} 条里混进了空串或非字符串")
        detail.append(f"{path.stem.replace('_dataset', '')} 均 {sum(len(r['retrieved_contexts']) for r in rows) / len(rows):.1f} 块/问")
    return "；".join(detail)


@check("builder：reference 与评估集逐条一致（没有错位）")
def _() -> str:
    by_question = {row["question"]: row["ground_truth"] for row in _eval_rows()}
    detail = []
    for path in _datasets():
        if not path.stem.startswith("bagu_v1"):
            continue  # course30 是另一份评估集,字段名与我们的不同,单独有一条检查
        rows = json.loads(path.read_text(encoding="utf-8"))
        for index, row in enumerate(rows):
            want = by_question.get(row["user_input"])
            if want is None:
                raise AssertionError(f"{path.name} 第 {index} 条的 question 不在评估集里:{row['user_input']!r}")
            if row["reference"] != want:
                raise AssertionError(f"{path.name} 第 {index} 条 reference 与评估集对不上（错位了）")
        # **不要求两份条数相同**:拒答样本进不了 RAGAS(见 docstring),
        # 换了检索旋钮之后拒答的可能不是同一条 —— 只要求"每一条都对得上"
        detail.append(f"{path.stem.replace('_dataset', '')}={len(rows)}/{len(by_question)}")
    return "、".join(detail)


@check("builder：raw 文件带 citations（chunk_id / routes / score），拒答率 ≤ 10%")
def _() -> str:
    detail = []
    for dataset in _datasets():
        path = dataset.with_name(dataset.name.replace("_dataset.json", "_raw.json"))
        if not path.exists():
            raise AssertionError(f"缺 {path.name}：排障信息（citations / 拒答 / 耗时）没落盘")
        doc = json.loads(path.read_text(encoding="utf-8"))
        rows = doc["rows"] if isinstance(doc, dict) else doc
        refused = [row for row in rows if row.get("refused")]
        rate = len(refused) / len(rows)
        if rate > MAX_REFUSAL_RATE:
            raise AssertionError(f"{path.name} 拒答率 {rate:.1%} > {MAX_REFUSAL_RATE:.0%}：检索大概率没接对")
        with_citations = [row for row in rows if row.get("citations")]
        if len(with_citations) < len(rows) - len(refused):
            raise AssertionError(f"{path.name}:有非拒答样本却没记 citations")
        missing = {"chunk_id", "doc_id", "source", "routes", "score"} - set(with_citations[0]["citations"][0])
        if missing:
            raise AssertionError(f"{path.name} 的 citations 里缺 {sorted(missing)}")
        detail.append(f"{path.stem.replace('_raw', '')} 拒答 {len(refused)}/{len(rows)}")
    return "；".join(detail)


@check("builder：那份 30 条的旧格式也能跑（同一套 builder 吃得下）")
def _() -> str:
    if not LEGACY_EVAL.exists():
        raise SkipCheck(f"{LEGACY_EVAL.name} 不在；owner：数据（随课程提供）")
    path = _dataset_path("course30")
    if not path.exists():
        raise SkipCheck(f"{path.relative_to(ROOT)} 还没生成；owner：M4.3 dataset builder")
    rows = json.loads(path.read_text(encoding="utf-8"))
    want = json.loads(LEGACY_EVAL.read_text(encoding="utf-8"))
    if len(rows) != len(want):
        raise AssertionError(f"原文件里有 {len(want)} 条，数据集里 {len(rows)} 条")
    for row in rows:
        if set(row) != set(SAMPLE_KEYS):
            raise AssertionError("键不对，见上一条")
    return f"{len(rows)} 条（原题；它的 ground_truth 只有几个字 → claim 少 → context_recall 反而容易满分）"


# ---------------------------------------------------------------------------
# D. RAGAS 运行与落盘
# ---------------------------------------------------------------------------


@check("runner：脚本能 --help（CLI 立起来了）")
def _() -> str:
    return _help_ok(_tool("run_ragas_eval.py"))


@check("落盘：至少一次 run，summary 的元数据列 + 四个指标列齐全")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck(f"{RUNS_DIR.relative_to(ROOT)} 还没有；owner：M4.4 跑第一版 RAGAS")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4 跑第一版 RAGAS")
    import csv

    path = summaries[-1]
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise AssertionError(f"summary 该只有一行（多次运行靠多个文件），实际 {len(rows)} 行")
    header = set(rows[0])
    missing = [col for col in (*SUMMARY_META_COLUMNS, *METRIC_NAMES) if col not in header]
    if missing:
        raise AssertionError(f"{path.name} 缺列 {missing}")
    return f"{path.name}：{len(header)} 列"


@check("落盘：四指标都落在 [0,1] 且非 NaN")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    import csv

    with summaries[-1].open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    scores = {}
    for name in METRIC_NAMES:
        if name not in row or row[name].strip() == "":
            raise AssertionError(f"{name} 是空的：这一列没算出来（看 errors 列）")
        value = float(row[name])
        if not 0.0 <= value <= 1.0:
            raise AssertionError(f"{name}={value} 不在 [0,1]")
        scores[name] = value
    return "、".join(f"{name}={value:.3f}" for name, value in scores.items())


@check("落盘：rows.csv 行数 == summary 的 rows")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    import csv

    path = summaries[-1]
    with path.open(encoding="utf-8", newline="") as handle:
        summary = next(csv.DictReader(handle))
    rows_path = path.with_name(path.name.replace(".summary.csv", ".rows.csv"))
    if not rows_path.exists():
        raise AssertionError(f"缺 {rows_path.name}：summary 之外的逐条分数没落盘")
    with rows_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != int(summary["rows"]):
        raise AssertionError(f"rows.csv 有 {len(rows)} 行，summary 写的是 {summary['rows']}")
    return f"{len(rows)} 行一致"


@check("落盘：summary 记的数据集指纹 == 当前数据集文件（结果对得上输入）")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    import csv

    with summaries[-1].open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    name = Path(row["dataset_file"]).name
    path = DATASET_DIR / name
    if not path.exists():
        raise SkipCheck(f"{name} 现在不在 datasets/ 下，无法比对指纹")
    if _sha256_12(path) != row["dataset_sha256_12"]:
        raise AssertionError(
            f"指纹对不上：{name} 现在是 {_sha256_12(path)}，summary 记的是 {row['dataset_sha256_12']} —— "
            "数据集重建过就得重跑评估，别拿旧分数解释新数据集"
        )
    return f"{name} @ {row['dataset_sha256_12']}"


@check("落盘：每份结果的检索旋钮都取自它自己的数据集 provenance")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    import csv

    verified: list[str] = []
    orphans: list[str] = []
    for path in summaries:
        with path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        raw_name = (row.get("raw_file") or "").strip()
        raw_path = DATASET_DIR / raw_name if raw_name else None
        # **先比指纹再比内容**:数据集原地重建（换旋钮重跑）是正常操作,
        # 那时旧结果的 provenance 就"失效"了 —— 必须靠哈希认出来,
        # 否则会拿**新** raw 文件的旋钮去核**旧**结果,报一个看起来像代码 bug 的错。
        stale = raw_path is None or not raw_path.exists() or _sha256_12(raw_path) != row.get("raw_sha256_12")
        if stale:
            orphans.append(path.name)
            if path == summaries[-1]:
                raise AssertionError(
                    f"最新那份结果（{path.name}）的数据集 {raw_name or '(空)'} 已不在原地"
                    "（被删或已被重建）—— 没有 provenance 就没法证明这四个旋钮是**建数据集时**那份配置。"
                    "实测踩过:数据集在 k=5/m=2 下建、中途配置改成 10/3,summary 差点把 k 记成 10"
                )
            continue
        assert raw_path is not None  # stale 为假 ⇒ 路径存在
        meta = json.loads(raw_path.read_text(encoding="utf-8"))["meta"]
        pairs = {
            "rag_fusion": meta.get("fusion"),
            "rag_k": meta.get("k"),
            "rag_candidate_m": meta.get("candidate_m"),
            "rag_rerank": meta.get("rerank"),
        }
        for column, want in pairs.items():
            if str(row[column]) != str(want):
                raise AssertionError(f"{path.name} 的 {column}={row[column]}，{raw_name} 的 meta 是 {want}")
        verified.append(f"{raw_name}=k{pairs['rag_k']}/m{pairs['rag_candidate_m']}")
    detail = f"{len(verified)} 份逐列核对通过（{'、'.join(verified)}）"
    if orphans:
        # 输入被删/被重建的历史读数**不是错误**:结论已经落进 docs/STATUS.md §0.4,
        # 但必须显式报出来 —— 不报的话,下一个人会以为它还核得动
        detail += f"；{len(orphans)} 份的输入已被删或重建,只剩历史读数不可再核"
    return detail


@check("落盘：groups.csv 分组行数之和 == rows（分组是用来解释总分的，不能漏样本）")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    import csv

    path = summaries[-1]
    groups_path = path.with_name(path.name.replace(".summary.csv", ".groups.csv"))
    if not groups_path.exists():
        raise AssertionError(f"缺 {groups_path.name}：verbatim 与 paraphrase 的差会被整份均值盖掉")
    with groups_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AssertionError("groups.csv 是空的")
    total = sum(int(row["rows"]) for row in rows)
    with path.open(encoding="utf-8", newline="") as handle:
        summary = next(csv.DictReader(handle))
    if total != int(summary["rows"]):
        raise AssertionError(f"分组行数之和 {total} != rows {summary['rows']}")
    return "、".join(f"{row['source']}/{row['kind']}={row['rows']}" for row in rows)


@check("落盘：多次运行不互相覆盖（文件名各自带时间戳）")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if not summaries:
        raise SkipCheck("还没有 summary.csv；owner：M4.4")
    names = [path.name for path in summaries]
    if len(set(names)) != len(names):
        raise AssertionError("文件名重复")
    for name in names:
        # run_<YYYYmmddTHHMMSSZ>_<tag>.summary.csv
        stamp = name.split("_")[1]
        if len(stamp) < 15 or "T" not in stamp:
            raise AssertionError(f"{name} 的时间戳格式不对：评估结果要能排序与区分")
    return f"{len(names)} 份历史结果并存（最新 {names[-1]}）"


# ---------------------------------------------------------------------------
# E. 数据现状与分层
# ---------------------------------------------------------------------------


@check("数据：Milvus 里两个新 source 都有块（M4.1 的入库结果）")
def _() -> str:
    from collections import Counter

    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import get_settings

    try:
        rows = MilvusVectorStore(get_settings().milvus).load_all()
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题,不是代码问题
        raise SkipCheck(
            f"连不上 Milvus（{type(exc).__name__}）：先确认 Windows 侧 Docker Desktop 起来了；"
            "owner：环境（check_env_services.py 能验）"
        ) from exc
    counts = Counter(row.source for row in rows)
    if not rows:
        # **空库是"还没灌",不是"契约坏了"**:作者 drop 掉集合、或新克隆的机器上都会走到这里。
        # 按项目口径:缺数据要 SKIP 并点名 owner,而不是 FAIL(FAIL 会被读成"代码回归")。
        raise SkipCheck(
            f"Milvus 里一个块都没有（{get_settings().milvus.collection}）—— 先灌语料："
            "`uv run project-rag ingest --path data/corpus/<source>_data`；"
            "owner：数据（M4.1 的入库结果，属于部署动作，不是代码）"
        )
    missing = [source for source in BAGU_SOURCES if counts.get(source, 0) == 0]
    if missing:
        raise AssertionError(
            f"库里没有 {missing} 的块（别的 source 有、这两个没有 —— 那是**灌错了**，不是没灌）："
            "跑 `uv run project-rag ingest --path data/corpus/<source>_data`，或 `project-rag doctor` 对账"
        )
    return "、".join(f"{source}={counts[source]}" for source in sorted(counts))


@check("范围：compare_runs.py 还能跑（只读已有 CSV、不花钱；噪声不再是流程的一环）")
def _() -> str:
    path = _tool("compare_runs.py")
    if not path.exists():
        raise SkipCheck("scripts/tools/compare_runs.py 还没落地；owner：M4 收尾")
    proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise AssertionError(f"退出码 {proc.returncode}：{proc.stderr.strip()[-300:]}")
    out = proc.stdout
    if "次运行" not in out or "■" not in out:
        raise AssertionError("没打印分组结果：噪声带无从读起")
    groups = out.count("■")
    # 只有某个分组跑过 ≥2 次时才会给出结论句;一条都没有时也得说清"每组只有 1 次"
    if "噪声带" not in out and "n=1" not in out.replace(" ", ""):
        raise AssertionError("既没给噪声带结论,也没说明样本不足")
    return f"{groups} 个分组；{'已给出 Δ 阈值' if '噪声带' in out else '目前每组只有 1 次运行'}"


@check("分层：src/ 里不出现 ragas（评估是脚本层的活儿，不进产品代码）")
def _() -> str:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "ragas" in stripped:
                hits.append(f"{path.relative_to(ROOT)}:{line_no}")
    if hits:
        raise AssertionError("src/ 里混进了 ragas：" + "、".join(hits))
    return f"{len(list(SRC.rglob('*.py')))} 个文件干净"


@check("分层：评估脚本不直接 import pymilvus（向量库只经 VectorStore 端口进）")
def _() -> str:
    hits: list[str] = []
    for name, text in _m4_script_texts().items():
        if "import pymilvus" in text or "from pymilvus" in text:
            hits.append(name)
    if hits:
        raise AssertionError("这些脚本绕过了端口：" + "、".join(hits))
    return f"{len(_m4_script_texts())} 个脚本都走端口"


@check("全链路：调参 A/B 有据可查（同一份评估集、不同检索旋钮的两组结果）")
def _() -> str:
    if not RUNS_DIR.exists():
        raise SkipCheck("还没有 run；owner：M4.4")
    summaries = sorted(RUNS_DIR.glob("run_*_*.summary.csv"))
    if len(summaries) < 2:
        raise SkipCheck(f"只有 {len(summaries)} 份结果，A/B 至少要两次运行；owner：M4.4")
    import csv

    # 按「哪份评估集 + 哪组旋钮」归拢;输入已被删的历史读数进不来(A/B 也就无从核起)
    by_eval_set: dict[str, dict[tuple[str, str], dict]] = {}
    for path in summaries:
        with path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        raw = DATASET_DIR / (row.get("raw_file") or "")
        if not raw.exists():
            continue
        eval_file = str(json.loads(raw.read_text(encoding="utf-8"))["meta"].get("source_file", "?"))
        by_eval_set.setdefault(eval_file, {})[(str(row["rag_k"]), str(row["rag_candidate_m"]))] = row
    pairs = {name: knobs for name, knobs in by_eval_set.items() if len(knobs) >= 2}
    if not pairs:
        raise SkipCheck(
            "现在只有一组可追溯的检索旋钮：retrieval_k/candidate_m 那组已经量过并**回退到 5/2**"
            "（−0.0034，按噪声口径是「打平」，见 docs/STATUS.md §0.4），10/3 的数据集按结论删了。"
            "剩下三组还没跑：融合 rrf vs weighted、重排父块 vs 子块、FAQ 阈值 0.85 重标定 —— "
            "**一轮 = 改一次配置 + 跑一次四指标**（112 条约 15 分钟 + 一次 judge 费用），"
            "不为量噪声重复跑；owner：M4 之后的调参轮次"
        )
    detail = []
    for eval_file, knobs in pairs.items():
        ordered = sorted(knobs.items(), key=lambda item: (int(item[0][0]), int(item[0][1])))
        first, last = ordered[0], ordered[-1]
        low = float(first[1]["mean_of_four"])
        high = float(last[1]["mean_of_four"])
        detail.append(
            f"{Path(eval_file).name}：k={first[0][0]}/m={first[0][1]} → {low:.4f}，"
            f"k={last[0][0]}/m={last[0][1]} → {high:.4f}（Δ{high - low:+.4f}）"
        )
    return "；".join(detail)


# ── 新评估集（八股 + PDF 教材，）──────────────────────────────────
# 为什么另起一组而不是改上面那组：`eval_set_bagu_v1.json`（112 条，只有八股、两种问法）
# 是 M4 的历史读数，它的分层契约必须继续成立；新那份是"作者真实数据的问答对"——
# 200 条里 150 条进 faq 表做演示直答、50 条做评估，来源多了 PDF 教材。
# 三份产物都由 `scripts/tools/build_qa_dataset.py` 造，都在 `data/`（不公开）。
NEW_EVAL_SET = ROOT / "data" / "eval" / "eval_set_bagu_pdf_v1.json"
QA_DATASET = ROOT / "data" / "eval" / "qa_bagu_pdf_v1.json"
FAQ_CSV = ROOT / "data" / "faq" / "qa_bagu_pdf_faq.csv"
DEMO_FAQ = ROOT / "demo" / "faq.csv"
NEW_SOURCES = ("bagu_ai", "bagu_java", "pdf")
QA_TOTAL, QA_FAQ, QA_EVAL = 200, 150, 50
FAQ_CSV_ROWS = 150
# 与 build_qa_dataset.QuestionFilter 同一个阈值：评估题与 faq 表里的题"近似"到这个程度
# 就可能在 FAQ 通道上命中（代码默认阈值 0.85；config.yaml 那个部署旋钮现在更高），评估就短路了
FAQ_OVERLAP_MAX = 0.5


def _require(path: Path, owner: str) -> Path:
    if not path.exists():
        raise SkipCheck(f"{path.relative_to(ROOT)} 不在本地（`data/` 不公开）；owner：{owner}")
    return path


def _grams(text: str, n: int = 3) -> set[str]:
    import re

    norm = re.sub(r"[\s\W_]+", "", text)
    return {norm[i : i + n] for i in range(max(0, len(norm) - n + 1))}


def _overlap(left: str, right: str) -> float:
    """输入:两个问题。行为:算三字组 Jaccard。输出:0~1。"""
    grams, other = _grams(left), _grams(right)
    union = grams | other
    return len(grams & other) / len(union) if union else 0.0


def _demo_questions() -> tuple[str, str]:
    """输入:无。行为:从 demo/README.md 的演示表里取那两句（FAQ 演示 / 知识库演示）。输出:两句。"""
    text = (ROOT / "demo" / "README.md").read_text(encoding="utf-8")
    faq_line = next((ln for ln in text.splitlines() if "FAQ 直答" in ln and "|" in ln), "")
    rag_line = next((ln for ln in text.splitlines() if "知识库" in ln and "|" in ln and "`" in ln), "")
    questions = []
    for line in (faq_line, rag_line):
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        quoted = next((cell for cell in cells if cell.startswith("`") and cell.endswith("`")), "")
        questions.append(quoted.strip("`"))
    if not all(questions):
        raise AssertionError("demo/README.md 的演示表里找不到那两句演示问题（表格被改过？）")
    return questions[0], questions[1]


@check("新评估集（八股+PDF）：结构 + 溯源齐全（source / kind / doc_id / page / chunk_id）")
def _() -> str:
    doc = _load_json(_require(NEW_EVAL_SET, "数据装载"))
    rows = doc.get("rows") or []
    if not rows:
        raise AssertionError("rows 是空的")
    need = {"id", "question", "ground_truth", "source", "kind", "doc_id", "page"}
    for index, row in enumerate(rows):
        missing = need - set(row)
        if missing:
            raise AssertionError(f"第 {index} 条缺 {sorted(missing)}")
        if row["source"] not in NEW_SOURCES:
            raise AssertionError(f"{row['id']}：未知 source {row['source']!r}（只认 {NEW_SOURCES}）")
        if len(str(row["ground_truth"]).strip()) < MIN_GROUND_TRUTH_CHARS:
            raise AssertionError(f"{row['id']}：ground_truth 太短（<{MIN_GROUND_TRUTH_CHARS} 字）")
        if not str(row["doc_id"]).strip():
            raise AssertionError(f"{row['id']}：doc_id 是空的 —— 评估题必须指回库里那份文档")
        if row["source"] == "pdf":
            if not isinstance(row["page"], int) or row["page"] < 1:
                raise AssertionError(f"{row['id']}：PDF 题的 page 必须是正整数，实际 {row['page']!r}")
            if not str(row.get("chunk_id") or "").strip():
                raise AssertionError(f"{row['id']}：PDF 题缺 chunk_id（换不了页/块就核不了溯源）")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise AssertionError("id 有重复")
    by_source: dict[str, int] = {}
    for row in rows:
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    if set(by_source) != set(NEW_SOURCES):
        raise AssertionError(f"来源不全：{sorted(by_source)}，应该都来自 {NEW_SOURCES}")
    return f"{len(rows)} 条：" + "、".join(f"{s}={by_source[s]}" for s in NEW_SOURCES)


@check(f"问答对数据集：{QA_TOTAL} 条 = {QA_FAQ} 进 faq + {QA_EVAL} 做评估，两个桶按行互斥")
def _() -> str:
    doc = _load_json(_require(QA_DATASET, "数据装载"))
    rows = doc.get("rows") or []
    if len(rows) != QA_TOTAL:
        raise AssertionError(f"总条数 {len(rows)}，契约是 {QA_TOTAL}")
    by_bucket: dict[str, int] = {}
    for row in rows:
        by_bucket[row.get("bucket", "?")] = by_bucket.get(row.get("bucket", "?"), 0) + 1
    if by_bucket != {"faq": QA_FAQ, "eval": QA_EVAL}:
        raise AssertionError(f"分桶 {by_bucket}，契约是 faq={QA_FAQ} / eval={QA_EVAL}")
    if len({row["id"] for row in rows}) != len(rows):
        raise AssertionError("id 有重复（两半会因为 id 撞车而覆盖）")
    faq_questions = {str(row["question"]) for row in rows if row["bucket"] == "faq"}
    eval_questions = {str(row["question"]) for row in rows if row["bucket"] == "eval"}
    if faq_questions & eval_questions:
        raise AssertionError(
            f"两个桶有 {len(faq_questions & eval_questions)} 条**同一道题**："
            "那条题既会被 FAQ 直答、又出现在评估集里 —— 评估时它根本不过检索"
        )
    csv_path = _require(FAQ_CSV, "数据装载")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        import csv as csv_module

        rows_in_csv = list(csv_module.DictReader(handle))
    if len(rows_in_csv) != FAQ_CSV_ROWS:
        raise AssertionError(f"{csv_path.name} 有 {len(rows_in_csv)} 行，契约是 {FAQ_CSV_ROWS}")
    if {str(row["问题"]) for row in rows_in_csv} != faq_questions:
        raise AssertionError("CSV 里的问题与数据集里 faq 桶对不上（有人改了一边没改另一边）")
    return f"{QA_TOTAL} 条 → faq {QA_FAQ}（CSV {len(rows_in_csv)} 行）/ eval {QA_EVAL}，两桶无交集"


@check(f"评估题不得与 faq 表那份 CSV 近似（相似度 > {FAQ_OVERLAP_MAX} 就会被 FAQ 直答截胡）")
def _() -> str:
    doc = _load_json(_require(NEW_EVAL_SET, "数据装载"))
    eval_rows = doc.get("rows") or []
    csv_path = _require(FAQ_CSV, "数据装载")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        import csv as csv_module

        faq_questions = [(row["问题"] or "").strip() for row in csv_module.DictReader(handle)]
    faq_grams = [(question, _grams(question)) for question in faq_questions if question]
    worst = (0.0, "", "")
    for row in eval_rows:
        question = str(row["question"]).strip()
        grams = _grams(question)
        for other, other_grams in faq_grams:
            union = grams | other_grams
            if not union:
                continue
            score = len(grams & other_grams) / len(union)
            if score > worst[0]:
                worst = (score, question, other)
            if score > FAQ_OVERLAP_MAX:
                raise AssertionError(
                    f"评估题「{question}」与 faq 表里的「{other}」相似度 {score:.2f} "
                    f"(> {FAQ_OVERLAP_MAX}) —— FAQ 通道会直答（阈值默认 0.85），评估等于没评检索"
                )
    return f"{len(eval_rows)} 条评估题 vs {len(faq_questions)} 条 FAQ，最相似 {worst[0]:.2f}（「{worst[1][:18]}」/「{worst[2][:18]}」）"


@check("演示语料是自写的八股题（不夹真实语料），且两条演示问题各归其道")
def _() -> str:
    """demo/ 是**公开**的自写语料；真实语料（`data/corpus/bagu_*`、PDF 教材）从不进仓库。

    这条钉三件事：① 演示语料里不出现真实语料的名字（否则等于把不公开的东西写进了公开文件）；
    ② README 里那条 **FAQ 演示问题**必须在 `demo/faq.csv` 里（不然 FAQ 通道演示不出来）；
    ③ README 里那条 **知识库演示问题**必须**不在** `demo/faq.csv` 里 ——
       实测踩过：两条都在，知识库那句被 FAQ 直答截胡（`channel=faq score=1.0000`），
       演示看着成功了，其实检索一次都没跑。
    """
    forbidden = ("bagu_ai_data", "bagu_java_data", "LLMBook", "JBL常见AI面试题", "大模型数学原理介绍")
    corpus = sorted((ROOT / "demo" / "corpus").glob("*.md"))
    if len(corpus) < 3:
        raise AssertionError(f"demo/corpus 里只有 {len(corpus)} 篇语料（演示至少要覆盖几个领域）")
    for path in [*corpus, DEMO_FAQ]:
        _require(path, "演示语料")
        text = path.read_text(encoding="utf-8")
        hit = [name for name in forbidden if name in text]
        if hit:
            raise AssertionError(f"{path.name} 里出现了真实语料的名字 {hit} —— 演示语料必须是自写的")
    with DEMO_FAQ.open(encoding="utf-8-sig", newline="") as handle:
        import csv as csv_module

        demo_questions = {(row["问题"] or "").strip() for row in csv_module.DictReader(handle)}
    faq_question, rag_question = _demo_questions()
    if faq_question not in demo_questions:
        raise AssertionError(
            f"README 里演示 FAQ 通道的那句「{faq_question}」不在 demo/faq.csv 里 —— 演示不出来"
        )
    if rag_question in demo_questions:
        raise AssertionError(
            f"README 里演示知识库通道的那句「{rag_question}」**也在** demo/faq.csv 里 —— "
            "它会被 FAQ 直答截胡，检索与生成一次都不跑（实测发生过）"
        )
    nearest = max(
        ((_overlap(rag_question, q), q) for q in demo_questions), default=(0.0, "")
    )
    if nearest[0] > FAQ_OVERLAP_MAX:
        raise AssertionError(
            f"知识库演示问题与 FAQ 里「{nearest[1][:20]}」相似度 {nearest[0]:.2f}，太近，有被截胡的风险"
        )
    return (
        f"演示语料 {len(corpus)} 篇（自写）；FAQ 演示「{faq_question}」在 CSV 里；"
        f"知识库演示「{rag_question}」不在（最近 {nearest[0]:.2f}）"
    )


def main() -> int:
    return report("M4 RAG 评估验收")


if __name__ == "__main__":
    sys.exit(main())
