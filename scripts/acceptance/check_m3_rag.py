#!/usr/bin/env python
"""验收：向量知识库 + RAG 问答。

用法：

    uv run python scripts/acceptance/check_m3_rag.py

本脚本是这份契约的**唯一权威**。
计划内的 SKIP 已全部转成真断言（「融合路由」那条见文件末尾的全链路检查）。
允许存在的 SKIP 只剩一类：前置条件不满足（容器 / GPU 沙箱）。

配置契约（本步钉住；默认值要改就得同时改这里 + config.yaml）：

    milvus:    host=127.0.0.1 port=10086 database=default collection=rag_chunks
    embedding: model_dir=models/bge-m3 rerank_model_dir=models/bge-reranker-v2-m3
               device/use_f16 是**部署选择**（本机 cuda + f16），不进默认值契约
    rag:       parent_chunk_size=1200 child_chunk_size=300 chunk_overlap=50
               retrieval_k=5 candidate_m=2 fusion=rrf rrf_k=60
               bm25_weight=0.5 vector_weight=0.5 rerank=True

``retrieval_k`` / ``candidate_m`` 曾被调成 10/3、随后又被评估集
量回了 5/2（均值 0.9347 vs 0.9313）——
**这就是评估集存在的意义：调参不再靠感觉**。
⚠️ ``config.py`` 里 ``RagConfig`` 的字段默认值**也要一起改**（本文件检查的是
**生效值**，所以它拦不住「yaml 改了、类默认值没改」这种半成品）。

``collection`` 叫 ``rag_chunks`` 而不是示例里的 ``edurag_xian_1``：名字跟项目走。

两条「写进配置就绕开」的坑：

1. ``milvus.port`` 是**宿主**端口 10086，不是常见默认的 19530 —— 服务端在容器里，
   compose 把 gRPC 19530 映射到 10086。
2. **召回方案是本项目自己定的，不是照搬现成的**：两路 = 进程内 BM25（关键字）
   + Milvus 稠密向量（语义），融合在用例层（RRF 或归一化加权，配置里选）。
   现成做法用 ``WeightedRanker(0.7, 1.0)`` 做引擎内融合，但要跟着用它的学习型稀疏向量；
   而学习型稀疏与 BM25 都是词面匹配，两路重叠度高 —— 少一路，每一路都说得清。
   顺带避开那处笔误：``WeightedRanker(0.7, 1.0)`` 的注释写成「稀疏 0.7、稠密 1.0」，
   而 pymilvus 按 ``reqs`` 顺序配对，信注释就会把权重配反。

环境变量覆盖沿用 ``RAG_`` 前缀 + ``__`` 分层：``RAG_MILVUS__PORT``、``RAG_RAG__RETRIEVAL_K``。

形状契约（**已实现**；要改形状就先改这里，再改代码）：

    domain: Chunk(chunk_id, text, source, doc_id, parent_id, parent_content, page)
            ChunkGroup(parent_text, children, child_starts)   # 切分器只切块,id 是用例层的事
            Embedding(dense)                           # 刻意只有稠密,词面那一路交给 BM25
            RetrievedChunk(chunk: Chunk, score: float)
            Citation(index, chunk, score, routes, rerank_score)   # page 是 property(问子块)
    ports:  Embedder.embed(texts) -> Sequence[Embedding]; Embedder.dim -> int
            TextChunker.split(text) -> Sequence[ChunkGroup]
            VectorStore.upsert(chunks, embeddings) -> int      # 返回库内总数
            VectorStore.search(query_embedding, *, k, source) -> Sequence[RetrievedChunk]
            VectorStore.delete_by_doc_id(doc_id) -> int        # 重灌前清旧块(日常路径)
            VectorStore.delete_by_source(source) -> int        # 整份语料下架
            VectorStore.load_all() -> Sequence[Chunk]          # 给 BM25 那一路取语料
            VectorStore.drop() -> None                         # 重建与验收用
            KeywordIndex.search(query, *, k, source) -> Sequence[RetrievedChunk]
            Reranker.rerank(query, texts) -> Sequence[float]   # 同序等长,越大越相关
            (融合函数 fuse_rankings 归用例层,不放引擎里)
    Deps:   embedder_factory / vector_store_factory / keyword_index_factory /
            reranker_factory（仍是惰性工厂）

重排契约（**顺序是全部**）：

    ports:  Reranker.rerank(query, texts) -> Sequence[float]   # 同序等长,越大越相关
    domain: Citation.rerank_score: float | None = None          # None = 没重排过

    管道顺序：

        召回(k×2) → 融合 → 折叠父块 → 重排 → 截断到 candidate_m

    顺序一一对应（左 = 常见做法，右 = 本项目与它的差别）：

        先按父块去重                  ←→  折叠父块（先去重，别重排重复的子块）
        pairs = [[query, 正文]]        ←→  重排**父块正文**（context_text）
        len(parent_docs) < 2: 跳过重排  ←→  只有 1 条候选时不调模型（排序不可能变）
        ranked_parent_docs[:M]        ←→  截断

    **为什么重排父块而不是子块**:喂进去的必须是模型真正会读的那段文字,
    引用清单上的分数才核对得上（RAGFlow / 得物技术 / 本节都这么做）。
    另一派（RAG-BUILD-SKILLS、LlamaIndex sentence-window）主张重排子块再映射父块,
    理由是「多数 Cross-Encoder 只有 512 token」——**那条在我们这里不成立**:
    bge-reranker-v2-m3 的 max_seq_length 实测 8192,而我们父块平均 498 / 最长 675 token。
    两边都没被证伪,所以**重排单位是个待评估的选择,不是定论**:换的代价是把
    ``collapse_parents`` 与 ``rerank_citations`` 换个位置（约 3 行）,
    触发条件与改法看这个旋钮自己的说明;评估集是唯一裁判。

    **排序分与重排分是两个字段,不合并**:``Citation.score`` 仍是召回/融合分。
    合并成一个字段就再也看不出「重排把融合的第 3 提到了第 1」——而那正是调重排时唯一要看的东西。
    两个分数**不可比**（RRF ≈0.016 / 重排是 sigmoid 后的 0~1）。

    **延迟指标（起纳入验收）**:模型加载完成后的单条重排 **p99 < 1s**。
    口径是**只算重排推理**,冷启动加载时间单独量、单独打印,**不参与判定**
    （含加载的话任何硬件都过不了:CPU 加载 1.6s、GPU 0.5s 就已经超了）。

    实测（候选取硬上界 2×retrieval_k 个最长父块 = 最坏输入）：

        CPU fp32   9 条 ≈ 6000 ms   ← 不达标,所以默认配置走 GPU
        GPU fp32   9 条 ≈  288 ms
        GPU fp16   9 条 ≈   75 ms   ← 达标，余量 13×
        GPU fp16   首次调用 ≈ 287 ms（含 CUDA kernel 首次编译,仍达标）

    CPU 上没有可达路径:24 核已吃满（16 线程反而更快）、无 AVX-512/AMX 所以 bf16 用不了、
    单条 0.2s 固定开销 + 2.2ms/token。要降到 1s 得同时上 ONNX int8 与更小的重排单位,
    两条都要评估集验证不伤质量 —— 而本机有 RTX 5080,`embedding.device=cuda` 一行就绕过去了。
    CPU 保留为**兜底**:换台没有 GPU 的机器时把 device 改回 cpu,验收里那条延迟检查会
    明确失败（而不是悄悄放行）。

分层（静态扫描，不靠自觉）：``pymilvus`` / ``milvus_model`` /
``sentence_transformers`` 只准出现在 ``adapters/``；``application/`` 只准依赖标准库 +
``domain`` / ``ports`` / ``observability``。（适配器**不要求**显式继承端口 —— 符合性由 pyright 断。）
"""

from __future__ import annotations

import ast
import importlib
import math
import os
import sys
import typing
import threading
from pathlib import Path

from _harness import check, report, env, SkipCheck

# 源码根:分层扫描要按文件解析 import
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"
# 验收语料:课程大纲(来源与形状见 data/README.md)
CORPUS_DIR = ROOT / "data" / "corpus" / "ai_data"

# 契约默认值（必须与 config/config.yaml 一致）。
# 10086 是宿主端口；default 是当前唯一存在的 Milvus 库。
EXPECTED_MILVUS = {
    "host": "127.0.0.1",
    "port": 10086,
    "database": "default",
    # 图片模态新增 image_keys 列;schema 建集合那一刻定格,所以换新集合名重灌。
    # 老 rag_chunks 留着回滚,不再是当前契约指向的集合。
    "collection": "rag_chunks_img_v1",
}
# 只钉「模型在哪」这类跨机器不变的东西。
# device / use_f16 **故意不在这里**：那是**部署选择**（本机 cuda、别人的机器 cpu），不是契约。
# 钉死具体值会让「换台机器就跑不了验收」—— 本机从 cpu 换到 cuda 时正是如此。
# 契约只剩两条：① yaml 里写了它们；② 适配器真的会读它们。由专门一条检查盯着。
EXPECTED_EMBEDDING = {
    "model_dir": "models/bge-m3",
    "rerank_model_dir": "models/bge-reranker-v2-m3",
}
EXPECTED_RAG = {
    "parent_chunk_size": 1200,
    "child_chunk_size": 300,
    "chunk_overlap": 50,
    # 一度调成 retrieval_k=10 / candidate_m=3，评估集量过之后**回退到 5/2**
    # （四指标均值 0.9347 → 0.9313：recall 涨了，但 precision 与 faithfulness 掉了，
    # 代价是多喂一块父块）。A/B 证据在，数据集按旋钮命名、可重跑。
    "retrieval_k": 5,
    "candidate_m": 2,
    "fusion": "rrf",
    "rrf_k": 60,
    "bm25_weight": 0.5,
    "vector_weight": 0.5,
    "rerank": True,
}


class _RecordingLLM:
    """记录收到的提示词,不联网、不花钱。"""

    def __init__(self, reply: str = "在订单页点申请退款 [1]") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def stream(self, messages):
        from project_rag.domain import LlmDelta

        for message in messages:
            if message.role == "user":
                self.prompts.append(message.content)
        yield LlmDelta(text=self.reply)
        # 收尾帧:端口吐的是帧,消费方看 last=True 那帧的结束原因
        yield LlmDelta(finish_reason="stop", last=True)


class _RecordingReranker:
    """假重排器:按「正文里有没有某个关键词」给分，好让排序结果可以预期。

    真模型是黑盒(连续分数、随模型版本漂移),拿它断排序等于把检查绑死在权重上。
    这一层要断的是**管道**:有没有调、喂进去的是什么文本、排序和截断谁先谁后。

    **必须定义在所有 @check 之前**:``@check`` 装饰器在**定义时就立即执行**函数,
    所以整份脚本的检查是 import 期按文件顺序跑完的 —— 后文定义的帮助函数,
    前文的检查根本看不到。踩过一次:CLI 那条 rag 检查报
    ``NameError: name '_RecordingReranker' is not defined``。
    """

    def __init__(self, prefer: str = "父块乙", best: float = 1.0, other: float = 0.1):
        self.prefer = prefer
        self.best = best
        self.other = other
        self.calls = []  # [(query, (喂进去的正文, ...))]

    def rerank(self, query, texts):
        self.calls.append((query, tuple(texts)))
        return [self.best if self.prefer in text else self.other for text in texts]


def _parented_store(embedder):
    """造 3 个各带独立父块的子块。排序无关紧要 —— 检查只关心「谁被提上来了」。"""
    from project_rag.adapters import InMemoryVectorStore
    from project_rag.domain import Chunk

    chunks = [
        Chunk("c1", "课程 集合框架 子块甲", "ai", "d1", "p1", "父块甲的正文"),
        Chunk("c2", "课程 版本号 子块乙", "ai", "d1", "p2", "父块乙的正文"),
        Chunk("c3", "课程 报错处理 子块丙", "ai", "d1", "p3", "父块丙的正文"),
    ]
    store = InMemoryVectorStore()
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))
    return store


def _gpu_invisible(device: str) -> str | None:
    """配置让用 GPU、但当前进程看不到 GPU 时，返回一句给人看的理由。

    本项目的验收脚本常跑在**沙箱**里，而沙箱挂的是私有 ``/dev``（实测只有 14 项），
    ``/dev/dxg`` 不在其中 —— 那是**沙箱**，不是环境坏了。写成 SkipCheck 的理由而不是 FAIL，
    是为了不让后来的人把沙箱现象当成 GPU 故障去修：这件事已经真实发生过一次，
    ``nvidia-smi`` 报 "GPU access blocked by the operating system" 被一路当成驱动问题排查。

    **凡是会真的构造嵌入/重排模型的检查，都要先问它一句** —— 否则在配了 cuda 的机器上
    从沙箱里跑，会得到一串看着像代码坏了的 ``RuntimeError: Found no NVIDIA driver``。
    """
    if not device.startswith("cuda"):
        return None
    import torch

    if torch.cuda.is_available():
        return None
    return (
        f"配置 device={device}，但当前进程看不到 GPU（沙箱的私有 /dev 里没有 /dev/dxg）。"
        "GPU 相关的检查要在**自己的终端**里跑默认配置；owner：本地环境"
    )


def _settings_cls():
    return importlib.import_module("project_rag.config").Settings

def _has(module_name: str, names: tuple[str, ...], owner: str):
    """导入模块并要求它有指定成员；缺了 → SKIP 并点名 owner（那一步做完这条自己变绿）。"""
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise SkipCheck(f"{module_name} 还没有 {missing}；owner：{owner}")
    return module

