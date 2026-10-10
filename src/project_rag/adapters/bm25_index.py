"""
进程内 BM25 关键字索引(``rank_bm25`` 的 ``BM25Plus``)。

**为什么关键字那一路不进 Milvus**:Milvus 2.4.4 没有内置 BM25(那是 2.5 的能力)。
拿正在跑的 2.4.4 实测过三条边界(记在 `docs/STATUS.md` §5 第 30 条):
稀疏向量**能存**(`SPARSE_FLOAT_VECTOR` + `SPARSE_INVERTED_INDEX` + IP 检索跑通),但
稀疏索引**只认 IP 度量**、`Function(BM25)` 只是"建集合不报错"(插入只给 `text` 会报
`Insert missed an field 'sparse'`)、`TEXT_MATCH` 表达式解析器不认。而且 BM25 的权重依赖
**全局统计**(df / N / avgdl),语料一变全库权重都要重算 —— 它不是"逐文档独立编码"能算出来的
东西,所以"把 BM25 存成稀疏向量"在 2.4.4 上等于自己维护词表与 IDF,比这里更糟。

**语料从哪来**:从 ``VectorStore`` 取 —— 全量文本在向量库里已经有唯一的一份,
不必再维护第二个存储。取全文只在**语料变了**的时候发生。

**索引按指纹缓存,指纹本身再按 TTL 缓存（加的第二层)**:实测 3938 块时
``VectorStore.fingerprint()`` 要 **397.6ms**,而它**每次查询都要调**;真正的 BM25 打分
(``get_scores``)只要 **0.6~1.2ms**。也就是说这一路 720ms 中位里 **55% 是"为了判断要不要重建索引"
而付的一次向量库往返**。所以指纹先问 ``Cache``(Redis GET ≈ 0.1ms),未命中/不可用才回源。
进缓存的是指纹的 **sha256 摘要**(64 字符),不是整条指纹(3938 块 × 65 字符 ≈ **250KB**)。

代价是「语料变了」最多晚 ``ttl`` 秒被发现(config 的 ``cache.corpus_fp_ttl_s``,默认 20)。
入库本身要几分钟,20 秒的窗口体感上不存在;要"上传完立刻可搜"就让写入方 bump 缓存键
(升级路径记在 `docs/deferred.md` 第 56 条)。**缓存故障一律降级**:Cache 是纯加速,
读/写抛异常只记 warning,行为退回"每查询读一次指纹"——今天的样子。

**索引按指纹缓存**:语料一变(或父子结构一变)就重建。这和 FAQ 匹配器是同一套做法 ——
两处都不该每次查询重算 IDF。

**这个类现在是进程级单例**(``cli/deps.py`` 里 `lru_cache`),所以它**会被多个线程并发调用**
(FastAPI 的线程池)。因此「换一份索引」必须是一次**原子发布**:见 ``_state`` 的注释。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from rank_bm25 import BM25Plus

from project_rag.adapters.chinese_tokens import tokens
from project_rag.domain import Chunk, RetrievedChunk
from project_rag.observability import get_logger, note_build
from project_rag.ports import Cache, KeywordIndex, VectorStore

_logger = get_logger("adapters.bm25_index")

# 语料指纹摘要的缓存键。版本号跟着**缓存值的含义**走(现在是"指纹的 sha256"):
# 换编码方式就手工 bump —— 与 `answer_faq.CACHE_KEY_PREFIX` 同一套规矩(注释见 CacheConfig)。
_CACHE_KEY = "corpus:fp:v1"

# 一份配套的快照:(语料摘要, 语料, BM25 索引)
_State = tuple[str, list[Chunk], "BM25Plus | None"]


def _digest(fingerprint: Sequence[str]) -> str:
    """输入:整条指纹(每块一行「主键 + 父块号」)。行为:算 sha256 摘要。输出:64 位十六进制。

    **为什么存摘要而不是原样**:3938 块的指纹约 **250KB**,塞进 Redis 又慢又浪费;
    对"变没变"这个判断,摘要与原文等价 —— 指纹相同必然摘要相同,摘要不同必然变了。
    """
    return hashlib.sha256("\x1e".join(fingerprint).encode("utf-8")).hexdigest()


class BM25KeywordIndex(KeywordIndex):
    """子块级 BM25。语料从向量库取,索引按指纹缓存,指纹本身按 TTL 缓存。"""

    def __init__(self, store: VectorStore, cache: Cache | None = None, ttl: int = 20) -> None:
        """输入:向量库(全量语料从它那里取)、可选缓存(纯加速)、缓存过期秒数。

        行为:记下依赖,索引留给第一次查询再建。输出:无。

        ``cache=None`` = 退回"每次查询都读一次指纹"(加缓存之前的语义)。
        验收里的假件走的就是这条路 —— 没有 Redis 也能把索引行为测全。
        """
        self._store = store
        self._cache = cache
        self._ttl = ttl
        # **整套快照只用一个属性装**:摘要/语料/索引必须同时换。
        # 拆成三个属性写的话,并发读的人可能拿到「新语料 + 旧索引」——
        # 长度不匹配直接 IndexError,或者分数与块错位(那更糟:它不报错)。
        # 单次属性赋值在 CPython 里是原子的,所以这里不需要锁。
        self._state: _State | None = None

    def search(
        self, query: str, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、返回条数、来源过滤。行为:BM25 打分 → 过滤来源 → 降序取前 k。输出:命中序列。"""
        self._refresh()
        state = self._state  # 单次读取:拿到的三元组一定是配套的
        if state is None:
            return []
        _, chunks, index = state
        if index is None:
            return []

        scores = index.get_scores(tokens(query))
        # 同分时按 chunk_id 兜底排序:顺序稳定,验收才写得死断言
        ranked = sorted(
            range(len(chunks)),
            key=lambda i: (-float(scores[i]), chunks[i].chunk_id),
        )

        hits: list[RetrievedChunk] = []
        for i in ranked:
            score = float(scores[i])
            if score <= 0.0:
                # 一个查询词都没沾上(词表里压根没有,分数恒为 0)—— **这不是命中**。
                # 实测:域外问法(「学费 多少钱」)全 0,域内问法 9.7~14.7,中间没有灰区。
                # 必须在这里停:融合那一步会给「第 1 名」排名分,
                # 把 0 分的垃圾当命中返回,等于让它挤掉真命中。降序排列,所以能直接 break。
                break
            chunk = chunks[i]
            if source is not None and chunk.source != source:
                continue
            hits.append(RetrievedChunk(chunk=chunk, score=score))
            if len(hits) == k:
                break
        return hits

    def _refresh(self) -> None:
        """输入:无。行为:摘要变了才重建索引,并把新快照原子地发布出去。输出:无。

        指纹取自主键 + 父块号:主键里含内容哈希,所以「指纹相同」蕴含「正文没变」。
        两个线程同时发现语料变了会各建一份 —— 白做一次功,但结果一致(可接受;
        加锁串行化反而会把 3927 块的分词变成所有请求一起等)。

 **这一路的账（在容器内实测,3927 块)**:
        * 冷启动(每进程一次,服务启动预热就是把这笔挪走):
          指纹 438ms + load_all 527ms + **jieba 全量分词 1521ms** + BM25Plus 建索引 44ms ≈ **2.1s**;
        * 常驻内存 **111MB/进程** = jieba 词典 60 + 语料正文 32 + 分词结果 18 + BM25 结构 **1**;
        * **每次查询打分只要 1.8ms**(7 个查询词 × 3927 篇,numpy 向量化) —— 慢的是"建",不是"查";
        * 多 worker = 每进程各付一份(内存 ×N + 冷启动 ×N),这也是本服务坚持单 worker 的理由之一。
        指纹常驻缓存(Redis `corpus:fp:v1`,存的是 64 字节 sha256,不是那 495KB)让稳态下
        每进程最多每 20s 回源一次;回源那一步走的是 **strong** 读(见 milvus_store 里两个常量的分工)。
        """
        digest = self._current_digest()
        state = self._state
        if state is not None and state[0] == digest:
            return
        # 真重建一次 = 这一轮请求确实为用户付了冷启动(实测:load_all 336ms + 分词 1.18s)。
        # 记在**重建处**而不是构造处:构造是惰性的,索引要到第一次检索才建;记在构造处
        # 会把"冷"标在一个没有成本发生的时刻上 —— 那账本就在骗人。
        note_build("bm25_index")
        chunks = list(self._store.load_all())
        index = BM25Plus([tokens(chunk.text) for chunk in chunks]) if chunks else None
        self._state = (digest, chunks, index)

    def _current_digest(self) -> str:
        """输入:无。行为:取语料指纹摘要 —— 先问缓存,未命中或缓存不可用才回源向量库。输出:摘要。"""
        if self._cache is not None:
            cached = self._safe_get()
            if cached is not None:
                return cached
        digest = _digest(self._store.fingerprint())
        self._safe_set(digest)
        return digest

    def _safe_get(self) -> str | None:
        """输入:无。行为:读缓存,没配缓存或抛异常都按未命中并记 warning。输出:摘要或 None。

        降级方向是**安全的**:拿不到缓存就回源向量库(正确但慢),而不是"当作没变"
        —— 后者会让新入库的文档一直检索不到,而且是静默的。
        """
        if self._cache is None:
            return None
        try:
            return self._cache.get(_CACHE_KEY)
        except Exception as e:  # noqa: BLE001 - 缓存是纯加速,任何故障都不该让检索失败
            _logger.warning("语料指纹缓存读取失败,回源向量库(每查询多付约 400ms): %s", e)
            return None

    def _safe_set(self, digest: str) -> None:
        """输入:摘要。行为:写回缓存,抛异常只记 warning。输出:无。"""
        if self._cache is None:
            return
        try:
            self._cache.set(_CACHE_KEY, digest, self._ttl)
        except Exception as e:  # noqa: BLE001 - 同上
            _logger.warning("语料指纹缓存写入失败,不影响本次检索: %s", e)
