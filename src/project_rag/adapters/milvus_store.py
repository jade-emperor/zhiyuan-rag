"""
Milvus 向量库适配器(**只管稠密一路**)。

**三个实测坑**(服务端 v2.4.4 + 客户端 pymilvus 2.5.18,属官方兼容表外的搭配):

- ``upsert`` 之后立刻 ``search`` / ``query`` 会**查不到刚写的数据**(默认一致性是 Bounded)。
  在 ``create_schema`` 里声明 ``consistency_level="Strong"`` **不生效**;
  在**每次查询调用上**传 ``consistency_level="Strong"`` 才生效(实测 0 条 → 3 条);
  ``flush()`` 也能行,但那是更重的锤子,不该每次写都敲。
- ``upsert`` 的返回值是 ``{"upsert_count": 本次写入数}``,**不是库内总数** ——
  端口契约要的是总数,所以这里写完之后额外查一次 ``count(*)``。
- ``get_collection_stats`` 的 ``row_count`` 在 flush 之前是 0,**不能拿它当"库里有几条"**。

维度不写死:集合在**第一次写入时按实际向量维度**建。这样换嵌入模型不用改这里 ——
维度对不上时 Milvus 会直接报错(响亮地失败),而不是把错维度的向量塞进去。
"""

from __future__ import annotations

from collections.abc import Sequence

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
_OUTPUT_FIELDS = ["text", "source", "doc_id", "parent_id", "parent_content"]

# 读一致性。必须是 "Strong":默认的 Bounded 会让我们读到写入前的状态(见模块 docstring)。
_READ_CONSISTENCY = "Strong"

# query(不是 search)要把主键也取回来 —— search 的主键在 hit["id"] 里,两者形状不同
_QUERY_FIELDS = ["chunk_id", *_OUTPUT_FIELDS]

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
            output_fields=_OUTPUT_FIELDS,
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
            output_fields=_QUERY_FIELDS,
            limit=_MAX_QUERY_ROWS,
            consistency_level=_READ_CONSISTENCY,
        )
        if len(rows) == _MAX_QUERY_ROWS:
            raise RuntimeError(
                f"库内子块数达到单次查询上限 {_MAX_QUERY_ROWS} —— 该给 load_all 加分页了"
            )
        return [self._row_to_chunk(row) for row in rows]

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
        }

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
        )
