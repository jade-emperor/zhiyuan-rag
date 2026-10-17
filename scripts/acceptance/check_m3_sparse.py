#!/usr/bin/env python
"""契约：**索引类型（FLAT / IVF_FLAT）与 BM25 稀疏向量**这两条新路。

用法：

    uv run python scripts/acceptance/check_m3_sparse.py

**这一份在堵什么洞**：

1. **索引类型必须由配置决定**（``milvus.index.type`` / ``nlist`` / ``nprobe``）——
   写死在适配器里的后果是"配置改了没生效，而没人知道"：FLAT 与 IVF_FLAT 的区别
   不是快慢一点点，而是**精确 vs 近似**（召回率换延迟）。三处都要钉：
   建集合时的 ``index_params``、检索时的 ``search_params``、以及"集合现在到底是哪种索引"（由 collections 打印）。
2. **关键字那一路可以走 Milvus 的稀疏倒排索引**（``keyword.backend=milvus``）：
   入库时把每块编码成稀疏向量、检索时编码查询。这里钉编码口径与两侧一致性 ——
   它们错起来的共同表现是"检索结果变少/变空"，**不报错**。
3. **稀疏列的边界**：没有那一列时不许静默降级（写进去 = 权重丢了；查它 = 假装没命中），
   空稀疏行 Milvus 会拒收，所以编码器必须保证非空。

**不需要容器**：Milvus 客户端在这一份里被替换成桩（构造期替换，见 ``_store``），
真实往返由 ``check_m3_sparse_milvus.py``（services 档）负责。
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from pathlib import Path

from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]


class _SchemaRecorder:
    """收集 ``add_field`` 的桩 schema（真 schema 要连服务端才造得出来）。"""

    def __init__(self) -> None:
        self.fields: list[dict] = []

    def add_field(self, name: str, datatype: object, **kwargs: object) -> None:
        self.fields.append({"name": name, "datatype": datatype, **kwargs})


class _IndexRecorder:
    """收集 ``add_index`` 的桩 index_params。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add_index(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))


class _Client:
    """只记不改的假 Milvus 客户端：够 ``_ensure_collection`` / ``search`` / ``upsert`` 跑完。"""

    def __init__(self, *, exists: bool = False, fields: tuple[str, ...] = ()) -> None:
        self.exists = exists
        self.fields = list(fields)
        self.created: dict | None = None
        self.indexes = _IndexRecorder()
        self.searches: list[dict] = []
        self.upserts: list[list[dict]] = []

    # ── 建集合那条路 ────────────────────────────────────────────────────
    def has_collection(self, name: str) -> bool:
        return self.exists

    def create_schema(self, **kwargs: object) -> _SchemaRecorder:
        self.schema = _SchemaRecorder()
        return self.schema

    def prepare_index_params(self) -> _IndexRecorder:
        self.indexes = _IndexRecorder()
        return self.indexes

    def create_collection(self, **kwargs: object) -> None:
        self.created = dict(kwargs)
        # 建完就算存在,并把刚建的列报出来:真实的 `_fields()` 就是这么看到的
        # (不这么做,`_require_page_column` 会以为集合还没有 page 列)
        self.exists = True
        self.fields = [str(field["name"]) for field in self.schema.fields]

    def describe_collection(self, name: str) -> dict:
        return {"fields": [{"name": item} for item in self.fields]}

    # ── 读写那条路 ──────────────────────────────────────────────────────
    def search(self, **kwargs: object) -> list:
        self.searches.append(dict(kwargs))
        return [[]]

    def query(self, **kwargs: object) -> list:
        fields = kwargs.get("output_fields")
        if isinstance(fields, list) and "count(*)" in fields:
            return [{"count(*)": 0}]
        return []

    def upsert(self, **kwargs: object) -> dict:
        data = kwargs.get("data")
        if isinstance(data, list):
            self.upserts.append(data)
        return {"upsert_count": 0}

    def list_indexes(self, name: str) -> list[str]:
        return []