def _imported_roots_text(text: str) -> set[str]:
    """输入:一段源码。行为:解析出它 import 的顶层模块名。输出:模块名集合。

    为什么不用 ``"jieba" in text``:docstring 里提一句「jieba 分词」也会命中 ——
    检查要盯的是**依赖**,不是文字。
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_roots(path: Path) -> set[str]:
    """输入:一个 .py 文件。行为:解析出它 import 的顶层模块名。输出:顶层模块名集合。"""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# ── 1. 配置（的真实内容） ────────────────────────────────────────────────
@check("配置：milvus / embedding / rag 三段齐全、是嵌套模型、默认值与契约一致")
def _() -> str:
    from pydantic import BaseModel

    settings = _settings_cls()()
    for section, expected in (
        ("milvus", EXPECTED_MILVUS),
        ("embedding", EXPECTED_EMBEDDING),
        ("rag", EXPECTED_RAG),
    ):
        if not hasattr(settings, section):
            raise AssertionError(
                f"Settings 缺 {section} 段；加进 config.yaml 与 Settings"
            )
        obj = getattr(settings, section)
        if not isinstance(obj, BaseModel):
            raise AssertionError(
                f"Settings.{section} 应是嵌套模型对象（像 llm 那样），"
                f"实际 {type(obj).__name__}"
            )
        for field, want in expected.items():
            got = getattr(obj, field, None)
            if got != want:
                raise AssertionError(f"{section}.{field} 期望 {want!r}，实际 {got!r}")
    return (
        f"milvus={settings.milvus.host}:{settings.milvus.port}/{settings.milvus.database} "
        f"collection={settings.milvus.collection} retrieval_k={settings.rag.retrieval_k}"
    )

@check("config/config.yaml 里确实写了这三段（代码里有默认值 ≠ 配置文件里有）")
def _() -> str:
    import yaml

    from project_rag.config import CONFIG_FILE

    data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    missing = [section for section in ("milvus", "embedding", "rag") if section not in data]
    if missing:
        raise AssertionError(
            f"config.yaml 缺段落 {missing}；只在代码里写默认值，读配置的人看不到它"
        )
    return f"config.yaml 段落：{sorted(data)}"

@check("配置可被环境变量覆盖（RAG_MILVUS__PORT / RAG_RAG__RETRIEVAL_K / RAG_EMBEDDING__DEVICE）")
def _() -> str:
    Settings = _settings_cls()
    with env(
        RAG_MILVUS__PORT="19531",
        RAG_RAG__RETRIEVAL_K="8",
        RAG_EMBEDDING__DEVICE="cuda",
    ):
        settings = Settings()
    got = (settings.milvus.port, settings.rag.retrieval_k, settings.embedding.device)
    want = (19531, 8, "cuda")
    if got != want:
        raise AssertionError(
            f"期望 {want}，实际 {got}（检查新配置段是否真的挂在 Settings 上、"
            "字段名与 yaml 是否一致）"
        )
    return f"port={got[0]} retrieval_k={got[1]} device={got[2]}"

@check("模型路径：相对路径挂到项目根、绝对路径原样生效（两种写法都必须对）")
def _() -> str:
    from project_rag.config import PROJECT_ROOT

    settings = _settings_cls()()
    want = (PROJECT_ROOT / "models" / "bge-m3").resolve()
    if settings.embedding.model_path != want:
        raise AssertionError(
            f"embedding.model_path 期望 {want}，实际 {settings.embedding.model_path}"
        )
    if not settings.embedding.model_path.is_absolute():
        raise AssertionError("model_path 必须是绝对路径 —— 模型加载不该看当前工作目录")

    with env(RAG_EMBEDDING__MODEL_DIR="/opt/models/bge-m3"):
        absolute = _settings_cls()().embedding.model_path
    if str(absolute) != "/opt/models/bge-m3":
        raise AssertionError(f"绝对路径没被原样采用，实际 {absolute}")
    return (
        f"model_path={settings.embedding.model_path} "
        f"rerank={settings.embedding.rerank_model_path.name}"
    )

@check("嵌入设备：yaml 里看得见 device/use_f16，代码默认值仍是 cpu（部署选择 ≠ 契约）")
def _() -> str:
    """这条替代了原来「把 device=cpu 钉进契约默认值」的做法。

    钉死具体值等于把**部署选择**当成**契约**：本机从 cpu 换成 cuda 时，
    一批毫不相干的检查会一起红，而它们想表达的其实是同一件事。
    契约只该管两件：配置里看得见、适配器真的读。后者由适配器那条检查盯着。
    """
    import yaml

    from project_rag.config import CONFIG_FILE, EmbeddingConfig

    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    section = raw.get("embedding") or {}
    for key in ("device", "use_f16"):
        if key not in section:
            raise AssertionError(
                f"config.yaml 的 embedding 段缺 {key} —— 设备是**部署选择**，"
                "该在 yaml 里看得见，而不是只藏在代码默认值里让人猜"
            )
    device = str(section["device"])
    if device != "cpu" and not device.startswith("cuda"):
        raise AssertionError(f"device 只认 cpu / cuda*，实际 {device!r}")

    # 代码默认值的本分是「换台机器也能跑」，所以必须是 cpu；cuda 归 yaml / 环境变量
    default = EmbeddingConfig.model_fields["device"].default
    if default != "cpu":
        raise AssertionError(
            f"EmbeddingConfig.device 的代码默认值应当是 cpu，实际 {default!r} —— "
            "默认值要能在没有 GPU 的机器上跑，cuda 是本机的部署选择"
        )
    if EmbeddingConfig.model_fields["use_f16"].default is not False:
        raise AssertionError("EmbeddingConfig.use_f16 的代码默认值应当是 False")
    return (
        f"yaml: device={device} use_f16={section['use_f16']}"
        "（代码默认仍 cpu/false —— 换机器不用改代码）"
    )


@check("演示后端必须显式开启：默认 bge_m3；设 hashing 时工厂给 HashingEmbedder 并打 WARNING")
def _() -> str:
    """`HashingEmbedder` 是「零模型也能跑通 ingest→search→界面」的演示实现，**不是语义模型**。

    它的坏法是本项目最怕的那一类：**不报错、不缺数据**，只是检索从"语义相近"退化成
    "字面相似" —— 使用者会以为一切正常。所以规矩是两条：默认必须是真模型；要演示实现
    必须**显式写出来**并且**必定告警**。这条把两条都钉住（告警用 logging 捕获断言，
    不靠"我记得它会打"）：

    1. `EmbeddingConfig.backend` 的代码默认值是 ``bge_m3``；
    2. `config/config.yaml` 的 embedding 段里看得见这个开关，且值是 ``bge_m3``；
    3. 把它设成 ``hashing`` 时，`_embedder_factory` 真的给 `HashingEmbedder`（256 维、
       零模型），并且日志里有一条 WARNING 说明"这是演示实现、非语义"。
    """
    import logging

    import yaml

    from project_rag.adapters.hashing_embedder import HashingEmbedder
    from project_rag.cli import deps as deps_module
    from project_rag.config import CONFIG_FILE, EmbeddingConfig

    default = EmbeddingConfig.model_fields["backend"].default
    if default != "bge_m3":
        raise AssertionError(
            f"EmbeddingConfig.backend 的代码默认值是 {default!r}，必须是 bge_m3 —— "
            "默认一旦变成 hashing，没有模型的机器会静默地用字面匹配冒充语义检索"
        )
    section = (yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}).get("embedding") or {}
    if section.get("backend") != "bge_m3":
        raise AssertionError(
            f"config.yaml 的 embedding.backend 应是 bge_m3，实际 {section.get('backend')!r} —— "
            "这个开关要在配置里看得见，而不是只藏在代码里"
        )

    Settings = _settings_cls()
    with env(RAG_EMBEDDING__BACKEND="hashing"):
        settings = Settings()

    collected: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    logger = logging.getLogger("project_rag.cli.deps")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        embedder = deps_module._embedder_factory(settings)  # pyright: ignore[reportPrivateUsage]
    finally:
        logger.removeHandler(handler)

    if not isinstance(embedder, HashingEmbedder):
        raise AssertionError(
            f"backend=hashing 时应给 HashingEmbedder，实际 {type(embedder).__name__} —— "
            "配置写了不生效，等于那行注释在骗人"
        )
    if not any("hashing" in record.getMessage() for record in collected):
        raise AssertionError(
            "选了 hashing 却没有 WARNING —— 使用者会以为检索正常，实际只按字面匹配。"
            f"捕获到的告警：{[r.getMessage() for r in collected]}"
        )
    return f"默认 bge_m3；hashing → {type(embedder).__name__}(dim={embedder.dim}) 且有 WARNING"


@check("配置：分块尺寸不自洽时 fail-fast（overlap ≥ child、child > parent 都拒）")
def _() -> str:
    from pydantic import ValidationError

    from project_rag.config import RagConfig

    bad_configs = (
        {"chunk_overlap": 300},  # overlap == child → 切分器原地打转
        {"chunk_overlap": 400},  # overlap > child
        {"parent_chunk_size": 200},  # child > parent
    )
    for kwargs in bad_configs:
        try:
            # model_validate 而不是 RagConfig(**kwargs):kwargs 的值类型混杂,
            # ** 展开时 pyright 会拿 dict 的值类型去逐个匹配字段类型
            # （int 匹配不上 fusion 的 Literal 和 rerank 的 bool），
            # 而这里要的本来就是「把一份坏配置交给校验器」
            RagConfig.model_validate(kwargs)
        except ValidationError:
            continue
        raise AssertionError(
            f"{kwargs} 应该被拒 —— 否则要等 ingest 跑起来才发现，代价是一整轮嵌入调用"
        )
    return f"{len(bad_configs)} 组坏配置都被拒"

# ── 2. 环境（容器依赖，没起就 SKIP） ──────────────────────────────────────────
@check("Milvus：用配置里的 host/port 连得上，且 database 必须是已存在的库")
def _() -> str:
    from pymilvus import MilvusClient

    settings = _settings_cls()()
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"

    try:
        client = MilvusClient(uri=target)  # 先不指定库，只测可达
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题，不是契约失败
        raise SkipCheck(
            f"连不上 Milvus（{target}）：{type(exc).__name__}: {str(exc)[:100]}；"
            "先在 Windows 侧 docker compose up -d，或跑 check_env_services.py 定位"
        ) from exc

    version = client.get_server_version()
    databases = list(client.list_databases())
    if settings.milvus.database not in databases:
        raise AssertionError(
            f"配置的库 {settings.milvus.database!r} 不存在，现有 {databases}。"
            "Milvus 不会自动建库：要么改成 default，要么在 ingest 里先 create_database"
        )

    scoped = MilvusClient(uri=target, db_name=settings.milvus.database)
    exists = scoped.has_collection(settings.milvus.collection)
    state = "已存在" if exists else "尚未创建（先跑一次 ingest）"
    return f"服务端 {version}，库 {settings.milvus.database} 可用，集合 {settings.milvus.collection} {state}"

# ── 3. 领域模型 / 端口 / 组合根 / 嵌入 ───────────────────────────
@check("导出完整：domain/models.py 与 ports/ 里定义的每个公开类型都能从包入口导入")
def _() -> str:
    """一条从三次真实事故里长出来的检查。

    漏一个导出会让 import 它的模块**整个起不来** —— 表现成十几条毫不相干的检查同时红
    (切分、ingest、融合一起挂),排查成本远高于它本身的体量。
    ``ChunkGroup`` / ``FusedChunk`` / ``RetrievalOptions`` 各踩过一次,所以钉死。
    """
    import ast

    def _public_classes(path: Path) -> list[str]:
        source = path.read_text(encoding="utf-8")
        return [
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
        ]

    missing: list[str] = []
    domain_module = importlib.import_module("project_rag.domain")
    for name in _public_classes(SRC / "domain" / "models.py"):
        if not hasattr(domain_module, name):
            missing.append(f"project_rag.domain 缺 {name}")

    ports_module = importlib.import_module("project_rag.ports")
    for path in sorted(SRC.joinpath("ports").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        for name in _public_classes(path):
            if not hasattr(ports_module, name):
                missing.append(f"project_rag.ports 缺 {name}")

    if missing:
        raise AssertionError(
            f"这些类型定义了却没从包入口导出：{missing}。"
            "domain/__init__.py 与 ports/__init__.py 的 **import 列表和 __all__ 各要加一行**"
            "（只加一处不够：少 import 报 ImportError，少 __all__ 会让 `from ... import *` 漏掉它）"
        )
    return "domain 与 ports 的公开类型全部可从包入口导入"


@check("领域模型：Chunk / Embedding / RetrievedChunk 是 frozen+slots dataclass，字段齐全")
def _() -> str:
    import dataclasses

    module = _has("project_rag.domain", ("Chunk", "Embedding", "RetrievedChunk"), "领域模型与端口")
    expected = {
        "Chunk": {"chunk_id", "text", "source", "doc_id", "parent_id", "parent_content"},
        # 只有 dense：稀疏向量已弃用（词面那一路交给 BM25），见本脚本 docstring
        "Embedding": {"dense"},
        "RetrievedChunk": {"chunk", "score"},
    }
    for name, want in expected.items():
        cls = getattr(module, name)
        if not dataclasses.is_dataclass(cls):
            raise AssertionError(f"{name} 不是 dataclass（domain 只用 dataclass,不引校验框架）")
        # getattr 而不是 cls.__dataclass_params__:类型存根没声明这个属性,
        # 而它确实是 dataclass 的运行时属性
        params = getattr(cls, "__dataclass_params__", None)
        if params is None or not (params.frozen and getattr(cls, "__slots__", None)):
            raise AssertionError(
                f"{name} 应为 frozen=True + slots=True（与 ChatMessage / FaqEntry 一致）"
            )
        missing = want - set(cls.__dataclass_fields__)
        if missing:
            raise AssertionError(f"{name} 缺字段 {sorted(missing)}")

    # parent_* 必须可省:父子分层之前入库的扁平块没有父块,读回来也得能跑
    module.Chunk("c1", "文本", "python", "doc_a")

    # 「不用稀疏向量」是**决策**,不是遗漏:词面那一路交给 BM25,两路各说各的。
    # 放松这条等于把决策悄悄退回 —— 会多出一个稀疏索引、多一路融合、多一维评估。
    if "sparse" in module.Embedding.__dataclass_fields__:
        raise AssertionError(
            "Embedding 不该再有 sparse 字段:本项目的词面检索走 BM25,不用嵌入模型的稀疏向量"
            "（理由见本脚本 docstring）"
        )
    return "Chunk / Embedding(只有 dense) / RetrievedChunk 形状一致,parent_* 可省"


@check("端口：Embedder / VectorStore / KeywordIndex 声明齐全")
def _() -> str:
    module = _has(
        "project_rag.ports",
        ("Embedder", "VectorStore", "KeywordIndex", "Reranker"),
        "/ 重排",
    )
    if not hasattr(module.KeywordIndex, "search"):
        raise AssertionError("KeywordIndex 缺 search；端口契约见本脚本 docstring")
    if not hasattr(module.Reranker, "rerank"):
        raise AssertionError(
            "Reranker 缺 rerank —— 形状是 rerank(query, texts) -> Sequence[float]，"
            "一次收一批、回一批（和 Embedder.embed 对称），见本脚本 docstring"
        )
    for name in ("embed", "dim"):
        if not hasattr(module.Embedder, name):
            raise AssertionError(f"Embedder 缺 {name}；端口契约见本脚本 docstring")
    for name in (
        "upsert",
        "search",
        "fingerprint",
        "load_all",
        "delete_by_doc_id",
        "delete_by_source",
        "drop",
    ):
        if not hasattr(module.VectorStore, name):
            raise AssertionError(f"VectorStore 缺 {name}；端口契约见本脚本 docstring")
    return (
        "Embedder.embed+dim / VectorStore.upsert+search+delete_by_doc_id"
        "+delete_by_source+drop 都已声明"
    )


@check("组合根：Deps 有 embedder_factory / vector_store_factory，默认 None（惰性）")
def _() -> str:
    import dataclasses

    deps_cls = _has("project_rag.cli.deps", ("Deps",), "领域模型与端口").Deps
    fields = {field.name: field for field in dataclasses.fields(deps_cls)}
    missing = sorted(
        {"embedder_factory", "vector_store_factory", "reranker_factory"} - set(fields)
    )
    if missing:
        raise SkipCheck(f"Deps 还没有 {missing}；owner：领域模型与端口 / 重排")
    for name in ("embedder_factory", "vector_store_factory", "reranker_factory"):
        if fields[name].default is not None:
            raise AssertionError(
                f"Deps.{name} 默认应为 None（不注入就不造），实际 {fields[name].default!r}"
            )
    return f"Deps 共 {len(fields)} 个字段,两个知识库工厂默认 None"


@check("端口语义：内存实现跑通 upsert → search → delete_by_source（幂等 / 过滤 / 空库）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.domain import Chunk, Embedding

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk("c1", "python 装饰器怎么写", "python", "d1"),
        Chunk("c2", "python 虚拟环境怎么建", "python", "d2"),
        Chunk("c3", "java 程序包不存在怎么解决", "java", "d3"),
    ]

    vectors = embedder.embed([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise AssertionError(
            f"embed 必须原样返回同序等长的向量:{len(chunks)} 条文本 → {len(vectors)} 条向量"
        )
    if embedder.dim != len(vectors[0].dense):
        raise AssertionError("Embedder.dim 与实际向量维度不一致（Milvus 建集合要靠它）")

    if store.upsert(chunks, vectors) != 3:
        raise AssertionError("upsert 应返回集合内的子块总数")
    if store.upsert(chunks, vectors) != 3:
        raise AssertionError("同样的 chunk_id 再写一次总数不该涨 —— 幂等靠 chunk_id 覆盖")

    query = "python 装饰器怎么写"
    query_vector = embedder.embed([query])[0]
    hits = store.search(query_vector, k=2)
    if len(hits) != 2:
        raise AssertionError(f"k=2 应返回 2 条,实际 {len(hits)}")
    if hits[0].chunk.chunk_id != "c1":
        raise AssertionError(f"最像的应是 c1,实际 {hits[0].chunk.chunk_id}")
    if hits[0].score <= hits[1].score:
        raise AssertionError("命中必须按分数降序")

    only_java = store.search(query_vector, k=10, source="java")
    if [hit.chunk.chunk_id for hit in only_java] != ["c3"]:
        raise AssertionError(
            f"source 过滤应只剩 java 那条,实际 {[hit.chunk.chunk_id for hit in only_java]}"
        )

    if list(InMemoryVectorStore().search(query_vector, k=5)) != []:
        raise AssertionError("空库必须给空序列而不是抛错 —— 用例层要能走「没检索到」分支")

    try:
        store.search(Embedding(dense=(1.0, 2.0)), k=1)
    except ValueError:
        pass
    else:
        raise AssertionError("查询向量维度不对时必须报错,不能静默算出一个错分数")

    if store.delete_by_doc_id("d2") != 1:
        raise AssertionError("delete_by_doc_id('d2') 应删掉 1 条 —— ingest 靠它清旧块")
    rest = [hit.chunk.chunk_id for hit in store.search(query_vector, k=10)]
    if rest != ["c1", "c3"]:
        raise AssertionError(f"按 doc_id 删之后应是 c1/c3，实际 {rest}")

    if store.delete_by_source("java") != 1:
        raise AssertionError("delete_by_source('java') 应删掉 1 条 —— 整份语料下架要靠它")
    remaining = [hit.chunk.chunk_id for hit in store.search(query_vector, k=10)]
    if remaining != ["c1"]:
        raise AssertionError(f"两个删除都做过之后应只剩 c1，实际 {remaining}")
    if store.delete_by_source("不存在的来源") != 0:
        raise AssertionError("删不存在的来源应返回 0，而不是报错")

    store.drop()
    if list(store.search(query_vector, k=5)) != []:
        raise AssertionError("drop 之后应是空库")
    return "upsert 幂等 / 按来源删 / k 截断 / source 过滤 / 空库 / drop / 维度校验 都符合语义"


@check("嵌入适配器：BGE-M3 出 1024 维稠密且已 L2 归一化（IP 就等于余弦），语义序正确")
def _() -> str:
    from project_rag.config import get_settings

    settings = get_settings()
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    model_path = settings.embedding.model_path
    if not (model_path / "config.json").exists():
        raise SkipCheck(
            f"本地没有模型（{model_path}）；取模型："
            ".venv/bin/python scripts/tools/fetch_models.py（owner：模型装载）"
        )

    try:
        from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder
    except ImportError as exc:
        raise AssertionError(
            f"adapters/embedder_bge_m3.py 还没落地（{exc}）；owner：嵌入适配器"
        ) from exc

    texts = ["退款流程怎么走？", "怎么申请退款", "怎么养猫"]
    embedder = BGEM3Embedder(settings.embedding)
    if embedder.dim != 1024:
        raise AssertionError(f"BGE-M3 稠密维度应为 1024,实际 {embedder.dim}")

    vectors = embedder.embed(texts)
    if len(vectors) != len(texts):
        raise AssertionError(f"embed 必须同序等长:{len(texts)} 条文本 → {len(vectors)} 条向量")
    for text, vector in zip(texts, vectors):
        if len(vector.dense) != embedder.dim:
            raise AssertionError(
                f"「{text}」稠密维度 {len(vector.dense)} != Embedder.dim {embedder.dim}"
            )
        norm = math.sqrt(sum(value * value for value in vector.dense))
        if abs(norm - 1.0) > 1e-3:
            raise AssertionError(
                f"「{text}」的向量没有 L2 归一化(norm={norm:.4f})—— "
                "Milvus 用 IP 度量就等于余弦,这个前提不能破"
            )

    def cosine(left: int, right: int) -> float:
        a, b = vectors[left].dense, vectors[right].dense
        dot = sum(x * y for x, y in zip(a, b))
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return dot / norm

    self_score, paraphrase, unrelated = cosine(0, 0), cosine(0, 1), cosine(0, 2)
    if abs(self_score - 1.0) > 1e-4:
        raise AssertionError(f"自己跟自己应当 cos≈1,实际 {self_score:.6f}")
    if not paraphrase > 0.8 > unrelated:
        raise AssertionError(
            f"语义序不对:改写 {paraphrase:.4f}、无关 {unrelated:.4f}；"
            "期望 改写 > 0.8 > 无关 —— 要么模型没加载对,要么稠密向量没归一化"
        )
    return (
        f"dim={embedder.dim} 已归一化 / "
        f"cos(改写)={paraphrase:.3f} > 0.8 > cos(无关)={unrelated:.3f}"
    )


M3_SDK_ROOTS = ("pymilvus", "milvus_model", "sentence_transformers")
# 至少要有真实使用点的 SDK。名单是手写的,漏了就等于那条纪律对它失效 ——
# 注:milvus_model 已弃用 —— 它总是连稀疏一起算,而本项目不用稀疏向量。
M3_SDK_REQUIRED = ("pymilvus", "sentence_transformers")


@check("分层扫描：pymilvus / milvus_model / sentence_transformers 只准出现在 adapters/")
def _() -> str:
    hits: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for root in _imported_roots(path) & set(M3_SDK_ROOTS):
            hits.setdefault(root, []).append(str(path.relative_to(SRC)))

    leaked = {
        root: where
        for root, where in hits.items()
        if any(not w.startswith("adapters") for w in where)
    }
    if leaked:
        raise AssertionError(
            f"SDK 泄漏到适配器之外：{leaked}。"
            "这是「换向量库 / 换模型不用改用例层」的唯一保证（同第一轮的 langchain 规则）"
        )
    absent = [root for root in M3_SDK_REQUIRED if root not in hits]
    if absent:
        raise AssertionError(
            f"{absent} 还没被任何适配器用到 —— 嵌入 / 向量库适配器尚未落地。"
            "期望 adapters/embedder_bge_m3.py 用 sentence_transformers"
        )
    return ", ".join(f"{root} ← {sorted(set(hits[root]))}" for root in sorted(hits))


# ── 6. 更后面的步（做完哪一步，哪一条自己变绿） ───────────────────────────────


# ── 5. 加载 / 切分 / ingest ───────────────────────────────────────────
@check("加载：真实语料读成 Document（source 去掉 _data、按路径排序、BOM 已吃）")
def _() -> str:
    from project_rag.adapters.document_loader import load_documents

    if not CORPUS_DIR.exists():
        raise SkipCheck(
            f"语料目录不存在（{CORPUS_DIR}）；来源见 data/README.md（owner：数据装载）"
        )
    documents = load_documents(CORPUS_DIR).documents
    if not documents:
        raise AssertionError(f"{CORPUS_DIR} 下没读到 .md/.txt —— 后缀过滤或 rglob 有问题")

    ids = [document.doc_id for document in documents]
    if ids != sorted(ids):
        raise AssertionError(f"文档顺序必须确定（按路径排序），实际 {ids}")
    for document in documents:
        if not document.text.strip():
            raise AssertionError(f"{document.doc_id} 读出来是空的（路径或编码有问题）")
        if document.source != "ai":
            raise AssertionError(
                f"source 应为 'ai'（ai_data 去掉 _data 后缀），实际 {document.source!r}"
            )
        if document.text.startswith("\ufeff"):
            raise AssertionError(f"{document.doc_id} 的 BOM 没被吃掉 —— 该用 utf-8-sig")
    return f"{len(documents)} 份文档：{ids}"


@check("切分边界：空串 / 纯空白 / 超短 / 无分隔符 800 字长串 都不炸且子块有上界")
def _() -> str:
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker

    # 父块开到 1200,好让 800 字长串只落进一个父块 —— 这条断的是子块切分本身
    chunker = ChineseRecursiveChunker(1200, 300, 50)
    if chunker.split("") or chunker.split("   \n\n  "):
        raise AssertionError("空串 / 纯空白应当切出 0 个父块 —— 适配器要丢掉空白块")
    short = chunker.split("你好")
    if len(short) != 1 or list(short[0].children) != ["你好"]:
        raise AssertionError(f"短文本应是「1 个父块装 1 个子块」，实际 {short}")
    groups = chunker.split("啊" * 800)
    lengths = [len(child) for group in groups for child in group.children]
    if lengths != [300, 300, 300]:
        raise AssertionError(
            f"无分隔符 800 字应逐字降级成 3×300，实际 {lengths}；"
            "这条同时证明 overlap 生效（800 + 2×50 = 900）"
        )
    return "空/空白 → 0 父块；短文本 → 1 父 1 子；800 字无分隔符 → 3×300"


@check("真实语料切分：块长分布正常（无空块、无超长块、chunk_id 不重复）")
def _() -> str:
    import statistics

    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import load_documents
    from project_rag.application import chunk_documents
    from project_rag.config import get_settings

    if not CORPUS_DIR.exists():
        raise SkipCheck(f"语料目录不存在（{CORPUS_DIR}）；owner：数据装载")

    settings = get_settings()
    child_size = settings.rag.child_chunk_size
    parent_size = settings.rag.parent_chunk_size
    chunks = chunk_documents(
        load_documents(CORPUS_DIR).documents,
        ChineseRecursiveChunker(
            parent_size, child_size, settings.rag.chunk_overlap
        ),
    )
    if not chunks:
        raise AssertionError("真实语料切不出任何块")

    lengths = [len(chunk.text) for chunk in chunks]
    if min(lengths) <= 0:
        raise AssertionError("出现空块 —— 空白块没被丢掉")
    if max(lengths) > child_size:
        raise AssertionError(
            f"出现超长子块 {max(lengths)} > child_size {child_size}（逐字降级没生效）"
        )
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise AssertionError("真实语料里出现了重复 chunk_id")

    # 父子分层真的发生了:子块是父块的子串,父块比子块少
    parents = {chunk.parent_id for chunk in chunks}
    if "" in parents:
        raise AssertionError("有子块没带 parent_id —— 分层没接上")
    if not 1 <= len(parents) < len(chunks):
        raise AssertionError(f"父块数 {len(parents)} 应大于 0 且小于子块数 {len(chunks)}")
    over = [chunk.chunk_id for chunk in chunks if len(chunk.parent_content) > parent_size]
    if over:
        raise AssertionError(f"{len(over)} 个子块的父块超过 parent_size {parent_size}")
    missing = [
        chunk.chunk_id for chunk in chunks if chunk.text.strip() not in chunk.parent_content
    ]
    if missing:
        raise AssertionError(f"{len(missing)} 个子块的文本不在父块里 —— 父子关系串了")
    return (
        f"{len(parents)} 父块 → {len(chunks)} 子块  min={min(lengths)} "
        f"中位={int(statistics.median(lengths))} max={max(lengths)}（上限 {child_size}）"
    )


@check("chunk_id：同一份输入两次切分 id 一致；改了内容 id 就变（幂等的根基）")
def _() -> str:
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.application import chunk_documents
    from project_rag.domain import Document

    chunker = ChineseRecursiveChunker(200, 50, 10)
    text = "第一段内容足够长,用来切出至少一块。\n\n第二段内容也足够长,用来切出另一块。"
    document = Document(doc_id="a.md", text=text, source="ai")

    first = chunk_documents([document], chunker)
    again = chunk_documents([document], chunker)
    if [chunk.chunk_id for chunk in first] != [chunk.chunk_id for chunk in again]:
        raise AssertionError("同一份输入两次切分的 chunk_id 不一致 —— upsert 幂等就没了")
    if len({chunk.chunk_id for chunk in first}) != len(first):
        raise AssertionError("同一批里出现了重复 chunk_id")

    changed = chunk_documents(
        [Document(doc_id="a.md", text=text + "补充一句话。", source="ai")], chunker
    )
    if {chunk.chunk_id for chunk in changed} == {chunk.chunk_id for chunk in first}:
        raise AssertionError("内容改了但 chunk_id 没变 —— 那样的「更新」根本不会落库")
    return f"{len(first)} 块的 id 稳定且唯一；改内容后 id 变化"


@check("主键摘要：chunk_id / parent_id 是 64 位十六进制（sha256），且装得进 schema 的 max_length")
def _() -> str:
    """钉两件事，都是为了让「id 口径悄悄变了」无法悄悄发生。

    ① **算法**：md5 已有实用的碰撞构造，而 ``chunk_id`` 是本项目**唯一**的幂等依据 ——
       一旦碰撞，两个不同的块会被当成同一个，``upsert`` **静默覆盖**掉一个，
       表现成「语料里少了一块」而没有任何报错。这种静默的数据丢失比多花几微秒糟得多。
    ② **长度**：摘要的十六进制串必须 ≤ Milvus schema 里那一列的 ``max_length``。
       换算法时最容易忘的就是这里，而超长**不是报错**、是写入失败或截断。
    """
    import re

    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.application import chunk_documents
    from project_rag.domain import Document

    chunks = chunk_documents(
        [Document(doc_id="a.md", text="第一段内容足够长,用来切出好几块。" * 8, source="ai")],
        ChineseRecursiveChunker(200, 50, 10),
    )
    if not chunks:
        raise AssertionError("一个块都没切出来，这条检查没法进行")

    hexchars = set("0123456789abcdef")
    for chunk in chunks:
        for name, value in (("chunk_id", chunk.chunk_id), ("parent_id", chunk.parent_id)):
            if len(value) != 64 or set(value) - hexchars:
                raise AssertionError(
                    f"{name} = {value!r} 不是 64 位小写十六进制 —— "
                    f"32 位说明还在用 md5；别的长度说明换了算法却没同步 schema 的 max_length"
                )

    source = (SRC / "adapters" / "milvus_store.py").read_text(encoding="utf-8")
    limits: dict[str, int] = {}
    for field in ("chunk_id", "parent_id"):
        found = re.search(rf'add_field\(\s*"{field}"[^)]*?max_length=(\d+)', source)
        if not found:
            raise AssertionError(f"milvus_store.py 里找不到 {field} 的 max_length 声明")
        limits[field] = int(found.group(1))
    too_small = {name: limit for name, limit in limits.items() if limit < 64}
    if too_small:
        raise AssertionError(
            f"schema 装不下 64 字符的 sha256 十六进制：{too_small}。"
            "改这个要 drop 集合 + 重灌，别等写入时才以「超长」暴露"
        )

    ingest_src = (SRC / "application" / "ingest_documents.py").read_text(encoding="utf-8")
    if "hashlib.md5" in ingest_src:
        raise AssertionError("ingest_documents.py 里还有 hashlib.md5 —— 主键摘要没换干净")
    if "_DIGEST" not in ingest_src:
        raise AssertionError(
            "ingest_documents.py 里没有 _DIGEST —— 摘要算法应当**只在一处定义**，"
            "散落的 hashlib.sha256(...) 早晚会分叉"
        )
    return f"chunk_id/parent_id 均为 64 位十六进制；schema max_length={limits}"


@check("父子分层：同一父块的子块共享 parent_id，不同父块不共享（关系不能串）")
def _() -> str:
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.application import chunk_documents
    from project_rag.domain import Document

    text = "第一段。" * 40 + "\n\n" + "第二段。" * 40
    documents = [Document(doc_id="a.md", text=text, source="ai")]
    chunker = ChineseRecursiveChunker(200, 100, 20)
    chunks = chunk_documents(documents, chunker)
    if len(chunks) < 2:
        raise AssertionError(f"这份文本应当切出多块，实际 {len(chunks)}")

    by_parent: dict[str, set[str]] = {}
    for chunk in chunks:
        by_parent.setdefault(chunk.parent_id, set()).add(chunk.parent_content)
    bad = {pid: texts for pid, texts in by_parent.items() if len(texts) != 1}
    if bad:
        raise AssertionError(f"同一个 parent_id 对应了多份父块正文:{list(bad)[:2]}")

    # 同一份输入两次切分,父块 id 与子块 id 都要一致(upsert 幂等靠它)
    again = chunk_documents(documents, chunker)
    if [c.chunk_id for c in chunks] != [c.chunk_id for c in again]:
        raise AssertionError("两次切分的子块 id 不一致 —— upsert 幂等没了")
    if [c.parent_id for c in chunks] != [c.parent_id for c in again]:
        raise AssertionError("两次切分的父块 id 不一致")
    return f"{len(by_parent)} 个父块 / {len(chunks)} 个子块;id 两次一致"


@check("还原父块：命中同一父块的多个子块 → 折叠成一条引用（不重复喂上下文）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import retrieve
    from project_rag.domain import Chunk, RetrievalOptions

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    parent_a = "退款与发票的完整说明。" * 5
    chunks = [
        Chunk("c1", "退款在订单页点申请退款", "ai", "d1", "pa", parent_a),
        Chunk("c2", "退款 1-3 个工作日到账", "ai", "d1", "pa", parent_a),
        Chunk("c3", "发票在订单页申请开票", "ai", "d1", "pa", parent_a),
        Chunk("c4", "java 程序包不存在怎么解决", "java", "d2", "pb", "另一份父块。"),
    ]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))

    citations = retrieve(
        "退款在订单页点申请退款",
        embedder=embedder,
        store=store,
        options=RetrievalOptions(k=3),
    )
    if len(citations) != 1:
        raise AssertionError(
            f"前 3 条命中同属一个父块,应折叠成 1 条引用,实际 {len(citations)} 条"
        )
    if citations[0].chunk.parent_id != "pa":
        raise AssertionError(f"留下的应是 pa 那条,实际 {citations[0].chunk.parent_id}")
    if citations[0].index != 1:
        raise AssertionError("折叠后必须重新编号（第 1 条就是 [1]）")

    # 给了上限就截断,并且同样重新编号
    capped = retrieve(
        "退款", embedder=embedder, store=store, options=RetrievalOptions(k=4, max_context=1)
    )
    if len(capped) != 1 or capped[0].index != 1:
        raise AssertionError(f"max_context=1 应只留一条且编号为 1,实际 {capped}")

    # 没有父块的旧数据按 chunk_id 自成一组 —— 不能因为没父块就把它们全折叠掉
    legacy = [Chunk("x1", "旧数据一", "ai", "d3"), Chunk("x2", "旧数据二", "ai", "d3")]
    legacy_store = InMemoryVectorStore()
    legacy_store.upsert(legacy, embedder.embed([c.text for c in legacy]))
    legacy_hits = retrieve(
        "旧数据一", embedder=embedder, store=legacy_store, options=RetrievalOptions(k=2)
    )
    if len(legacy_hits) != 2:
        raise AssertionError(f"没有 parent_id 的块应各自成组,实际折叠成 {len(legacy_hits)} 条")
    return "同父 3 条 → 1 条引用;重新编号;无父块的旧数据不被误折叠"


@check("提示词：喂给模型的是父块正文（子块用来检索,父块用来生成）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import answer_rag
    from project_rag.domain import Chunk, RetrievalOptions

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    parent_text = "父块里有完整上下文:退款在订单页申请,1-3 个工作日到账。"
    store.upsert(
        [Chunk("c1", "退款在订单页申请", "ai", "d1", "pa", parent_text)],
        embedder.embed(["退款在订单页申请"]),
    )

    llm = _RecordingLLM(reply="在订单页申请退款 [1]")
    answer_rag(
        "退款怎么申请",
        embedder=embedder,
        store=store,
        llm=llm,
        options=RetrievalOptions(k=1),
    )
    prompt = llm.prompts[0]
    if parent_text not in prompt:
        raise AssertionError(f"提示词里没有父块正文 —— 上下文被截窄了;实际提示词={prompt[:200]!r}")
    if "context_text" in prompt:
        raise AssertionError("提示词里混进了代码符号,说明渲染用错了字段")
    return "提示词里进的是父块正文（不是只匹配到的那一小段子块）"


@check("ingest：内存实现端到端（文档 → 块 → 向量 → 入库），重跑不翻倍（幂等）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.application import chunk_documents, ingest_documents
    from project_rag.domain import Document

    documents = [
        Document(
            doc_id="a.md",
            text="退款流程怎么走？在订单页点申请退款。\n\n发票怎么开？在订单页申请开票。",
            source="ai",
        ),
        Document(doc_id="b.md", text="装饰器怎么写？用 @ 语法糖包一层函数。", source="java"),
    ]
    chunker = ChineseRecursiveChunker(200, 60, 10)
    embedder = HashingEmbedder()
    store = InMemoryVectorStore()

    first = ingest_documents(
        documents, chunker=chunker, embedder=embedder, store=store, batch_size=2
    )
    if first.documents != 2 or first.chunks < 2:
        raise AssertionError(f"统计不对：{first}")
    if first.in_store != first.chunks:
        raise AssertionError(f"库内条数应等于块数（切分不会产生重复块）：{first}")
    if first.batches != (first.chunks + 1) // 2:
        raise AssertionError(f"批次数应按 batch_size=2 切：{first}")

    second = ingest_documents(
        documents, chunker=chunker, embedder=embedder, store=store, batch_size=64
    )
    if second.in_store != first.in_store:
        raise AssertionError(
            f"同一份输入重跑，库内条数不该变：{first.in_store} → {second.in_store}"
        )

    # 改了内容再灌:旧块必须被清掉。不清的话旧块会留下,而且**分数往往更高**
    # (它和当初的问法更像),模型会拿过期答案回答用户 —— 实测 0.694 vs 0.267。
    edited = [
        Document(doc_id="a.md", text="退款流程改版了：现在在售后中心申请。", source="ai"),
        documents[1],
    ]
    stale = {chunk.chunk_id for chunk in chunk_documents([documents[0]], chunker)} - {
        chunk.chunk_id for chunk in chunk_documents([edited[0]], chunker)
    }
    third = ingest_documents(
        edited, chunker=chunker, embedder=embedder, store=store, batch_size=64
    )
    if third.removed == 0:
        raise AssertionError("重新 ingest 时 removed 应为正数 —— 没清旧块就等于没更新")
    left = {
        hit.chunk.chunk_id
        for hit in store.search(embedder.embed(["退款"])[0], k=100)
    }
    survived = left & stale
    if survived:
        raise AssertionError(f"改了内容重灌后旧块还在库里:{sorted(survived)}")

    hits = store.search(embedder.embed(["退款流程怎么走？"])[0], k=1)
    if not hits:
        raise AssertionError("入库之后应当能检索到东西")
    if hits[0].chunk.source != "ai":
        raise AssertionError(
            f"最像的应当是含原句那一块（source=ai），实际 {hits[0].chunk.source!r}"
        )
    return (
        f"文档 2 → {first.chunks} 块 / {first.batches} 批 / 重跑仍 {second.in_store} 块 / 可检索"
    )


@check("批大小只有 config 一个真源：ingest_documents.DEFAULT_BATCH_SIZE == RagConfig.ingest_batch_size")
def _() -> str:
    """用例层的默认批大小原先是个裸 64,``ingest --batch-size`` 里还有第二个 64。

    用例层不能 import config(分层法),所以那个字面量留着了 —— 但**它必须跟配置一致**,
    否则「不传 batch_size 直接调用例」与「走 CLI」会得到两种批大小。这条检查真的比对。
    """
    from project_rag.application.ingest_documents import DEFAULT_BATCH_SIZE
    from project_rag.config import RagConfig

    want = RagConfig().ingest_batch_size
    if DEFAULT_BATCH_SIZE != want:
        raise AssertionError(
            f"ingest_documents.DEFAULT_BATCH_SIZE={DEFAULT_BATCH_SIZE} 与 "
            f"RagConfig().ingest_batch_size={want} 漂了 —— 改一处得改两处"
        )
    return f"DEFAULT_BATCH_SIZE == RagConfig.ingest_batch_size == {want}"


@check("CLI：ingest --dry-run 跑通并打印块长统计（不加载模型、不写库）")
def _() -> str:
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "project_rag", "ingest", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}。"
            "ingest 子命令要能在没装 Milvus / 没加载模型的情况下跑 dry-run"
        )
    if "切分:" not in proc.stdout or "--dry-run" not in proc.stdout:
        raise AssertionError(f"dry-run 没打印切分统计，实际 stdout={proc.stdout[-300:]!r}")
    return proc.stdout.strip().splitlines()[1]


@check("Milvus 适配器：建集合 → upsert 幂等 → search → 按 doc/source 删 → drop（临时集合）")
def _() -> str:
    import uuid

    from pymilvus import MilvusClient

    from project_rag.adapters import HashingEmbedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import get_settings
    from project_rag.domain import Chunk

    settings = get_settings()
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"
    try:
        MilvusClient(uri=target).list_databases()  # 只探活:决定这条是 SKIP 还是真判
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题,不是契约失败
        raise SkipCheck(
            f"Milvus 连不上（{target}）：{type(exc).__name__}: {str(exc)[:100]}；"
            "先在 Windows 侧 docker compose up -d"
        ) from exc

    # 临时集合:验收不该碰生产集合,也不该留下垃圾(模式同 FAQ 通道的临时表)
    store = MilvusVectorStore(
        settings.milvus.model_copy(
            update={"collection": f"rag_acceptance_{uuid.uuid4().hex[:8]}"}
        )
    )
    # 用哈希嵌入而不是 BGE-M3:这几条只验契约(建/写/查/删/幂等),不验语义。
    # 每次都加载 2G 模型会把验收从 2 秒拖成 20 秒,而且验证的东西并没有变多。
    embedder = HashingEmbedder(dim=1024)
    try:
        chunks = [
            Chunk("c1", "退款流程怎么走", "ai", "doc_a"),
            Chunk("c2", "发票怎么开", "ai", "doc_a"),
            Chunk("c3", "java 程序包不存在", "java", "doc_b"),
        ]
        vectors = embedder.embed([chunk.text for chunk in chunks])

        if store.upsert(chunks, vectors) != 3:
            raise AssertionError(
                "upsert 的返回值必须是【库内总数】—— Milvus 原生返回的是本次写入数,"
                "适配器要回查一次 count(*)"
            )
        if store.upsert(chunks, vectors) != 3:
            raise AssertionError("同样的 chunk_id 再写一次总数不该涨 —— 幂等靠主键覆盖")

        # 指纹:关键字那一路靠它判断「要不要重建索引」,所以它必须真的随语料变
        fingerprint = list(store.fingerprint())
        if len(fingerprint) != 3 or not all("\x1f" in row for row in fingerprint):
            raise AssertionError(f"指纹应是 3 条「主键+父块号」,实际 {fingerprint}")

        hits = store.search(embedder.embed(["退款流程怎么走"])[0], k=2)
        if len(hits) != 2:
            raise AssertionError(f"k=2 应返回 2 条,实际 {len(hits)}")
        if hits[0].chunk.chunk_id != "c1":
            raise AssertionError(f"最像的应是 c1,实际 {hits[0].chunk.chunk_id}")
        if abs(hits[0].score - 1.0) > 1e-4:
            raise AssertionError(
                f"向量已 L2 归一化,自己跟自己应当是 IP=1.0,实际 {hits[0].score:.4f} —— "
                "先查 metric_type 是不是 IP"
            )

        only_java = store.search(
            embedder.embed(["java 程序包不存在"])[0], k=10, source="java"
        )
        if [hit.chunk.chunk_id for hit in only_java] != ["c3"]:
            raise AssertionError(
                f"source 过滤应只剩 c3,实际 {[hit.chunk.chunk_id for hit in only_java]}"
            )

        if store.delete_by_doc_id("doc_a") != 2:
            raise AssertionError("delete_by_doc_id('doc_a') 应删掉 2 条(doc_a 有两个块)")
        after_doc = [
            hit.chunk.chunk_id
            for hit in store.search(embedder.embed(["发票怎么开"])[0], k=10)
        ]
        if after_doc != ["c3"]:
            raise AssertionError(f"按 doc_id 删之后应只剩 c3,实际 {after_doc}")

        if store.delete_by_source("java") != 1:
            raise AssertionError("delete_by_source('java') 应删掉 1 条")
        left = [
            hit.chunk.chunk_id
            for hit in store.search(embedder.embed(["发票怎么开"])[0], k=10)
        ]
        if left != []:
            raise AssertionError(f"两个删除都做过之后应是空库,实际 {left}")

        store.drop()
        if store.search(embedder.embed(["退款流程怎么走"])[0], k=1):
            raise AssertionError("drop 之后集合不该存在,检索要给空序列")
        return "upsert 幂等(总数) / IP=1.0 / source 过滤 / 按来源删 / drop 全部符合端口语义"
    finally:
        store.drop()  # 失败也要清掉临时集合


@check("组合根：真实装配接的是 Milvus / BGE-M3，不是内存实现（不许静默退回假实现）")
def _() -> str:
    import inspect

    from project_rag.cli.deps import build_real_deps

    source = inspect.getsource(build_real_deps)
    missing = [
        name
        for name in ("MilvusVectorStore", "BGEM3Embedder", "BM25KeywordIndex", "BGEReranker")
        if name not in source
    ]
    if missing:
        raise AssertionError(
            f"build_real_deps 里没有装配 {missing} —— 真实装配必须接真实现；owner：向量库装配 / 重排"
        )
    fake = [name for name in ("InMemoryVectorStore", "HashingEmbedder") if name in source]
    if fake:
        raise AssertionError(
            f"真实装配里出现了内存实现 {fake}:那是「静默退回假实现」,"
            "等于把假结果当真答案返回。内存实现只给验收与本地联调"
        )
    return "build_real_deps 接 MilvusVectorStore + BGEM3Embedder,未接内存实现"


@check("rag 用例：检索为空时不调 LLM（没资料还让模型自由发挥就是幻觉源头）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import answer_rag

    llm = _RecordingLLM()
    result = answer_rag(
        "库里根本没有的问题",
        embedder=HashingEmbedder(),
        store=InMemoryVectorStore(),
        llm=llm,
    )
    if not result.refused:
        raise AssertionError("空库应当直接拒答（refused=True）")
    if result.citations:
        raise AssertionError("拒答时不该有引用")
    if llm.prompts:
        raise AssertionError(
            "检索为空时调用 LLM 了 —— 那等于让模型凭自己的知识编答案,正是幻觉的源头"
        )
    return f"空库 → 拒答、零 LLM 调用：{result.answer}"


@check("rag 用例：上下文带编号、引用与答案的 [n] 对齐（不是两套序号）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import answer_rag
    from project_rag.domain import Chunk, RetrievalOptions

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk("c1", "退款在订单页点申请退款", "ai", "d1"),
        Chunk("c2", "发票在订单页申请开票", "ai", "d1"),
    ]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))

    llm = _RecordingLLM(reply="退款在订单页办理 [1]")
    result = answer_rag(
        "退款在订单页点申请退款",
        embedder=embedder,
        store=store,
        llm=llm,
        options=RetrievalOptions(k=2),
    )
    prompt = llm.prompts[0]
    for needle in ("[1]", "[2]", "退款在订单页点申请退款"):
        if needle not in prompt:
            raise AssertionError(f"提示词里缺 {needle!r} —— 编号和问题都必须进提示词")
    if [citation.index for citation in result.citations] != [1, 2]:
        raise AssertionError(
            f"引用要从 1 连续编号，实际 {[c.index for c in result.citations]}"
        )
    if result.citations[0].chunk.chunk_id != "c1":
        raise AssertionError(f"第 1 条引用应是 c1，实际 {result.citations[0].chunk.chunk_id}")
    if result.answer != llm.reply:
        raise AssertionError("答案应原样返回模型输出（这层不做二次加工）")
    return "提示词含 [1][2] 与问题；引用从 1 连续编号且与检索同序"


@check("CLI：注入 Deps 的 rag 命令打印答案 + 引用清单（不联网、不花钱）")
def _() -> str:
    import io
    from contextlib import redirect_stdout

    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.cli import Deps, cli
    from project_rag.domain import Chunk

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    store.upsert(
        [
            Chunk("c1", "退款在订单页点申请退款", "ai", "d1"),
            # 必须 ≥2 条候选:只有 1 条时 retrieve 会跳过重排（排序不可能变）
            Chunk("c2", "发票在订单页申请开票", "ai", "d1"),
        ],
        embedder.embed(["退款在订单页点申请退款", "发票在订单页申请开票"]),
    )
    reranker = _RecordingReranker(prefer="退款", best=0.5313, other=0.14)
    deps = Deps(
        llm_factory=lambda: _RecordingLLM(reply="在订单页点申请退款 [1]"),
        embedder_factory=lambda: embedder,
        vector_store_factory=lambda: store,
        # rag 是两路融合:关键字那一路也要注入,否则命令会 fail-fast
        keyword_index_factory=lambda: BM25KeywordIndex(store),
        # rag.rerank 默认 True,组合根必须装得出重排器,否则命令会 fail-fast
        reranker_factory=lambda: reranker,
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli(["rag", "-q", "退款怎么办"], deps=deps)
    out = buffer.getvalue()
    if code != 0:
        raise AssertionError(f"退出码 {code}；stdout={out!r}")
    if "在订单页点申请退款 [1]" not in out:
        raise AssertionError(f"没打印答案，实际 stdout={out!r}")
    # 断「有没有引用清单」而不是「标题长什么样」:标题里带融合参数是正常的,
    # 把断言绑死在格式细节上,改一次输出就要改一次检查
    if "引用" not in out or "[1]" not in out or "[2]" not in out:
        raise AssertionError(f"没打印两条引用清单，实际 stdout={out!r}")
    if not reranker.calls:
        raise AssertionError(
            "rag 命令一次都没调重排器 —— Deps.reranker_factory 没接进用例，"
            "或者命令没读 settings.rag.rerank"
        )
    # 断「重排分真的打出来了」:0.5313 是假重排器给的分,融合分不可能长这样。
    # 只匹配前两位小数,这样 .2f/.3f/.4f 都能命中 —— 别把断言绑死在格式上
    if "0.53" not in out:
        raise AssertionError(
            f"引用清单里没有重排分 —— 排了序却没让人看见它。stdout={out!r}"
        )
    return "rag 命令输出答案 + 两条引用，且引用清单里带着重排分（全程零网络）"


@check("CLI：search 在真实语料上召回（集合空则 SKIP 并点名 ingest）")
def _() -> str:
    import subprocess

    from pymilvus import MilvusClient

    from project_rag.config import get_settings

    settings = get_settings()
    # 这条要起子进程跑 search,而 search 会加载嵌入模型 —— 沙箱里看不到 GPU 时,
    # 子进程会以 "Found no NVIDIA driver" 退出,看着像检索坏了。先问一句。
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"
    try:
        client = MilvusClient(uri=target)
        rows = client.query(
            collection_name=settings.milvus.collection,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        total = int(rows[0]["count(*)"]) if rows else 0
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题
        raise SkipCheck(f"Milvus 连不上（{target}）：{type(exc).__name__}") from exc
    if total == 0:
        raise SkipCheck(
            f"集合 {settings.milvus.collection} 是空的；先跑 "
            ".venv/bin/python -m project_rag ingest（owner：数据装载）"
        )

    proc = subprocess.run(
        [sys.executable, "-m", "project_rag", "search", "-q", "课程版本", "--k", "3"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}")
    if "命中 0 条" in proc.stdout:
        raise AssertionError(
            f"「课程版本」在 {total} 块的语料里一条都没召回；stdout={proc.stdout[:300]!r}"
        )
    # 断「这一段在不在」而不是「标题长什么样」:rag.rerank 默认开着,
    # 重排那一段就该出现 —— 不出现说明 retrieve 没拿到重排器(或命令没读配置)
    if "重排" not in proc.stdout:
        raise AssertionError(
            f"search 没打印重排那一段 —— rag.rerank 默认开着,真链路上却没走重排。"
            f"stdout={proc.stdout[:400]!r}"
        )
    return f"库内 {total} 块，search 正常召回且打出了重排段"


@check("BM25 那一路：罕见 token 查询能精确命中含它的块（稠密那一路会含糊的地方）")
def _() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.domain import Chunk

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk("c1", "内部培训手册的课程版本是 V6.0。", "ai", "d1"),
        Chunk("c2", "faild to load module canberra-gtk-module 报错怎么解决", "ai", "d2"),
        Chunk("c3", "退款在订单页点申请退款。", "ai", "d3"),
    ]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))
    index = BM25KeywordIndex(store)

    hits = index.search("canberra-gtk-module", k=1)
    if not hits or hits[0].chunk.chunk_id != "c2":
        raise AssertionError(
            f"罕见 token 应当精确命中 c2，实际 {[h.chunk.chunk_id for h in hits]}"
        )
    if hits[0].score <= 1.0:
        raise AssertionError(
            f"BM25 的原始分没有上界（实测强命中 > 1），实际 {hits[0].score:.4f}；"
            "如果这里小于 1，说明被归一化过 —— 那会让融合时的量纲判断失真"
        )
    return f"「canberra-gtk-module」→ c2，原始分 {hits[0].score:.2f}（无上界，不是相似度）"


@check("BM25 那一路：k 截断 / source 过滤 / 空库给空序列 / 语料变了会重建")
def _() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.domain import Chunk

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    index = BM25KeywordIndex(store)
    if list(index.search("随便问问", k=3)) != []:
        raise AssertionError("空库必须给空序列（用例层要能走「没检索到」分支）")

    # 词表外的问题必须给空序列,不能返回 0 分的那几条
    # （实测域外问法全 0、域内 9.7~14.7,中间没有灰区,所以这条断言很硬）
    out_of_domain = index.search("学费 多少钱 天气 写诗", k=3)
    if list(out_of_domain) != []:
        raise AssertionError(
            f"域外问法应给空序列,实际返回 {len(out_of_domain)} 条 "
            f"（分数 {[round(h.score, 3) for h in out_of_domain]}）—— "
            "0 分的命中会在融合里拿到排名分,挤掉真命中"
        )

    chunks = [
        Chunk("c1", "退款在订单页点申请退款", "ai", "d1"),
        Chunk("c2", "退款 1-3 个工作日到账", "ai", "d1"),
        Chunk("c3", "java 程序包不存在怎么解决", "java", "d2"),
    ]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))
    if len(index.search("退款", k=1)) != 1:
        raise AssertionError("k=1 应当只回 1 条")
    only_java = index.search("程序包", k=10, source="java")
    if [hit.chunk.chunk_id for hit in only_java] != ["c3"]:
        raise AssertionError(f"source 过滤应只剩 c3，实际 {[h.chunk.chunk_id for h in only_java]}")

    # 语料变了要能重建 —— 指纹缓存不能变成「永远用旧索引」
    store.upsert(
        [Chunk("c4", "装饰器怎么写：用 @ 语法糖", "ai", "d3")], embedder.embed(["装饰器怎么写：用 @ 语法糖"])
    )
    if not index.search("装饰器", k=1):
        raise AssertionError("新写入的块检索不到 —— 索引没跟着语料重建")
    return "空库空序列 / k 截断 / source 过滤 / 语料变化后自动重建"


@check("BM25 索引：语料没变就不再 load_all（每查询只读廉价指纹）—— 冷启动那次省下的那一笔")
def _() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.domain import Chunk

    class _CountingStore(InMemoryVectorStore):
        """真内存实现 + 计数。

        继承而不是另写一个假件:端口有 7 个方法,假件少一个 pyright 就报「不是 VectorStore」;
        而且这条要验的正是真实现的缓存行为。定义在函数里是因为它只服务这一条检查。
        """

        def __init__(self) -> None:
            super().__init__()
            self.loads = 0
            self.fingerprints = 0

        def load_all(self):
            self.loads += 1
            return super().load_all()

        def fingerprint(self):
            self.fingerprints += 1
            return super().fingerprint()

    store = _CountingStore()
    embedder = HashingEmbedder()
    chunks = [Chunk("c1", "退款在订单页点申请退款", "ai", "d1")]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))

    index = BM25KeywordIndex(store)
    if not index.search("退款", k=1):
        raise AssertionError("第一次检索就该建好索引并命中")
    if (store.loads, store.fingerprints) != (1, 1):
        raise AssertionError(
            f"建索引该 load_all 一次 + 读指纹一次,实际 loads={store.loads} fingerprints={store.fingerprints}"
        )

    index.search("退款", k=1)
    index.search("申请退款", k=1)
    if store.loads != 1:
        raise AssertionError(
            f"语料没变时一次都不该再 load_all（那是每查询几 MB 正文）,实际 {store.loads} 次"
        )
    if store.fingerprints != 3:
        raise AssertionError(f"每次查询都该读一次指纹,实际 {store.fingerprints} 次")

    store.upsert(
        [Chunk("c9", "发票怎么开", "ai", "d9")], embedder.embed(["发票怎么开"])
    )
    if not index.search("发票", k=1):
        raise AssertionError("语料变了必须重建索引,否则新块永远检索不到")
    if store.loads != 2:
        raise AssertionError(f"语料变了只该重建一次,实际 load_all {store.loads} 次")
    return "语料没变:只读指纹(load_all 仍是 1 次);语料变了:重建一次"


@check("BM25 索引：并发换语料不会「旧索引配新语料」—— 快照发布必须原子（进程级单例的前提）")
def _() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.adapters import bm25_index as bm25_module
    from project_rag.domain import Chunk

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    few = [Chunk(f"s{i}", f"退款流程第 {i} 步说明", "ai", "d1") for i in range(3)]
    store.upsert(few, embedder.embed([chunk.text for chunk in few]))
    index = BM25KeywordIndex(store)
    index.search("退款", k=3)  # 先建好索引

    many = [Chunk(f"b{i}", f"退款流程第 {i} 步说明", "ai", "d2") for i in range(40)]
    real_tokens = bm25_module.tokens
    fired: list[int] = []

    def hooked_tokens(text: str):
        """在「本次检索已选定索引、还没遍历语料」的时刻,让另一个线程把语料换掉。

        这**正是**以前那个实现的缺陷窗口:它先取 ``self._index``,调用 ``tokens()``
        之后**又去读 ``self._chunks``** —— 两个线程一交错,本次调用就会拿着旧索引去索引新语料
        (长度不匹配 → IndexError)。把 ``tokens()`` 打桩是为了把这个竞态变成**确定性**的:
        靠线程调度去撞窗口的检查会时红时绿,那种检查不算检查。

        边界说清楚:这条检查复现的是那个具体窗口,**不构成对原子性的完整证明** ——
        完整保证来自实现约定「整份快照只用一个属性装,单次赋值原子发布」。
        """
        if not fired:
            fired.append(1)
            store.upsert(many, embedder.embed([chunk.text for chunk in many]))
            worker = threading.Thread(target=lambda: index.search("退款", k=3))
            worker.start()
            worker.join()
        return real_tokens(text)

    setattr(bm25_module, "tokens", hooked_tokens)
    try:
        hits = index.search("退款", k=3)
    finally:
        setattr(bm25_module, "tokens", real_tokens)
    if not hits:
        raise AssertionError("换了语料之后仍应命中")
    return f"并发换语料后不炸、命中 {len(hits)} 条（快照是配套的）"


@check("分词口径只有一处：关键字那几路（FAQ 匹配 / BM25 两档）共用 chinese_tokens")
def _() -> str:
    """**三处**文本匹配都走同一个分词器 —— 少一处就是"同一条查询在两处切出不同的词"。

    这条以前只盯两处（FAQ 匹配器 + 进程内 BM25），``keyword.backend=milvus`` 那一档加进来之后
    **第三个用户（``bm25_sparse.py``）漏在外面**了：它自己 import jieba 也能跑，而且症状是静默的
    —— 入库按一种切法算权重、检索按另一种切法编码查询，命中率掉一截而没有任何报错。
    「口径只在一处」这句话里的"一处"必须把**所有用户**枚举完，否则它只是当时那两处的巧合。
    """
    users = ("matcher_bm25", "bm25_index", "bm25_sparse")
    shared = (SRC / "adapters" / "chinese_tokens.py").read_text(encoding="utf-8")
    for name in users:
        text = (SRC / "adapters" / f"{name}.py").read_text(encoding="utf-8")
        if "from project_rag.adapters.chinese_tokens import tokens" not in text:
            raise AssertionError(f"{name} 没有用共享分词器 —— 分词口径分叉了")
        # 查 import 语句而不是子串:docstring 里提一句 "jieba 分词" 不该算分叉
        if "jieba" in _imported_roots_text(text):
            raise AssertionError(f"{name} 里还直接 import jieba —— 分词口径分叉了")
    if "jieba" not in shared:
        raise AssertionError("chinese_tokens 里没有 jieba，那它拿什么分词？")
    return f"{len(users)} 处都用 adapters/chinese_tokens.tokens：{' / '.join(users)}，没有第二份分词实现"


@check("CLI：search --route bm25 在真实语料上召回（不加载嵌入模型）")
def _() -> str:
    import subprocess

    from pymilvus import MilvusClient

    from project_rag.config import get_settings

    settings = get_settings()
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"
    try:
        client = MilvusClient(uri=target)
        rows = client.query(
            collection_name=settings.milvus.collection,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        total = int(rows[0]["count(*)"]) if rows else 0
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题
        raise SkipCheck(f"Milvus 连不上（{target}）：{type(exc).__name__}") from exc
    if total == 0:
        raise SkipCheck(
            f"集合 {settings.milvus.collection} 是空的；先跑 "
            ".venv/bin/python -m project_rag ingest（owner：数据装载）"
        )

    proc = subprocess.run(
        [sys.executable, "-m", "project_rag", "search", "-q", "课程版本", "--route", "bm25", "--k", "3"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}")
    if "[BM25]" not in proc.stdout:
        raise AssertionError(f"没打印 BM25 那一段；stdout={proc.stdout[:300]!r}")
    if "命中 0 条" in proc.stdout.split("[BM25]")[-1]:
        raise AssertionError(f"「课程版本」在 BM25 上一條都没召回；stdout={proc.stdout[:400]!r}")
    return f"库内 {total} 块，--route bm25 正常召回（全程未加载嵌入模型）"


def _hit(chunk_id: str, score: float):
    """造一条命中,给融合的几条检查共用。"""
    from project_rag.domain import Chunk, RetrievedChunk

    return RetrievedChunk(chunk=Chunk(chunk_id, f"文本{chunk_id}", "ai", "d1"), score=score)


@check("融合 RRF：分数 = Σ w/(rrf_k+rank)，手算对得上；两路同中的块合并成一条")
def _() -> str:
    from project_rag.application import fuse_rankings

    # 向量路:c1 第 1、c2 第 2;BM25 路:c2 第 1、c3 第 2 —— c2 被两路同时命中
    rankings = {
        "vector": [_hit("c1", 0.9), _hit("c2", 0.8)],
        "bm25": [_hit("c2", 15.0), _hit("c3", 9.0)],
    }
    fused = fuse_rankings(rankings, mode="rrf", weights={"vector": 1.0, "bm25": 1.0}, rrf_k=60)

    got = {item.chunk.chunk_id: round(item.score, 6) for item in fused}
    want = {"c1": 1 / 61, "c2": 1 / 62 + 1 / 61, "c3": 1 / 62}
    if got != {key: round(value, 6) for key, value in want.items()}:
        raise AssertionError(f"RRF 手算对不上：期望 {want}，实际 {got}")
    if [item.chunk.chunk_id for item in fused] != ["c2", "c1", "c3"]:
        raise AssertionError(f"融合后顺序不对：{[i.chunk.chunk_id for i in fused]}")
    both = fused[0]
    if set(both.routes) != {"vector", "bm25"}:
        raise AssertionError(f"c2 应记着两路都命中，实际 {both.routes}")
    return "Σw/(k+rank) 手算一致；c2 合并成一条并记下两路"


@check("融合 RRF：只看排名、不看分数（某一路分数整体放大 100 倍，顺序必须不变）")
def _() -> str:
    from project_rag.application import fuse_rankings

    rankings = {
        "vector": [_hit("c1", 0.9), _hit("c2", 0.8)],
        "bm25": [_hit("c2", 15.0), _hit("c3", 9.0)],
    }
    scaled = {
        "vector": [_hit("c1", 90.0), _hit("c2", 80.0)],  # ×100
        "bm25": [_hit("c2", 15.0), _hit("c3", 9.0)],
    }
    weights = {"vector": 1.0, "bm25": 1.0}
    plain = [i.chunk.chunk_id for i in fuse_rankings(rankings, mode="rrf", weights=weights, rrf_k=60)]
    blown = [i.chunk.chunk_id for i in fuse_rankings(scaled, mode="rrf", weights=weights, rrf_k=60)]
    if plain != blown:
        raise AssertionError(
            f"RRF 应当对分数的量纲完全不敏感：{plain} vs {blown}。"
            "这正是选 RRF 的理由 —— 两路分数不可比（余弦 0.59 vs BM25 14.7）"
        )
    return f"{plain} —— 分数放大 100 倍后顺序不变"


@check("融合 weighted：先各自 min-max 归一化再加权（不归一化就等于只有 BM25 说话）")
def _() -> str:
    from project_rag.application import fuse_rankings

    rankings = {
        "vector": [_hit("c1", 0.9), _hit("c2", 0.8)],  # 归一化 → c1=1.0, c2=0.0
        "bm25": [_hit("c2", 15.0), _hit("c3", 9.0)],  # 归一化 → c2=1.0, c3=0.0
    }
    fused = fuse_rankings(
        rankings, mode="weighted", weights={"vector": 0.4, "bm25": 0.6}
    )
    got = {item.chunk.chunk_id: round(item.score, 6) for item in fused}
    want = {"c1": 0.4, "c2": 0.6, "c3": 0.0}
    if got != want:
        raise AssertionError(
            f"加权融合结果不对：期望 {want}，实际 {got}。"
            "注意 c2 是 0.6 而不是 15 上下 —— 没归一化的话 BM25 会把权重放大 25 倍"
        )
    # 权重真的在起作用:把权重翻过来,排第一的应该换人
    flipped = fuse_rankings(rankings, mode="weighted", weights={"vector": 0.9, "bm25": 0.1})
    if flipped[0].chunk.chunk_id != "c1":
        raise AssertionError(f"调大向量权重后 c1 应排第一，实际 {flipped[0].chunk.chunk_id}")
    return "归一化后加权值符合手算；权重翻转能改变第一名"


@check("融合：权重 ≤ 0 等于关掉那一路；不认识的 mode 直接报错（不许悄悄退化）")
def _() -> str:
    from project_rag.application import fuse_rankings

    rankings = {
        "vector": [_hit("c1", 0.9), _hit("c2", 0.8)],
        "bm25": [_hit("c2", 15.0), _hit("c3", 9.0)],
    }
    only_bm25 = fuse_rankings(rankings, mode="rrf", weights={"vector": 0.0, "bm25": 1.0})
    ids = {item.chunk.chunk_id for item in only_bm25}
    if "c1" in ids:
        raise AssertionError(f"向量路权重 0 时 c1 不该出现，实际 {ids}")
    if ids != {"c2", "c3"}:
        raise AssertionError(f"关掉向量路后只剩 BM25 的两条，实际 {ids}")

    try:
        fuse_rankings(rankings, mode="加权平均")
    except ValueError:
        pass
    else:
        raise AssertionError("不认识的融合方式必须抛 ValueError —— 配置写错要当场炸")
    return "权重 0 关掉那一路；不认识的 mode 抛 ValueError"


@check("融合真的接上了：两路进 retrieve 后引用带 routes；配置旋钮不再是没人用的摆设")
def _() -> str:
    from project_rag.adapters import BM25KeywordIndex, HashingEmbedder, InMemoryVectorStore
    from project_rag.application import retrieve
    from project_rag.config import get_settings
    from project_rag.domain import Chunk, RetrievalOptions

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk("c1", "退款在订单页点申请退款", "ai", "d1"),
        Chunk("c2", "canberra-gtk-module 报错怎么解决", "ai", "d2"),
    ]
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))
    index = BM25KeywordIndex(store)

    citations = retrieve(
        "canberra-gtk-module",
        embedder=embedder,
        store=store,
        keyword_index=index,
        options=RetrievalOptions(k=2),
    )
    if not citations:
        raise AssertionError("两路都装配了却一条都没检索到")
    if not any("bm25" in citation.routes for citation in citations):
        raise AssertionError(
            f"没有任何一条来自 BM25 —— 关键字那一路没接进融合；实际 "
            f"{[c.routes for c in citations]}"
        )

    # 旋钮必须真的接上配置:配了却没人读,那这几个键就只是摆设
    settings = get_settings().rag
    defaults = RetrievalOptions()
    for name in ("fusion", "rrf_k", "bm25_weight", "vector_weight"):
        if getattr(defaults, name) != getattr(settings, name):
            raise AssertionError(
                f"RetrievalOptions.{name} 默认值 {getattr(defaults, name)!r} "
                f"与 config 的 {getattr(settings, name)!r} 不一致"
            )
    source = (SRC / "cli" / "commands" / "rag.py").read_text(encoding="utf-8")
    unused = [
        f"settings.rag.{name}"
        for name in ("fusion", "rrf_k", "bm25_weight", "vector_weight")
        if f"settings.rag.{name}" not in source
    ]
    if unused:
        raise AssertionError(f"rag 命令没有把 {unused} 传给检索 —— 配置写了没人读")
    return "BM25 那一路进了融合；rag 命令读齐 fusion/rrf_k/两路权重"


# ── 重排 ────────────────────────────────────────────────────────────────
#
# 顺序是这一步的全部:召回 → 融合 → 折叠父块 → 重排父块正文 → 截断到 candidate_m。
# 下面逐条钉住它 —— 换顺序等于换分数对应的那段文字,必须由检查看着。
#
# _RecordingReranker / _parented_store / _gpu_invisible 都定义在**文件顶部**：
# @check 在定义时就立即执行，后文定义的帮助函数前文的检查看不到（见顶部注释）。

@check("两个本地适配器：import 前关联网，且设备/精度都从配置读（嵌入 use_f16、重排 rerank_use_f16）")
def _() -> str:
    seen = []
    for name in ("embedder_bge_m3.py", "reranker_bge.py"):
        path = SRC / "adapters" / name
        if not path.exists():
            raise AssertionError(f"adapters/{name} 不存在（owner：重排）")
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        setter = next((i for i, line in enumerate(lines) if "HF_HUB_OFFLINE" in line), -1)
        importer = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith("from sentence_transformers")
            ),
            -1,
        )
        if setter < 0:
            raise AssertionError(
                f"{name} 没有设 HF_HUB_OFFLINE —— huggingface.co 不通,"
                "每次加载都要等 hub 超时(实测 135s → 1.2s)"
            )
        if importer < 0:
            raise AssertionError(f"{name} 没 import sentence_transformers,那它拿什么加载模型")
        if setter > importer:
            raise AssertionError(
                f"{name} 把 HF_HUB_OFFLINE 放在 import 之后（第 {setter + 1} 行 vs "
                f"第 {importer + 1} 行）—— ST 读的是 import 期的环境变量,放后面等于没设"
            )
        # 设备与精度必须**从配置读**,不能在适配器里写死 ——
        # 否则 yaml 改成 cuda/true 也不会有任何效果,而这一点从代码上看不出来。
 # 精度**分开**:嵌入读 use_f16、重排读 rerank_use_f16（拆的,
        # 理由见 EmbeddingConfig 的注释:两者收益/精度风险不同,共用一个开关就拧不动)
        wanted = ("config.device", "config.rerank_use_f16" if name.startswith("reranker") else "config.use_f16")
        for key in wanted:
            if key not in source:
                raise AssertionError(
                    f"{name} 没读 {key} —— 配置里改了设备/精度它不会听"
                )
        seen.append(f"{name}({setter + 1}<{importer + 1})")
    return "、".join(seen) + "；嵌入读 use_f16、重排读 rerank_use_f16、设备读 device"


@check("重排适配器：真模型给相关句的分显著高于无关句（bge-reranker-v2-m3，慢）")
def _() -> str:
    import time

    from project_rag.config import get_settings

    settings = get_settings()
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    if not settings.embedding.rerank_model_path.exists():
        raise SkipCheck(
            f"重排模型不在 {settings.embedding.rerank_model_path}；先跑 "
            "scripts/tools/fetch_models.py --with-reranker（owner：数据装载）"
        )
    from project_rag.adapters.reranker_bge import BGEReranker

    reranker = BGEReranker(settings.embedding)
    texts = [
        "课程使用的版本是 3.0，如果需要更新请联系教务老师。",
        "Java 的 ArrayList 底层是数组，LinkedList 底层是双向链表。",
        "canberra-gtk-module 报错需要安装 gtk 相关依赖。",
    ]
    start = time.perf_counter()
    scores = list(reranker.rerank("课程使用的版本是多少", texts))
    elapsed = time.perf_counter() - start

    if len(scores) != len(texts):
        raise AssertionError(
            f"rerank 必须与输入同序等长：给 {len(texts)} 条回了 {len(scores)} 条"
        )
    if not all(math.isfinite(score) for score in scores):
        raise AssertionError(f"分数必须是有限数，实际 {scores}")
    # 相关 >0.5 > 无关：bge-reranker 是单标签 CrossEncoder,predict 出来的是 sigmoid 后的
    # 相关性分。实测相关 0.99 / 无关 0.000 —— 这个巨大落差正是「重排能在一条 query 内部
    # 重新排序」的原因,也是它和余弦相似度(全挤在 0.5~0.8)的根本区别。
    if not scores[0] > 0.5 > max(scores[1:]):
        raise AssertionError(
            f"相关句应 >0.5 且无关句 <0.5，实际 {[round(s, 4) for s in scores]}；"
            "如果所有分数都挤在一起,说明取的不是 sigmoid 后的输出"
        )
    return (
        f"相关 {scores[0]:.4f} vs 无关 {max(scores[1:]):.4f}"
        f"（{len(texts)} 条 {elapsed * 1000:.0f}ms，device={settings.embedding.device}）"
    )


def _worst_parents(limit: int) -> list[str]:
    """从真实语料切出父块，按长度降序取前 limit 个 —— 这是**最坏输入**。

    折叠后的候选数最多是 ``2 × retrieval_k``（两路各 k 条，折叠只减不增），
    所以按这个上界、且挑最长的那些来量，量出来的延迟对任何输入都成立。
    """
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import load_documents
    from project_rag.config import get_settings

    settings = get_settings()
    chunker = ChineseRecursiveChunker(
        settings.rag.parent_chunk_size,
        settings.rag.child_chunk_size,
        settings.rag.chunk_overlap,
    )
    parents = [
        group.parent_text
        for document in load_documents(CORPUS_DIR).documents
        for group in chunker.split(document.text)
    ]
    return sorted(parents, key=len, reverse=True)[:limit]


@check("重排延迟：模型加载完后单条 p99 < 1s（的硬指标；冷启动加载不计入）")
def _() -> str:
    """口径：只算**重排推理**，不含模型加载。

    为什么不含：含加载的话任何硬件都过不了 —— CPU 加载 1.6s、GPU 0.5s，光加载就超 1s。
    冷启动时间**单独量、单独打印**：让人看得见它，但不让它参与判定。
    """
    import time

    from project_rag.config import get_settings

    settings = get_settings()
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    if not settings.embedding.rerank_model_path.exists():
        raise SkipCheck(
            f"重排模型不在 {settings.embedding.rerank_model_path}；owner：数据装载"
        )
    if not CORPUS_DIR.exists():
        raise SkipCheck(f"语料目录不存在（{CORPUS_DIR}）；owner：数据装载")

    from project_rag.adapters.reranker_bge import BGEReranker

    limit = 2 * settings.rag.retrieval_k
    worst = _worst_parents(limit)
    if len(worst) < limit:
        raise SkipCheck(
            f"语料只切出 {len(worst)} 个父块，不够拼出最坏输入（{limit}）；owner：数据装载"
        )

    start = time.perf_counter()
    reranker = BGEReranker(settings.embedding)
    cold_start = time.perf_counter() - start

    query = "课程使用的版本是多少"
    reranker.rerank(query, worst)  # 预热：CUDA kernel 首次编译归到冷启动，不算进 p99
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        reranker.rerank(query, worst)
        samples.append(time.perf_counter() - start)
    samples.sort()
    # 20 个样本时 p99 就等于最大值 —— 样本这么少不装作能估出真 p99，
    # 直接取最坏那一次：保守，但不会撒谎
    p99 = samples[min(len(samples) - 1, math.ceil(0.99 * len(samples)) - 1)]
    if p99 >= 1.0:
        raise AssertionError(
            f"重排 p99 = {p99 * 1000:.0f} ms ≥ 1s（{limit} 条父块 / "
            f"{sum(len(t) for t in worst)} 字，device={settings.embedding.device}）。"
            "CPU 上没有可达路径（实测 9 条 ≈6s）—— 把 embedding.device 改成 cuda，"
            "或按 重新评估重排单位"
        )
    return (
        f"device={settings.embedding.device} use_f16={settings.embedding.use_f16}，"
        f"最坏输入 {limit} 条父块（{sum(len(t) for t in worst)} 字）→ "
        f"p99 {p99 * 1000:.0f} ms（冷启动加载 {cold_start:.2f}s，不计入）"
    )


@check("重排 fp16：与 fp32 排序一致（分差极小 + top3 集合相同），不许 f16 悄悄改结果")
def _() -> str:
    """f16 是**为延迟开的质量风险开关**，所以必须有人盯着它有没有改排序。

    实测（9 条 900 字父块）：最大分差 0.00007，top1 与 top3 集合都一致。
    阈值放到 0.01 是因为这条要拦的是「精度掉了档」，不是浮点末位噪声。
    """
    from project_rag.config import get_settings

    settings = get_settings()
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    if not settings.embedding.use_f16:
        raise SkipCheck(
            "配置里 use_f16=False，此刻没有 f16/fp32 差异要查。"
            "注意这条**不是「跳过就算过」**：一开 f16 它就会真的比"
        )
    if not settings.embedding.rerank_model_path.exists():
        raise SkipCheck(
            f"重排模型不在 {settings.embedding.rerank_model_path}；owner：数据装载"
        )

    from project_rag.adapters.reranker_bge import BGEReranker

    texts = _worst_parents(2 * settings.rag.retrieval_k)
    query = "课程使用的版本是多少"
    base = settings.embedding
    strict = list(
        BGEReranker(base.model_copy(update={"use_f16": False})).rerank(query, texts)
    )
    loose = list(
        BGEReranker(base.model_copy(update={"use_f16": True})).rerank(query, texts)
    )
    if len(strict) != len(loose):
        raise AssertionError(
            f"两种精度回的条数不一样（{len(strict)} vs {len(loose)}）—— 适配器有问题"
        )

    def _top(texts_scores: list[float], k: int) -> set[int]:
        return set(
            sorted(range(len(texts_scores)), key=texts_scores.__getitem__, reverse=True)[:k]
        )

    spread = max(abs(a - b) for a, b in zip(strict, loose))
    top1_same = _top(strict, 1) == _top(loose, 1)
    top3_same = _top(strict, 3) == _top(loose, 3)
    if spread > 0.01 or not top1_same or not top3_same:
        raise AssertionError(
            f"f16 改了排序：最大分差 {spread:.5f}（阈值 0.01）、"
            f"top1 一致={top1_same}、top3 集合一致={top3_same}。"
            f"fp32={[round(s, 4) for s in strict]} fp16={[round(s, 4) for s in loose]}"
        )
    return (
        f"分差 {spread:.5f}（阈值 0.01），top1 与 top3 集合都一致"
        f"（{len(texts)} 条真实父块）"
    )


@check("重排：rerank_citations 填 rerank_score、重新排序、原召回分与 routes 原样保留")
def _() -> str:
    from project_rag.application import rerank_citations
    from project_rag.domain import Chunk, Citation

    fake = _RecordingReranker()
    if rerank_citations("随便问", (), reranker=fake) != ():
        raise AssertionError("没有候选时应当直接给空序列")
    if fake.calls:
        raise AssertionError("没有候选还去调重排器 —— 白跑一次模型前向")

    citations = (
        Citation(1, Chunk("c2", "子块乙", "ai", "d1"), 0.5, ("vector",)),
        Citation(2, Chunk("c1", "子块甲", "ai", "d1"), 0.9, ("vector", "bm25")),
    )
    # 两条都不含关键词 → 同分 → 按 chunk_id 兜底，顺序必须稳定
    tie = rerank_citations("随便问", citations, reranker=_RecordingReranker(prefer="查无此词"))
    if [c.chunk.chunk_id for c in tie] != ["c1", "c2"]:
        raise AssertionError(f"同分时顺序不稳定：{[c.chunk.chunk_id for c in tie]}")
    if [c.index for c in tie] != [1, 2]:
        raise AssertionError(f"重排后必须重新从 1 编号，实际 {[c.index for c in tie]}")
    kept = tie[0]
    if (kept.score, kept.routes) != (0.9, ("vector", "bm25")):
        raise AssertionError(
            f"重排不该动召回分和 routes（那是「它从哪来的」记录），"
            f"实际 score={kept.score} routes={kept.routes}"
        )

    ranked = rerank_citations("随便问", citations, reranker=_RecordingReranker(prefer="子块乙"))
    if [c.chunk.chunk_id for c in ranked] != ["c2", "c1"]:
        raise AssertionError(f"重排该把 c2 提到第 1，实际 {[c.chunk.chunk_id for c in ranked]}")
    if [c.rerank_score for c in ranked] != [1.0, 0.1]:
        raise AssertionError(f"rerank_score 没填对：{[c.rerank_score for c in ranked]}")
    return "空序列不调模型；按重排分重排；score/routes 保留；同分按 chunk_id 稳定"


@check("重排：适配器回的分数比候选少时必须抛错（少几条不能被 zip 悄悄吞掉）")
def _() -> str:
    from project_rag.application import rerank_citations
    from project_rag.domain import Chunk, Citation

    class _ShortReranker:
        def rerank(self, query, texts):
            return [0.5] * (len(texts) - 1)

    citations = tuple(
        Citation(i, Chunk(f"c{i}", f"子块{i}", "ai", "d1"), 0.5) for i in (1, 2, 3)
    )
    try:
        rerank_citations("随便问", citations, reranker=_ShortReranker())
    except ValueError as exc:
        return f"少给分数 → ValueError：{exc}"
    raise AssertionError(
        "重排器回的分数比候选少时必须抛 ValueError。zip 会静默截断，"
        "表现成「有几条候选凭空消失」——比当场报错难查得多"
    )


@check("retrieve：传了重排器就真用它，且喂进去的是父块正文（模型读什么就重排什么）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder
    from project_rag.application import retrieve
    from project_rag.domain import RetrievalOptions

    embedder = HashingEmbedder()
    store = _parented_store(embedder)
    options = RetrievalOptions(k=3)

    plain = retrieve("课程", embedder=embedder, store=store, options=options)
    if len(plain) < 2:
        raise AssertionError(f"这条检查需要 ≥2 个候选才有意义，实际 {len(plain)}")
    if any(c.rerank_score is not None for c in plain):
        raise AssertionError("没传重排器时 rerank_score 必须留空 —— 别假装重排过")

    target = plain[-1].chunk  # 融合排在最后的那个父块
    fake = _RecordingReranker(prefer=target.parent_content or target.text)
    ranked = retrieve("课程", embedder=embedder, store=store, reranker=fake, options=options)

    if not fake.calls:
        raise AssertionError("传了重排器却一次都没调用 —— retrieve 没把它接上")
    fed = fake.calls[0][1]
    if any("子块" in text for text in fed):
        raise AssertionError(
            f"喂给重排器的是子块正文 {fed} —— 必须是 context_text（父块正文）。"
            "重排子块更快，但那样「分数最高」对应的是一段模型没读过的文字，"
            "引用清单上的分数就没法核对了"
        )
    if ranked[0].chunk.parent_id != target.parent_id:
        raise AssertionError(
            f"融合排最后的 {target.parent_id} 应当被重排提到第 1，"
            f"实际 {ranked[0].chunk.parent_id} —— 重排没有真的改变排序"
        )

    # 那组检索 A/B 的旋钮：rerank_input="child" 要真的换成**子块正文**。
    # **两个方向都要钉**：只钉 parent 的话，旋钮没接上（永远喂父块）也照样绿。
    child_fake = _RecordingReranker(prefer=target.text)
    retrieve(
        "课程",
        embedder=embedder,
        store=store,
        reranker=child_fake,
        options=RetrievalOptions(k=3, rerank_input="child"),
    )
    fed_child = child_fake.calls[0][1]
    if any("父块" in text for text in fed_child):
        raise AssertionError(
            f'rerank_input="child" 时喂的该是子块正文，实际 {fed_child} —— 旋钮没接上'
            "（读出来会显示「两种喂法一样」，把 A/B 做成平局）"
        )
    return (
        f"喂进 {len(fed)} 条父块正文；融合末位 {target.parent_id} → 重排首位；"
        f'rerank_input="child" 时 {len(fed_child)} 条喂的是子块正文'
    )


@check("retrieve：截断发生在重排之后（candidate_m 取的是重排后的前 N 条）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder
    from project_rag.application import retrieve
    from project_rag.domain import RetrievalOptions

    embedder = HashingEmbedder()
    store = _parented_store(embedder)

    plain = retrieve("课程", embedder=embedder, store=store, options=RetrievalOptions(k=3))
    if len(plain) < 2:
        raise AssertionError(f"这条检查需要 ≥2 个候选才有意义，实际 {len(plain)}")
    target = plain[-1].chunk

    fake = _RecordingReranker(prefer=target.parent_content or target.text)
    only = retrieve(
        "课程",
        embedder=embedder,
        store=store,
        reranker=fake,
        options=RetrievalOptions(k=3, max_context=1),
    )
    if len(only) != 1:
        raise AssertionError(f"max_context=1 应只留 1 条，实际 {len(only)}")
    if only[0].chunk.parent_id != target.parent_id:
        raise AssertionError(
            f"留下的应是**重排后**的第 1 名（{target.parent_id}），"
            f"实际 {only[0].chunk.parent_id} —— 先截断再重排就会留下融合的第 1 名，"
            "那等于重排只在一小撮候选里挑，白做了"
        )
    return f"k=3 全量重排 → 截断到 1，留下的是重排首位 {target.parent_id}"


@check("重排：只有 1 个候选时跳过（排序不可能变，别为它跑一次模型前向）")
def _() -> str:
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import retrieve
    from project_rag.domain import Chunk, RetrievalOptions

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    store.upsert(
        [Chunk("c1", "课程 版本号", "ai", "d1", "p1", "父块甲的正文")],
        embedder.embed(["课程 版本号"]),
    )
    fake = _RecordingReranker(prefer="父块甲")
    found = retrieve(
        "课程", embedder=embedder, store=store, reranker=fake, options=RetrievalOptions(k=3)
    )
    if len(found) != 1:
        raise AssertionError(f"只有一个候选，实际回了 {len(found)} 条")
    if fake.calls:
        raise AssertionError(
            "只有一个候选时不该调重排器 —— 一条候选的排序不可能变，"
            "这一次前向（CPU ≈1.1s）是纯浪费。原始实现也做了同样的短路"
        )
    if found[0].rerank_score is not None:
        raise AssertionError("跳过了重排就不该填 rerank_score —— 没算过就别写数")
    return "1 个候选 → 不调重排器，rerank_score 留空"


@check("配置：rag.rerank 写进了 config.yaml，rag / search 命令真的读它")
def _() -> str:
    import yaml

    from project_rag.config import CONFIG_FILE, RagConfig, get_settings
    from project_rag.domain import RetrievalOptions

    data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    if "rerank" not in (data.get("rag") or {}):
        raise AssertionError(
            "config.yaml 的 rag 段缺 rerank —— 只在代码里写默认值，读配置的人看不到它"
        )
    if RagConfig().rerank is not True:
        raise AssertionError(f"RagConfig.rerank 默认应为 True，实际 {RagConfig().rerank!r}")
    if get_settings().rag.rerank is not True:
        raise AssertionError("环境里读到的 rag.rerank 不是 True，检查 config.yaml")

    # 决策钉死:重排开关**不进** RetrievalOptions。
    if hasattr(RetrievalOptions(), "rerank"):
        raise AssertionError(
            "RetrievalOptions 不该有 rerank 开关：重排开不开由「有没有传重排器」表达"
            "（和 keyword_index=None 表示没有 BM25 那一路同理）。"
            "两处都能表达就有两个真值来源 —— options.rerank=False 又传了重排器时谁赢？"
        )
    for name in ("rag.py", "search.py"):
        source = (SRC / "cli" / "commands" / name).read_text(encoding="utf-8")
        if "settings.rag.rerank" not in source:
            raise AssertionError(f"cli/commands/{name} 没读 settings.rag.rerank —— 配置写了没人读")
    return "rag.rerank 在 yaml 与默认值里都是 True；rag/search 两个命令都读它"


@check("脚本自身：检查不许引用后文定义的帮助函数（@check 在定义时就立即执行）")
def _() -> str:
    """这条是**本脚本自己踩过的坑**长出来的。

    ``@check(name)`` 会在**定义时立即执行**函数，所以整份脚本的检查是 import 期
    按文件顺序跑完的。于是「A 检查用了后面才定义的帮助函数」在写的时候完全不报错，
    而是在跑到 A 的时候抛 ``NameError`` —— 报的是那个帮助函数的名字，
    看着像它写错了，不像顺序问题。

    真实事故：CLI 那条 rag 检查用了重排那一节才定义的 ``_RecordingReranker``，
    整条红成 ``NameError``；而把同一段逻辑单独抽出来跑却完全正常，很容易白查半天。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    # 顶层定义的名字 → 行号（只看 def / class，变量与 import 不参与）
    defined: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            defined[node.name] = node.lineno

    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        title = ""
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                title = str(decorator.args[0].value)
            elif isinstance(decorator, ast.Name):
                title = decorator.id
        if not title:
            continue  # 不是 @check 装饰的（比如 main）
        used = {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
        }
        late = [
            (name, defined[name])
            for name in sorted(used & defined.keys())
            if defined[name] > node.lineno
        ]
        if late:
            where = "、".join(f"{name}@{line}" for name, line in late)
            problems.append(f"「{title}」(第 {node.lineno} 行) 用了后文定义的 {where}")

    if problems:
        raise AssertionError(
            "这些检查引用了**后文**才定义的帮助函数，跑起来会是 NameError：\n    "
            + "\n    ".join(problems)
            + "\n修法：把那个帮助函数移到**文件顶部**（所有 @check 之前）"
        )
    return f"{len(defined)} 个顶层定义全部先于引用它的检查"


