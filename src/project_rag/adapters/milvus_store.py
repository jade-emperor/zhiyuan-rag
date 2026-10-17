"""
Milvus 向量库适配器(**只管稠密一路**)。

维度不写死:集合在**第一次写入时按实际向量维度**建。这样换嵌入模型不用改这里 ——
维度对不上时 Milvus 会直接报错,而不是把错维度的向量塞进去。

文件末尾还有两个**集合级**的工具(``list_collections`` / ``drop_collection``):端口 ``VectorStore``
讲的是"一个集合怎么读写",而"库里有哪几个集合、某个集合要不要删"是**运维视角**的东西 ——
有了 ``page`` 列之后,旧集合不能写、新集合要建/要删,这类事必须有接口(见 ``cli/commands/drop.py``)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pymilvus import DataType, MilvusClient

from project_rag.config import MilvusConfig
from project_rag.domain import Chunk, Embedding, RetrievedChunk, SparseVector
from project_rag.observability import get_logger
from project_rag.ports import VectorStore

_logger = get_logger("adapters.milvus_store")

# 字段上限(按 UTF-8 字节数算,中文一个字 3 字节 —— 保守取值)。
# Milvus 自己也会拦,这里先拦一下是为了报错里能点名是哪个子块。
_MAX_TEXT = 8192
_MAX_PARENT = 32768

# 两个向量列的名字。稠密那一路叫 dense_vector(一直如此),稀疏那一列是关键字那一路的
# (见 adapters/bm25_sparse.py)。**列名写在这里一处**,别处只引用常量。
_DENSE_FIELD = "dense_vector"
_SPARSE_FIELD = "sparse_vector"

# 取回来的标量字段。**不取向量本身**:融合与重排都用不上,拉回来纯属浪费带宽。
# ``page`` 不在这里:老集合(见 _output_fields)没有这一列,取它会让整份语料查不出来。
_OUTPUT_FIELDS = ["text", "source", "doc_id", "parent_id", "parent_content"]

# 页码列名,以及"没有页码"在**存储层**的表示。
#
# 为什么用哨兵而不是 NULL:Milvus 2.4.4 **不支持 nullable** —— 建集合时 `nullable=True` 被
# 原样收下(describe 里连字段都不回),插入一行缺这个字段时却报
# `Insert missed an field 'page' to collection without set nullable==true or set default_value`。
# 「服务端接受 ≠ 支持」—— 实测如此。
# 哨兵取 **-1**:页码从 1 开始,负数不可能与真实页号撞;而 0 会被读成"第 0 页"。
# **翻译只发生在这一层**(`_to_row` 写、`_page_of_row` 读),领域层永远是 None。
_PAGE_FIELD = "page"
_PAGE_NONE = -1

# 两档读一致性,**分工是刻意的**:
#
# * ``search`` 那一路用**配置里的** ``milvus.read_consistency``（用户热路径，那档的差价与代价
#   写在 ``config.MilvusConfig.read_consistency`` 旁边）；
# * **其余读固定 strong**：指纹（判 BM25 索引还作不作数）、写后回查总数、全量导出。
#   这三处不在热路径上，要的正是"看到最新"—— 拿旧状态判"语料没变"会让新入库的文档一直
#   检索不到（静默错误），写后回查用 bounded 会数出偏小的块数（前端卡片就骗人了）。
_WRITE_READ_CONSISTENCY = "Strong"
# 配置里的小写值 → pymilvus 认的写法(它要大驼峰)。翻译只发生在这一层。
_CONSISTENCY_LEVELS = {"bounded": "Bounded", "eventually": "Eventually", "strong": "Strong"}

# query(不是 search)要把主键也取回来 —— search 的主键在 hit["id"] 里,两者形状不同。
# 具体取哪些字段由 ``_output_fields()`` 按集合的实际情况决定。
_PRIMARY_KEY = "chunk_id"

# 指纹只取「主键 + 父块号」:主键里含内容哈希,内容一变它就变;父块号覆盖父子结构的变化。
# 两者都是短字符串 —— 实测 1283 块的指纹约 80KB,而 load_all() 要把 ~6MB 正文拉回来。
# **页码不进指纹**:它不参与 BM25 的索引(那一路只用正文),加进来只会让每次重灌都白重建索引。
_FINGERPRINT_FIELDS = ["chunk_id", "parent_id"]

# 数块数时只取这一列(见 chunk_counts):不拉正文,几千块的清单刷新才有意义。
_DOC_ID_FIELD = "doc_id"

# 重算稀疏向量时要连稠密向量一起读回来(upsert 是整行替换,见 load_with_vectors)。
_DENSE_OUTPUT_FIELDS = ["text", "source", "doc_id", "parent_id", "parent_content", _DENSE_FIELD]

# Milvus 单次 query 的条数上限。到顶就报错,**不静默截断** ——
# 少喂一半语料给 BM25 是一种没有报错的质量损失,最难查。
_MAX_QUERY_ROWS = 16384


class MilvusVectorStore(VectorStore):
    """Milvus 上的子块向量库。"""

    def __init__(self, config: MilvusConfig, *, sparse: bool = False) -> None:
        """输入:Milvus 配置 + 要不要稀疏列。行为:建客户端(**不建集合**,集合在第一次写入时按实际维度建)。

        ``sparse=True`` 时,新建的集合会多一列 ``sparse_vector``(SPARSE_FLOAT_VECTOR +
        ``SPARSE_INVERTED_INDEX``)—— 关键字那一路走 ``keyword.backend=milvus`` 时才需要它。
        **这是 schema 级的开关**:老集合没有这一列,写入与稀疏检索都会当场报错
        (报错里给三条路),不会静默降级 —— 静默降级的表现是"检索结果变少",最难查。
        """
        self._collection = config.collection
        self._search_consistency = _CONSISTENCY_LEVELS[config.read_consistency]
        self._index = config.index
        self._sparse = sparse
        self._client = MilvusClient(
            uri=f"http://{config.host}:{config.port}", db_name=config.database
        )

    def upsert(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Embedding],
        *,
        sparse: Sequence[SparseVector] | None = None,
    ) -> int:
        """输入:子块 + 同序等长稠密向量 + 可选的同序等长稀疏向量。

        行为:按 chunk_id 覆盖写入并回查总数。输出:库内子块总数。

        「要不要给稀疏向量」由**集合有没有那一列**决定,两侧对不上都当场报错:
        该给没给 = 那行写不进去(Milvus 拒收空稀疏行);不该给却给了 = 权重被静默丢掉。
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks({len(chunks)}) 与 embeddings({len(embeddings)}) 必须等长同序"
            )
        if sparse is not None and len(sparse) != len(chunks):
            raise ValueError(
                f"sparse({len(sparse)}) 与 chunks({len(chunks)}) 必须等长同序"
            )
        if not chunks:
            return self._count()

        self._ensure_collection(len(embeddings[0].dense))
        self._require_page_column()
        has_sparse = _SPARSE_FIELD in self._fields()
        if has_sparse and sparse is None:
            raise RuntimeError(
                f"集合 {self._collection} 有 {_SPARSE_FIELD} 列,但这次写入没给稀疏向量 —— "
                "Milvus 会拒收空稀疏行。谁写谁负责编码(见 application/ingest_documents.py "
                "的 sparse_encoder);查配置:`keyword.backend` 是不是 milvus?"
            )
        if not has_sparse and sparse is not None:
            raise RuntimeError(
                f"集合 {self._collection} 没有 {_SPARSE_FIELD} 列(建它的时候 "
                "`keyword.backend` 还不是 milvus)—— 现在写进去,稀疏权重会被静默丢掉。三条路:"
                "① 换一个新集合名(改 config 的 milvus.collection)再 ingest,最省事也可回滚;"
                f"② 原地重建:`project-rag drop --collection {self._collection} --yes` 之后再 ingest;"
                "③ 确认 `keyword.backend` 与集合是哪一档,别把两档混着用。"
            )
        rows = [
            self._to_row(chunk, embedding, None if sparse is None else sparse[i])
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
        ]
        self._client.upsert(collection_name=self._collection, data=rows)
        return self._count()

    def search(
        self,
        query_embedding: Embedding,
        *,
        k: int,
        source: str | None = None,
    ) -> Sequence[RetrievedChunk]:
        """输入:查询向量、返回条数、来源过滤。行为:IP 相似检索(向量已归一化 → 就是余弦)。输出:前 k 条。"""
        if not self._client.has_collection(self._collection):
            # 空库给空序列,不抛错 —— 用例层要能走「没检索到」分支
            return []

        hits = self._client.search(
            collection_name=self._collection,
            data=[list(query_embedding.dense)],
            anns_field=_DENSE_FIELD,
            search_params=self._dense_search_params(),
            limit=k,
            output_fields=self._output_fields(),
            filter=f'source == "{source}"' if source is not None else "",
            consistency_level=self._search_consistency,
        )
        return [
            RetrievedChunk(chunk=self._to_chunk(hit), score=float(hit["distance"]))
            for hit in hits[0]
        ]

    def search_sparse(
        self,
        query_sparse: SparseVector,
        *,
        k: int,
        source: str | None = None,
    ) -> Sequence[RetrievedChunk]:
        """输入:查询的稀疏向量、返回条数、来源过滤。行为:稀疏列上做 IP 倒排检索。输出:前 k 条。

        **分数为 0 的行不算命中**(一个词都没沾上):稀疏 IP 会把"没有任何共同维度"的行
        打成 0 分,而候选不足 k 时 Milvus 照样把它们返回 —— 不在这里滤掉,
        融合那一步会给"第 1 名"排名分,0 分的行就挤掉了真命中
        (进程内 BM25 那一路也是这个判据)。
        """
        if not self._client.has_collection(self._collection):
            return []
        if _SPARSE_FIELD not in self._fields():
            raise RuntimeError(
                f"集合 {self._collection} 没有 {_SPARSE_FIELD} 列 —— 关键字那一路"
                "(`keyword.backend=milvus`)要它。要么换回 `keyword.backend=memory`,"
                "要么按新 schema 重灌一份。"
                "**不许把它当成「没检索到」**:那会让答案静默变差。"
            )

        hits = self._client.search(
            collection_name=self._collection,
            data=[dict(query_sparse)],
            anns_field=_SPARSE_FIELD,
            # 稀疏索引只认 IP,而且不带参数(2.4.4 实测:传 metric_type 之外的都报错)
            search_params={"metric_type": "IP", "params": {}},
            limit=k,
            output_fields=self._output_fields(),
            filter=f'source == "{source}"' if source is not None else "",
            consistency_level=self._search_consistency,
        )
        return [
            RetrievedChunk(chunk=self._to_chunk(hit), score=float(hit["distance"]))
            for hit in hits[0]
            if float(hit["distance"]) > 0.0
        ]

    def load_with_vectors(self) -> tuple[tuple[Chunk, tuple[float, ...]], ...]:
        """输入:无。行为:取出全部子块**连同稠密向量**。输出:(子块, 向量) 序列。

        给"重算稀疏向量"那条路用(``project-rag sparse``):Milvus 的 upsert 是**整行替换**
        (2.4.4 没有部分更新,``partial_update`` 是 2.5 服务端的能力),所以重写稀疏列时
        必须把稠密向量一起带回去 —— 否则那一次的 upsert 会把稠密列清空,
        表现成"稀疏检索好了、向量检索全空",而且不报错。
        """
        if not self._client.has_collection(self._collection):
            return ()
        rows = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=[_PRIMARY_KEY, *_DENSE_OUTPUT_FIELDS],
            limit=_MAX_QUERY_ROWS,
            consistency_level=_WRITE_READ_CONSISTENCY,
        )
        if len(rows) == _MAX_QUERY_ROWS:
            raise RuntimeError(
                f"库内子块数达到单次查询上限 {_MAX_QUERY_ROWS} —— 该给 load_with_vectors 加分页了"
            )
        out: list[tuple[Chunk, tuple[float, ...]]] = []
        for row in rows:
            vector = row.get(_DENSE_FIELD)
            if vector is None:
                raise RuntimeError(
                    f"子块 {row.get(_PRIMARY_KEY)!r} 读不到 {_DENSE_FIELD} —— "
                    "重写稀疏列时会把它连稠密向量一起覆盖掉,先查清楚再说"
                )
            out.append((self._row_to_chunk(row), tuple(float(x) for x in vector)))
        return tuple(out)

    def has_sparse_column(self) -> bool:
        """输入:无。行为:这个库能不能接稀疏向量。输出:布尔。

        **集合还不存在时按配置回答**(``sparse=True`` 时第一次写入会带上那一列)——
        调用方是 ingest 的预检:它要判的是"这批稀疏向量写得进去吗",
        而不是"此刻磁盘上有没有那一列"(那时集合可能还没建)。
        """
        if not self._client.has_collection(self._collection):
            return self._sparse
        return _SPARSE_FIELD in self._fields()

    def rebuild_index(self) -> list[str]:
        """输入:无。行为:按当前配置**重建**两个索引(先删后建,再 load)。输出:做过的事(人话)。

        **为什么需要它**:索引类型是建集合那一刻定格的,改 ``milvus.index.type`` 不会自动生效。
        FLAT 与 IVF_FLAT 之间换、或者调 ``nlist``,都在这里重建 —— 数据不动
        (索引是数据之上的结构),所以不用重灌。代价是重建期间这一路检索不可用。
        """
        if not self._client.has_collection(self._collection):
            raise RuntimeError(f"集合 {self._collection} 不存在 —— 先把语料灌进去")
        steps: list[str] = []
        for index_name in self._client.list_indexes(self._collection):
            detail = self._client.describe_index(self._collection, str(index_name))
            field = str(detail.get("field_name", "?"))
            self._client.release_collection(self._collection)
            self._client.drop_index(
                collection_name=self._collection, index_name=str(index_name)
            )
            steps.append(f"删掉 {field} 上的旧索引（{index_name}）")

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name=_DENSE_FIELD,
            index_type=self._index.type,
            metric_type="IP",
            params=({"nlist": self._index.nlist} if self._index.type == "IVF_FLAT" else {}),
        )
        if _SPARSE_FIELD in self._fields():
            index_params.add_index(
                field_name=_SPARSE_FIELD,
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )
            steps.append("重建 sparse_vector 的 SPARSE_INVERTED_INDEX")
        self._client.create_index(self._collection, index_params)
        self._client.load_collection(self._collection)
        steps.append(
            f"按配置建索引：dense_vector = {self._index.type}"
            + (f"（nlist={self._index.nlist}, nprobe={self._index.nprobe}）" if self._index.type == "IVF_FLAT" else "")
        )
        _logger.info("已重建 %s 的索引：%s", self._collection, "; ".join(steps))
        return steps

    def delete_by_doc_id(self, doc_id: str) -> int:
        """输入:文档标识。行为:按标量过滤删除这份文档的全部子块(ingest 写之前调)。输出:删除条数。"""
        if not self._client.has_collection(self._collection):
            return 0
        result = self._client.delete(
            collection_name=self._collection, filter=f'doc_id == "{doc_id}"'
        )
        return int(result.get("delete_count", 0))

    def load_all(self) -> Sequence[Chunk]:
        """输入:无。行为:取出全部子块(顺序稳定)。输出:子块序列。

        给关键字那一路建索引用的(BM25 要看到全量文本)。
        """
        if not self._client.has_collection(self._collection):
            return []
        rows = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=[_PRIMARY_KEY, *self._output_fields()],
            limit=_MAX_QUERY_ROWS,
            consistency_level=_WRITE_READ_CONSISTENCY,
        )
        if len(rows) == _MAX_QUERY_ROWS:
            raise RuntimeError(
                f"库内子块数达到单次查询上限 {_MAX_QUERY_ROWS} —— 该给 load_all 加分页了"
            )
        return [self._row_to_chunk(row) for row in rows]

    def fingerprint(self) -> Sequence[str]:
        """输入:无。行为:取全部块的主键 + 父块号,拼成排序后的指纹。输出:指纹序列。

        比 ``load_all()`` 便宜一个量级(只取两个短字段,不取正文与父块正文):
        给关键字那一路做「索引还作不作数」的判断用。
        """
        if not self._client.has_collection(self._collection):
            return []
        rows = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=_FINGERPRINT_FIELDS,
            limit=_MAX_QUERY_ROWS,
            consistency_level=_WRITE_READ_CONSISTENCY,
        )
        if len(rows) == _MAX_QUERY_ROWS:
            raise RuntimeError(
                f"库内子块数达到单次查询上限 {_MAX_QUERY_ROWS} —— 该给 fingerprint 加分页了"
            )
        # \x1f 是项目里惯用的字段分隔符(缓存 value 契约也用它):两个字段都是十六进制串,
        # 不含这个字符,拼起来不会有歧义
        return sorted(
            f"{row['chunk_id']}\x1f{row.get('parent_id') or ''}" for row in rows
        )

    def chunk_counts(self) -> Mapping[str, int]:
        """输入:无。行为:按 ``doc_id`` 数一遍块数。输出:``doc_id -> 块数``。

        只取 ``doc_id`` 一列 —— 知识库清单每次刷新都要问一次"库里到底有什么",
        而 ``load_all()`` 会把全部正文与父块正文(几千块约 6MB)拉回来。
        读一致性与 ``fingerprint`` 同档(strong):清单刚从库里数出来的数,
        不能用可能偏旧的快照去判"这份文档搜不搜得到"。
        """
        if not self._client.has_collection(self._collection):
            return {}
        rows = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=[_DOC_ID_FIELD],
            limit=_MAX_QUERY_ROWS,
            consistency_level=_WRITE_READ_CONSISTENCY,
        )
        if len(rows) == _MAX_QUERY_ROWS:
            raise RuntimeError(
                f"库内子块数达到单次查询上限 {_MAX_QUERY_ROWS} —— 该给 chunk_counts 加分页了"
            )
        counts: dict[str, int] = {}
        for row in rows:
            doc_id = str(row.get(_DOC_ID_FIELD) or "")
            if doc_id:
                counts[doc_id] = counts.get(doc_id, 0) + 1
        return counts

    def delete_by_source(self, source: str) -> int:
        """输入:来源标签。行为:按标量过滤删除该来源的全部子块(整份语料下架时用)。输出:删除条数。"""
        if not self._client.has_collection(self._collection):
            return 0
        result = self._client.delete(
            collection_name=self._collection, filter=f'source == "{source}"'
        )
        return int(result.get("delete_count", 0))

    def drop(self) -> None:
        """输入:无。行为:删除整个集合(重建与验收用,数据不可恢复)。输出:无。"""
        if self._client.has_collection(self._collection):
            self._client.drop_collection(self._collection)

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _fields(self) -> set[str]:
        """输入:无。行为:问一次集合里有哪些列。输出:列名集合;集合不存在给空集。

        每次现问而不是缓存:验收与重灌会在同一个进程里 drop / 重建集合,
        缓存住的"没有 page 列"会让重建之后的写入莫名其妙地继续报错。
        一次 describe 相对一次 query/upsert 是噪声级的开销。
        """
        if not self._client.has_collection(self._collection):
            return set()
        return {
            str(field["name"])
            for field in self._client.describe_collection(self._collection)["fields"]
        }

    def _output_fields(self) -> list[str]:
        """输入:无。行为:按集合**实际有的**列决定取哪些标量字段。输出:字段名列表。

        **老集合(没有 page 列)要能读**:早期建的集合里那些块没有页码,读回来是 None ——
        与「父子分层之前入库的扁平块 parent_id 为空串」是同一个约定:旧数据也得能跑。
        硬要 page 只会让整份语料在迁移完成之前一句都答不出来。
        """
        fields = list(_OUTPUT_FIELDS)
        if _PAGE_FIELD in self._fields():
            fields.append(_PAGE_FIELD)
        return fields

    def _require_page_column(self) -> None:
        """输入:无。行为:确认集合有 page 列,没有就抛错并给出路。输出:无。

        **写入**与读取相反,必须报错而不是"没有这一列就不写":那等于把页码静默丢掉,
        而这一列的全部意义就是让页码落库 —— 灌完 500 页语料才发现页码没进去,代价是再灌一次
        (CPU 上 8 分钟,还要重新嵌入)。宁可当场停下。
        """
        if _PAGE_FIELD in self._fields():
            return
        raise RuntimeError(
            f"集合 {self._collection} 没有 {_PAGE_FIELD} 列(早期建的老集合)"
            " —— 现在写进去,页码会被静默丢掉。三条路(先 `project-rag collections` 看现状):"
            "① 已有带 page 列的新集合 → 改 config 里 milvus.collection 指过去(最省事,可回滚);"
            "② 想原地重建 → `project-rag drop --collection "
            f"{self._collection} --yes` 之后再 ingest(第一次写入会把新 schema 建出来);"
            "③ 想换名字 → 改 config 再逐个 source 重灌。"
            "⚠️ 别只重灌一部分 source:那会把别的 source 留在老集合里,而那些块没有页码。"
        )

    def _count(self) -> int:
        """输入:无。行为:回查库内子块总数(upsert 的返回值不是总数)。输出:条数;集合不存在给 0。"""
        if not self._client.has_collection(self._collection):
            return 0
        rows = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=["count(*)"],
            consistency_level=_WRITE_READ_CONSISTENCY,
        )
        return int(rows[0]["count(*)"]) if rows else 0

    def _dense_search_params(self) -> dict:
        """输入:无。行为:把配置里的索引参数翻成 pymilvus 的检索参数。输出:search_params。

        ``FLAT`` 是暴力检索,没有参数可调;``IVF_FLAT`` 要告诉它探几个簇(``nprobe``)。
        翻译只发生在这一层 —— 配置里写的是 ``milvus.index.*``,别处不该出现 "nprobe" 这个词。
        """
        if self._index.type == "IVF_FLAT":
            return {
                "metric_type": "IP",
                "params": {"nprobe": self._index.nprobe},
            }
        return {"metric_type": "IP", "params": {}}

    def _ensure_collection(self, dim: int) -> None:
        """输入:向量维度。行为:集合不存在就按这个维度建出来(含两个索引)。输出:无。

        建出来的 schema 是**这一档配置的定格**:要不要稀疏列由构造时的 ``sparse`` 决定,
        稠密索引类型由 ``milvus.index.type`` 决定。两者都是"建集合那一刻定型"的东西 ——
        改配置不会自动改已有集合(换索引要 ``collections --reindex``,换 schema 要重灌)。
        """
        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=_MAX_TEXT)
        schema.add_field(_DENSE_FIELD, DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("source", DataType.VARCHAR, max_length=64)
        # doc_id 是 ingest 用来清旧块的抓手(见 delete_by_doc_id)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        # 父子分层字段:父子分层之前入库的扁平块没有父块,所以允许空串;新写入的都带
        schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
        schema.add_field("parent_content", DataType.VARCHAR, max_length=_MAX_PARENT)
        # 页码(1 起)。**建不出 nullable 的列**(2.4.4 不支持,见 _PAGE_NONE),
        # 所以每一行都必须带这个字段:没有页码时写哨兵 -1。
        schema.add_field(_PAGE_FIELD, DataType.INT64)
        if self._sparse:
            # 关键字那一路的稀疏列(词哈希维度 → BM25 权重)。见 adapters/bm25_sparse.py。
            schema.add_field(_SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name=_DENSE_FIELD,
            index_type=self._index.type,  # FLAT(精确)/ IVF_FLAT(近似,探 nprobe 个簇)
            metric_type="IP",  # 向量已 L2 归一化 → IP 就等于余弦
            params=({"nlist": self._index.nlist} if self._index.type == "IVF_FLAT" else {}),
        )
        if self._sparse:
            # 稀疏列只能建倒排索引、只认 IP(2.4.4 实测:别的组合直接报错)
            index_params.add_index(
                field_name=_SPARSE_FIELD,
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
        )
        _logger.info("已创建集合 %s(dim=%d)", self._collection, dim)

    def _to_row(
        self, chunk: Chunk, embedding: Embedding, sparse: SparseVector | None
    ) -> dict:
        """输入:子块 + 稠密向量 + 稀疏向量(没有就给 None)。行为:转成 Milvus 的行。输出:行字典。

        超长字段在这里提前拦(报错能点名是哪个子块),而不是等 Milvus 回一句英文错误。
        """
        text_bytes = len(chunk.text.encode("utf-8"))
        parent_bytes = len(chunk.parent_content.encode("utf-8"))
        if text_bytes > _MAX_TEXT:
            raise ValueError(
                f"子块 {chunk.chunk_id} 的文本 {text_bytes} 字节,超过字段上限 {_MAX_TEXT};"
                "调小 chunk_size 或调大 _MAX_TEXT"
            )
        if parent_bytes > _MAX_PARENT:
            raise ValueError(
                f"子块 {chunk.chunk_id} 的父块 {parent_bytes} 字节,超过字段上限 {_MAX_PARENT}"
            )
        row = {
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            _DENSE_FIELD: list(embedding.dense),
            "source": chunk.source,
            "doc_id": chunk.doc_id,
            "parent_id": chunk.parent_id,
            "parent_content": chunk.parent_content,
            # 没有页码写哨兵:Milvus 要求每一行都带上所有非 nullable 的字段
            _PAGE_FIELD: chunk.page if chunk.page is not None else _PAGE_NONE,
        }
        if sparse is not None:
            # 空稀疏行会被 Milvus 拒收(`empty sparse float vector row`)—— 编码器已经保证
            # 非空(保留维度 0 兜底),这里再挡一道:报错点名是哪个块,别让它变成一句英文异常
            if not sparse:
                raise ValueError(
                    f"子块 {chunk.chunk_id} 的稀疏向量是空的 —— Milvus 拒收空稀疏行,"
                    "编码器该用保留维度兜底(见 adapters/bm25_sparse.py)"
                )
            row[_SPARSE_FIELD] = {int(dim): float(weight) for dim, weight in sparse.items()}
        return row

    @staticmethod
    def _page_of_row(row: dict) -> int | None:
        """输入:一行(可能来自没有 page 列的老集合)。行为:把哨兵翻回领域语义。输出:页码;没有给 None。

        **哨兵只活在适配器这一层**:领域层永远是 ``None`` —— 「没有页码」和「第 0 页」
        在类型上必须能分开,因为它们导致的行为完全不同(前者前端显示"页码未知",后者是错的)。
        """
        value = row.get(_PAGE_FIELD)
        if value is None:  # 老集合没有这一列
            return None
        page = int(value)
        return page if page > 0 else None

    @staticmethod
    def _row_to_chunk(row: dict) -> Chunk:
        """输入:Milvus query 返回的一行。行为:还原成领域对象。输出:子块。"""
        return Chunk(
            chunk_id=str(row["chunk_id"]),
            text=str(row["text"]),
            source=str(row["source"]),
            doc_id=str(row.get("doc_id", "")),
            parent_id=str(row.get("parent_id", "")),
            parent_content=str(row.get("parent_content", "")),
            page=MilvusVectorStore._page_of_row(row),
        )

    @staticmethod
    def _to_chunk(hit: dict) -> Chunk:
        """输入:Milvus 的一条命中。行为:把标量字段还原成领域对象。输出:子块。"""
        entity = hit["entity"]
        return Chunk(
            chunk_id=str(hit["id"]),
            text=str(entity["text"]),
            source=str(entity["source"]),
            doc_id=str(entity.get("doc_id", "")),
            parent_id=str(entity.get("parent_id", "")),
            parent_content=str(entity.get("parent_content", "")),
            page=MilvusVectorStore._page_of_row(entity),
        )


# ── 集合级工具(运维视角,不属于 VectorStore 端口) ───────────────────────────
@dataclass(frozen=True, slots=True)
class CollectionInfo:
    """库里一个集合的现状(``project-rag collections`` 就是打这个)。"""

    name: str
    rows: int  # 真实块数(query 的行数,**不是** count(*))
    capped: bool  # 撞到单次查询上限(16384) ⇒ 真实块数 ≥ rows
    has_page: bool  # 有没有 page 列
    dim: int | None
    has_sparse: bool = False  # 有没有稀疏列(keyword.backend=milvus 建出来的才有)
    index_type: str = "?"  # 稠密列上的索引类型(FLAT / IVF_FLAT / …);读不到给 "?"
    index_params: str = ""  # 索引参数的人话(nlist=…),FLAT 为空
    error: str = ""  # 读这个集合时出的错(空 = 读到了) —— 别的工具建的集合不算项目数据


def _client(config: MilvusConfig) -> MilvusClient:
    """输入:Milvus 配置。行为:建一个客户端。输出:客户端。"""
    return MilvusClient(uri=f"http://{config.host}:{config.port}", db_name=config.database)


def _rows_of(client: MilvusClient, name: str) -> tuple[int, bool]:
    """输入:客户端 + 集合名。行为:数真实块数(只取主键)。输出:``(块数, 是否撞上限)``。

    **不用 ``count(*)``**:它把"已按 ``doc_id`` 删掉但还没压实"的旧块一起算进去
    (实测同一时刻 ``count(*)`` 报 2844、``query`` 只回 1491)。
    盘点集合大小时给人一个虚高的数,比不给还糟。
    """
    # 上限就是 16384(Milvus 的 (offset+limit) 窗口上限):多要一行会被服务端直接拒
    # (实测 16385 → "invalid max query result window")。所以撞到上限就报"≥ 这个数"。
    rows = client.query(
        collection_name=name,
        filter="",
        output_fields=[_PRIMARY_KEY],
        limit=_MAX_QUERY_ROWS,
        consistency_level=_WRITE_READ_CONSISTENCY,
    )
    return (len(rows), len(rows) >= _MAX_QUERY_ROWS)


def list_collections(config: MilvusConfig) -> tuple[CollectionInfo, ...]:
    """输入:Milvus 配置。行为:列出每个集合的块数、``page``/稀疏列、维度、**稠密索引**。输出:按名字排序的清单。

    索引那两列是给"换 FLAT / IVF_FLAT"用的:改了配置之后要能一眼看出**这个集合现在
    到底是哪种索引** —— 否则"我改了配置怎么没变快"会查很久(索引是建集合那一刻定格的,
    改配置不会自动重排,得 `collections --reindex`)。
    """
    client = _client(config)
    infos: list[CollectionInfo] = []
    for name in sorted(client.list_collections()):
        # 库里可能有**别的工具**建的集合(Attu 手建的、上一次探针留下的):
        # 它们的列名不是项目约定,数块数会直接抛错。一个读不出来的集合不该让整条命令失效
        # —— 记下原因照常列出,人一眼就知道那不是项目数据。
        try:
            fields = {
                field["name"]: field for field in client.describe_collection(name)["fields"]
            }
            rows, capped = _rows_of(client, name)
        except Exception as exc:  # noqa: BLE001 - 只影响这一个集合的展示
            infos.append(
                CollectionInfo(
                    name=name, rows=0, capped=False, has_page=False, dim=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        dense = fields.get(_DENSE_FIELD, {})
        index_type = "?"
        index_params = ""
        # 注意:`list_indexes` 给的是**索引名**(不是字典),详情得一个个 describe_index。
        # pymilvus 2.5 客户端 + Milvus 2.4.4 服务端实测就是这个形状。
        for index_name in client.list_indexes(name):
            detail = client.describe_index(name, str(index_name))
            if str(detail.get("field_name", "")) != _DENSE_FIELD:
                continue
            index_type = str(detail.get("index_type", "?"))
            raw = detail.get("params") or {}
            params = raw if isinstance(raw, dict) else {}
            index_params = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
            break
        infos.append(
            CollectionInfo(
                name=name,
                rows=rows,
                capped=capped,
                has_page=_PAGE_FIELD in fields,
                dim=(dense.get("params") or {}).get("dim"),
                has_sparse=_SPARSE_FIELD in fields,
                index_type=index_type,
                index_params=index_params,
            )
        )
    return tuple(infos)


def drop_collection(config: MilvusConfig, name: str) -> int:
    """输入:Milvus 配置 + 集合名。行为:删掉这个集合。输出:删之前的块数(0 = 本来就没有)。

    **不可恢复** —— 但语料在磁盘上,重灌就能回来;所以调用方(``cli/commands/drop.py``)
    的规矩是"先说清会删什么,``--yes`` 才真删"。
    """
    client = _client(config)
    if not client.has_collection(name):
        return 0
    rows, _capped = _rows_of(client, name)
    client.drop_collection(name)
    _logger.info("已删掉集合 %s(删前 %d 块)", name, rows)
    return rows