def _store(config, *, sparse: bool, exists: bool = False, fields: tuple[str, ...] = ()):
    """输入:Milvus 配置 + 要不要稀疏档。行为:造一个客户端是桩的 store。输出:(store, 桩客户端)。

    **构造期就得把 `MilvusClient` 换成桩**:`__init__` 会真连一次 Milvus,
    而这一份属于 ``--set ci``(零容器)。只换客户端这一层,``__init__`` 里的接线照常执行。
    """
    import project_rag.adapters.milvus_store as milvus_store
    from project_rag.adapters.milvus_store import MilvusVectorStore

    client = _Client(exists=exists, fields=fields)
    original = milvus_store.MilvusClient
    milvus_store.MilvusClient = lambda **kw: client  # type: ignore[assignment]
    try:
        store = MilvusVectorStore(config, sparse=sparse)
    finally:
        milvus_store.MilvusClient = original
    return store, client


BASE_FIELDS = (
    "chunk_id",
    "text",
    "dense_vector",
    "source",
    "doc_id",
    "parent_id",
    "parent_content",
    "page",
)


def _data_chunk(text: str = "装饰器 是 给 函数 加 行为 的 语法"):
    """输入:文本。行为:造一个最小子块。输出:Chunk。"""
    from project_rag.domain import Chunk

    return Chunk(chunk_id="c1", text=text, source="demo", doc_id="d1", parent_id="p1")


# ══ 一、索引类型由配置决定 ═════════════════════════════════════════════════
@check("建集合时的索引类型与参数来自配置（FLAT 不带参数；IVF_FLAT 带 nlist）")
def _() -> str:
    from project_rag.config import MilvusConfig, MilvusIndexConfig
    from project_rag.domain import Embedding

    flat, flat_client = _store(MilvusConfig(index=MilvusIndexConfig(type="FLAT")), sparse=False)
    flat.upsert([_data_chunk()], [Embedding(dense=(0.1, 0.2))])
    flat_index = flat_client.indexes.calls[0]
    if flat_index["index_type"] != "FLAT" or flat_index["params"]:
        raise AssertionError(f"FLAT 应该不带参数，实际 {flat_index}")
    if flat_index["field_name"] != "dense_vector" or flat_index["metric_type"] != "IP":
        raise AssertionError(f"索引该建在归一化后的稠密列上、用 IP：{flat_index}")

    ivf, ivf_client = _store(
        MilvusConfig(index=MilvusIndexConfig(type="IVF_FLAT", nlist=64, nprobe=8)), sparse=False
    )
    ivf.upsert([_data_chunk()], [Embedding(dense=(0.1, 0.2))])
    ivf_index = ivf_client.indexes.calls[0]
    if ivf_index["index_type"] != "IVF_FLAT" or ivf_index["params"] != {"nlist": 64}:
        raise AssertionError(f"IVF_FLAT 该带上 nlist=64，实际 {ivf_index}")
    return "FLAT → 无参数；IVF_FLAT → nlist 来自配置（不是写死的）"


@check("检索时的 search_params 跟着配置走（IVF_FLAT 才带 nprobe）")
def _() -> str:
    from project_rag.config import MilvusConfig, MilvusIndexConfig
    from project_rag.domain import Embedding

    flat, flat_client = _store(
        MilvusConfig(index=MilvusIndexConfig(type="FLAT")),
        sparse=False,
        exists=True,
        fields=BASE_FIELDS,
    )
    flat.search(Embedding(dense=(0.1, 0.2)), k=3)
    if flat_client.searches[0]["search_params"]["params"] != {}:
        raise AssertionError(f"FLAT 不该带检索参数：{flat_client.searches[0]['search_params']}")

    ivf, ivf_client = _store(
        MilvusConfig(index=MilvusIndexConfig(type="IVF_FLAT", nlist=64, nprobe=7)),
        sparse=False,
        exists=True,
        fields=BASE_FIELDS,
    )
    ivf.search(Embedding(dense=(0.1, 0.2)), k=3)
    params = ivf_client.searches[0]["search_params"]
    if params["params"] != {"nprobe": 7} or params["metric_type"] != "IP":
        raise AssertionError(f"IVF_FLAT 该带 nprobe=7：{params}")
    return "FLAT → params={}；IVF_FLAT → params={'nprobe': 配置值}（且 metric 仍是 IP）"


