"""验收脚本共用的假件(端口的进程内替身)。

**为什么抽出来**:`_FakeLLM` 在 5 份脚本里各写一遍、`_FakeVectorStore` 5 份、
`_FakeFaqStore` 4 份、`_FakeMatcher` 4 份…… **合计 530 行**,抽成一份约 200 行。
而且它们**已经开始漂**:同一个 `_FakeKeywordIndex` 有 4 个版本(有的记调用、有的不记),
同一个 `_FakeConversationStore` 有 3 个版本。漂了就意味着"同一个假件在不同的检查里行为不同",
这种差异没人能一眼看出来。

两条规矩:

1. **只共享形状,不共享断言** —— 检查的逻辑留在脚本里;
2. **刻意的残缺留在脚本里,不往这里搬**。例:`check_m5_qa` 的假向量库故意只有 ``search`` ——
   "被测代码一旦越权调 ``upsert`` 就 AttributeError"本身就是那条检查要的证据。
   所以这里**同时**提供完整的 ``FakeVectorStore`` 和只有 ``search`` 的 ``SearchOnlyVectorStore``,
   用哪个由脚本选,不给默认。

同类的还有两个 LLM:`FakeLLM`(**调用即发生**)与 `PacedFakeLLM`(**生成器**,帧在被迭代时才产出)——
差别只有这一点,但延迟观测正是靠它钉"没有预取",不能合并。

域类型(``Embedding`` / ``FaqMatch`` / ``LlmDelta``)在方法里**局部 import**:放模块级的话,
``project_rag.domain`` 一旦有问题就是 import 期崩、整份报告一行都打不出来;局部 import 会让它
落进对应那条检查的 FAIL 里,看得见是哪一步没落地。
"""

from __future__ import annotations

import time
from typing import Any