@check("全链路：融合路由—— FAQ 未命中时知识库通道兜底真的被调到")
def _() -> str:
    """这边只钉**接口**：知识库通道能被「合流」当兜底整条调起来。

    详细的路由契约（FAQ 命中 / 阈值边界 / 拒答 / 故障 / 惰性构造）在 `check_m5_qa.py`，
    这里不重复。这条原先是一条点名合流的 SKIP —— 合流入口落地后它自己转绿，
    正是那时候写下「那一步做完这条自己变绿」的意思。
    """
    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.application import stream_rag
    from project_rag.domain import Chunk, RetrievalOptions

    # 用 importlib + getattr 而不是 `from ... import answer_question`:
    # 合流入口的代码还没落地时，静态导入会让 pyright 报「未知导入符号」——
    # 契约先红是对的，但不该顺手把 pyright 也弄红（那是两码事）。
    answer_question = getattr(
        importlib.import_module("project_rag.application"), "answer_question", None
    )
    if answer_question is None:
        raise AssertionError(
            "application.answer_question 还没落地（的合流入口）；"
            "owner：合流入口，落地后这条自己变绿"
        )

    class _EmptyFaqStore:
        """空表 = FAQ 一定不命中,整条交给知识库通道。"""

        def load_all(self):
            return []

    class _NoMatcher:
        def best_match(self, query, candidates):
            return None

    embedder = HashingEmbedder()
    store = InMemoryVectorStore()
    text = "装饰器是给函数加行为的语法"
    store.upsert([Chunk("c1", text, "ai", "d1")], embedder.embed([text]))
    llm = _RecordingLLM(reply="装饰器是给函数加行为的语法 [1]")

    result = answer_question(
        "装饰器是什么",
        faq_store=_EmptyFaqStore(),
        matcher=_NoMatcher(),
        knowledge=lambda history, timing: stream_rag(
            "装饰器是什么",
            embedder=embedder,
            store=store,
            llm=llm,
            options=RetrievalOptions(k=3, max_context=1),
            history=history,
            timing=timing,
        ),
        faq_threshold=0.85,
    )
    if result.channel != "rag":
        raise AssertionError(
            f"FAQ 不命中时该由知识库通道兜底,实际 channel={result.channel!r}"
        )
    if not result.citations:
        raise AssertionError(
            "知识库通道没给出引用 —— 兜底那条路没真的跑起来(检索 → 提示词 → 生成)"
        )
    if not llm.prompts or text not in llm.prompts[0]:
        raise AssertionError("喂给模型的提示词里没有检索到的正文,兜底链路没接上")
    return f"FAQ 未命中 → 知识库通道答,{len(result.citations)} 条引用"