@check("配置层拦住无意义的组合（nprobe > nlist、nlist < 1）")
def _() -> str:
    from project_rag.config import MilvusIndexConfig

    bad = 0
    attempts = (
        lambda: MilvusIndexConfig(type="IVF_FLAT", nlist=8, nprobe=16),
        lambda: MilvusIndexConfig(type="IVF_FLAT", nlist=0, nprobe=1),
        lambda: MilvusIndexConfig(type="IVF_FLAT", nlist=8, nprobe=0),
    )
    for attempt in attempts:
        try:
            attempt()
        except ValueError:
            bad += 1
    if bad != 3:
        raise AssertionError(f"三种坏组合该全部被拦，实际拦下 {bad} 种")
    return "nprobe>nlist / nlist<1 / nprobe<1 都在配置层直接报错（不等 Milvus）"


# ══ 二、稀疏列的边界 ═══════════════════════════════════════════════════════
@check("集合有稀疏列时：写入必须带稀疏向量，返回的行里也真有那一列")
def _() -> str:
    from project_rag.config import MilvusConfig
    from project_rag.domain import Embedding

    fields = (*BASE_FIELDS, "sparse_vector")
    store, client = _store(MilvusConfig(), sparse=True, exists=True, fields=fields)

    try:
        store.upsert([_data_chunk()], [Embedding(dense=(0.1, 0.2))])
    except RuntimeError as exc:
        if "稀疏" not in str(exc):
            raise AssertionError(f"报错该点明稀疏向量，实际：{exc}") from exc
    else:
        raise AssertionError("集合有稀疏列却没给 sparse —— 必须当场拒绝（Milvus 会拒收空行）")

    store.upsert([_data_chunk()], [Embedding(dense=(0.1, 0.2))], sparse=[{7: 1.5}])
    row = client.upserts[0][0]
    if row.get("sparse_vector") != {7: 1.5}:
        raise AssertionError(f"写进 Milvus 的行没有带上稀疏列：{sorted(row)}")
    return "没给 → RuntimeError（点名稀疏向量）；给了 → 行里带 sparse_vector"


@check("集合没有稀疏列时：带稀疏向量写入 / 稀疏检索都要报错（不许静默降级）")
def _() -> str:
    from project_rag.config import MilvusConfig
    from project_rag.domain import Embedding

    store, _client = _store(MilvusConfig(), sparse=False, exists=True, fields=BASE_FIELDS)
    try:
        store.upsert([_data_chunk()], [Embedding(dense=(0.1, 0.2))], sparse=[{7: 1.5}])
    except RuntimeError as exc:
        if "静默" not in str(exc):
            raise AssertionError(f"报错该说清「会被静默丢掉」，实际：{exc}") from exc
    else:
        raise AssertionError("往没有稀疏列的集合写稀疏权重 —— 那是静默丢数据，必须报错")

    try:
        store.search_sparse({7: 1.0}, k=3)
    except RuntimeError as exc:
        if "没有" not in str(exc) or "检索" not in str(exc):
            raise AssertionError(f"稀疏检索的报错该点明「没有那一列」且不许当没命中，实际：{exc}") from exc
    else:
        raise AssertionError("集合没有稀疏列时 search_sparse 必须报错，不能返回空（会被当成没检索到）")
    return "写入与检索两侧都当场报错，并给出去哪三条路"


@check("稀疏检索：只认 IP、不传额外参数，且把 0 分（一个词都没沾上）滤掉")
def _() -> str:
    from project_rag.config import MilvusConfig

    class _HittingClient(_Client):
        def search(self, **kwargs: object) -> list:
            self.searches.append(dict(kwargs))
            # 两条命中：一条真有分、一条 0 分（Milvus 在候选不足 k 时会回这种）
            return [
                [
                    {"id": "c1", "distance": 3.2, "entity": {"text": "命中", "source": "demo"}},
                    {"id": "c2", "distance": 0.0, "entity": {"text": "没沾上", "source": "demo"}},
                ]
            ]

    import project_rag.adapters.milvus_store as milvus_store
    from project_rag.adapters.milvus_store import MilvusVectorStore

    client = _HittingClient(exists=True, fields=(*BASE_FIELDS, "sparse_vector"))
    original = milvus_store.MilvusClient
    milvus_store.MilvusClient = lambda **kw: client  # type: ignore[assignment]
    try:
        store = MilvusVectorStore(MilvusConfig(), sparse=True)
    finally:
        milvus_store.MilvusClient = original

    hits = store.search_sparse({7: 1.0}, k=3)
    params = client.searches[0]["search_params"]
    if client.searches[0]["anns_field"] != "sparse_vector" or params["metric_type"] != "IP":
        raise AssertionError(f"稀疏检索该打在 sparse_vector 上、用 IP：{client.searches[0]}")
    if params["params"]:
        raise AssertionError(f"稀疏索引不吃额外参数：{params}")
    if [hit.chunk.chunk_id for hit in hits] != ["c1"]:
        raise AssertionError(f"0 分那条不该算命中：{[h.chunk.chunk_id for h in hits]}")
    return "打在 sparse_vector + IP + 无额外参数；0 分的行被滤掉（只留真命中的 1 条）"


