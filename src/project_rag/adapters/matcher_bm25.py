"""
BM25 匹配适配器:jieba 分词 + BM25 打分 + 自身分归一化成 0~1 + 两道覆盖率闸门 + 索引复用。

分数口径 = top1 的 BM25 原始分 ÷「同一条问题问自己」的 BM25 原始分（夹到 0~1），再加上
两道**覆盖率**闸门（**下界**在调用方：``answer_faq`` 拿 ``score >= threshold`` 判命中）：:

    命中 ⟺ 置信度 ≥ threshold
           且 提问覆盖率 ≥ min_query_coverage     （用户问的词，这条答得了多少）
           且 条目覆盖率 ≥ min_entry_coverage     （这条问的词，用户提了多少）

**为什么要覆盖率**（实测的那次翻车）：FAQ 表里有一条**两个字**的条目 `协程`，于是
「线程、进程、协程的对比」被它答掉，阈值调到 0.98 也没用。算一下分子就清楚了 ——
``rank_bm25`` 的 ``BM25Plus`` 对**每一个查询词**都加 ``idf(q)·delta``，**哪怕这个词在这条文档里
一次都没出现**（它实现的是"每个查询词给一个底分"，不是论文里"只给命中的词"）::

    问「线程、进程、协程的对比」的 top1 = 「协程」
      词      库内 df   idf    在 top1 里   贡献
      线程        3    3.919      否        3.919   ← 底分
      进程        0    0.000      否        0.000
      协程        1    5.017      是       13.513   ← 唯一真命中
      的         76    0.687      否        0.687   ← 底分
      对比        1    5.017      否        5.017   ← 底分
    分子 23.136 / 分母(自身分) 13.513 = 1.712 —— 而这条提问与「协程」的关系其实只有"含这个词"

三件事叠在一起才这么糟：① 提问里"别处出现过、这条没有"的词白送底分；② 置信度是"分子 ÷
这条问自己的分"，短条目的自身分极低（两个字、``|d|=1``），随便一点底分就顶穿；
③ 比值夹回 1.0 之后，1.712 与 1.000 记成同一个数 —— **光靠阈值分不开**。
覆盖率正是对着 ① 和 ②：提问里出现而这条答不了的词越多，提问覆盖率越低（门槛拦它）。

**为什么只留两道覆盖率、不加"上界"**（也量过）：上界要跟比值的**绝对大小**比，而比值随语料规模漂
（``idf=ln((N+1)/df)``）—— 实测库 150 → 300 → 617 → 1324 条时「请问，<原问题>」的最高未剪辑比值
1.000 → 1.407 → 1.334 → 1.311，`score_ceiling=1.10` 会把**合法提问**的放行率从 100% 砍到
60%/69%/71%（假阴性）。而"夹回 0~1 + 只留两道覆盖率"在 150/300/617 三种规模上与"不夹取 + 上界"
**逐项相同**（一字不差与礼貌语全放行、无关问法与留出题误答数一样）—— 所以只留这两道：
少一个旋钮、少一处漂移源，分数也回到 0~1。

**为什么用这个参照系,而不是「top1 领先第二名的幅度」**:
  - margin 度量的是区分度,不是相似度。它的病是**分数随语料冗余度衰减**:库里只要
    有一条近似重复的问法,完全一致的问题也会掉到 0.14,永远越不过阈值;而且条目越多
    分数越低,阈值被迫随语料规模漂移,根本标不出来(实测 N=3 时 0.411 → N=50 时 0.335,
    同时无关问法最高能到 0.481 —— 阈值区间是空集)。
  - 「问自己」的参照系稳定:分子分母同源,语料变大时两者一起变。实测一字不差 = 1.000
    (与语料规模无关)、改写问法 0.2~0.7、完全无关 ≤ 0.274(N=50)。
    但注意它只是让阈值有意义,不是让 FAQ 能理解改写 —— 改写问法归知识库通道(向量检索)。

真实语料复核(N=463 条):
  一字不差 1.000 / 无关问法 ≤ 0.303 / 库内近邻(次高分占比)中位 0.631、最高 0.912。

已知边界(不是 bug,是分工):
  - 多意图提问（「…，另外帮我写一首诗」）在**长问题**上照旧命中（尾缀把提问覆盖率稀释得不够多），
    在**短问题**上会被覆盖率门槛拦下、交给知识库通道 —— 拦下是安全的那一侧。
  - 它度量的是「像不像原句」,不是「意思等不等价」。同义改写分数会掉到 0.2~0.7 被拦掉,
    这是**故意的**:FAQ 只答近乎一字不差的问题,改写交给知识库通道。
  - 符号型问题（`&和&&的区别`、`==和equals`、`#{}和${}`）在分词那一步就把符号丢了
    （`tokens()` 只留含 ``\\w`` 的词），所以它们的索引只剩「和/的/区别」这类虚词 ——
    任何「X 和 Y 的区别」都可能蹭到它们。修法在分词口径上（要连带重算稀疏向量），
    目前不做。
"""
from collections.abc import Sequence

from rank_bm25 import BM25Plus

