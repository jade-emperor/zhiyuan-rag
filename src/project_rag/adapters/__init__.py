# 这里只 re-export **轻量**适配器。两类"重"适配器要按完整路径 import:
#
#   * sentence_transformers + torch 那一类(embedder_bge_m3 / reranker_bge)—— 秒级 + 上 G 内存;
#   * **llm_deepseek** —— 它拉的是 langchain_openai + langchain_deepseek,实测
#     `import project_rag.adapters.llm_deepseek` = **3510ms**。而 import 任何一个子模块都会先跑
#     这个 `__init__`,所以它只要留在名单里,`--help`(实测 4.5s)和每一次
# `from project_rag.adapters import …` 都得替它付这 3.5 秒 —— 挪出去。
from .bm25_index import BM25KeywordIndex
from .cache_redis import RedisCache
from .chunker_recursive import ChineseRecursiveChunker
from .conversation_mysql import MySQLConversationStore
from .document_loader import LoadedCorpus, load_documents, supported_suffixes
from .faq_mysql import MySQLFaqStore
from .hashing_embedder import HashingEmbedder
from .matcher_bm25 import BM25TextMatcher
from .memory_vector_store import InMemoryVectorStore
from .milvus_store import MilvusVectorStore
from .office_files import OfficeFileSource
from .pdf_files import PdfFileSource
from .text_files import TextFilesSource

__all__ = [
    "BM25KeywordIndex",
    "BM25TextMatcher",
    "ChineseRecursiveChunker",
    "HashingEmbedder",
    "InMemoryVectorStore",
    "LoadedCorpus",
    "MilvusVectorStore",
    "MySQLConversationStore",
    "MySQLFaqStore",
    "OfficeFileSource",
    "PdfFileSource",
    "RedisCache",
    "TextFilesSource",
    "load_documents",
    "supported_suffixes",
]