# ══ 三、BM25 编码口径 ═════════════════════════════════════════════════════
@check("BM25 文档侧权重等于公式算出来的值（idf × 长度归一）")
def _() -> str:
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, _dimension
    from project_rag.adapters.chinese_tokens import tokens

    corpus = ["装饰器 装饰器 语法", "语法 糖", "完全 不同 的 文本"]
    k1, b = 1.2, 0.5
    encoder = BM25SparseEncoder(k1=k1, b=b)
    stats = encoder.fit(corpus)
    vector = encoder.encode_documents([corpus[0]])[0]

    # 手算:口径与编码器一致(同一个分词器),统计与权重全部自己算一遍
    docs = [tokens(text) for text in corpus]
    n = float(len(docs))
    avgdl = sum(len(doc) for doc in docs) / len(docs)
    counts = Counter(_dimension(term) for term in docs[0])
    df = Counter(
        dim for doc in docs for dim in {_dimension(term) for term in doc}
    )
    want = {}
    for dim, tf in counts.items():
        idf = math.log(1 + (n - df[dim] + 0.5) / (df[dim] + 0.5))
        norm = 1 - b + b * (len(docs[0]) / avgdl)
        want[dim] = idf * tf * (k1 + 1) / (tf + k1 * norm)
    if set(vector) != set(want):
        raise AssertionError(f"维度对不上：编码器 {sorted(vector)} vs 手算 {sorted(want)}")
    for dim, value in want.items():
        if abs(vector[dim] - value) > 1e-9:
            raise AssertionError(f"维度 {dim} 的权重 {vector[dim]} ≠ 手算 {value}")
    if stats.n_docs != len(corpus) or stats.avg_len != avgdl:
        raise AssertionError(f"统计口径不对：{stats}（手算 N={len(corpus)}, avgdl={avgdl}）")
    if stats.vocabulary != len(df):
        raise AssertionError(f"词表该按「块内去重后的 df」算：{stats.vocabulary} vs {len(df)}")
    return f"权重与手算一致（k1={k1}、b={b}）；N={stats.n_docs}、avgdl={stats.avg_len:.2f}、词表={stats.vocabulary}"


@check("查询侧只放出现过的词、权重恒为 1；两侧维度必须一致")
def _() -> str:
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, _dimension

    from project_rag.adapters.chinese_tokens import tokens

    encoder = BM25SparseEncoder()
    query = encoder.encode_query("装饰器 装饰器 语法")
    want_dims = {_dimension(term) for term in tokens("装饰器 装饰器 语法")}
    if set(query.values()) != {1.0}:
        raise AssertionError(f"查询侧权重必须恒为 1：{query}")
    if set(query) != want_dims:
        raise AssertionError(f"查询侧该是「出现过的词」：{sorted(query)} vs {sorted(want_dims)}")
    if len(want_dims) == len(tokens("装饰器 装饰器 语法")):
        raise AssertionError("这条夹具里该有重复词,否则验不到「去重」")

    other = BM25SparseEncoder()
    same = {_dimension(term) for term in tokens("装饰器")}
    if set(other.encode_query("装饰器")) != same:
        raise AssertionError("两个编码器实例对同一个词给出的维度必须相同（否则检索永远空）")

    # 查询里出现、但查询编码为空的情况留给索引层判（见下一条）
    if encoder.encode_query("？？？"):
        raise AssertionError("只有标点的查询该编码成空向量（由索引层判成没命中）")
    return "查询侧 = 出现过的词 × 权重 1；跨实例维度稳定；纯标点查询编码为空"


