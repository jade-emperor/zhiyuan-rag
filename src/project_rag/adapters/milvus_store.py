"""
Milvus 向量库适配器(**只管稠密一路**)。

维度不写死:集合在**第一次写入时按实际向量维度**建。这样换嵌入模型不用改这里 ——
维度对不上时 Milvus 会直接报错,而不是把错维度的向量塞进去。

文件末尾还有两个**集合级**的工具(``list_collections`` / ``drop_collection``):端口 ``VectorStore``
讲的是"一个集合怎么读写",而"库里有哪几个集合、某个集合要不要删"是**运维视角**的东西 ——
P0-5 加 ``page`` 列之后,旧集合不能写、新集合要建/要删,这类事必须有接口(见 ``cli/commands/drop.py``)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pymilvus import DataType, MilvusClient

from project_rag.config import MilvusConfig
from project_rag.domain import Chunk, Embedding, RetrievedChunk
from project_rag.observability import get_logger
from project_rag.ports import VectorStore

_logger = get_logger("adapters.milvus_store")

# 字段上限(按 UTF-8 字节数算,中文一个字 3 字节 —— 保守取值)。
# Milvus 自己也会拦,这里先拦一下是为了报错里能点名是哪个子块。
_MAX_TEXT = 8192
_MAX_PARENT = 32768

# 取回来的标量字段。**不取向量本身**:融合与重排都用不上,拉回来纯属浪费带宽。
# ``page`` 不在这里:老集合(见 _output_fields)没有这一列,取它会让整份语料查不出来。
_OUTPUT_FIELDS = ["text", "source", "doc_id", "parent_id", "parent_content"]

# P0-5:页码列名,以及"没有页码"在**存储层**的表示。
#
# 为什么用哨兵而不是 NULL:Milvus 2.4.4 **不支持 nullable** —— 建集合时 `nullable=True` 被
# 原样收下(describe 里连字段都不回),插入一行缺这个字段时却报
# `Insert missed an field 'page' to collection without set nullable==true or set default_value`。
# 「服务端接受 ≠ 支持」,实测记录见 STATUS §0.3。
# 哨兵取 **-1**:页码从 1 开始,负数不可能与真实页号撞;而 0 会被读成"第 0 页"。
# **翻译只发生在这一层**(`_to_row` 写、`_page_of_row` 读),领域层永远是 None。
_PAGE_FIELD = "page"
_PAGE_NONE = -1

# 读一致性。必须是 "Strong":默认的 Bounded 会让我们读到写入前的状态(见模块 docstring)。
_READ_CONSISTENCY = "Strong"

# query(不是 search)要把主键也取回来 —— search 的主键在 hit["id"] 里,两者形状不同。
# 具体取哪些字段由 ``_output_fields()`` 按集合的实际情况决定。
_PRIMARY_KEY = "chunk_id"

# 指纹只取「主键 + 父块号」:主键里含内容哈希,内容一变它就变;父块号覆盖父子结构的变化。
# 两者都是短字符串 —— 实测 1283 块的指纹约 80KB,而 load_all() 要把 ~6MB 正文拉回来。
# **页码不进指纹**:它不参与 BM25 的索引(那一路只用正文),加进来只会让每次重灌都白重建索引。
_FINGERPRINT_FIELDS = ["chunk_id", "parent_id"]

# Milvus 单次 query 的条数上限。到顶就报错,**不静默截断** ——
# 少喂一半语料给 BM25 是一种没有报错的质量损失,最难查。
_MAX_QUERY_ROWS = 16384


class MilvusVectorStore(VectorStore):
    """Milvus 上的子块向量库。"""

    def __init__(self, config: MilvusConfig) -> None:
        """输入:Milvus 配置。行为:建客户端(**不建集合**,集合在第一次写入时按实际维度建)。输出:无。"""
        self._collection = config.collection
        self._client = MilvusClient(
            uri=f"http://{config.host}:{config.port}", db_name=config.database
        )

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Embedding]) -> int:
        """输入:子块 + 同序等长向量。行为:按 chunk_id 覆盖写入并回查总数。输出:库内子块总数。"""
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks({len(chunks)}) 与 embeddings({len(embeddings)}) 必须等长同序"
            )
        if not chunks:
            return self._count()

        self._ensure_collection(len(embeddings[0].dense))
        self._require_page_column()
        rows = [
            self._to_row(chunk, embedding)
            for chunk, embedding in zip(chunks, embeddings)
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
            limit=k,
            output_fields=self._output_fields(),
            filter=f'source == "{source}"' if source is not None else "",
            consistency_level=_READ_CONSISTENCY,
        )
        return [
            RetrievedChunk(chunk=self._to_chunk(hit), score=float(hit["distance"]))
            for hit in hits[0]
        ]

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
            consistency_level=_READ_CONSISTENCY,
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
            consistency_level=_READ_CONSISTENCY,
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

        **老集合(没有 page 列)要能读**:P0-5 之前入库的块没有页码,读回来是 None ——
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
        而 P0-5 的全部意义就是让页码落库 —— 灌完 500 页语料才发现页码没进去,代价是再灌一次
        (CPU 上 8 分钟,还要重新嵌入)。宁可当场停下。
        """
        if _PAGE_FIELD in self._fields():
            return
        raise RuntimeError(
            f"集合 {self._collection} 没有 {_PAGE_FIELD} 列(P0-5 之前建的)"
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
            consistency_level=_READ_CONSISTENCY,
        )
        return int(rows[0]["count(*)"]) if rows else 0

    def _ensure_collection(self, dim: int) -> None:
        """输入:向量维度。行为:集合不存在就按这个维度建出来(含索引)。输出:无。"""
        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=_MAX_TEXT)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("source", DataType.VARCHAR, max_length=64)
        # doc_id 是 ingest 用来清旧块的抓手(见 delete_by_doc_id)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        # 父子分层字段:父子分层之前入库的扁平块没有父块,所以允许空串;新写入的都带
        schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
        schema.add_field("parent_content", DataType.VARCHAR, max_length=_MAX_PARENT)
        # P0-5:页码(1 起)。**建不出 nullable 的列**(2.4.4 不支持,见 _PAGE_NONE),
        # 所以每一行都必须带这个字段:没有页码时写哨兵 -1。
        schema.add_field(_PAGE_FIELD, DataType.INT64)

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_type="FLAT",  # 精确检索:小语料比 IVF_FLAT 更稳,结果可断言
            metric_type="IP",  # 向量已 L2 归一化 → IP 就等于余弦
        )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
        )
        _logger.info("已创建集合 %s(dim=%d)", self._collection, dim)

    def _to_row(self, chunk: Chunk, embedding: Embedding) -> dict:
        """输入:子块 + 向量。行为:转成 Milvus 的行,超长字段提前拦。输出:行字典。"""
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
        return {
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "dense_vector": list(embedding.dense),
            "source": chunk.source,
            "doc_id": chunk.doc_id,
            "parent_id": chunk.parent_id,
            "parent_content": chunk.parent_content,
            # 没有页码写哨兵:Milvus 要求每一行都带上所有非 nullable 的字段
            _PAGE_FIELD: chunk.page if chunk.page is not None else _PAGE_NONE,
        }

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
    has_page: bool  # 有没有 P0-5 加的 page 列
    dim: int | None


