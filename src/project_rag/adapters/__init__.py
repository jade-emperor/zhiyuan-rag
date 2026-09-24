# 这里只 re-export **轻量**适配器。重适配器(embedder_bge_m3 / reranker_bge)要按完整
# 路径 import:它们一旦进这个包入口,import project_rag.adapters 就会把
# sentence_transformers + torch 拖起来(秒级 + 上 G 内存),`--help` 也得交这份钱。
from .bm25_index import BM25KeywordIndex
from .cache_redis import RedisCache
from .chunker_recursive import ChineseRecursiveChunker
from .conversation_mysql import MySQLConversationStore
from .faq_mysql import MySQLFaqStore
from .hashing_embedder import HashingEmbedder
from .llm_deepseek import DeepSeekLLM
from .matcher_bm25 import BM25TextMatcher
from .memory_vector_store import InMemoryVectorStore
from .milvus_store import MilvusVectorStore
from .text_files import load_text_files

__all__ = [
    "BM25KeywordIndex",
    "BM25TextMatcher",
    "ChineseRecursiveChunker",
    "DeepSeekLLM",
    "HashingEmbedder",
    "InMemoryVectorStore",
    "MilvusVectorStore",
    "MySQLConversationStore",
    "MySQLFaqStore",
    "RedisCache",
    "load_text_files",
]