@check("空文本也能写出非空向量（保留维度 0），且真实词永远不落在 0 维")
def _() -> str:
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, _dimension

    encoder = BM25SparseEncoder()
    encoder.fit(["装饰器 语法"])
    vector = encoder.encode_documents(["？？？"])[0]
    if not vector:
        raise AssertionError("空块编码成空向量 —— Milvus 会拒收整行（empty sparse float vector row）")
    if 0 not in vector:
        raise AssertionError(f"空块该落在保留维度 0 上：{vector}")
    if vector[0] > 1e-3:
        raise AssertionError(f"保留维度的权重必须小到不影响排序：{vector[0]}")
    for term in ("装饰器", "语法", "Milvus", "a", "1", "中文词组"):
        if _dimension(term) == 0:
            raise AssertionError(f"真实词 {term!r} 落到了保留维度 0 —— 会和空块撞在一起")
    return "空块 → {0: 极小权重}；任何真实词的维度都 ≥ 1"


@check("未 fit 就编码要报错；全库没见过的词按「只出现一次」给 idf")
def _() -> str:
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, _dimension
    from project_rag.adapters.chinese_tokens import tokens

    fresh = BM25SparseEncoder()
    try:
        fresh.encode_documents(["装饰器"])
    except RuntimeError as exc:
        if "fit" not in str(exc):
            raise AssertionError(f"报错该指向 fit：{exc}") from exc
    else:
        raise AssertionError("没 fit 就编码 —— idf 是空的，权重会静默算错")

    corpus = ["装饰器 语法", "语法 糖"]
    encoder = BM25SparseEncoder()
    encoder.fit(corpus)
    docs = [tokens(text) for text in corpus]
    known = {term for doc in docs for term in doc}
    n = float(len(docs))
    avgdl = sum(len(doc) for doc in docs) / len(docs)
    # 已知词与生僻词都从**分词器的输出**里取:写死字符串会被 jieba 切成别的词
    # (实测"全新词xyz"被切成好几个),那样验的就成了分词器而不是 idf 口径
    document = "语法 zzqqxx"
    doc_terms = tokens(document)
    seen_terms = [term for term in doc_terms if term in known]
    if not seen_terms:
        raise AssertionError(f"这条夹具需要一个语料里也有的词:{doc_terms} 与 {sorted(known)}")
    seen_term = seen_terms[0]
    rare = [term for term in doc_terms if term not in known]
    if not rare:
        raise AssertionError("这条夹具需要一个语料里没有的词,但分词器把 zzqqxx 切成了已知词")
    vector = encoder.encode_documents([document])[0]
    if not vector:
        raise AssertionError("新文档里的生僻词不该让整块编码变空")

    def weight_of(term: str, document: str) -> float:
        doc = tokens(document)
        tf = doc.count(term)
        if tf == 0:
            return 0.0
        df = sum(1 for item in docs if term in item) or 1  # 生僻词按 df=1
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        norm = 1 - encoder._b + encoder._b * (len(doc) / avgdl)
        return idf * tf * (encoder._k1 + 1) / (tf + encoder._k1 * norm)

    want_unseen = weight_of(rare[0], document)
    got_unseen = vector.get(_dimension(rare[0]))
    if got_unseen is None or abs(got_unseen - want_unseen) > 1e-9:
        raise AssertionError(f"生僻词该按 df=1 的 idf：{got_unseen} ≠ {want_unseen}")
    want_seen = weight_of(seen_term, document)
    got_seen = vector.get(_dimension(seen_term))
    if got_seen is None or abs(got_seen - want_seen) > 1e-9:
        raise AssertionError(f"语料里有的词该用它自己的 df：{got_seen} ≠ {want_seen}")
    return "未 fit → RuntimeError；生僻词按 df=1，已知词按真实 df（两条都与手算一致）"


