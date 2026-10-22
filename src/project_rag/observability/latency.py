"""
延迟观测:一次回答的 TTFT / tokens per second / 分段耗时,一行日志说清。

为什么要有它:用户体感到的「慢」可能是四件完全不同的事 —— ①FAQ 全表匹配、②检索
(嵌入 + 两路召回 + 融合 + 折叠父块 + 重排)、③模型首字(TTFT)、④模型吐字速度(tps)。
不分开量就只能猜,而排查延迟时「猜」是最贵的做法:改一个旋钮、跑一遍、还是慢。

三个口径写死在这里(**改口径 = 改契约**):

- ``ttft_ms``:从流开始到**第一帧非空 token** —— 用户看到第一个字的时刻;
- ``gen_ms`` :第一帧 token 到最后一帧 token 的墙钟(**不含** TTFT);
- ``tps``    :``tokens / gen_ms``;不足两帧时给 ``-``(单帧算不出速率,不编数)。

**思考模式**另外三个字段(思考模型才有):

- ``thinking``    :配置档位(auto/off/low/high/max) —— **静态标签,不是测量值**;
- ``llm_think_ms``:``llm_start`` → **最后一帧思考帧**;一帧思考都没看到给 ``-``;
- ``think_tokens``:**思考帧数**;``0`` 是「这一轮没看到思考」,不是「没量到」。

`llm_think_ms` 与 `llm_ttft_ms` **同一个原点**(``llm_start``),所以能直接相减:
差值 = 「想完到吐出第一个字」的空档。判断「没想」和「没调模型」的分界是 `llm_ttft_ms`:
它是 ``-`` ⟺ 压根没调 provider(FAQ 命中、检索为空拒答)。

``tokens`` 数的是**非空 token 帧的个数**。DeepSeek 的 SSE 一个 chunk 通常就是一个 token,
所以这是个够用的近似;要精确值得引 tokenizer(那是另一件事,目前不做)。
``think_tokens`` 同理数思考帧 —— **思考帧绝不计入 ``tokens``**:混进去会让「答案多少字」
凭空变大,还会让 TTFT 提前到思考开始那一刻,而那正是这个指标要回答的问题。

**``tokens`` 不是账单上的 token**:前者是帧数(给 tps 用的近似),后者是 provider 回报的
计费口径(``llm_model`` / ``llm_input_tokens`` / ``llm_cached_input_tokens`` /
``llm_output_tokens``,见 ``LlmUsage``)。
两个都在账上、两个都不许互相替代 —— 拿帧数去乘单价,金额会偏(思考帧、缓存命中的 input
都不在帧数里);反过来拿计费 token 去算 tps,速率会虚高。报表用的是后三个。

**为什么不用 contextvars 传账本**:帧流是同步生成器,会在线程池的不同线程里被 ``next()``
(api 的 ``_in_thread``),contextvar 的写入会随调用上下文丢失 —— 那会得到「有时有账、
有时没账」的日志,比没有日志更坏。显式传一个可变对象最不容易骗自己。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .logging_setup import get_logger

_logger = get_logger("observability.latency")

# 一行日志里的字段顺序就是这张表。顺序固定,awk/grep 才敢按列取。
_FIELDS = (
    "label",
    "request_id",
    "channel",
    "thinking",
    "cold",
    "builds",
    "faq_ms",
    "rewrite_ms",
    "sources_ms",
    "retrieve_ms",
    "embed_ms",
    "vector_ms",
    "bm25_ms",
    "fuse_ms",
    "collapse_ms",
    "rerank_ms",
    "llm_think_ms",
    "llm_ttft_ms",
    "think_tokens",
    "llm_model",
    "llm_input_tokens",
    "llm_cached_input_tokens",
    "llm_output_tokens",
    "ttft_ms",
    "tokens",
    "gen_ms",
    "tps",
    "total_ms",
    "refused",
    "truncated",
    "aborted",
)
_NONE = "-"
# 吐字窗口的下限:短于它算出来的「速率」是噪声(几帧挤在同一毫秒里到手时的产物)
_MIN_GEN_MS = 1.0

# 这几个打点问的是「**检索**那一段花了多久」,所以开了查询改写之后,它们的起点是
# **改写结束的时刻**,不是开账时刻:一次 LLM 改写能花掉一秒多,把它算进 `retrieve_ms`,
# 这个数就再也回答不了"检索本身有没有变慢"。改写自己花了多久由 `rewrite_ms` 单独报。
# 没改写时这些点压根不存在,起点自然还是开账时刻。
_AFTER_REWRITE = ("embed", "retrieve")

# ── 冷启动证据 ───────────────────────────────────────────────────────────────
# 组合根里那四个 `@lru_cache` 重资源工厂(嵌入 / 重排 / 向量库 / BM25 索引)**每次真正构造**
# 时调一次 `note_build()`。为什么需要它:账本原来分不清「这一轮慢」是慢在服务本身还是慢在
# 「这一轮顺手把 2G 的模型加载了」。实测过这个形状 —— 28 条样本里 rerank 中位 668ms
# 而 p95 **5383ms**(8 倍),不分层的话 p95 全是冷启动的噪声,别处省下的那点时间会被淹掉。
# 判据用「本次请求期间构造过重资源」而不是「进程启动后第几个请求」:预热(warmup)会把
# 冷启动挪到启动阶段,那时计数器已经涨过了,所以第一个真实请求就是 warm —— 与我们想要的
# 语义一致(该请求确实没有为用户付加载时间)。
_builds: list[str] = []


def note_build(name: str) -> None:
    """输入:资源名(``embedder`` / ``reranker`` / ``vector_store`` / ``keyword_index``)。

    行为:记一次「真的构造了」。输出:无。**只该由组合根的重资源工厂调用。**
    """
    _builds.append(name)


def build_count() -> int:
    """输入:无。行为:本进程累计构造过几次重资源。输出:次数。"""
    return len(_builds)


def build_names() -> tuple[str, ...]:
    """输入:无。行为:构造过的资源名(按发生顺序,可重复)。输出:元组。"""
    return tuple(_builds)



def _ms(value: float | None) -> str:
    """输入:毫秒数或 None。行为:渲染成一位小数。输出:文本;没量到给 ``-``(不编数)。"""
    return _NONE if value is None else f"{value:.1f}"


def _flag(value: bool) -> str:
    """输入:布尔。行为:渲染成 ``true``/``false``。输出:文本。"""
    return "true" if value else "false"


def _count(value: int | None) -> str:
    """输入:整数或 None。行为:渲染成文本。输出:文本;没量到给 ``-``(不编 0)。

    **``0`` 与 ``-`` 必须分得开**:0 = 真的一个 token 都没用(空回答),
    ``-`` = provider 没告诉我们 / 这一路压根没调模型。折成 0 会让"少报了多少"看不见。
    """
    return _NONE if value is None else str(value)


@dataclass(slots=True)
class StreamTiming:
    """一次回答的计时账本 —— 可变、显式传递(理由见模块 docstring)。

    打点用 ``mark(名字)``,名字是自由字符串,当前约定这几个:

    - ``"faq"``:FAQ 匹配那一段结束(合流入口的第一个动作);
    - ``"rewrite"``:查询改写结束(知识库通道的第一个动作, **配了改写才有这个点**)。
      它是 ``rewrite_ms`` 的定义,同时也是 ``embed``/``retrieve`` 两个点的起点(见
      ``_AFTER_REWRITE``):那两格问的是"检索花了多久",不该把一次 LLM 改写算进去;
    - ``"sources"``:不用手动打 —— ``streaming.timed`` 收到 sources 帧时自动记;
    - ``"retrieve"``:整个检索返回(``stream_rag`` 里 ``retrieve()`` 的下一步);
    - ``"embed"`` / ``"vector"`` / ``"bm25"`` / ``"fuse"`` / ``"collapse"`` / ``"rerank"``:
      检索内部的六段(查询嵌入 → 向量检索 → 关键字那一路 → 融合 → 重排 → 按父块折叠),
      由 ``retrieve()`` 打 —— 它们让「检索 1.8s 到底花在哪」不用猜。
      ``"vector"``/``"bm25"`` 这两路**并行**跑,所以那两格的分段值由各路 ``span()`` 自报
      (相邻打点之差在这里量不出各自时长,见 ``span``);两条打点仍在召回段结束时落下,
      它们只负责让后面几段的差值接得上;
    - ``"llm_start"``:提示词已拼好、准备调 provider(``llm_ttft_ms`` 与 ``llm_think_ms`` 的**起点**)。
    """

    label: str
    # 一次请求的追踪号:**默认自己生成**,CLI / API / 任何调用方都不必记得传
    # (漏传的后果是「日志里有账,但对不上是哪一次请求」—— 而那正是排障时最需要的一条)。
    # API 会把自己那一份交进来,好让响应头 `X-Request-Id` 与这条账对得上。
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    channel: str | None = None
    # 配置档位。**账本不认识配置**:组合根读 config,把「这一轮按哪一档跑的」交进来,
    # 没交就是 None → 渲染成 '-'。在这里 import config 会把可观测层焊到配置上(分层也不许)。
    thinking: str | None = None
    # 开账时刻的构造计数:收账时一比就知道这一轮有没有为冷启动买单(见 note_build)。
    builds0: int = field(default_factory=build_count)
    started: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)
    # 各路**自己报**的段耗时(毫秒)。只有并行跑的那两路需要它:相邻打点之差量不出
    # "谁花了多久"(见 ``span``)。空着就退回打点之差 —— 单路那几条老路一字不变。
    spans: dict[str, float] = field(default_factory=dict)
    first_token: float | None = None
    last_token: float | None = None
    tokens: int = 0
    first_thought: float | None = None
    last_thought: float | None = None
    think_tokens: int = 0
    # provider 回报的用量(**只在真调了模型的那一行上有值**,见 cost.py 的判据)。
    # 模型名单独存一份:单价是按模型配的,而"这次到底是哪个模型答的"只有 provider 知道
    # (配置里写的是我们**要求**它用哪个,两者不一定一样)。
    llm_model: str | None = None
    input_tokens: int | None = None
    # 输入里命中缓存的那部分(provider 的 prompt cache)。**单价与未命中差 50 倍**,
    # 所以它必须单独记账;没回报时是 None(渲染成 ``-``),不是 0。
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    refused: bool = False
    truncated: bool = False
    done: bool = False
    _logged: bool = False

    def mark(self, name: str) -> None:
        """输入:打点名。行为:记下**第一次**到达该点的时刻(重复来不改它)。输出:无。

        **线程安全**这件事只说一遍:时刻先取好,落库是 ``dict.setdefault`` 一次原子调用
        (CPython 的 dict 操作不会被线程切换打断),所以 ``answer_rag`` 那两路子线程
        各报各的名字不会互相踩;同名以先到的为准 —— 与串行时"只认第一次"是同一个口径。
        """
        self.marks.setdefault(name, time.perf_counter())

    def span(self, name: str, ms: float) -> None:
        """输入:段名 + 这一段的耗时(毫秒)。行为:记下**第一次**报来的值(重复来不改)。输出:无。

        为什么要有它:``vector`` 与 ``bm25`` 两路**并行**跑(见 ``answer_rag._gather``),
        相邻两个打点之差在这里量不出各自的时长 —— 先结束的那一路会被算成负数;而不管怎么
        摆那两个点,融合那一段都会把"等另一路"的时间吸进去。改由**各路自己报自己的时长**,
        ``values()`` 优先取这里的值,取不到才退回打点之差。
        """
        self.spans.setdefault(name, ms)

    def token(self, text: str) -> None:
        """输入:一帧 token 的文本。

        行为:非空才算一个 token(空帧是 provider 的收尾噪声,见 ``streaming.py``);
              第一帧非空 token 定下 TTFT,每一帧都推后末帧时刻。
        输出:无。
        """
        if not text:
            return
        now = time.perf_counter()
        if self.first_token is None:
            self.first_token = now
        self.last_token = now
        self.tokens += 1

    def think(self, text: str) -> None:
        """输入:一帧**思考**文本(``reasoning_content``)。

        行为:非空才算一帧;第一帧定下思考开始,每帧推后思考结束。
        输出:无。

        **为什么不复用 ``token()``**:两者数的是完全不同的东西。思考帧混进 ``tokens``
        会让「答案有多少 token」凭空变大,还会把 TTFT 提前到思考开始那一刻 ——
        而 TTFT 存在的意义恰恰是量「用户等第一个字等了多久」。
        传进来的文本本身不存(账本只记时刻与条数,不留内容,免得日志里出现模型的草稿)。
        """
        if not text:
            return
        now = time.perf_counter()
        if self.first_thought is None:
            self.first_thought = now
        self.last_thought = now
        self.think_tokens += 1

    def finish(self, *, channel: str | None, refused: bool, truncated: bool) -> None:
        """输入:收尾帧的字段。行为:记下结局(done 到了就不算中断)。输出:无。"""
        if channel:
            self.channel = channel
        self.refused = refused
        self.truncated = truncated
        self.done = True

    def usage(
        self,
        *,
        model: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
    ) -> None:
        """输入:provider 回报的一次用量(模型名 + 三个 token 数)。行为:记下来。输出:无。

        **收的是三个基本类型,不是一个 ``LlmUsage`` 对象**:账本是纯计时的,
        它只 import 标准库(那条分层法律有契约盯着)。
        让 ``application`` 那一层把领域对象拆开交进来,账本因此不认识 domain。
        

        **只在"真的调了模型"的那一层调它**(见 ``application/answer_rag.py``):
        一次请求只落一行账,用量就记在那一行上;同一轮记两遍就是双倍计入金额,
        而那种错在报表上看起来只是"花得有点多"。

        重复调用时**后来的覆盖先前的**:provider 是在流末尾给的这一个数,
        而"末尾那个"才是完整的一次调用(前面若有中间回报,通常是增量)。
        """
        if model:
            self.llm_model = model
        if input_tokens is not None:
            self.input_tokens = input_tokens
        if cached_input_tokens is not None:
            self.cached_input_tokens = cached_input_tokens
        if output_tokens is not None:
            self.output_tokens = output_tokens

    @property
    def aborted(self) -> bool:
        """输入:无。行为:判断这一轮是不是没等到收尾帧。输出:中断(客户端断开/上游抛错)为真。"""
        return not self.done

    @property
    def builds(self) -> int:
        """输入:无。行为:这一轮期间构造过几次重资源(嵌入/重排/向量库/BM25 索引)。输出:次数。

        ``0`` = 这一轮**没有为用户加载模型** —— 也就是"热"的那一档。
        """
        return build_count() - self.builds0

    @property
    def cold(self) -> bool:
        """输入:无。行为:判断这一轮冷不冷。输出:这一轮构造过重资源为真。

        **别用「进程启动后第几个请求」当判据**:预热(warmup)会把加载挪到启动阶段,
        那时计数器已经涨过,所以第一个真实请求就该是热的 —— 这正是我们要的语义。
        """
        return self.builds > 0

    @property
    def ttft_ms(self) -> float | None:
        """输入:无。行为:算第一个字的延迟。输出:毫秒;一帧 token 都没出给 None。"""
        if self.first_token is None:
            return None
        return (self.first_token - self.started) * 1000.0

    @property
    def llm_think_ms(self) -> float | None:
        """输入:无。行为:算思考窗口(``llm_start`` → **最后一帧**思考)。输出:毫秒;没看到思考帧给 None。

        **为什么原点取 ``llm_start`` 而不是第一帧思考**:取第一帧思考的话,「思考之前的等待」
        (排队 + 预填充)就被划到窗口外面去了,而用户等的正是这段总和;同一个原点还有个好处 ——
        它和 ``llm_ttft_ms`` 可以直接相减,差值就是「想完到吐出第一个字」之间的空档。

        返回 None(渲染成 ``-``)表示**一帧思考都没看到**,不是「量失败了」:它和
        ``llm_ttft_ms='-'``(压根没调模型)是两件事,三种组合的读法见模块 docstring。
        """
        return self._since("llm_start", self.last_thought)

    @property
    def gen_ms(self) -> float | None:
        """输入:无。行为:算吐字窗口(首帧 token → 末帧 token)。输出:毫秒;不足两帧给 None。

        不足两帧时**不给 0**:单帧答不出速率,报成 0ms 会被读成「快得没边」。
        """
        if self.tokens < 2 or self.first_token is None or self.last_token is None:
            return None
        return (self.last_token - self.first_token) * 1000.0

    @property
    def tps(self) -> float | None:
        """输入:无。行为:算 tokens per second。输出:速率;算不出来给 None(不编数)。

        三种「算不出来」:不足两帧(单帧没有窗口)、窗口非正、窗口 < 1ms ——
        最后那种是 SSE 成串交付(几帧在同一毫秒里到手)时的产物,拿它算速率会得到
        几十万的假数字。**_MIN_GEN_MS 是刻意的下限,不是随手写的**。
        """
        gen_ms = self.gen_ms
        if self.tokens < 2 or gen_ms is None or gen_ms < _MIN_GEN_MS:
            return None
        return self.tokens * 1000.0 / gen_ms

    @property
    def total_ms(self) -> float | None:
        """输入:无。行为:算从开始到现在。输出:毫秒。"""
        return (time.perf_counter() - self.started) * 1000.0

    def _offset(self, name: str) -> float | None:
        """输入:打点名。行为:算「从本段开始到该打点」。输出:毫秒;没打过给 None。

        「本段开始」对 ``_AFTER_REWRITE`` 里那几个点是**改写结束的时刻**(理由见那个常量的注释);
        其余字段一律从开账算起。
        """
        at = self.marks.get(name)
        if at is None:
            return None
        origin = self.started
        if name in _AFTER_REWRITE:
            origin = self.marks.get("rewrite", origin)
        return (at - origin) * 1000.0

    def _since(self, name: str, at: float | None) -> float | None:
        """输入:打点名 + 另一个时刻。行为:算两点之间的间隔。输出:毫秒;缺任一端给 None。"""
        from_mark = self.marks.get(name)
        if from_mark is None or at is None:
            return None
        return (at - from_mark) * 1000.0

    def _gap(self, start: str, end: str) -> float | None:
        """输入:两个打点名。行为:算它们之间的间隔(**段耗时**)。输出:毫秒;缺任一端给 None。"""
        first, second = self.marks.get(start), self.marks.get(end)
        if first is None or second is None:
            return None
        return (second - first) * 1000.0

    def _segment(self, name: str, start: str, end: str) -> float | None:
        """输入:段名 + 该段两端的打点名。

        行为:段耗时 —— **自己报过就用报的**(``span()``,并行的那两路),没报过才用打点之差。
        输出:毫秒;两边都缺给 None。
        """
        reported = self.spans.get(name)
        if reported is not None:
            return reported
        return self._gap(start, end)

    def values(self) -> dict[str, str]:
        """输入:无。行为:把账本渲染成 ``{字段名: 文本值}``(顺序即 ``_FIELDS``)。输出:字典。

        ``line()`` 与落表**共用这一份**:两处各渲染一次迟早会漂(日志里有、表里没有)。

        每个字段的口径:``faq_ms``/``rewrite_ms``/``sources_ms``/``retrieve_ms``/``embed_ms`` 是**从本段开始**
        到该打点(``retrieve_ms`` 是检索总时长;``embed_ms`` 是检索的第一件事,所以累计=本段时长);
        ``embed_ms``/``retrieve_ms`` 有一个例外:开了查询改写时它们从**改写结束**算起
        (见 ``_AFTER_REWRITE``),``rewrite_ms`` 单独报改写那段;
        ``vector_ms``/``bm25_ms`` 是**这两路各自报的自己的时长**(两路并行跑,会重叠,
        所以六段之和**可以大于** ``retrieve_ms``;自己没报时退回相邻打点之差);
        ``fuse_ms``/``rerank_ms``/``collapse_ms`` 是**相邻两个打点之差**
        (段耗时,顺序是融合 → 重排 → 按父块折叠,和 ≈ ``retrieve_ms`` 减去两路召回那一段);``llm_think_ms`` 是 ``llm_start`` 到**最后一帧思考**;
        ``llm_ttft_ms`` 是 ``llm_start`` 到第一帧 token;``think_tokens``/``tokens`` 是帧数;
        ``llm_model``/``llm_input_tokens``/``llm_cached_input_tokens``/``llm_output_tokens`` 是
        **provider 回报的计费口径**(没调模型或没回报时是 ``-``,报表按它们算钱,
        见 ``application/cost.py``;缓存命中的输入单价与未命中差 50 倍,所以它是单独一列);
        ``thinking`` 是配置档位(唯一的字符串字段之一,与 ``label``/``channel`` 同类);
        ``ttft_ms``/``total_ms`` 从本段开始算。拿不到的一律 ``-`` —— 「没量到」和「量到 0」必须区分得开。
        """
        values = {
            "label": self.label,
            "request_id": self.request_id,
            "channel": self.channel or _NONE,
            "thinking": self.thinking or _NONE,
            "cold": _flag(self.cold),
            "builds": str(self.builds),
            "faq_ms": _ms(self._offset("faq")),
            # 改写耗时 = 开账 → 改写结束(它是知识库通道里的第一件事)。没开改写就是 '-'
            "rewrite_ms": _ms(self._offset("rewrite")),
            "sources_ms": _ms(self._offset("sources")),
            "retrieve_ms": _ms(self._offset("retrieve")),
            "embed_ms": _ms(self._offset("embed")),
            "vector_ms": _ms(self._segment("vector", "embed", "vector")),
            "bm25_ms": _ms(self._segment("bm25", "vector", "bm25")),
            "fuse_ms": _ms(self._gap("bm25", "fuse")),
            # 顺序:融合 → 重排 → 按父块折叠。没装重排器时没有 rerank 打点,
            # 折叠直接从融合之后算起。
            "rerank_ms": _ms(self._gap("fuse", "rerank")),
            "collapse_ms": _ms(
                self._gap("rerank", "collapse")
                if "rerank" in self.marks
                else self._gap("fuse", "collapse")
            ),
            "llm_think_ms": _ms(self.llm_think_ms),
            "llm_ttft_ms": _ms(self._since("llm_start", self.first_token)),
            "think_tokens": str(self.think_tokens),
            # 计费口径的三个字段(不是帧数,见模块 docstring)
            "llm_model": self.llm_model or _NONE,
            "llm_input_tokens": _count(self.input_tokens),
            "llm_cached_input_tokens": _count(self.cached_input_tokens),
            "llm_output_tokens": _count(self.output_tokens),
            "ttft_ms": _ms(self.ttft_ms),
            "tokens": str(self.tokens),
            "gen_ms": _ms(self.gen_ms),
            "tps": _ms(self.tps),
            "total_ms": _ms(self.total_ms),
            "refused": _flag(self.refused),
            "truncated": _flag(self.truncated),
            "aborted": _flag(self.aborted),
        }
        return values

    def line(self) -> str:
        """输入:无。行为:把 ``values()`` 渲染成一行 ``key=value``(字段与顺序固定)。输出:这一行文本。"""
        return self._render(self.values())

    @staticmethod
    def _render(values: dict[str, str]) -> str:
        """输入:字段字典。行为:按 ``_FIELDS`` 的顺序拼成一行。输出:文本。"""
        return " ".join(f"{name}={values[name]}" for name in _FIELDS)

    def emit(self) -> None:
        """输入:无。行为:记一条 INFO 日志(**只记一次**),并把同一份字段交给落表 handler。输出:无。

        幂等是刻意的:正常收尾和中断/异常是两条路径,都会调它;重复调用不该刷两条账。
        级别用 INFO:控制台只放行 INFO 及以上(见 ``config.yaml``),否则「跑一遍看延迟」
        还得先去翻文件。

        **日志与落表共用同一份 ``values()``**(只渲染一次):否则 `total_ms` 这种"现算"的字段
        会在日志里和表里差几毫秒 —— 两边对不上时,查的人不知道该信哪个。
        落表靠 ``extra={"ledger": …}``:handler 只认这个键,所以**任何**走账本的地方
        (CLI / HTTP / WS)都自动落表,不需要在每个调用点各接一次。
        """
        if self._logged:
            return
        self._logged = True
        values = self.values()
        _logger.info("latency %s", self._render(values), extra={"ledger": values})