# ── 读一致性与精度开关的接线────────────────────────────────────
@check("读一致性：search 走配置档，其余读（指纹/写后回查/全量导出）固定 strong")
def _() -> str:
    """实测背景（本机 3927 块，各 30 次采样）：同一份数据、同一个 FLAT 索引，
    ``strong`` 中位 **399ms**、``bounded`` 中位 **2ms** —— 200×。那 400ms 是等"保证时间戳"
    对齐的同步往返，不是计算（与 k、与取哪些字段都无关）。

    所以分工必须钉住：**热路径（search）可配**，**变更检测与写后回查固定 strong**
    —— 后者用 bounded 会拿旧状态判"语料没变"（新入库的文档一直检索不到，静默错误），
    或把刚写完的块数数少（前端卡片骗人）。两个常量合成一个就等于把这条分工丢了。
    """
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import MilvusConfig
    from project_rag.domain.models import Chunk, Embedding

    class SpyClient:
        """只记录传下去的 consistency_level，不做任何真调用。"""

        def __init__(self) -> None:
            self.seen: list[tuple[str, str | None]] = []

        def has_collection(self, name: str) -> bool:
            return True

        def search(self, **kw):  # noqa: ANN003, ANN201
            self.seen.append(("search", kw.get("consistency_level")))
            return [[]]  # 空命中：不构造 RetrievedChunk，省掉造夹具

        def describe_collection(self, name: str) -> dict:
            """_output_fields() 会问"集合有哪些列" —— 给一份够用的列清单。"""
            names = [
                "chunk_id",
                "text",
                "dense_vector",
                "source",
                "doc_id",
                "parent_id",
                "parent_content",
                "page",
            ]
            return {"fields": [{"name": item} for item in names]}

        def query(self, **kw):  # noqa: ANN003, ANN201
            self.seen.append(("query", kw.get("consistency_level")))
            return [{"count(*)": 0}] if "count(*)" in (kw.get("output_fields") or []) else []

    def spy_for(
        level: typing.Literal["bounded", "eventually", "strong"],
    ) -> tuple[SpyClient, MilvusVectorStore]:
        """造一个「客户端是桩」的 store。

        **必须在构造期就把 `MilvusClient` 换成桩**：`__init__` 会真连一次 Milvus，而这条检查
        属于 `--set ci`（零容器）—— 容器没起时它会以 `Fail connecting to server on 127.0.0.1:10086`
        的样子假红（看着像代码坏了，其实只是没起中间件）。只替换客户端这一层，
        `__init__` 里那行「配置档 → consistency 常量」的映射照常执行，接线仍然被验到。
        """
        import project_rag.adapters.milvus_store as milvus_store

        spy = SpyClient()
        original = milvus_store.MilvusClient
        milvus_store.MilvusClient = lambda **kw: spy  # type: ignore[assignment]
        try:
            store = MilvusVectorStore(MilvusConfig(read_consistency=level))
        finally:
            milvus_store.MilvusClient = original
        return spy, store

    vector = Embedding(dense=tuple([0.0] * 4))
    three: tuple[tuple[typing.Literal["bounded", "eventually", "strong"], str], ...] = (
        ("bounded", "Bounded"),
        ("strong", "Strong"),
        ("eventually", "Eventually"),
    )
    for level, want in three:
        spy, store = spy_for(level)
        store.search(vector, k=3)
        got = spy.seen[-1][1]
        if got != want:
            raise AssertionError(f"read_consistency={level!r} 时 search 应传 {want!r}，实际 {got!r}")
    spy, store = spy_for("bounded")
    store._count()
    store.load_all()
    store.fingerprint()
    wrong = [item for item in spy.seen if item[1] != "Strong"]
    if wrong:
        raise AssertionError(
            f"这些读不该跟着 search 一起放松：{wrong} —— 指纹用 bounded 会让新入库的文档"
            "一直检索不到（静默），写后回查会数少块数"
        )
    return "search 随配置（bounded/strong/eventually 三档都验过）；指纹 / 回查 / 导出固定 Strong"


