"""
消耗聚合:把账本里的 token 按**三档单价 × 两个时段**折成钱(纯函数,`check_p0_7_cost.py` 钉住)。

**钱是从哪来的**:provider 在流结束时回报这次用了多少 token(``domain.LlmUsage``),
账本把它们记在**调用模型的那一行**上(见 ``observability/latency.py``);
这里只做"读回来 × 单价"这一件事。**不估、不猜、不按字数折算** ——
估出来的金额看起来总是很合理,然后和账单对不上,而那种错没人查得出来。

**为什么是三档 × 两段**(作者的单价表就是这么给的)::

    输入(缓存命中)     空闲 0.02 / 高峰 0.04 元每百万
    输入(缓存未命中)   空闲 1    / 高峰 2
    输出              空闲 4    / 高峰 8

把它们揉成一个数的代价不是"差一点":命中与未命中差 **50 倍**,高峰与空闲差 **2 倍** ——
而报表上仍会显示一个"合理"的金额,只是它可能差几十倍。

**时段怎么判**(作者给的价目表原话,逐字抄在这里,因为这段文字就是口径)::

    空闲时段价格为高峰时段价格的一半。北京时间周一至周五(不含中国法定节假日)
    9:00 - 12:00、14:00 - 18:00 为高峰时段;其余时段,包括周末及中国法定节假日全天
    均为空闲时段。

这句话里有**三个**条件(星期 / 钟点 / 日历),缺任何一个都会算错,而且错得安静 ——
报表上仍旧是一个合理的金额:漏了星期,周末上午算高峰(贵一倍);漏了日历,春节里算高峰
(贵一倍);漏了钟点,午休与深夜算高峰(贵一倍)。它们全在 ``PeriodRule`` 里,由
``check_p0_7_cost.py`` 逐条钉住。

口径七条(改它们 = 改契约):

1. **时段从账本行的墙上时间读**(``ports.ledger.LEDGER_TS``,存储层盖的戳),并且那个戳
   **必须带时区**(``2026-03-03T14:00:00+08:00``):价目表说的是**北京时间**,而"14:00"
   离开了时区就不是一个时刻 —— 容器里的进程钟是 UTC、宿主是 CST,同一张表里两种都有过。
   没带时区的行按**高峰**价计并计进 ``zone_unknown_calls``(见口径 2);
2. **判不出来的一律偏保守 + 如实计数**,分三种原因(它们的修法不一样,所以分开数):
   ``period_unknown_calls`` = 连时间戳都没有 / ``zone_unknown_calls`` = 有时间戳但没时区 /
   ``calendar_unknown_calls`` = 那一年的法定节假日还没填进配置。三种都按**高峰**价计,
   宁可多报也不少报 —— 少报的钱从报表上看不出来;
3. **缓存命中数不知道时,整段输入按未命中价计**,并计进 ``cache_unknown_calls``。
   这一档是**上界**(未命中价 ≥ 命中价),报表必须说出来,否则用户会把它当成精确值;
4. **没配单价的模型不计金额**:那一行给 ``None`` + 进 ``unpriced_models``,合计里不含它
   —— 报表因此能明确告诉使用者"把这个名字填进 config.yaml";
5. **没量到 token 的调用只计数**(``unmetered_calls``),不进金额,也不进按模型的分行
   (老账本与"provider 没回报用量"都落在这一档);
6. **先按(模型 × 时段)把 token 加起来再乘单价**(不是逐行乘完再求和):同一档的单价一样,
   一次乘法少一堆浮点误差,而且"总 token × 单价"是人能心算复核的式子;
7. **金额只在最后四舍五入**(``_MONEY_DIGITS`` 位):中间每一步都 round 的话,
   几十次调用累积的误差会让总数与手算对不上 —— 而"报表和手算差几分钱"是最招人怀疑的。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from project_rag.ports.ledger import LEDGER_TS

# 金额保留几位小数。**比"分"多两位**:单价是"每百万 token"级的,单次调用常常
# 只有 0.0001 元;只留两位的话一整天的账会被抹成 0.00。
_MONEY_DIGITS = 6
_NONE = "-"

# 两个时段的固定名字(接口、CLI、界面共用这一套字符串)。
PEAK = "peak"
OFF_PEAK = "off_peak"

# 价目表里的"北京时间"。**写死 +08:00**:中国自 1991 年起没有夏令时,固定偏移就是准确的,
# 而且不需要 IANA 时区库(容器里不一定有 tzdata,`ZoneInfo("Asia/Shanghai")` 会直接抛)。
# 名字就叫 `UTC+8`(不写 CST —— 那个缩写在大洋两岸指两个不同的钟,排障时会误导)。
BEIJING = timezone(timedelta(hours=8), "UTC+8")


def parse_windows(windows: Sequence[Sequence[str]]) -> tuple[tuple[time, time], ...]:
    """输入:``[["09:00", "12:00"], …]`` 形式的钟点窗口。行为:解析成 ``time`` 对。输出:元组。

    跨午夜的窗口(起 > 止)原样留着 —— 判定时用"起 ≤ t 或 t < 止"处理,不在这里拆成两段
    (拆了就有两份真源,而且出问题时说不清是哪一段)。
    """
    parsed: list[tuple[time, time]] = []
    for window in windows:
        if len(window) != 2:
            raise ValueError(f"钟点窗口该是 ['HH:MM', 'HH:MM'],收到 {list(window)!r}")
        start, end = window
        hour, minute = start.split(":")
        end_hour, end_minute = end.split(":")
        parsed.append((time(int(hour), int(minute)), time(int(end_hour), int(end_minute))))
    return tuple(parsed)


def parse_holidays(ranges: Sequence[Sequence[str]]) -> tuple[tuple[date, date], ...]:
    """输入:``[["2026-02-15", "2026-02-23"], …]`` 形式的日期区间。行为:解析成 ``date`` 对。输出:元组。

    **一个区间是一段连着放的日子**(国务院的放假通知就是这么写的:"2月15日至23日放假")。
    含头含尾 —— ``["2026-01-01", "2026-01-01"]`` 就是一天。
    """
    parsed: list[tuple[date, date]] = []
    for span in ranges:
        if len(span) != 2:
            raise ValueError(
                f"节假日该写成 ['2026-01-01', '2026-01-03'] 这样的**区间**,收到 {list(span)!r}"
                "(单个日子也写成 [同一天, 同一天])"
            )
        start, end = date.fromisoformat(span[0]), date.fromisoformat(span[1])
        if start > end:
            raise ValueError(f"节假日区间的起止反了:{list(span)!r}")
        parsed.append((start, end))
    return tuple(parsed)


def in_windows(clock: time, windows: Sequence[tuple[time, time]]) -> bool:
    """输入:一个钟点、若干窗口。行为:判它在不在窗口里(**左闭右开**,支持跨午夜)。输出:布尔。

    右开是刻意的:``09:00–12:00`` 里的 12:00:00 已经不算高峰 —— 午休从 12:00 整开始。
    """
    for start, end in windows:
        if start <= end:
            if start <= clock < end:
                return True
        elif clock >= start or clock < end:  # 跨午夜(如 23:00–01:00)
            return True
    return False


@dataclass(frozen=True, slots=True)
class PeriodRule:
    """高峰时段的口径:**工作日(不含法定节假日)的那几个钟点窗口**。

    三个字段就是价目表那句话里的三个条件,``PeriodRule.of()`` 从配置的原始形状建出来
    (组合根 ``cli/deps.py`` 干这件事),用例层只认这个对象。

    * ``weekdays`` —— ISO 星期(1=周一 … 7=周日)。**空集 = 不按星期过滤**(每天都可能高峰);
    * ``windows`` —— 高峰钟点窗口。**空 = 不分时段**:所有调用都按高峰价(那是"这个部署
      没有分时段的价目表",而不是"全天都贵")。此时连时间戳都不问,也不报"时段不明";
    * ``holidays`` —— 中国法定节假日(含头含尾的日期区间)。落在这里的日子**全天按空闲**。

    **调休上班的周末不算工作日**:放假通知里"1月4日(周日)上班"那种日子,按价目表的
    原话仍是"周末" → 空闲。这条有专门的用例盯着(它是最容易被"顺手优化"掉的一条)。
    """

    weekdays: frozenset[int] = frozenset()
    windows: tuple[tuple[time, time], ...] = ()
    holidays: tuple[tuple[date, date], ...] = ()

    @classmethod
    def of(
        cls,
        *,
        weekdays: Sequence[int] = (),
        windows: Sequence[Sequence[str]] = (),
        holidays: Sequence[Sequence[str]] = (),
    ) -> PeriodRule:
        """输入:配置里的原始形状(星期的整数、``["HH:MM","HH:MM"]``、``["YYYY-MM-DD",…]``)。

        行为:解析成判定用的规则。输出:``PeriodRule``。格式错**当场抛** —— 写错一个冒号
        会让"高峰"永远匹配不上,而报表上看不出异常,只是钱一直按另一档算(差一倍)。
        """
        return cls(
            weekdays=frozenset(int(day) for day in weekdays),
            windows=parse_windows(windows),
            holidays=parse_holidays(holidays),
        )

    @property
    def split(self) -> bool:
        """输入:无。行为:判这个部署分不分时段。输出:布尔(没配窗口 = 不分)。"""
        return bool(self.windows)

    @property
    def covered_years(self) -> frozenset[int]:
        """输入:无。行为:节假日表覆盖了哪些年份。输出:年份集合。

        报表拿它判"那一年的节假日我到底知不知道":不知道就**不能假装那天是工作日**
        (见 ``classify`` 的 ``calendar`` 一档)。
        """
        return frozenset(
            year
            for start, end in self.holidays
            for year in range(start.year, end.year + 1)
        )

    def classify(self, moment: datetime | None) -> tuple[str, str | None]:
        """输入:这一次调用发生的时刻(带时区最好)。行为:判高峰/空闲。输出:``(时段, 缺口原因)``。

        缺口原因是 ``None`` / ``"clock"``(没有时间戳)/ ``"zone"``(有时间戳但没时区)/
        ``"calendar"``(那一年的节假日没配) —— 后三种都**按高峰价计**,调用方据此计数。
        名字要与报表里的字段一一对应,所以写在这里、不由调用方猜。
        """
        if not self.windows:
            return PEAK, None
        if moment is None:
            return PEAK, "clock"
        if moment.tzinfo is None:
            # 有钟点、没时区 = 不知道是哪一刻。**不许拿它当北京时间用**:容器里的进程钟
            # 是 UTC,宿主是 CST,差 8 小时足以把高峰判成空闲(少报一半的钱)。
            return PEAK, "zone"
        local = moment.astimezone(BEIJING)
        if self.weekdays and local.isoweekday() not in self.weekdays:
            return OFF_PEAK, None
        if not in_windows(local.time(), self.windows):
            return OFF_PEAK, None
        if any(start <= local.date() <= end for start, end in self.holidays):
            return OFF_PEAK, None
        if local.year not in self.covered_years:
            # 工作日 + 在窗口里,但这一年的放假通知还没填进配置:那天**可能**是春节,
            # 而"没填"与"那天不放假"是两件事 —— 按高峰计(贵的那档)并如实计数。
            return PEAK, "calendar"
        return PEAK, None


def _moment(row: Mapping[str, str]) -> datetime | None:
    """输入:一行账本。行为:读存储层盖的那个时间戳。输出:``datetime``;没有/解析不了给 None。

    **解析不了给 None(→ 按高峰计 + 计入"时段不明"),不抛异常**:一行坏数据不该让整张报表打不开。

    带不带时区是两件事:带(``2026-03-03T14:00:00+08:00``)才是"哪一刻",由 ``PeriodRule``
    换算成北京时间再判;不带就是"钟点",判不了 —— 见 ``PeriodRule.classify`` 的 ``zone`` 一档。
    """
    raw = row.get(LEDGER_TS)
    if not raw or raw == _NONE:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ModelRate:
    """一个模型在**某一个时段**的三档单价(每百万 token)。``None`` = 这一档没配。"""

    input_cache_hit: float | None = None
    input_cache_miss: float | None = None
    output: float | None = None

    @property
    def configured(self) -> bool:
        """输入:无。行为:判这个模型有没有配过价。输出:布尔。"""
        return any(
            value is not None
            for value in (self.input_cache_hit, self.input_cache_miss, self.output)
        )


def rates_of(prices: Mapping[str, object], model: str, *, off_peak: bool) -> ModelRate:
    """输入:配置里的单价表、模型名、这一次是不是空闲时段。行为:取出该时段的三档价。输出:``ModelRate``。

    ``prices`` 里每一项是 ``config.ModelPriceConfig``(结构上就是三档 ``RateConfig``)。
    这里**用鸭子类型读它**,是为了让用例层不认识 pydantic —— 验收可以塞一个同形状的假对象。
    空闲那一档没配时**回落到高峰价**(= 这个部署不分时段),而不是当成"未配单价"。
    """
    price = prices.get(model)
    if price is None:
        return ModelRate()

    def rate(name: str) -> float | None:
        tier = getattr(price, name, None)
        if tier is None:
            return None
        value = getattr(tier, OFF_PEAK if off_peak else PEAK, None)
        if value is None:
            value = getattr(tier, PEAK, None)
        return None if value is None else float(value)

    return ModelRate(
        input_cache_hit=rate("input_cache_hit"),
        input_cache_miss=rate("input_cache_miss"),
        output=rate("output"),
    )


@dataclass(frozen=True, slots=True)
class CostLine:
    """一个模型 + 一个时段的消耗。``total_cost`` 为 ``None`` = **这个模型没配单价**。"""

    model: str
    period: str  # "peak" / "off_peak"
    calls: int  # 量到 token 的调用次数
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_cost: float | None

    @property
    def uncached_input_tokens(self) -> int:
        """输入:无。行为:未命中的输入 token。输出:数(已夹到 >=0)。"""
        return max(self.input_tokens - self.cached_input_tokens, 0)

    @property
    def priced(self) -> bool:
        """输入:无。行为:判这一档算不算得出钱。输出:布尔。"""
        return self.total_cost is not None


@dataclass(frozen=True, slots=True)
class PeriodTotal:
    """一个时段的合计(界面/CLI 直接照这张表念,和 provider 的价目表一行行对得上)。"""

    period: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_cost: float


@dataclass(frozen=True, slots=True)
class CostReport:
    """一个时间窗的消耗汇总。

    ``all_priced`` / ``metered_calls`` / ``unmetered_calls`` / ``period_unknown_calls`` /
    ``zone_unknown_calls`` / ``calendar_unknown_calls`` / ``cache_unknown_calls`` 必须一起读:
    "这一窗花了 3.2 元"只有在**计量与配置都齐**时才是完整的账;否则它只是**已知的那部分**,
    而这句话必须能从字段里读出来。
    """

    window: str
    currency: str
    metered_calls: int
    unmetered_calls: int
    period_unknown_calls: int
    zone_unknown_calls: int
    calendar_unknown_calls: int
    cache_unknown_calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_cost: float
    all_priced: bool
    unpriced_models: tuple[str, ...]
    periods: tuple[PeriodTotal, ...]
    lines: tuple[CostLine, ...]

    @property
    def total_tokens(self) -> int:
        """输入:无。行为:输入 + 输出。输出:总数。"""
        return self.input_tokens + self.output_tokens


def _tokens(row: Mapping[str, str], key: str) -> int | None:
    """输入:一行账本 + 字段名。行为:取整数 token 数,``-``/空/缺失/解析不了都算「没量到」。输出:int 或 None。

    用 ``float`` 中转再取整:字段是文本,而"1234.0"这种形态在换过 provider 之后
    出现过 —— 直接 ``int()`` 会抛 ValueError,把一条好数据变成"解析失败"。
    """
    value = row.get(key)
    if value is None or value in ("", _NONE):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _number(row: Mapping[str, str], key: str) -> float | None:
    """输入:一行账本 + 字段名。行为:取数值,``-``/空/缺失/解析不了都算「没量到」。输出:浮点或 None。"""
    value = row.get(key)
    if value is None or value in ("", _NONE):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _called_llm(row: Mapping[str, str]) -> bool:
    """输入:一行账本。行为:判这一行**到底调没调模型**。输出:布尔。

    判据是"有没有 provider 那边留下的痕迹":第一帧 token 的时刻(``llm_ttft_ms``)、
    第一帧思考的时刻(``llm_think_ms``)、模型名、或 token 数 —— 任一有值就算调过。
    FAQ 命中、检索为空直接拒答、寒暄这三条路压根不调模型,这几格全是 ``-``。

    **为什么不用 ``channel == "rag"``**:2026-10 之前记的账里,一次合流请求是**两行**
    (合流入口一行 + 知识库通道一行),两行的 channel 都是 rag —— 用 channel 判会把那些老账
    的调用次数数成两倍,而金额就是 ×2 的错。用量只记在真调 provider 的那一行上,
    这里判的也正是"这一行有没有真的在调模型"。
    """
    if any(
        _number(row, key) is not None
        for key in ("llm_ttft_ms", "llm_think_ms", "llm_input_tokens", "llm_output_tokens")
    ):
        return True
    # 只有模型名、没有任何用量:说明 provider 回报了是哪一次调用,但没给 token 数
    # (或给的是 0 —— 那也算调过)。这一档要计进「调了但没量到」。
    return row.get("llm_model") not in (None, "", _NONE)


def _cost_of(tokens: int, price: float | None) -> float | None:
    """输入:token 数、每百万 token 的单价。行为:折算成钱。输出:金额;单价没配给 None。

    ``price`` 给 0 与给 None 是两件事:0 是"这个模型确实不要钱"(本地模型),
    None 是"我们不知道它多少钱"。后者不许显示成 0.00 —— 那是在编数字。
    """
    if price is None:
        return None
    return tokens / 1_000_000.0 * price


@dataclass(slots=True)
class _Bucket:
    """按(模型 × 时段)攒 token 的桶。"""

    input: int = 0
    cached: int = 0
    output: int = 0
    calls: int = 0
    cache_unknown: int = 0


def summarize_cost(
    rows: Sequence[Mapping[str, str]],
    *,
    prices: Mapping[str, object],
    currency: str,
    window: str,
    rule: PeriodRule | None = None,
) -> CostReport:
    """输入:窗口内的账本行、单价表、币种、窗口标签、时段规则(不给 = 不分时段)。

    行为:按(模型 × 时段)聚 token → 乘该时段的三档单价 → 汇总;没配单价的模型单独列出来。
    输出:``CostReport``。空窗返回全 0 的报告(报表要能打开,不能抛错)。
    """
    rule = rule if rule is not None else PeriodRule()
    buckets: dict[tuple[str, str], _Bucket] = {}
    unmetered = 0
    period_unknown = 0
    zone_unknown = 0
    calendar_unknown = 0
    for row in rows:
        if not _called_llm(row):
            continue
        input_tokens = _tokens(row, "llm_input_tokens")
        output_tokens = _tokens(row, "llm_output_tokens")
        if input_tokens is None and output_tokens is None:
            # 调了模型但没量到 token:provider 没回报用量、或那一轮中途断了,
            # **以及**加这套东西之前记下的老账(那时账本里压根没有这几个字段)。
            # 只计数、不进按模型的分行 —— 那种行的模型名是 ``-``,把它列进
            # "未配单价的模型"会让报表说"请把 `-` 填进 config.yaml"(实测触发过)。
            unmetered += 1
            continue

        # 判时段(含"判不出来"的三种原因)。缺口**按原因分开数**:它们的修法不一样
        # (补时间戳 / 让写入方盖上时区 / 把那一年的放假通知填进配置)。
        period, gap = rule.classify(_moment(row))
        if gap == "clock":
            period_unknown += 1
        elif gap == "zone":
            zone_unknown += 1
        elif gap == "calendar":
            calendar_unknown += 1

        model = row.get("llm_model") or _NONE
        bucket = buckets.setdefault((model, period), _Bucket())
        bucket.calls += 1
        bucket.input += input_tokens or 0
        bucket.output += output_tokens or 0
        cached = _tokens(row, "llm_cached_input_tokens")
        if cached is None:
            # provider 没回报缓存明细:整段输入按**未命中**价计(上界),并计数。
            bucket.cache_unknown += 1
        else:
            bucket.cached += min(cached, input_tokens or 0)

    lines: list[CostLine] = []
    unpriced: list[str] = []
    per_period: dict[str, PeriodTotal] = {}
    total_cost = 0.0
    metered = 0
    cache_unknown_total = 0
    input_total = 0
    cached_total = 0
    output_total = 0
    for model, period in sorted(buckets):
        bucket = buckets[(model, period)]
        rates = rates_of(prices, model, off_peak=period == OFF_PEAK)
        uncached = max(bucket.input - bucket.cached, 0)
        # 三档分别算;**命中价没配**时那一档按未命中价计(等价于"这个模型没有缓存价")。
        hit_price = (
            rates.input_cache_hit if rates.input_cache_hit is not None else rates.input_cache_miss
        )
        parts = (
            _cost_of(bucket.cached, hit_price),
            _cost_of(uncached, rates.input_cache_miss),
            _cost_of(bucket.output, rates.output),
        )
        if any(part is None for part in parts):
            line_cost: float | None = None
            if model not in unpriced:
                unpriced.append(model)
        else:
            line_cost = round(sum(part for part in parts if part is not None), _MONEY_DIGITS)
            total_cost += line_cost
        metered += bucket.calls
        cache_unknown_total += bucket.cache_unknown
        input_total += bucket.input
        cached_total += bucket.cached
        output_total += bucket.output
        lines.append(
            CostLine(
                model=model,
                period=period,
                calls=bucket.calls,
                input_tokens=bucket.input,
                cached_input_tokens=bucket.cached,
                output_tokens=bucket.output,
                total_cost=line_cost,
            )
        )
        previous = per_period.get(period)
        per_period[period] = PeriodTotal(
            period=period,
            calls=bucket.calls + (previous.calls if previous else 0),
            input_tokens=bucket.input + (previous.input_tokens if previous else 0),
            output_tokens=bucket.output + (previous.output_tokens if previous else 0),
            total_cost=(line_cost or 0.0) + (previous.total_cost if previous else 0.0),
        )

    # 一行都没量到 token 时不该报"未配置单价":那不是"缺单价",是"没有可算的量"
    # (空窗口、全是老账、或 provider 没回报用量)。真正的缺口由 ``unmetered_calls`` 说。
    if not buckets:
        unpriced = []
    return CostReport(
        window=window,
        currency=currency,
        metered_calls=metered,
        unmetered_calls=unmetered,
        period_unknown_calls=period_unknown,
        zone_unknown_calls=zone_unknown,
        calendar_unknown_calls=calendar_unknown,
        cache_unknown_calls=cache_unknown_total,
        input_tokens=input_total,
        cached_input_tokens=cached_total,
        output_tokens=output_total,
        total_cost=round(total_cost, _MONEY_DIGITS),
        all_priced=not unpriced,
        unpriced_models=tuple(unpriced),
        periods=tuple(per_period[name] for name in (PEAK, OFF_PEAK) if name in per_period),
        lines=tuple(lines),
    )