class FakeEmbedder:
    """假嵌入器:回固定向量 + 记调用次数;``dim`` 只为满足端口形状。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def dim(self) -> int:
        return 2

    def embed(self, texts):
        from project_rag.domain import Embedding

        self.calls += 1
        return [Embedding(dense=(1.0, 0.0)) for _ in texts]


class FakeFaqStore:
    """假 FAQ 存储。``error`` 模拟 MySQL 连不上,``delay`` 模拟慢查询。"""

    def __init__(
        self, entries=(), *, error: Exception | None = None, delay: float = 0.0
    ) -> None:
        self.entries = list(entries)
        self.error = error
        self.delay = delay
        self.loads = 0

    def load_all(self):
        self.loads += 1
        if self.error is not None:
            raise self.error
        if self.delay:
            time.sleep(self.delay)
        return list(self.entries)


class FakeMatcher:
    """假匹配器。``none=True`` = 「什么都不像」,``error`` = 匹配器坏掉,``delay`` = 慢。"""

    def __init__(
        self,
        score: float | None = None,
        *,
        none: bool = False,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.score = score
        self.none = none
        self.error = error
        self.delay = delay
        self.calls = 0

    def best_match(self, query, candidates):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.none or self.score is None or not candidates:
            return None
        from project_rag.domain import FaqMatch

        return FaqMatch(entry=candidates[0], score=self.score)


class FakeKeywordIndex:
    """假关键字索引:回固定命中 + 记每次查询的 ``(k, source)``。``delay`` 模拟慢检索。"""

    def __init__(self, hits=(), *, delay: float = 0.0) -> None:
        self.hits = list(hits)
        self.delay = delay
        self.searches: list[tuple[int, str | None]] = []

    def search(self, query, *, k, source=None):
        self.searches.append((k, source))
        if self.delay:
            time.sleep(self.delay)
        return list(self.hits)


class FakeReranker:
    """假重排器:按候选顺序给递减分。只证明"重排器接下去了",不验排序质量(那是知识库那条契约的事)。"""

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, query, texts):
        self.calls += 1
        return [1.0 - 0.1 * index for index in range(len(texts))]


class FakeConversationStore:
    """内存会话历史 + 记录每次调用。

    ``record_error`` / ``read_error`` 用来验「存储坏了要原样上抛」;
    ``record`` 里顺手做一遍裁剪,好让「用例传下来的 keep 是多少」也能被看见。
    """

    def __init__(
        self,
        turns=None,
        *,
        record_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.turns: dict[str, list] = {key: list(value) for key, value in (turns or {}).items()}
        self.record_error = record_error
        self.read_error = read_error
        self.records: list[tuple[str, Any, int]] = []
        self.reads: list[tuple[str, int]] = []
        self.clears: list[str] = []

    def record(self, session_id, turn, *, keep: int) -> None:
        self.records.append((session_id, turn, keep))
        if self.record_error is not None:
            raise self.record_error
        bucket = self.turns.setdefault(session_id, [])
        bucket.append(turn)
        if len(bucket) > keep:
            del bucket[:-keep]

    def recent(self, session_id, *, limit: int):
        self.reads.append((session_id, limit))
        if self.read_error is not None:
            raise self.read_error
        return tuple(self.turns.get(session_id, [])[-limit:])

    def clear(self, session_id) -> int:
        self.clears.append(session_id)
        return len(self.turns.pop(session_id, []))


class FakeVectorStore:
    """假向量库(**完整端口形状**:7 个方法)。``delay`` 模拟慢检索,``loads`` 记 ``load_all`` 次数。

    需要"故意只有 search"的场合用 ``SearchOnlyVectorStore``(见模块 docstring)。
    """

    def __init__(self, hits=(), *, delay: float = 0.0) -> None:
        self.hits = list(hits)
        self.delay = delay
        self.loads = 0
        self.searches: list[tuple[int, str | None]] = []
        self.sparse_searches: list[tuple[dict, int, str | None]] = []
        self.upserted_sparse: list[dict] | None = None

    def search(self, query_embedding, *, k, source=None):
        self.searches.append((k, source))
        if self.delay:
            time.sleep(self.delay)
        return list(self.hits)

    def search_sparse(self, query_sparse, *, k, source=None):
        """稀疏那一路:假件按"命中就是全部 hits"给,顺序不变(真实排序由适配器负责)。"""
        self.sparse_searches.append((dict(query_sparse), k, source))
        return list(self.hits)

    def upsert(self, chunks, embeddings, *, sparse=None) -> int:
        # sparse 记下来:检查要断言"该带稀疏向量的那一路真的带上了"(见 check_m3_sparse)
        self.upserted_sparse = None if sparse is None else [dict(item) for item in sparse]
        return len(chunks)

    def delete_by_doc_id(self, doc_id: str) -> int:
        return 0

    def has_sparse_column(self) -> bool:
        """假件按「能接」回答:它没有 schema 这一层(要验「没有那一列」的子类覆盖它)。

        返回类型写死成 ``bool``:不写的话 pyright 会推断成 ``Literal[True]``,
        子类返回 False 时就报"不兼容的覆写"。
        """
        return True

    def load_all(self):
        self.loads += 1
        return [hit.chunk for hit in self.hits]

    def fingerprint(self):
        # 与真适配器同口径:子块 id + 父块 id 排序后的指纹(BM25 靠它判断"语料变没变")
        return sorted(f"{hit.chunk.chunk_id}\x1f{hit.chunk.parent_id}" for hit in self.hits)

    def chunk_counts(self):
        """按 doc_id 数一遍(与真适配器同口径):知识库清单用它回答"这份现在搜不搜得到"。"""
        counts: dict[str, int] = {}
        for hit in self.hits:
            counts[hit.chunk.doc_id] = counts.get(hit.chunk.doc_id, 0) + 1
        return counts

    def delete_by_source(self, source: str) -> int:
        return 0

    def drop(self) -> None:
        return None


class SearchOnlyVectorStore:
    """**只有 ``search``** 的假向量库 —— 残缺是刻意的。

    用来钉「这一层没有越权直接写库」:被测代码一旦调 ``upsert`` / ``delete_*``,
    这里就 AttributeError,而那条检查会红。
    """

    def __init__(self, hits=()) -> None:
        self.hits = list(hits)
        self.searches: list[tuple[int, str | None]] = []

    def search(self, query_embedding, *, k, source=None):
        self.searches.append((k, source))
        return list(self.hits)

    def search_sparse(self, query_sparse, *, k, source=None):
        """同 ``search``:只读那一路的第二种检索,不是写库,所以这里给。"""
        return list(self.hits)


class FakeLLM:
    """假 LLM:**调用即发生**(``stream`` 是普通函数,返回现成的迭代器)。

    这一点是刻意的 —— 写成生成器函数的话,「调了但没消费」与「压根没调」就分不开,
    而好几条检查正是靠 ``calls`` 区分这两件事。

    ``pieces`` 是逐帧吐的正文;``reply`` 是"整段当一帧"(两者给一个,``reply`` 优先)。
    ``usage`` 是"provider 在收尾帧上回报的用量":给 ``(模型, 输入, 输出)``
    就挂在 ``last=True`` 那一帧上 —— 与真适配器的形状一致(见 ``llm_deepseek._usage_of``)。
    """

    def __init__(
        self,
        pieces=("答", "案"),
        *,
        reply: str | None = None,
        finish_reason: str = "stop",
        usage: tuple[str, int, int] | None = None,
    ) -> None:
        # ``reply`` 是**可写属性**:有的检查构造完还要换一句(``llm.reply = "..."``),
        # 因为那一轮要区分「模型兜底的话」与「知识库的话」。给了 reply 就不再吐 pieces。
        self.reply = reply
        self.pieces = tuple(pieces) if reply is None else ()
        self.finish_reason = finish_reason
        self.usage = usage
        self.calls = 0
        self.seen: list[tuple[str, str]] = []

    @property
    def text(self) -> str:
        return self.reply if self.reply is not None else "".join(self.pieces)

    @property
    def prompts(self) -> list[str]:
        """收到的 user 消息(检查常拿它断"历史/正文进没进提示词")。"""
        return [content for role, content in self.seen if role == "user"]

    def frames(self):
        from project_rag.domain import LlmDelta, LlmUsage

        if self.reply is not None:
            yield LlmDelta(text=self.reply)
        else:
            for piece in self.pieces:
                yield LlmDelta(text=piece)
        # 收尾帧:端口吐的是帧,结束原因在这一帧上;
        # 用量也挂在这一帧(与真适配器同一个约定:收尾帧带 finish_reason + usage)
        model, input_tokens, output_tokens = self.usage or ("", 0, 0)
        yield LlmDelta(
            finish_reason=self.finish_reason,
            last=True,
            usage=(
                None
                if self.usage is None
                else LlmUsage(
                    model=model, input_tokens=input_tokens, output_tokens=output_tokens
                )
            ),
        )

    def stream(self, messages):
        self.calls += 1
        self.seen = [(message.role, message.content) for message in messages]
        return iter(list(self.frames()))


class PacedFakeLLM:
    """按节奏吐帧的假 LLM(**生成器**:帧在被迭代时才产出)。

    与 ``FakeLLM`` 只差这一点,但这一点不能省:延迟观测要钉「没有预取」——
    ``produced`` 数的是"已经产出几帧";``stream`` 若写成普通函数,所有帧在调用时就全出来了,
    "TTFT = 第一帧到达时刻"这件事就再也没法验。

    ``first_delay`` = provider 的首字延迟(第一帧之前的 sleep);
    ``gap`` = 每帧正文之间的间隔;``think_pieces`` = 正文**之前**吐的思考帧,``think_gap`` 是它们的间隔 ——
    这样 ``llm_think_ms``(到最后一帧思考)与 ``llm_ttft_ms``(到第一帧正文)之差恰好是一个 ``gap``,
    两个口径可以互相验算。
    """

    def __init__(
        self,
        pieces=("答", "案"),
        *,
        think_pieces=(),
        first_delay: float = 0.0,
        think_gap: float = 0.0,
        gap: float = 0.0,
        finish_reason: str = "stop",
        usage: tuple[str, int, int] | None = None,
    ) -> None:
        self.pieces = tuple(pieces)
        self.think_pieces = tuple(think_pieces)
        self.first_delay = first_delay
        self.think_gap = think_gap
        self.gap = gap
        self.finish_reason = finish_reason
        # 与 FakeLLM 同一个约定:``(模型, 输入, 输出)`` 挂在收尾帧上(见 domain.LlmUsage)
        self.usage = usage
        self.calls = 0
        self.produced = 0

    def stream(self, messages):
        from project_rag.domain import LlmDelta, LlmUsage

        self.calls += 1
        if self.first_delay:
            time.sleep(self.first_delay)
        for piece in self.think_pieces:
            if self.think_gap:
                time.sleep(self.think_gap)
            self.produced += 1
            # 思考走**另一个字段**(reasoning_text):与正文分开,理由见 LlmDelta 的注释
            yield LlmDelta(reasoning_text=piece)
        for piece in self.pieces:
            if self.gap:
                time.sleep(self.gap)
            self.produced += 1
            yield LlmDelta(text=piece)
        model, input_tokens, output_tokens = self.usage or ("", 0, 0)
        yield LlmDelta(
            finish_reason=self.finish_reason,
            last=True,
            usage=(
                None
                if self.usage is None
                else LlmUsage(
                    model=model, input_tokens=input_tokens, output_tokens=output_tokens
                )
            ),
        )


class NoFolders:
    """``DocumentStore`` 的**目录那半**的空实现 —— 给"只关心文档"的假件补端口形状。

    **为什么要有它**:目录(``documents.folder`` + ``document_folders``)是登记表的另一半,
    端口 ``DocumentStore`` 因此多了七个方法。而三十多份检查里有二十来处只写了文档那半的假件 ——
    每处补七个 no-op 会漂成二十个版本(这个项目已经为"同一段壳子抄很多遍"付过一次代价,
    见 ``_harness.py`` 的 docstring)。

    **为什么端口不拆成两个**:目录与文档在真适配器里是同一族表、同一次连接 ——
    "登记一行时顺带写目录"拆开就要跨适配器,那才是真的糟。

    **它什么都不记、也不报错**:这些假件所在的检查**不碰目录接口**。真碰了的话,读会得到空、
    写会静默成功 —— 所以**验目录的检查不许用它**(``check_m7_5_folders.py`` 里那份是完整实现)。
    """

    def list_folders(self):
        """输入:无。行为:没有目录。输出:空元组。"""
        return ()

    def get_folder(self, folder_id):
        """输入:目录 ID。行为:没有目录。输出:``None``。"""
        return None

    def folder_named(self, name):
        """输入:目录名。行为:没有目录。输出:``None``。"""
        return None

    def add_folder(self, folder) -> None:
        """输入:目录。行为:不做。输出:无。"""
        return None

    def update_folder(self, folder) -> None:
        """输入:目录。行为:不做。输出:无。"""
        return None

    def remove_folder(self, folder_id) -> int:
        """输入:目录 ID。行为:不做。输出:0。"""
        return 0

    def set_folder(self, document_id, folder_id) -> int:
        """输入:文档 ID + 目录 ID。行为:不做。输出:0。"""
        return 0