# 分词口径与 BM25 关键字索引共用同一份实现 —— 两处各写一份的话,
# 「同一条查询在两处切出不同的词」这种 bug 极难查
from project_rag.adapters.chinese_tokens import tokens
from project_rag.domain import FaqEntry, FaqMatch
from project_rag.ports import TextMatcher

# 两道覆盖率门槛的类默认值（**改它们 = 改 FAQ 通道的命中口径**，
# 要连带跑 `scripts/acceptance/faq_hijack_report.py` 与 `calibrate_threshold.py` 重新标定）。
#
# **为什么只留覆盖率、而不是再加一道上界**：
# 判"这条答得了这个提问吗"只需要两个**纯 token 计数比值**（提问覆盖率 / 条目覆盖率）——
# 它们不含 idf、不含分数，**与语料规模无关**。上界要跟"比值的绝对大小"比，而比值会随语料漂
# （idf=ln((N+1)/df)）：实测库 150 → 300 → 617 → 1324 条时「请问，<原问题>」的最高未剪辑比值
# 1.000 → 1.407 → 1.334 → 1.311，`1.10` 那一档会把合法提问的放行率从 100% 砍到 60%/69%/71%。
# 而**只留覆盖率（夹取 + 0.5 + 0.8）在 150/300/617 三种规模上与"不夹取 + 上界"逐项相同**
# （一字不差与礼貌语全放行、无关问法与留出题误答数一样）——所以上界被删掉了：少一个旋钮、
# 少一处漂移源。分数因此回到 0~1（见 `_rank`）。
DEFAULT_MIN_QUERY_COVERAGE = 0.5
DEFAULT_MIN_ENTRY_COVERAGE = 0.8


def query_coverage(query_tokens: Sequence[str], entry_tokens: frozenset[str]) -> float:
    """输入:提问的分词 + 条目的分词集合。行为:提问的实词有多大比例出现在这条条目里。输出:0~1。

    **方向很重要**:这个比例低 = 用户问的东西这条答不了（「线程、进程、协程的对比」对「协程」
    只有 0.20）。它管的是"别答自己答不了的提问"。
    """
    if not query_tokens:
        return 1.0
    return sum(1 for term in query_tokens if term in entry_tokens) / len(query_tokens)


def entry_coverage(query_tokens: frozenset[str], entry_tokens: Sequence[str]) -> float:
    """输入:提问的分词集合 + 条目的分词。行为:这条条目的实词有多大比例出现在提问里。输出:0~1。

    **与提问覆盖率是两个方向**:这个比例低 = 这条问的东西用户没提（「&和&&的区别」对
    「cookie和session的区别」只剩「和/的/区别」蹭上）。没有它，两条只有虚词相同的
    "X 和 Y 的区别"会互相命中。
    """
    if not entry_tokens:
        return 1.0
    return sum(1 for term in entry_tokens if term in query_tokens) / len(entry_tokens)