def _client(config: MilvusConfig) -> MilvusClient:
    """输入:Milvus 配置。行为:建一个客户端。输出:客户端。"""
    return MilvusClient(uri=f"http://{config.host}:{config.port}", db_name=config.database)


def _rows_of(client: MilvusClient, name: str) -> tuple[int, bool]:
    """输入:客户端 + 集合名。行为:数真实块数(只取主键)。输出:``(块数, 是否撞上限)``。

    **不用 ``count(*)``**:它把"已按 ``doc_id`` 删掉但还没压实"的旧块一起算进去
    (实测同一时刻 ``count(*)`` 报 2844、``query`` 只回 1491,见 STATUS §0)。
    盘点集合大小时给人一个虚高的数,比不给还糟。
    """
    # 上限就是 16384(Milvus 的 (offset+limit) 窗口上限):多要一行会被服务端直接拒
    # (实测 16385 → "invalid max query result window")。所以撞到上限就报"≥ 这个数"。
    rows = client.query(
        collection_name=name,
        filter="",
        output_fields=[_PRIMARY_KEY],
        limit=_MAX_QUERY_ROWS,
        consistency_level=_READ_CONSISTENCY,
    )
    return (len(rows), len(rows) >= _MAX_QUERY_ROWS)


def list_collections(config: MilvusConfig) -> tuple[CollectionInfo, ...]:
    """输入:Milvus 配置。行为:列出库里每个集合的块数、有没有 ``page`` 列、向量维度。输出:按名字排序的清单。"""
    client = _client(config)
    infos: list[CollectionInfo] = []
    for name in sorted(client.list_collections()):
        fields = {field["name"]: field for field in client.describe_collection(name)["fields"]}
        rows, capped = _rows_of(client, name)
        dense = fields.get("dense_vector", {})
        infos.append(
            CollectionInfo(
                name=name,
                rows=rows,
                capped=capped,
                has_page=_PAGE_FIELD in fields,
                dim=(dense.get("params") or {}).get("dim"),
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
