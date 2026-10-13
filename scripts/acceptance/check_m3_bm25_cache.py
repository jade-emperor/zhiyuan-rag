#!/usr/bin/env python
"""P0-1 契约：BM25 那一层的「语料指纹」缓存 —— 省掉每查询约 400ms 的那次向量库往返。

用法::

    uv run python scripts/acceptance/check_m3_bm25_cache.py

**为什么值得一个单独的脚本**：这条契约**不需要容器、不需要模型、不联网**（全内存假件），
而它要配一份**反复跑很多次**的反向测试（每次都要把 `bm25_index.py` 改坏再还原）。
把它塞进 2400 行的 `check_m3_rag.py` 会让每次反向测试都等好几分钟加载 BGE-M3 + 打真 Milvus ——
所以按"快慢分家"拆出来，与 `check_m5_api`（无需容器）、`check_doc_formats`（夹具现造）同一个路子。

**这条契约在钉什么**（实测的由来）：3938 块时 ``VectorStore.fingerprint``
要 **397.6ms**，而它**每次检索都要调**；真正的 BM25 打分 ``get_scores`` 只要 **0.6~1.2ms**。
也就是说这一路 720ms 中位里 55% 是"为了判断要不要重建索引"而付的一次 Milvus 往返。
修法是指纹先问 Redis，未命中才回源。**这里的每一条都在钉"省了"与"没省出问题"两件事**：

1. 省了：TTL 内第 2、3 次检索**不再**问向量库要指纹（这就是那笔钱）；
2. 没省出问题：TTL 过期后语料真变了**仍会被发现**并重建 —— 缓存不许把变化吃掉；
3. 没省出问题：缓存读/写**抛异常只降级**（退回每查询读指纹），不抛给调用方，
   更不许"当作没变"（那会让新入库的文档一直检索不到，而且是静默的）；
4. 进缓存的是指纹的 **sha256 摘要**（64 字符）而不是整条指纹（3938 块 ≈ **250KB**）；
5. ``cache=None`` 时行为与加缓存之前**完全一致**（每查询读一次指纹）——
   这条保证验收里的假件（`check_m3_rag` 那条）测的还是老语义。

TTL 用**假时钟**测，不 ``sleep``：sleep 出来的检查又慢又会 flaky。

**假件为什么是"委托"而不是"继承"**：`check_m3_rag` 里那个计数向量库定义在**函数体内**
（它继承必须能解析到基类，所以只能在拿到 import 之后定义）。这里改成"包一层 + 7 个方法全部转发"：
基类只在 ``__init__`` 里惰性 import，类型注解走 ``TYPE_CHECKING``，于是**模块顶层
一个 project_rag 的适配器都不用碰**，脚本启动更快，pyright 也照样能查签名。
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from _harness import check, report

# domain 只用标准库,顶层 import 它不会拖起 torch / pymilvus,而且假件要在运行期造 Chunk
from project_rag.domain import Chunk

if TYPE_CHECKING:  # 只为类型:运行期一个字节都不 import
    from project_rag.domain import Embedding, RetrievedChunk, SparseVector
    from project_rag.ports import VectorStore


class _CountingStore:
    """真内存向量库外面包一层计数器：``fingerprint`` / ``load_all`` 各被调几次。

    **7 个方法全部转发**，不实现任何口径 —— 这条契约要验的是真实现的缓存行为，
    自己写一份假向量库就等于把被验对象换掉了。计数器只有两个：

    * ``fingerprints``：那笔 397.6ms 的钱（每查询一次 → 缓存后 TTL 内只一次）；
    * ``loads``：那笔 1.18s 的分词+建索引（只在语料真变了时才该涨）。
    """

    def __init__(self) -> None:
        from project_rag.adapters import InMemoryVectorStore

        self._inner: VectorStore = InMemoryVectorStore()
        self.loads = 0
        self.fingerprints = 0

    def upsert(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Embedding],
        *,
        sparse: Sequence[SparseVector] | None = None,
    ) -> int:
        """输入:子块 + 向量(+ 可选稀疏向量)。行为:转发。输出:库内总数。"""
        return self._inner.upsert(chunks, embeddings, sparse=sparse)

    def search(
        self, query_embedding: Embedding, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:查询向量、条数、来源过滤。行为:转发。输出:命中序列。"""
        return self._inner.search(query_embedding, k=k, source=source)

    def search_sparse(
        self, query_sparse: SparseVector, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:查询的稀疏向量。行为:转发(这条契约验的是进程内 BM25,不走这一路)。输出:命中序列。"""
        return self._inner.search_sparse(query_sparse, k=k, source=source)

    def delete_by_doc_id(self, doc_id: str) -> int:
        """输入:文档标识。行为:转发。输出:删除条数。"""
        return self._inner.delete_by_doc_id(doc_id)

    def has_sparse_column(self) -> bool:
        """输入:无。行为:转发(这一档的集合有没有稀疏列,与本条契约无关)。输出:布尔。"""
        return self._inner.has_sparse_column()

    def fingerprint(self) -> Sequence[str]:
        """输入:无。行为:**计数后**转发。输出:指纹序列。"""
        self.fingerprints += 1
        return self._inner.fingerprint()

    def load_all(self) -> Sequence[Chunk]:
        """输入:无。行为:**计数后**转发。输出:全部子块。"""
        self.loads += 1
        return self._inner.load_all()

    def chunk_counts(self) -> Mapping[str, int]:
        """输入:无。行为:转发。输出:``doc_id -> 块数``。"""
        return self._inner.chunk_counts()

    def delete_by_source(self, source: str) -> int:
        """输入:来源标签。行为:转发。输出:删除条数。"""
        return self._inner.delete_by_source(source)

    def drop(self) -> None:
        """输入:无。行为:转发。输出:无。"""
        self._inner.drop()


class _TtlCache:
    """带**假时钟**的内存缓存：能确定性地测 TTL，也能注入读/写故障。

    只实现 ``Cache`` 端口的两个方法（``get`` / ``set``）—— 刻意残缺，
    因为它本来就是"最小可判定的替身"。
    """

    def __init__(self, *, get_error: str | None = None, set_error: str | None = None) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.now = 1000.0
        self.gets: list[str] = []
        self.sets: list[str] = []
        self.get_error = get_error
        self.set_error = set_error

    def advance(self, seconds: float) -> None:
        """输入:秒数。行为:把假时钟往前拨。输出:无。"""
        self.now += seconds

    def get(self, key: str) -> str | None:
        """输入:键。行为:按假时钟判过期,可注入故障。输出:值或 None。"""
        self.gets.append(key)
        if self.get_error is not None:
            raise RuntimeError(self.get_error)
        item = self.data.get(key)
        if item is None:
            return None
        value, expire_at = item
        return None if self.now >= expire_at else value

    def set(self, key: str, value: str, ttl: int) -> None:
        """输入:键、值、TTL。行为:按假时钟记下过期时刻,可注入故障。输出:无。"""
        self.sets.append(key)
        if self.set_error is not None:
            raise RuntimeError(self.set_error)
        self.data[key] = (value, self.now + ttl)


def _store_with(texts: list[tuple[str, str, str]]) -> _CountingStore:
    """输入:``(chunk_id, 正文, doc_id)`` 列表。行为:灌进计数向量库并把计数清零。输出:该向量库。"""
    from project_rag.adapters import HashingEmbedder

    store = _CountingStore()
    chunks = [Chunk(cid, text, "ai", doc) for cid, text, doc in texts]
    store.upsert(chunks, HashingEmbedder().embed([c.text for c in chunks]))
    store.loads = 0
    store.fingerprints = 0
    return store


# 三条**保证能命中**的查询:前两条是 check_m3_rag 里用过的,第三条干脆是整块正文
# （子串倒是可能被 jieba 切得不一样,别在这种地方赌分词）。
_QUERIES = ("退款", "申请退款", "退款在订单页点申请退款")


@check("BM25 指纹缓存：TTL 内第 2、3 次检索不再问向量库要指纹（这就是省下的 ~400ms）")
def _cache_hit_skips_store() -> str:
    from project_rag.adapters import BM25KeywordIndex

    store = _store_with([("c1", "退款在订单页点申请退款", "d1")])
    cache = _TtlCache()
    index = BM25KeywordIndex(store, cache, 20)

    for query in _QUERIES:
        if not index.search(query, k=1):
            raise AssertionError(f"「{query}」应该命中 —— 缓存不该影响召回")
    if store.fingerprints != 1:
        raise AssertionError(
            f"3 次检索只该问向量库要 1 次指纹,实际 {store.fingerprints} 次"
            f"（每次约 397.6ms,等于把省下来的钱又付回去了）"
        )
    if store.loads != 1:
        raise AssertionError(f"语料没变就不该再 load_all,实际 {store.loads} 次")
    if len(cache.gets) != 3:
        raise AssertionError(f"每次检索都该读一次缓存,实际 {len(cache.gets)} 次")
    if len(cache.sets) != 1:
        raise AssertionError(f"只该写回一次缓存,实际 {len(cache.sets)} 次")
    return "3 次检索：缓存读 3 次 / 向量库指纹 1 次 / load_all 1 次"


@check("BM25 指纹缓存：TTL 过期后语料变了仍会被发现并重建（缓存不许吃掉变化）")
def _expiry_still_detects_change() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder

    store = _store_with([("c1", "退款在订单页点申请退款", "d1")])
    cache = _TtlCache()
    index = BM25KeywordIndex(store, cache, 20)
    index.search("退款", k=1)

    cache.advance(21)  # 假时钟往前拨过 TTL —— 不 sleep
    new = [Chunk("c9", "发票怎么开", "ai", "d9")]
    store.upsert(new, HashingEmbedder().embed([c.text for c in new]))

    if not index.search("发票", k=1):
        raise AssertionError("TTL 过期后必须回源、发现语料变了、重建索引 —— 否则新块永远检索不到")
    if (store.fingerprints, store.loads) != (2, 2):
        raise AssertionError(
            f"过期后该「回源 1 次 + 重建 1 次」,实际 fingerprints={store.fingerprints} loads={store.loads}"
        )
    return "过期后回源一次即发现变化：指纹 2 次 / load_all 2 次 / 新块可检索"


@check("BM25 指纹缓存：缓存读/写抛异常只降级（退回每查询读指纹），不抛给调用方")
def _cache_failure_degrades() -> str:
    from project_rag.adapters import BM25KeywordIndex

    store = _store_with([("c1", "退款在订单页点申请退款", "d1")])

    read_broken = BM25KeywordIndex(store, _TtlCache(get_error="redis 连接被拒"), 20)
    for _ in range(2):
        if not read_broken.search("退款", k=1):
            raise AssertionError("缓存读挂了也必须能检索（缓存是纯加速）")
    if store.fingerprints != 2:
        raise AssertionError(
            f"读失败时该退回「每查询读一次指纹」,实际 {store.fingerprints} 次"
            f"—— 若为 0 说明把失败当成了「没变」,那是静默错误"
        )

    write_broken = BM25KeywordIndex(store, _TtlCache(set_error="redis 只读"), 20)
    if not write_broken.search("退款", k=1):
        raise AssertionError("缓存写挂了也必须能检索")
    if store.fingerprints != 3:
        raise AssertionError(f"写失败后下一次仍该回源,实际 {store.fingerprints} 次")
    return "读挂 / 写挂都只是降级：每次回源，检索照常命中"


@check("BM25 指纹缓存：进缓存的是 sha256 摘要(64 字符)，不是整条指纹(3938 块 ≈ 250KB)")
def _cache_holds_digest() -> str:
    from project_rag.adapters import BM25KeywordIndex

    store = _store_with([("c1", "退款在订单页点申请退款", "d1")])
    raw = "".join(store.fingerprint())  # 独立算一遍期望值,不调被测代码的私有函数
    expected = hashlib.sha256("\x1e".join(store.fingerprint()).encode("utf-8")).hexdigest()
    store.loads = 0
    store.fingerprints = 0

    cache = _TtlCache()
    BM25KeywordIndex(store, cache, 20).search("退款", k=1)

    if len(cache.data) != 1:
        raise AssertionError(f"该只写一个键,实际 {list(cache.data)}")
    key, (value, _expire_at) = next(iter(cache.data.items()))
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise AssertionError(f"缓存值该是 64 位十六进制摘要,实际 {len(value)} 字符：{value[:80]!r}")
    if value != expected:
        raise AssertionError("摘要口径与「sha256(指纹按 \\x1e 连接)」不一致 —— 换编码要同步改契约")
    if len(set(cache.gets)) != 1:
        raise AssertionError(f"同一个键才对,实际用了 {set(cache.gets)}")
    return f"键 {key!r} / 值 64 字符摘要（整条指纹是 {len(raw)} 字符）"


@check("BM25 指纹缓存：`cache=None` 时行为与加缓存之前一致（每查询读一次指纹）")
def _no_cache_keeps_old_semantics() -> str:
    from project_rag.adapters import BM25KeywordIndex

    store = _store_with([("c1", "退款在订单页点申请退款", "d1")])
    index = BM25KeywordIndex(store)  # 不传缓存 = 加缓存之前的语义

    index.search("退款", k=1)
    index.search("申请退款", k=1)
    if (store.loads, store.fingerprints) != (1, 2):
        raise AssertionError(
            f"无缓存时该「每查询读一次指纹、只 load 一次」,实际 loads={store.loads} fingerprints={store.fingerprints}"
        )
    return "无缓存：指纹 2 次 / load_all 1 次 —— 与加缓存之前一致"


def main() -> int:
    return report(
        "P0-1 契约：BM25 语料指纹缓存（省掉每查询约 400ms 的向量库往返）",
        notes=(
            "说明：本脚本全内存假件，**不需要容器、不需要模型、不联网**；"
            "TTL 用假时钟测，不 sleep。",
        ),
        hint=(
            "提示：真环境里这条缓存挂在 Redis（`config.yaml` 的 `cache.corpus_fp_ttl_s`）。"
            "Redis 挂了不影响正确性，只是退回每查询约 400ms —— 日志里会有一条 warning。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