class BM25TextMatcher(TextMatcher):
    """把 FAQ 问题当文档、用户提问当 query,用 BM25 排序 + 自身分归一化成置信度。"""

    def __init__(
        self,
        *,
        min_query_coverage: float = DEFAULT_MIN_QUERY_COVERAGE,
        min_entry_coverage: float = DEFAULT_MIN_ENTRY_COVERAGE,
    ) -> None:
        """输入:两个覆盖率门槛(0 = 关掉那一道)。行为:记下闸门并初始化索引缓存。输出:无。

        参数由 ``config.faq.*`` 经 ``cli/deps.py`` 传进来;不传就用类默认值(与配置默认值同源,
        有契约钉着两边一致)。
        """
        for name, value in (
            ("min_query_coverage", min_query_coverage),
            ("min_entry_coverage", min_entry_coverage),
        ):
            if value < 0 or value > 1:
                raise ValueError(f"faq.{name} 是比例,必须落在 [0, 1]，收到 {value}")
        self._min_query_coverage = min_query_coverage
        self._min_entry_coverage = min_entry_coverage
        # 指纹:id+question元组序列,用于比对语料是否变了,index存BM25索引,也就是IDF等等参数
        self._fingerprint: tuple[tuple[int, str], ...] | None = None
        self._index: BM25Plus | None = None
        # 每个条目自己的分词(建索引时一并算好):算「问自己得几分」与两个覆盖率都要用。
        # **不在这里省**:每查询重新 jieba 一遍 150~463 条问题是几十毫秒,而 FAQ 通道的全部
        # 意义就是"快且便宜"。
        self._doc_tokens: list[list[str]] = []
        self._doc_token_sets: list[frozenset[str]] = []

    def best_match(self, query: str, candidates: Sequence[FaqEntry]) -> FaqMatch | None:
        """输入:用户问题 + 候选问答对。行为:BM25 打分 → 过三道闸门 → 取最像的那条。输出:匹配;候选为空给 None。

        没过闸门时**给一条 score=0 的匹配**(不是 None):调用方照样走兜底,
        而日志/账本里还看得到"最接近的是哪条、为什么被拦下"。
        """
        entries = list(candidates)
        if not entries:
            return None

        top_index, _, normalized = self._rank(query, entries)
        return FaqMatch(entry=entries[top_index], score=normalized[top_index])

    def score_all(self, query: str, candidates: Sequence[FaqEntry]) -> list[float]:
        """输入:问题 + 候选。行为:按 best_match 的同一口径给每个候选打分。输出:与候选同序的分数。

        给诊断脚本用(`calibrate_threshold.py` / `faq_hijack_report.py` 靠它量
        "库内近邻有多像"、"哪些候选被门槛拦下")。
        它必须和 best_match 走同一个私有实现 ——
        否则脚本量出来的口径和线上跑的口径是两回事。

        **返回的是置信度(过不了闸门的是 0),不是排序依据**;排序次序见 ``_rank``。
        """
        entries = list(candidates)
        if not entries:
            return []
        _, _, normalized = self._rank(query, entries)
        return normalized

    def _rank(
        self, query: str, entries: list[FaqEntry]
    ) -> tuple[int, list[float], list[float]]:
        """输入:问题 + 候选。行为:原始 BM25 分排序 → 自身分归一 → 过闸门。输出:(top1 下标, 原始分, 同序置信度)。

        排序必须用原始分,不能用归一化分:
          语料一大就有一批候选同时顶到 1.0(以前还被夹取),这时拿归一化分 argmax 等于按 id
          顺序抽签 —— 实测在 463 条真实 FAQ 上,给原问题接一句无关请求后,13.8% 的提问会答错
          条目且分数仍是 1.000(直接越过阈值)。归一化分只当**置信度**用,不当排序依据。

        参照系只由**这条候选**定义:拿它的问题去问它自己得几分。分母与分子同源 ——
        语料变大变冗余时两者一起变,所以分数不漂移(这也是不改成"领先第二名多少"的原因)。

        **过不了闸门的候选记 0 分,并且排序**:**跳过**它们取下一个 —— "最像的那条答不了
        这个提问"时,第二名可能正是能答的那条。一条都过不了就退回首名(score=0)。
        """
        index = self._index_for(entries)  # 索引在这里取:不每次重建
        asked = tokens(query)
        raw = [float(s) for s in index.get_scores(asked)]

        # 取第一个最大值(同分时保留 id 更小的那条,顺序稳定)
        order = sorted(range(len(raw)), key=lambda i: (-raw[i], i))
        top_index = order[0]
        baseline = float(index.get_scores(self._doc_tokens[top_index])[top_index])

        if raw[top_index] <= 0.0 or baseline <= 0.0:
            # 没有可比性(或除零):全 0 —— 调用方照样走兜底,
            # 但日志里还能看到最接近的那条(下标照样返回)。
            return top_index, raw, [0.0] * len(raw)

        # **夹回 0~1**:夹取只是"报出来的置信度"好看（同一个词在提问里重复多遍会让比值 >1），
        # 判"这条答得了这个提问吗"靠的是下面两道**覆盖率**（它们数与语料规模无关），
        # 所以夹取不会把判别信息弄丢（对比实测见 `DEFAULT_MIN_QUERY_COVERAGE` 那段注释）。
        confidences = [max(0.0, min(1.0, s / baseline)) for s in raw]
        asked_set = frozenset(asked)
        kept = [
            confidences[index_] if self._passes(asked, asked_set, index_) else 0.0
            for index_ in range(len(entries))
        ]
        top_index = next((i for i in order if kept[i] > 0.0), top_index)
        return top_index, raw, kept

    def _passes(
        self, asked: Sequence[str], asked_set: frozenset[str], position: int
    ) -> bool:
        """输入:提问分词(+集合)+ 候选位置。行为:过两道覆盖率闸门。输出:是否算命中。

        两道闸门都是**"为了不答错"**而不是"为了答对":下界(调用方)管"像不像",
        提问覆盖率管"用户问的这条答得了吗",条目覆盖率管"这条问的是用户在问的吗"。
        ``0`` 表示关掉那一道(留一条回滚的路,不必改代码)。
        """
        if (
            self._min_query_coverage
            and query_coverage(asked, self._doc_token_sets[position]) < self._min_query_coverage
        ):
            return False
        return not (
            self._min_entry_coverage
            and entry_coverage(asked_set, self._doc_tokens[position]) < self._min_entry_coverage
        )

    def _index_for(self, entries: list[FaqEntry]) -> BM25Plus:
        """
        语料没变就复用索引,变了就重建。
        输入:问答对列表
        行为:比对 id+question 指纹,变了才重建(顺带缓存每条自己的分词 —— 覆盖率要用)
        输出:BM25 索引
        """
        fingerprint = tuple((e.id, e.question) for e in entries)
        if fingerprint != self._fingerprint or self._index is None:
            self._doc_tokens = [tokens(e.question) for e in entries]
            self._doc_token_sets = [frozenset(doc) for doc in self._doc_tokens]
            # 语料**已经分好词**再交给 BM25Plus:不然它自己会再 jieba 一遍(两倍分词开销)
            self._index = BM25Plus(self._doc_tokens)
            self._fingerprint = fingerprint
        return self._index