@check("词维度跨**进程**稳定（内置 hash() 是按进程加盐的，一用就废）")
def _() -> str:
    import subprocess

    from project_rag.adapters.bm25_sparse import _dimension

    terms = ["装饰器", "向量检索", "BM25"]
    here = {term: _dimension(term) for term in terms}
    # 另起一个进程算同一批词:ingest 与 API 就是两个进程,维度必须逐字相同。
    # 用内置 hash() 的话这一步会给出另一组数(而且不报错)—— 检索结果会静默变空。
    code = (
        "from project_rag.adapters.bm25_sparse import _dimension;"
        f"print([_dimension(t) for t in {terms!r}])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise AssertionError(f"子进程算维度失败:{proc.stderr.strip()[:200]}")
    there = [int(item) for item in proc.stdout.strip().strip("[]").split(",")]
    if there != [here[term] for term in terms]:
        raise AssertionError(
            f"同一个词在两个进程里落到了不同维度:{there} vs {[here[t] for t in terms]}"
            " —— ingest 与检索就再也对不上了（静默空结果）"
        )
    return "三个词在两个独立进程里维度逐字相同（内置 hash() 这种按进程加盐的写法过不了）"


@check("保留维度 0 被排除在映射之外（源码级：映射必须从 1 起）")
def _() -> str:
    from project_rag.adapters.bm25_sparse import _dimension

    for term in ("装饰器", "语法", "Milvus", "a", "1", "中文词组"):
        if _dimension(term) == 0:
            raise AssertionError(f"真实词 {term!r} 落到了保留维度 0 —— 会和空块撞在一起")
    # **为什么这条读源码**:映射空间是 2³¹ 量级,"某个词恰好落到 0"用采样验不出来
    # (要跑十亿次才可能撞上),而撞上之后不报错、只是那一个词的权重和空块混在一起。
    # 所以保留位这件事按源码断言:映射必须从 1 起。
    source = (ROOT / "src" / "project_rag" / "adapters" / "bm25_sparse.py").read_text(
        encoding="utf-8"
    )
    if not re.search(r"return 1 \+ int\.from_bytes\(digest", source):
        raise AssertionError(
            "维度映射必须从 1 起（0 是空文档的保留位）—— 去掉那个 `1 +` 之后，"
            "真实词与空块会撞在同一个维度上（概率极低，但发生时不报错）"
        )
    return "采样 + 源码双保险：0 是空块专用，真实词一律落在 ≥ 1 的维度上"


@check("deps 只在 milvus 那档装配稀疏编码器（memory 那档装了就会去撞没有那一列的集合）")
def _() -> str:
    import subprocess

    # **这条必须另起进程**:它要读两次配置(memory / milvus),而 get_settings() 是进程级单例。
    # 这正是踩过的那个坑:`keyword.backend` 还是 memory,而编码器被无条件装配 ——
    # ingest 先算了一遍全库 BM25 统计,再撞上"集合没有 sparse_vector 列"。
    def encoder_wired(backend: str) -> bool:
        code = (
            "import os;"
            f"os.environ['RAG_KEYWORD__BACKEND'] = {backend!r};"
            "from project_rag.cli.deps import build_real_deps;"
            "print(build_real_deps().sparse_encoder_factory is not None)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=180
        )
        if proc.returncode != 0:
            raise AssertionError(f"子进程装配失败({backend}):{proc.stderr.strip()[:200]}")
        return proc.stdout.strip() == "True"

    if encoder_wired("memory"):
        raise AssertionError(
            "keyword.backend=memory 时不该装配 sparse_encoder_factory —— 装了它,"
            "ingest 会先白算一遍全库统计,再撞上「这个集合没有稀疏列」（而且集合本来就是按 memory 建的）"
        )
    if not encoder_wired("milvus"):
        raise AssertionError("keyword.backend=milvus 时必须装配稀疏编码器,否则写不出稀疏向量")
    return "memory → 不装配（不白算、不撞列）；milvus → 装配（写入与检索共用同一个实例）"


@check("带稀疏编码器写「接不了稀疏向量」的库：在**嵌入之前**就失败（不白花嵌的钱）")
def _() -> str:
    from _fakes import FakeEmbedder, FakeVectorStore

    from project_rag.adapters.bm25_sparse import BM25SparseEncoder
    from project_rag.application import ingest_documents
    from project_rag.domain import ChunkGroup, Document

    class _NoSparseStore(FakeVectorStore):
        """明确"接不了稀疏向量"的库（模拟 keyword.backend=memory 建出来的集合）。

        直接**继承共用假件**而不是手写一个：手写会漏端口方法（pyright 当场就报了
        `search`/`fingerprint`/`drop` 都没有），而共用假件本来就是"完整端口形状"。
        """

        def has_sparse_column(self) -> bool:
            return False

    class _Chunker:
        def split(self, text):  # noqa: ANN001, ANN201
            return [ChunkGroup(parent_text=text, children=(text,), child_starts=(0,))]

    store, embedder = _NoSparseStore(), FakeEmbedder()
    try:
        ingest_documents(
            [Document(doc_id="d", source="s", text="装饰器 语法", pages=())],
            chunker=_Chunker(),
            embedder=embedder,
            store=store,
            sparse_encoder=BM25SparseEncoder(),
        )
    except RuntimeError as exc:
        if "稀疏" not in str(exc):
            raise AssertionError(f"报错该点明稀疏向量:{exc}") from exc
    else:
        raise AssertionError("库接不了稀疏向量却照写 —— 权重会被静默丢掉,必须当场拒绝")
    if embedder.calls:
        raise AssertionError(
            f"预检必须在嵌入之前(嵌入是整条 ingest 最贵的一步),实际 embed 调了 {embedder.calls} 次"
        )
    return "在第一次嵌入之前就报错（embed 一次都没调），并指出「配置与集合不是同一档」"


# ══ 四、索引这一层的接线 ═══════════════════════════════════════════════════
@check("MilvusSparseKeywordIndex：纯标点查询直接判没命中，其余交 store.search_sparse")
def _() -> str:
    from project_rag.adapters.bm25_sparse import BM25SparseEncoder, MilvusSparseKeywordIndex

    class _Store:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, int, str | None]] = []

        def has_sparse_column(self) -> bool:
            return True

        def search_sparse(self, query_sparse, *, k, source=None):
            self.calls.append((dict(query_sparse), k, source))
            return ()  # 内容不重要：这一条只验接线（空序列即可，类型也说得通）

    store = _Store()
    index = MilvusSparseKeywordIndex(store=store, encoder=BM25SparseEncoder())
    if index.search("？？？", k=3) != []:
        raise AssertionError("纯标点查询该直接返回空 —— 拿空稀疏向量去检索没有意义")
    if store.calls:
        raise AssertionError("纯标点查询不该真的打到 Milvus")
    index.search("装饰器 语法", k=5, source="bagu_ai")
    vector, k, source = store.calls[0]
    if k != 5 or source != "bagu_ai" or set(vector.values()) != {1.0}:
        raise AssertionError(f"接线不对：{(vector, k, source)}")
    return "空查询短路（不打库）；其余把「出现过的词 × 1」与 k/source 原样交给 store"


