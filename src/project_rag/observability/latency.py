"""
延迟观测:一次回答的 TTFT / tokens per second / 分段耗时,一行日志说清。

为什么要有它:用户体感到的「慢」可能是四件完全不同的事 —— ①FAQ 全表匹配、②检索
(嵌入 + 两路召回 + 融合 + 折叠父块 + 重排)、③模型首字(TTFT)、④模型吐字速度(tps)。
不分开量就只能猜,而排查延迟时「猜」是最贵的做法:改一个旋钮、跑一遍、还是慢。

三个口径写死在这里(**改口径 = 改契约**,`check_m5_5_latency.py` 钉住):

- ``ttft_ms``:从流开始到**第一帧非空 token** —— 用户看到第一个字的时刻;
- ``gen_ms`` :第一帧 token 到最后一帧 token 的墙钟(**不含** TTFT);
- ``tps``    :``tokens / gen_ms``;不足两帧时给 ``-``(单帧算不出速率,不编数)。

**思考模式**另外三个字段(M5.8,思考模型才有):

- ``thinking``    :配置档位(auto/off/low/high/max) —— **静态标签,不是测量值**;
- ``llm_think_ms``:``llm_start`` → **最后一帧思考帧**;一帧思考都没看到给 ``-``;
- ``think_tokens``:**思考帧数**;``0`` 是「这一轮没看到思考」,不是「没量到」。

`llm_think_ms` 与 `llm_ttft_ms` **同一个原点**(``llm_start``),所以能直接相减:
差值 = 「想完到吐出第一个字」的空档。判断「没想」和「没调模型」的分界是 `llm_ttft_ms`:
它是 ``-`` ⟺ 压根没调 provider(FAQ 命中、检索为空拒答)。

``tokens`` 数的是**非空 token 帧的个数**。DeepSeek 的 SSE 一个 chunk 通常就是一个 token,
所以这是个够用的近似;要精确值得引 tokenizer(那是另一件事,见 `deferred.md`)。
``think_tokens`` 同理数思考帧 —— **思考帧绝不计入 ``tokens``**:混进去会让「答案多少字」
凭空变大,还会让 TTFT 提前到思考开始那一刻,而那正是这个指标要回答的问题。

**为什么不用 contextvars 传账本**:帧流是同步生成器,会在线程池的不同线程里被 ``next()``
(api 的 ``_in_thread``),contextvar 的写入会随调用上下文丢失 —— 那会得到「有时有账、
有时没账」的日志,比没有日志更坏。显式传一个可变对象最不容易骗自己。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .logging_setup import get_logger

_logger = get_logger("observability.latency")

# 一行日志里的字段顺序就是这张表。顺序固定,awk/grep 才敢按列取。
_FIELDS = (
    "label",
    "channel",
    "thinking",
    "faq_ms",
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


def _ms(value: float | None) -> str:
    """输入:毫秒数或 None。行为:渲染成一位小数。输出:文本;没量到给 ``-``(不编数)。"""
    return _NONE if value is None else f"{value:.1f}"


def _flag(value: bool) -> str:
    """输入:布尔。行为:渲染成 ``true``/``false``。输出:文本。"""
    return "true" if value else "false"


@dataclass(slots=True)
class StreamTiming:
    """一次回答的计时账本 —— 可变、显式传递(理由见模块 docstring)。

    打点用 ``mark(名字)``,名字是自由字符串,当前约定这几个:

    - ``"faq"``:FAQ 匹配那一段结束(合流入口的第一个动作);
    - ``"sources"``:不用手动打 —— ``streaming.timed`` 收到 sources 帧时自动记;
    - ``"retrieve"``:整个检索返回(``stream_rag`` 里 ``retrieve()`` 的下一步);
    - ``"embed"`` / ``"vector"`` / ``"bm25"`` / ``"fuse"`` / ``"collapse"`` / ``"rerank"``:
      检索内部的六段(查询嵌入 → 向量检索 → 关键字那一路 → 融合 → 折叠父块 → 重排),
      由 ``retrieve()`` 打 —— 它们让「检索 1.8s 到底花在哪」不用猜;
    - ``"llm_start"``:提示词已拼好、准备调 provider(``llm_ttft_ms`` 与 ``llm_think_ms`` 的**起点**)。
    """

    label: str
    channel: str | None = None
    # 配置档位。**账本不认识配置**:组合根读 config,把「这一轮按哪一档跑的」交进来,
    # 没交就是 None → 渲染成 '-'。在这里 import config 会把可观测层焊到配置上(分层也不许)。
    thinking: str | None = None
    started: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)
    first_token: float | None = None
    last_token: float | None = None
    tokens: int = 0
    first_thought: float | None = None
    last_thought: float | None = None
    think_tokens: int = 0
    refused: bool = False
    truncated: bool = False
    done: bool = False
    _logged: bool = False

    def mark(self, name: str) -> None:
        """输入:打点名。行为:记下**第一次**到达该点的时刻(重复来不改它)。输出:无。"""
        self.marks.setdefault(name, time.perf_counter())

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

    @property
    def aborted(self) -> bool:
        """输入:无。行为:判断这一轮是不是没等到收尾帧。输出:中断(客户端断开/上游抛错)为真。"""
        return not self.done

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
        """输入:打点名。行为:算「从本段开始到该打点」。输出:毫秒;没打过给 None。"""
        at = self.marks.get(name)
        return None if at is None else (at - self.started) * 1000.0

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

    def line(self) -> str:
        """输入:无。行为:把账本渲染成一行 ``key=value``(字段与顺序固定)。输出:这一行文本。

        每个字段的口径:``faq_ms``/``sources_ms``/``retrieve_ms``/``embed_ms`` 是**从本段开始**
        到该打点(``retrieve_ms`` 是检索总时长;``embed_ms`` 是检索的第一件事,所以累计=本段时长);
        ``vector_ms``/``bm25_ms``/``fuse_ms``/``collapse_ms``/``rerank_ms`` 是**相邻两个打点之差**
        (段耗时,六段之和 ≈ ``retrieve_ms``);``llm_think_ms`` 是 ``llm_start`` 到**最后一帧思考**;
        ``llm_ttft_ms`` 是 ``llm_start`` 到第一帧 token;``think_tokens``/``tokens`` 是帧数;
        ``thinking`` 是配置档位(唯一的字符串字段之一,与 ``label``/``channel`` 同类);
        ``ttft_ms``/``total_ms`` 从本段开始算。拿不到的一律 ``-`` —— 「没量到」和「量到 0」必须区分得开。
        """
        values = {
            "label": self.label,
            "channel": self.channel or _NONE,
            "thinking": self.thinking or _NONE,
            "faq_ms": _ms(self._offset("faq")),
            "sources_ms": _ms(self._offset("sources")),
            "retrieve_ms": _ms(self._offset("retrieve")),
            "embed_ms": _ms(self._offset("embed")),
            "vector_ms": _ms(self._gap("embed", "vector")),
            "bm25_ms": _ms(self._gap("vector", "bm25")),
            "fuse_ms": _ms(self._gap("bm25", "fuse")),
            "collapse_ms": _ms(self._gap("fuse", "collapse")),
            "rerank_ms": _ms(self._gap("collapse", "rerank")),
            "llm_think_ms": _ms(self.llm_think_ms),
            "llm_ttft_ms": _ms(self._since("llm_start", self.first_token)),
            "think_tokens": str(self.think_tokens),
            "ttft_ms": _ms(self.ttft_ms),
            "tokens": str(self.tokens),
            "gen_ms": _ms(self.gen_ms),
            "tps": _ms(self.tps),
            "total_ms": _ms(self.total_ms),
            "refused": _flag(self.refused),
            "truncated": _flag(self.truncated),
            "aborted": _flag(self.aborted),
        }
        return " ".join(f"{name}={values[name]}" for name in _FIELDS)

    def emit(self) -> None:
        """输入:无。行为:记一条 INFO 日志,内容就是 ``line()``(**只记一次**)。输出:无。

        幂等是刻意的:正常收尾和中断/异常是两条路径,都会调它;重复调用不该刷两条账。
        级别用 INFO:控制台只放行 INFO 及以上(见 ``config.yaml``),否则「跑一遍看延迟」
        还得先去翻文件。
        """
        if self._logged:
            return
        self._logged = True
        _logger.info("latency %s", self.line())