@check("重排半精度：rerank_use_f16 与嵌入的 use_f16 **分开**，且只在 CUDA 上生效")
def _() -> str:
    """为什么分开：实测（受控两份权重）重排 2 条 1142 字父块 fp32 98ms → fp16 31ms（3.2×），
    嵌入 8 条 22.5 → 9.2ms（2.4×）—— 收益不同、精度风险不同，用一个开关绑在一起就拧不动。

    这里用一个假的 ``CrossEncoder`` 记录"有没有调 half()"，所以**不需要模型、不需要 GPU**：
    真去加载模型的话，这条检查在没有 4.4G 模型的机器上只能 SKIP（等于没检查）。
    """
    import project_rag.adapters.reranker_bge as module
    from project_rag.config import EmbeddingConfig

    class FakeCrossEncoder:
        instances: list["FakeCrossEncoder"] = []

        def __init__(self, path: str, device: str = "cpu") -> None:
            self.path = path
            self.device = device
            self.halved = False
            FakeCrossEncoder.instances.append(self)

        def half(self) -> None:  # noqa: D102
            self.halved = True

    original = module.CrossEncoder
    module.CrossEncoder = FakeCrossEncoder
    try:
        cases = (
            ({"device": "cuda", "use_f16": True, "rerank_use_f16": True}, True, "cuda + 重排开关开"),
            ({"device": "cuda", "use_f16": True, "rerank_use_f16": False}, False, "cuda + 重排开关关"),
            ({"device": "cpu", "use_f16": True, "rerank_use_f16": True}, False, "cpu（fp16 更慢，不许开）"),
            ({"device": "cuda", "use_f16": False, "rerank_use_f16": True}, True, "只看重排自己的开关"),
        )
        for overrides, want_half, why in cases:
            FakeCrossEncoder.instances.clear()
            module.BGEReranker(EmbeddingConfig(**overrides))
            got = FakeCrossEncoder.instances[-1].halved
            if got != want_half:
                raise AssertionError(f"{why}：期望 half()={'调' if want_half else '不调'}，实际 {'调' if got else '不调'}")
    finally:
        module.CrossEncoder = original
    return "4 种组合都验过：重排只看 rerank_use_f16，且仅 CUDA 生效"

def main() -> int:
    return report("向量知识库 + RAG 验收")

if __name__ == "__main__":
    sys.exit(main())