@check("内存实现也走同一套口径：稀疏往返 + 0 分不算命中")
def _() -> str:
    from project_rag.adapters import InMemoryVectorStore
    from project_rag.domain import Chunk, Embedding

    store = InMemoryVectorStore()
    chunks = [
        Chunk(chunk_id="a", text="A", source="s1", doc_id="d1"),
        Chunk(chunk_id="b", text="B", source="s1", doc_id="d1"),
        Chunk(chunk_id="c", text="C", source="s2", doc_id="d2"),
    ]
    store.upsert(
        chunks,
        [Embedding(dense=(1.0, 0.0)) for _ in chunks],
        sparse=[{1: 2.0}, {1: 1.0}, {3: 9.0}],
    )
    hits = store.search_sparse({1: 1.0}, k=5)
    if [hit.chunk.chunk_id for hit in hits] != ["a", "b"]:
        raise AssertionError(f"该只回有交集的两条、按内积降序：{[(h.chunk.chunk_id, h.score) for h in hits]}")
    if [hit.score for hit in hits] != [2.0, 1.0]:
        raise AssertionError("稀疏分数该是内积")
    filtered = store.search_sparse({1: 1.0}, k=5, source="s2")
    if filtered:
        raise AssertionError("来源过滤之后不该还有命中（s2 那条与查询没有交集）")
    return "往返 + 内积排序 + 0 分不算命中 + 来源过滤，都与 Milvus 那一路同口径"


def main() -> int:
    return report(
        "索引类型（FLAT / IVF_FLAT）+ BM25 稀疏向量",
        hint="真实往返（建集合 / 稀疏检索 / 重建索引）见 check_m3_sparse_milvus.py（services 档）",
    )


if __name__ == "__main__":
    sys.exit(main())
