from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

# 项目根目录的路径和配置文件的路径,我这里的配置优先级：传入参数(如果传入参数) > 环境变量 > .env文件 > 配置文件 > 默认值
# 本文件位于 src/project_rag/config.py,所以 parents[0]=project_rag, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


def project_path(raw: str | Path) -> Path:
    """输入:配置里的一个路径文本。行为:相对路径挂到**项目根**下(绝对路径原样)。输出:路径。

    **"相对路径按项目根解析"只有这一处实现**:上传目录、语料目录、模型目录、日志目录都走它。
    散着写过的后果是每一处都要自己记得「``/`` 遇到绝对右值会丢弃左值」这个细节 ——
    忘一次就是"配置里写了绝对路径反而找不到"。
    """
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


# 嵌套配置类,传入yaml的子配置字典,将字典中的值转化为对应的字段类型,如果没传入的字段,则使用默认值
class LLMConfig(BaseModel):
    deepseek_api_key: SecretStr = SecretStr("")
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_s: float = 60.0
    # 思考模式(DeepSeek V4 系列支持,**一个变量同时管开关与强度**):
    #   auto         —— 不发任何参数,沿用服务端默认(开启、high)= 加这个字段之前的行为
    #   off          —— 关掉思考:实测首个内容字 **~0.5s**(思考时 2~15s)
    #   low/high/max —— 开启并指定强度(模型支持档位见 GET /models 的 effort.supported_levels)
    # 两个要知道的副作用:① 思考模式下 temperature 会被 provider **忽略**(不报错也不生效),
    # 只有 off 时才用得上;② 思考计入输出 token 计费,而且会吃掉 max_tokens 预算
    # (思考吃满时 content 为 0 帧、finish_reason=length → 产品会按「空回答」降级成拒答话术)。
    thinking: Literal["auto", "off", "low", "high", "max"] = "auto"
    # 让 provider 在流末尾回报这次用了多少 token(OpenAI 协议的 ``stream_options.include_usage``)。
    # **关掉它不会让服务坏,只会让报表说"没量到 token"** —— 那正是这个开关存在的意义:
    # 有些代理不认这个参数(直接 400),那时把它设成 false 就能继续跑,账本上那一格是 ``-``。
    stream_usage: bool = True

    # 验证器,这个方法会自动被调用,验证DEEPSEEK_API_KEY字段的值,如果未设置或者为空,会抛出异常
    @field_validator("deepseek_api_key")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:LLM 的密钥字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密钥。"""
        if not v.get_secret_value():
            raise ValueError("RAG_LLM__DEEPSEEK_API_KEY 未设置或为空")
        return v


class MySQLConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 10093
    user: str = "rag"
    password: SecretStr = SecretStr("")
    database: str = "rag"
    # 连接超时(秒)。原先在 faq_mysql / conversation_mysql 里各写一个 5。
    # 它决定「MySQL 挂了之后要等多久才报错」,本机 Docker 与跨网部署的合理值不同。
    connect_timeout_s: int = 5

    @field_validator("password")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:MySQL 密码字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密码。"""
        if not v.get_secret_value():
            raise ValueError("RAG_MYSQL__PASSWORD 未设置或为空")
        return v


class RedisConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 10087
    password: SecretStr = SecretStr("")
    db: int = 0
    # 读/连超时(秒)。原先在 cache_redis 里写死 1.0。
    # **它就是「降级放大」的时长**:Redis 一挂,_safe_get 要等这么久才按未命中返回,
    # 期间请求转去调大模型。调大 = 故障时每个请求多等一会儿。
    socket_timeout_s: float = 1.0

    @field_validator("password")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:Redis 密码字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密码。"""
        if not v.get_secret_value():
            raise ValueError("RAG_REDIS__PASSWORD 未设置或为空")
        return v


class FaqConfig(BaseModel):
    """FAQ 通道的命中口径（口径与推导见 ``adapters/matcher_bm25.py``）。

    命中 = 置信度 ≥ ``threshold`` **且**两个覆盖率都过门槛：

        置信度 = top1 原始 BM25 分 ÷「这条问自己」的分（夹到 0~1）
        提问覆盖率 = 提问的实词有多大比例出现在命中的条目里
        条目覆盖率 = 命中的条目的实词有多大比例出现在提问里

    **为什么要覆盖率**（的那次）：`rank_bm25` 的 `BM25Plus` 给**每一个查询词**
    都加 `idf(q)·delta`（哪怕这条文档里没有它），而置信度的分母只是"这条问自己的分" ——
    于是提问里"别处出现过、这条没有"的词越多，比值越高；短条目（如两个字的一条）自身分极低，
    随便一点底分就顶穿，而比值夹回 1.0 之后 **1.712 与 1.000 记成同一个数，光靠阈值分不开**。
    两个覆盖率正是拦它的：提问覆盖率低 ⇒ 这条答不了用户问的；条目覆盖率低 ⇒ 这条问的不是用户问的。
    实测（`scripts/acceptance/faq_hijack_report.py`，150 条真实 FAQ + 50 条留出题）：
    提问覆盖 0.5 + 条目覆盖 0.8 → 一字不差与礼貌语 **150/150 全放行**、留出题被误答 **6 → 1**、
    无关问法最高分 0.206 → **0.000**（剩的那条是符号被分词丢掉）。

    **为什么没有"上界"这道旋钮**：试过，量出来它随语料规模漂（`idf=ln((N+1)/df)`，
    库 150 → 1324 条时「请问，…」的最高值 1.000 → 1.311，1.10 那一档把合法提问砍掉三成），
    而**只留两个覆盖率的成绩与它逐项相同** —— 于是删掉：少一个旋钮、少一处漂移源。
    两个门槛都可以用 ``0`` 关掉（回滚用）。
    """

    # 下界：一字不差 ≈ 1.000、完全无关 ≤ 0.303（463 条真实语料），
    # 所以 0.85 表示「只答近乎一字不差的问题」，改写问法交给知识库通道。
    threshold: float = 0.85
    # 提问覆盖率：用户问的词至少这么大比例要出现在命中的条目里。
    # 作者那条只有 0.20（5 个词里只沾上「协程」）→ 拦下，交给知识库。
    min_query_coverage: float = 0.5
    # 条目覆盖率：命中的条目里至少这么大比例的词要被用户提到。
    # 没有它，两条只有虚词相同的"X 和 Y 的区别"会互相命中。
    min_entry_coverage: float = 0.8
    # ── 数据侧：什么样的"问题"才准进这张表（`scripts/tools/load_faq_csv.py` 装载时校验）──
    # 规则 = **分词后至少 min_question_tokens 个实词**，**或者**整串至少 min_question_chars 个字。
    # 为什么按"词"：病根是**单个实词**的条目 —— 它的"问自己"分极低，任何含这个词的提问都能把
    # 置信度顶到 1.0（实测「线程、进程、协程的对比」被两个字的那条 `协程` 答掉，阈值 0.98 也没用）。
    # 为什么留一条"或足够长"：`IllegalArgumentException` 只有一个实词，但它是个合格的问题。
    # 两个都填 0 = 关掉这道校验（回滚用）。**改这里就等于改"什么能入库"**，改完记得重跑
    # `scripts/tools/load_faq_csv.py --dry-run` 看有没有行被拒。
    min_question_tokens: int = 2
    min_question_chars: int = 8
    table: str = "faq"

    @model_validator(mode="after")
    def _check_gates(self) -> "FaqConfig":
        """输入:无。行为:两个覆盖率门槛必须落在 [0,1]（0 = 关掉那一道）。输出:self。"""
        for name in ("min_query_coverage", "min_entry_coverage"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"faq.{name} 是比例,必须落在 [0, 1]，收到 {value}")
        for name in ("min_question_tokens", "min_question_chars"):
            if getattr(self, name) < 0:
                raise ValueError(f"faq.{name} 不能为负（0 = 关掉那一条）,收到 {getattr(self, name)}")
        return self


class CacheConfig(BaseModel):
    """FAQ 答案缓存的旁路参数(``answer_faq`` 的 cache-aside)。

    **为什么 TTL 走配置**:它和 ``faq.threshold`` 是同一类东西 —— 业务旋钮,不是机制。
    留在代码里意味着「把 5 分钟改成 15 分钟要重新部署」。

    **为什么不把缓存键前缀也搬进来**:``answer_faq.CACHE_KEY_PREFIX = "faq:v1:"`` 里的
    版本号跟着**缓存 value 的格式**(``_encode`` 的 ``<source>\\x1f<answer>``)走。让它可配
    = 允许两个格式不同的部署共用一份 Redis,读到对方的值会**静默解错**。
    版本号是机制,留在代码里,改 ``_encode`` 时手工 bump。

    **作用域提醒**:该缓存当前只服务 ``faq`` 单通道命令 —— 合流(``qa`` / FastAPI)
    不读也不写缓存。
    """

    # 过期秒数。落配置之前的字面量就是 300,所以这里必须也是 300 ——
    # 加配置项不许悄悄改变默认语义(有验收脚本钉住类默认值)。
    ttl_s: int = 300

    # BM25 索引「语料指纹」的缓存秒数（加)。
    # 为什么值得一个旋钮:指纹查询在 3938 块时实测 **397.6ms**,而它**每次检索都要调**;
    # 真正的 BM25 打分只要 ~1ms。缓存后每查询变成一次 Redis GET(~0.1ms),
    # 代价是「语料变了」最多晚这么多秒被发现。
    # 为什么默认 20 而不是跟 ttl_s 一样 300:这个窗口决定"新入库的文档多久能被检索到",
    # 5 分钟太久(入库本身才几分钟);而 20 秒对体感无影响。
    # 要立刻生效得让写入方 bump 缓存键 —— 目前没做。
    corpus_fp_ttl_s: int = 20


class ConversationConfig(BaseModel):
    """会话历史:表名、保留轮数、**整个历史块的字符预算**。

    轮数走配置,不在读、写两处各硬编码一个 5 —— 两边用的是同一个值,改一处两处都变。
    0 或负数由读侧拒绝(见 Session)。

    **字符预算是第二道闸,也是"滑动窗口"里真正会滑的那一半**:只按轮数限的话,
    一轮可以很长(用户贴一份文档来问、或模型答满 4096 个 token),5 轮就能把提示词撑到
    几十万字符 —— 那是**真金白银的输入 token**(每一轮都要把它重发一遍),还可能顶穿
    模型的上下文。所以:先按轮数取最近 N 轮,再从**最新**往回装进 ``history_max_chars``,
    装不下的**整轮丢掉**(不截断半轮:半个答案进提示词比没有更糟),至少保留最新一轮。
    ``0`` = 只按轮数(这个部署不限大小)。

    **单位是字符,不是 token**:算 token 要装 provider 的分词器,而估出来的数会漂 ——
    字符数是精确、可复核、换模型也不变的。想更省就在这里调小。
    """

    table: str = "conversations"
    history_rounds: int = 5
    history_max_chars: int = 0

    @field_validator("history_max_chars")
    @classmethod
    def _check_chars(cls, value: int) -> int:
        """输入:字符预算。行为:不许为负(负预算会让历史永远为空,而那是"静默失忆")。输出:原值。"""
        if value < 0:
            raise ValueError(f"conversation.history_max_chars 不能为负(0 = 不限),收到 {value}")
        return value


class RateLimitConfig(BaseModel):
    """限流(滑动窗口,默认计数在 Redis;见 ``adapters/ratelimit_redis.py``)。

    **为什么默认开着**:关着的时候,一个循环脚本就能把模型与 token 额度吃光,
    而这件事在日志里长得跟"今天大家都很活跃"一模一样。默认值按"同事几人到几十人"定:
    一分钟 60 次问答够任何人用,但挡住脚本。

    每一档 ``<= 0`` = 关掉这一档(不是"限 0 次")。
    """

    enabled: bool = True
    # 计数存在哪:`redis`(跨进程共享、重启不清零、时钟以 Redis 为准)/ `memory`(单进程)。
    # 默认 Redis —— 限流的前提是"换个进程/重启一下也绕不过去"。
    backend: str = "redis"
    # 键的命名空间 + 版本号(同一个 Redis 被别的应用或别的版本共用时不互相踩)。
    key_prefix: str = "ratelimit:v1:"
    window_s: int = 60
    # 问答:HTTP 的 /query 与 WS 的**每条消息**共用这一档(两条入口,一份额度)
    query: int = 60
    # 上传:一次上传背后是几分钟的 CPU 嵌入,闸门要更紧
    upload: int = 10
    # 登录:防口令爆破
    login: int = 10


class RateConfig(BaseModel):
    """**一个计价档**的两个时段价:高峰与空闲(单位:元 / 每百万 token)。

    为什么要分时段:provider 的空闲时段打折(价目表上是半价)。哪一段算空闲由
    ``CostConfig.peak`` 的规则给(工作日 + 钟点窗口 + 非节假日);`off_peak` 那一格
    **留空**时,该档一律按 ``peak`` 价计(等价于"这个部署不分时段")。
    """

    peak: float | None = None
    off_peak: float | None = None

    @field_validator("peak", "off_peak")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        """输入:单价。行为:不许为负(负单价 = 报表算出负的钱)。输出:原值。"""
        if v is not None and v < 0:
            raise ValueError(f"单价不能为负,收到 {v}")
        return v

    def for_period(self, *, off_peak: bool) -> float | None:
        """输入:这一次是不是空闲时段。行为:取对应那一档的价。输出:单价;没配给 None。

        **空闲档没配时回落到高峰价**,不是 None:那表示"这个部署没打算分时段",
        而不是"这个时段不知道多少钱"(后者会让整段时间的账都算不出来)。
        """
        if off_peak and self.off_peak is not None:
            return self.off_peak
        return self.peak


class ModelPriceConfig(BaseModel):
    """一个模型的**三档**单价:输入(命中缓存)/ 输入(未命中)/ 输出。

    **为什么输入要拆两档**:provider 的 prompt cache 命中价与未命中价差着一个量级
    (实测 0.02 vs 1 元/百万,50 倍)。合成一档的话,账单会差出几十倍,而且**报表上看不出来**
    ——它只会显示一个"合理"的金额。

    三档都可以不填;不填的那一档就是"未配置单价",报表会如实说而不是当 0 元。
    """

    input_cache_hit: RateConfig = RateConfig()
    input_cache_miss: RateConfig = RateConfig()
    output: RateConfig = RateConfig()

    @property
    def configured(self) -> bool:
        """输入:无。行为:判这个模型有没有配过价。输出:布尔。"""
        return any(
            rate.peak is not None or rate.off_peak is not None
            for rate in (self.input_cache_hit, self.input_cache_miss, self.output)
        )


class PeakConfig(BaseModel):
    """**高峰时段**的判定口径(贵的那一档;其余时间空闲,价目表上是半价)。

    作者给的价目表原话就是口径：

        空闲时段价格为高峰时段价格的一半。北京时间周一至周五(不含中国法定节假日)
        9:00 - 12:00、14:00 - 18:00 为高峰时段;其余时段,包括周末及中国法定节假日
        全天均为空闲时段。

    所以高峰 = **工作日** ∩ **那几个钟点窗口** ∩ **不是法定节假日**。三个条件分别落在
    ``weekdays`` / ``windows`` / ``CostConfig.holidays`` 上,判定在
    ``application/cost.py:PeriodRule``(纯函数,``check_p0_7_cost.py`` 钉住)。

    **窗口与星期留空是两种不同的意思**,别混:

    * ``windows`` 空 = **不分时段**(所有调用都按 ``peak`` 价,报表不报"时段不明");
    * ``weekdays`` 空 = **不按星期过滤**(每天都可能高峰,只按钟点判)。
    """

    # ISO 星期:1=周一 … 7=周日。空列表 = 不按星期过滤。**调休上班的周末不算工作日**
    # (价目表说的是"周一至周五"与"周末",不是"调休日历"),这条有用例盯着。
    weekdays: list[int] = []
    # 高峰钟点窗口,**按北京时间读**(写这里是"北京时间的 9 点",不是服务器本地的 9 点)。
    # ``[起, 止)`` 左闭右开:``["09:00","12:00"]`` 里的 12:00 整已经不算高峰(午休开始)。
    # 支持跨午夜(如 ``["22:00","02:00"]``)。
    windows: list[list[str]] = []

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, value: list[int]) -> list[int]:
        """输入:星期列表。行为:逐个校验是 ISO 1–7。输出:原值。

        **0/7 之外的写法必须当场炸**:``0``(有些人按 C 的星期写)会让周一永远匹配不上,
        于是每个周一都按空闲价算 —— 报表上看不出异常,只是钱少了一半。
        """
        for day in value:
            if day < 1 or day > 7:
                raise ValueError(f"cost.peak.weekdays 用 ISO 星期(1=周一 … 7=周日),收到 {day}")
        return value

    @field_validator("windows")
    @classmethod
    def _check_windows(cls, value: list[list[str]]) -> list[list[str]]:
        """输入:高峰窗口列表。行为:逐个校验是 ``[HH:MM, HH:MM]``。输出:原值。

        **格式错必须当场炸**:写错一个冒号会让"高峰"永远匹配不上,而报表上看不出异常 ——
        只是钱一直按空闲算(少报一半)。
        """
        import re as _re

        pattern = _re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
        for window in value:
            if len(window) != 2 or not all(pattern.match(part) for part in window):
                raise ValueError(
                    f"cost.peak.windows 的每一项都该是 ['HH:MM', 'HH:MM'],收到 {window!r}"
                )
        return value


class CostConfig(BaseModel):
    """消耗报表:按模型配单价(分时段、分缓存),把账本里的 token 折成钱。

    **为什么单价要写在配置里、而不是查官网**:① 同一个模型在不同代理/不同套餐下价格不同;
    ② 价格会变,而"改一个数字"不该动代码;③ 更要紧的是 —— **写死的价格会在涨价后
    静默变成错的**,而报表上那几个数字看上去永远是对的。

    键就是 provider 回报的模型名(``llm_model``,通常等于 ``llm.model``)。
    没配单价的模型**不会被当成 0 元**:它在报表里单列出来,提醒你把这个名字填进来。
    不知道自己该填哪个名字时看报表的 ``unpriced_models`` —— 那里就是 provider 报的原名。
    """

    # 币种符号,只用于显示。**不做汇率换算**:换算需要一份会过期的汇率表,
    # 而"这个数到底是不是人民币"比"换算得准不准"更要紧。
    currency: str = "CNY"
    # 高峰那两档怎么判(工作日 + 钟点窗口)。默认空 = 不分时段。
    peak: PeakConfig = PeakConfig()
    # 中国法定节假日,**含头含尾的日期区间**(``[["2026-02-15","2026-02-23"], …]``)。
    # 落在这里的日子全天算空闲 —— 春节里的一次调用不该按高峰价收费。
    # 来源:国务院办公厅每年 11 月前后发的《关于XXXX年部分节假日安排的通知》
    # **只有工作日+窗口内的调用才需要它**:
    # 周末与夜里本来就已经是空闲了。
    # 那一年没填 → 报表按高峰计并计进 ``calendar_unknown_calls``(不假装那天不放假)。
    holidays: list[list[str]] = []
    # ``模型名 -> 三档单价``。默认空表 = 谁都没配 = 报表只报 token,不报钱。
    models: dict[str, ModelPriceConfig] = {}

    @field_validator("holidays")
    @classmethod
    def _check_holidays(cls, value: list[list[str]]) -> list[list[str]]:
        """输入:节假日区间列表。行为:逐个校验是 ``[YYYY-MM-DD, YYYY-MM-DD]`` 且起 ≤ 止。输出:原值。

        **写成扁平的一串日期会被当场拒掉**:那种形状读起来像对、跑起来每年只休息一天,
        而报表上只会显示"钱少了",看不出是配置写错了。
        """
        from datetime import date as _date

        for span in value:
            if len(span) != 2:
                raise ValueError(
                    f"cost.holidays 的每一项都该是 ['2026-01-01', '2026-01-03'] 这样的区间,"
                    f"收到 {span!r}(单个日子写成 [同一天, 同一天])"
                )
            try:
                start, end = _date.fromisoformat(span[0]), _date.fromisoformat(span[1])
            except ValueError as exc:
                raise ValueError(f"cost.holidays 里有不是日期的项:{span!r}({exc})") from exc
            if start > end:
                raise ValueError(f"cost.holidays 的区间起止反了:{span!r}")
        return value

    def prices(self) -> dict[str, ModelPriceConfig]:
        """输入:无。行为:把单价表原样交给用例层(它只认识这个模型的字段)。输出:字典。

        **为什么要有这一道转换**:``application/cost.py`` 只认识"三档 × 两个时段"这几个数,
        不认识 pydantic 的嵌套形状。转换只此一处,免得接口层与 CLI 各拼一遍、拼法还不一样。
        """
        return dict(self.models)


class CaptchaConfig(BaseModel):
    """登录/注册前置的图形验证码。"""

    enabled: bool = True
    # 一张图的有效期(秒)。太短用户来不及输入,太长等于给爆破窗口。
    ttl_s: int = 120
    # 字符数。4 位 + 一次性 + 限流,足以挡住脚本化爆破。
    length: int = 4


class RegistrationConfig(BaseModel):
    """员工自助注册。"""

    enabled: bool = True
    # 非空 = 注册要填邀请码(发放给部门负责人的一次性口令)。
    invite_code: str = ""
    # 自助注册的口令下限(比管理员建号的 8 位更严:这条入口无人值守)。
    min_password_chars: int = 10


class ApiConfig(BaseModel):
    """HTTP/WebSocket 服务(``project-rag-api``)的运行参数。"""

    @model_validator(mode="after")
    def _check_threadpool(self) -> ApiConfig:
        """输入:无。行为:线程池上限必须 >= 1。输出:self。"""
        if self.threadpool_size < 1:
            raise ValueError(f"api.threadpool_size 必须 >= 1,收到 {self.threadpool_size}")
        return self

    # 是否由后端托管前端产物。**兼容模式默认 true**：桌面壳、旧的一体化部署与本地
    # `uv run project-rag-api` 仍然打开就能用；前后端分离部署在 compose 里显式设 false，
    # 后端只提供 API，前端由 Vite / Nginx 单独发。
    serve_frontend: bool = True
    # 启动预热:先把嵌入/重排模型、语料索引、Milvus 连接建好,把「第一个请求的冷启动」
    # 从**第一个用户**身上挪到启动阶段(实测:不预热时首个请求比热请求多付 4.4s)。
    # 预热只走到「检索出引用」为止,**不调大模型**,所以不花 token 钱;
    # 失败只记一条 WARNING,不拦启动。关掉它 = 启动更快、但首个请求自己扛冷启动。
    warmup: bool = True
    # 上传的原文件落盘目录(相对**项目根**,与 models/data 同一套解析方式)。
    # 落盘而不是只进向量库:解析要读原文件、前端要能"预览",而且删文档时三处痕迹要一起清。
    uploads_dir: str = "data/uploads"
    # 语料**源材料**目录(相对项目根)。知识库页拿它和登记表/向量库对账:
    # 这里放着、但还没入库的文件会在清单上显示成「未入库」—— 后端直接往这儿放文件时,
    # 前端才看得见(以前只念登记表,放进去的文件前端根本不知道)。
    # **它不是知识库本身**:入库的产物在 Milvus 里,这里只是"可以灌的东西"的出处;
    # 删除文档时也不动这个目录里的任何文件(删库不删源,见 application/documents.py)。
    corpus_dir: str = "data/corpus"
    # 单个上传文件的大小上限(MB)。**不是安全边界,是一道礼貌的闸**:
    # 真正防滥用要靠反代/限流。定 50 是因为语料里最大的 PDF 也就几 MB。
    max_upload_mb: int = 50
    # 绑定地址。类默认**必须**留着 0.0.0.0:WSL 里绑回环时 Windows 浏览器连不上(实测),
    # 进容器后绑回环也映射不出去。改成可配是为了「本机调试只监听自己」这类场景。
    host: str = "0.0.0.0"
    port: int = 8080
    # 预热用的那条查询。**它是隐藏耦合**:预热要真的检索到东西,才能把
    # 「构造嵌入/重排/向量库/BM25」那 4.4s 付掉;语料换成别的领域后,
    # 这条查询可能检索不到任何块 —— 换成该领域里必然存在的一句即可。
    warmup_query: str = "装饰器有什么用"
    # CORS 允许的来源。默认全开是**本机开发**的选择;对外必须收紧,
    # 否则任何站点都能拿用户的浏览器打这个服务。
    # "全开 + 绑非回环地址"这个组合会在启动时**打一条 WARNING**(见 api/app.py 的
    # `_warn_wide_cors`):不是拦你,是让这件事在日志里留个痕。
    cors_origins: list[str] = ["*"]
    # 限流。**它同时是"防滥用"与"防爆破"**:问答烧 token、上传烧 CPU、登录烧口令。
    rate_limit: RateLimitConfig = RateLimitConfig()
    # 图形验证码。关掉它 = 登录/注册只剩限流一道闸(仅限纯内网、无对外入口的部署)。
    captcha: CaptchaConfig = CaptchaConfig()
    # 自助注册。默认开着 = 员工用企业邮箱前缀自己开通;要审批制就关掉它,
    # 改由管理员 `project-rag user add` 建号。``invite_code`` 非空时还要填邀请码。
    registration: RegistrationConfig = RegistrationConfig()
    # anyio 线程池上限。同步重活(嵌入/检索/重排/生成)全走 ``run_in_threadpool``,
    # 这个数同时就是「服务端最多同时处理多少个请求」的上限。默认 40(anyio 默认值);
    # 调大 = 更多请求进 GPU 队列,但争用更重,收益必须在压测里量。
    threadpool_size: int = 40


class MilvusIndexConfig(BaseModel):
    """稠密向量的**索引类型**与它的两个参数（见 ``adapters/milvus_store.py``）。

    为什么做成旋钮：``FLAT`` 是暴力精确检索（结果是"标准答案"，验收好断言），
    ``IVF_FLAT`` 先按 k-means 分 ``nlist`` 个簇、检索时只探 ``nprobe`` 个簇 ——
    快，但**是近似**（召回率换延迟）。语料规模与查询量决定该用哪个，
    所以它属于部署配置，不该写死在适配器里。

    * ``type=FLAT`` → 建索引时不带任何参数；``nlist`` / ``nprobe`` 都不生效；
    * ``type=IVF_FLAT`` → 建索引用 ``nlist``，检索用 ``nprobe``。

    **``nprobe`` 必须 ≤ ``nlist``**：探的簇比总簇数还多没有意义（Milvus 不报错，
    只是白填一个数），这里 fail-fast —— 与 ``ApiConfig.port`` 那次同一个口径：
    配置写错就该在启动时炸，而不是在检索结果里慢慢体现。
    """

    type: Literal["FLAT", "IVF_FLAT"] = "FLAT"
    # 簇数。经验值 ~ 4·√N（N=块数）：3927 块 → 约 250；取 128 是"少分点簇、每簇更准"的保守侧。
    # 语料涨到十万块再往上调，并且**重建索引**才会生效（改配置不会自动重排已有索引，
    # 见 `project-rag collections --reindex`）。
    nlist: int = 128
    # 检索时探几个簇。越大越接近 FLAT（nprobe=nlist 就退化成分簇暴力），越小越快越可能漏。
    nprobe: int = 16

    @model_validator(mode="after")
    def _check(self) -> MilvusIndexConfig:
        """输入:无。行为:把两个参数的关系当场校验。输出:self。"""
        if self.nlist < 1:
            raise ValueError(f"milvus.index.nlist 必须 ≥ 1，收到 {self.nlist}")
        if self.nprobe < 1:
            raise ValueError(f"milvus.index.nprobe 必须 ≥ 1，收到 {self.nprobe}")
        if self.nprobe > self.nlist:
            raise ValueError(
                f"milvus.index.nprobe({self.nprobe}) 不能大于 nlist({self.nlist})"
                " —— 探的簇比总簇数还多没有意义"
            )
        return self


class MilvusConfig(BaseModel):
    host: str = "127.0.0.1"
    # 宿主端口:服务端在容器里,compose 把 gRPC 19530 映射到 10086
    port: int = 10086
    # 只准填已存在的库(milvus内置):Milvus 不会自动建库,命名库得先 create_database
    database: str = "default"
    collection: str = "rag_chunks"
    # 读一致性档（Milvus 的 consistency_level）。**只作用于 search 那一路**；
    # 指纹（变更检测）与写后回查总数固定用 strong —— 那两处要的是"看到最新"，且不在热路径上
    # （见 adapters/milvus_store.py 里两个常量的分工）。
    #
    # 这一档的差价是**一次等保证时间戳的往返**（本机实测中位：strong ~400ms / bounded ~2ms，
    # 与 k、与取哪些字段无关）；换来的语义是"写完立刻能搜到"。
    # 灌完立刻可见另有兜底：ingest 每批写完做一次 strong 的 `_count()` 回查，那次强读会把可见性推上去。
    #
    # 类默认保持 "strong"（= 这段代码一直以来的行为，默认值一改语义就变，有契约钉着）。
    # 要省那 400ms 就在 config.yaml 里写 bounded —— 那会失去"刚删完立刻搜不到"的保证。
    read_consistency: Literal["bounded", "eventually", "strong"] = "strong"
    # 稠密向量的索引类型与参数（FLAT / IVF_FLAT）。**类默认 FLAT** = 这段代码一直以来的行为；
    # 想快就在 config.yaml 里写 IVF_FLAT（近似检索，见 MilvusIndexConfig 的说明）。
    index: MilvusIndexConfig = MilvusIndexConfig()


class KeywordConfig(BaseModel):
    """关键字（词面）那一路：走哪份索引、BM25 的两个常数。

    两档后端（**一档决定"索引存哪"，也决定集合 schema 要不要稀疏列**）：

    * ``memory``（默认）—— 进程内 ``rank_bm25.BM25Plus``，语料从向量库全量读出后本地建索引。
      行为与一直以来的实现一致；代价是每进程约 111MB 常驻 + 冷启动约 2.1s（实测见
      ``adapters/bm25_index.py`` 的账）。
    * ``milvus`` —— 入库时把文档编码成**稀疏向量**写进同一个集合的稀疏列
      （``bm25_sparse`` 适配器），检索交给 Milvus 的 ``SPARSE_INVERTED_INDEX``。
      进程内不再需要全量语料与 jieba 索引，代价是**集合必须带稀疏列** ——
      换这一档要重灌（老集合没有那一列，写/查都会当场报错，不会静默降级）。

    ⚠️ 类默认是 ``memory``：它是这段代码一直以来的行为，而且换档要重灌语料，
    所以默认值不动，要换在 ``config.yaml`` 里写（有契约钉着，见 ``check_m1_1_config``）。
    """

    backend: Literal["memory", "milvus"] = "memory"
    # BM25 的两个常数（只对 milvus 那档生效；memory 那档用 rank_bm25 自己的默认值，
    # 恰好也是 1.5 / 0.75，所以两档的"形状"一致，只是变体不同：BM25Plus vs 标准 BM25）。
    bm25_k1: float = 1.5  # 词频饱和速度：越大越"词频说话"
    bm25_b: float = 0.75  # 长度归一强度：0 = 不归一，1 = 完全按长度惩罚

    @model_validator(mode="after")
    def _check(self) -> KeywordConfig:
        """输入:无。行为:两个 BM25 常数取值当场校验。输出:self。"""
        if self.bm25_k1 <= 0:
            raise ValueError(f"keyword.bm25_k1 必须 > 0，收到 {self.bm25_k1}")
        if not 0.0 <= self.bm25_b <= 1.0:
            raise ValueError(f"keyword.bm25_b 必须在 [0, 1] 内，收到 {self.bm25_b}")
        return self


class EmbeddingConfig(BaseModel):
    # 目录既能相对项目根,也能写绝对路径 —— 由下面的 model_path 负责解析
    model_dir: str = "models/bge-m3"
    # 先把位置定下来,免得后面改两处配置
    rerank_model_dir: str = "models/bge-reranker-v2-m3"
    device: str = "cpu"
    # 嵌入模型是否用 fp16。**与重排分开**：两者的收益与精度风险不是一回事。
    use_f16: bool = False
 # 重排模型是否用 fp16。实测（RTX 5080 Laptop，9 条 1104 字父块）
    #   fp32 398ms → fp16 101ms（**3.9×**）；嵌入侧 fp16 在批量 32 时 2.17 → 0.73ms/条（3×）。
    # 精度影响两边都要用评估集验（`data/eval/eval_set_bagu_pdf_v1.json` 那 50 条），
    # 所以默认 false —— **不改现有行为**，要开自己开，开完拿评估集说话。
    # 只在 CUDA 上生效（CPU 上 fp16 更慢，适配器里判 device）。
    rerank_use_f16: bool = False
    # 批处理:同刻排队的调用合成一批,减少小前向在 GPU 上互相切片。
    # ``*_batch_size`` = 单次前向的条数(显存旋钮);``*_max_*`` = 一次调度最多攒多少条
    # (吞吐与尾延迟的取舍)。单个调用没有额外等待:队列里只有它时立刻开跑。
    embed_batch_size: int = 32
    embed_batch_max_texts: int = 64
    rerank_batch_size: int = 32
    rerank_batch_max_pairs: int = 128

    @model_validator(mode="after")
    def _check_batching(self) -> EmbeddingConfig:
        """输入:无。行为:批处理四个旋钮必须 >= 1。输出:self。"""
        for name in (
            "embed_batch_size",
            "embed_batch_max_texts",
            "rerank_batch_size",
            "rerank_batch_max_pairs",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"embedding.{name} 必须 >= 1，收到 {value}")
        return self

    # 嵌入实现:**默认必须是真语义模型**。``hashing`` 只为演示与验收存在 ——
    # 它是字符 bigram 哈希(零依赖、零模型、确定性),只保证「文本越像、向量越近」,
    # **不保证语义相近**。默认值一旦被改成 hashing,没有模型的新机器会「看起来一切正常、
    # 检索全是噪声」,而这种坏法**不报错** —— 所以有条契约专门钉默认值(见 check_m3_rag)。
    # 演示用法(不必下载 4.4G 模型):`RAG_EMBEDDING__BACKEND=hashing`
    backend: Literal["bge_m3", "hashing"] = "bge_m3"
    # property装饰器,将方法调用转为属性调用
    @property
    def model_path(self) -> Path:
        """输入:无。行为:model_dir 解析成绝对路径(相对路径挂到项目根下)。输出:嵌入模型绝对路径。

        pathlib 的 / 遇到绝对右值会丢弃左值,所以 YAML 里写绝对路径也照样对。
        """
        return (PROJECT_ROOT / self.model_dir).resolve()

    @property
    def rerank_model_path(self) -> Path:
        """输入:无。行为:rerank_model_dir 解析成绝对路径。输出:重排模型绝对路径。"""
        return (PROJECT_ROOT / self.rerank_model_dir).resolve()


class RagConfig(BaseModel):
    """分块与检索参数。"""

    parent_chunk_size: int = 1200
    child_chunk_size: int = 300
    chunk_overlap: int = 50
    # ingest 的嵌入批大小。**不是切分参数**,是「一次喂几条给嵌入模型」——
    # 旋钮而非机制:CPU 上批量越大越划算(padding 摊销 + 矩阵乘吃满),
    # 显存小的机器反而要调小。原先在 ingest_documents 与 ingest --batch-size 各写一个 64。
    ingest_batch_size: int = 64
    retrieval_k: int = 5
    candidate_m: int = 2
    # 融合方式。两路 = 进程内 BM25 关键字 + Milvus 稠密向量(不用嵌入模型的稀疏向量:
    # 它和 BM25 都是词面匹配,重叠度高,留着只会让融合多一路、评估多一维)。
    #   rrf      —— 只用排名,免标定,对两路分数量纲不敏感
    #   weighted —— 各自归一化后加权,能表达「哪一路更可信」,但要调权重
    fusion: Literal["rrf", "weighted"] = "rrf"
    # RRF 的平滑常数:分数 = Σ w / (rrf_k + rank)。越大越弱化头部优势,60 是常见默认。
    rrf_k: int = 60
    # 两路权重:rrf 模式是排名权重,weighted 模式是分数权重。
    bm25_weight: float = 0.5
    vector_weight: float = 0.5
    # 重排:两路融合后,用 CrossEncoder 对**父块正文**再精排一次。
    #   顺序:召回 → 融合 → 折叠父块 → 重排 → 截断到 candidate_m
    #   CPU 上 ≈1.1 s/条(900 字),一条 query 多花 5~10s;关掉它就退回纯融合排序。
    #   它是评估用的消融开关,不是永久配置 —— 评估集上用它比「开/关重排」的指标
    rerank: bool = True

    @model_validator(mode="after")
    def _check_sizes(self) -> RagConfig:
        """输入:无。行为:在校验之后,再加一层自定义检验参数的逻辑。输出:self。

        为什么在配置层拦:overlap >= child_size 时切分器要么抛错、要么原地打转,
        等 ingest 跑一半才发现,代价是一整轮嵌入调用。
        """
        if self.chunk_overlap >= self.child_chunk_size:
            raise ValueError(
                f"chunk_overlap({self.chunk_overlap}) 必须小于 "
                f"child_chunk_size({self.child_chunk_size})"
            )
        if self.child_chunk_size > self.parent_chunk_size:
            raise ValueError(
                f"child_chunk_size({self.child_chunk_size}) 不能大于 "
                f"parent_chunk_size({self.parent_chunk_size})"
            )
        if self.bm25_weight <= 0 and self.vector_weight <= 0:
            raise ValueError(
                "bm25_weight 与 vector_weight 不能同时为 0 —— 两路都不给权重,"
                "融合结果全是 0,排序退化成随机"
            )
        if self.ingest_batch_size <= 0:
            raise ValueError(
                f"ingest_batch_size 必须为正,收到 {self.ingest_batch_size}"
            )
        if self.rrf_k < 0:
            raise ValueError(f"rrf_k 不能为负,收到 {self.rrf_k}")
        return self


class RewriteConfig(BaseModel):
    """查询改写:合流通道里 FAQ 未命中之后、向量检索之前那一步。

    四类策略(与训练数据同一套编号,见 ``domain/rewrite.py``):0 直接检索 /
    1 假设答案 / 2 子查询分解 / 3 去噪改写。

    **默认关**(``enabled=false``)。这不是保守,是因为它每一步都要**多花一次 LLM 调用**
    来生成检索语句,而收益取决于语料本身 —— 开之前该像 ``rag.rerank`` 那样先量一次,
    它是消融开关,不是"装了就该开"的开关。
    """

    enabled: bool = False
    # 判定器:``lora`` = `tools/lora` 微调出来的那个模型;``rule`` = 规则兜底
    # (准确率差得多,只在没有权重时用;见 `adapters/strategy_rule.py`)。
    classifier: Literal["lora", "rule"] = "lora"
    # 微调产出物与基座。**两份都不在版本库里**(`logs/`、`models/` 都在 .gitignore),
    # 所以"换了机器忘了同步"是最常见的失败形状 —— 缺了就在组合根降级成"原样检索"
    # 并记一条 WARNING,不让一个可选模块把整个问答拖下水。
    adapter_dir: str = "logs/lora/run1/lora_adapter"
    # 空 = 读 adapter_dir 旁边的 run_meta.json(训练时自己写下的那个基座路径),
    # 再退到 models/chinese-roberta-wwm-ext。人记得不如训练记录准。
    model_dir: str = ""
    # 分类器放哪跑。**默认 cpu**:卡上那点显存要留给嵌入与重排(BGE-M3 + reranker
    # 已经占着大头),而 4 分类的短句推理在 CPU 上是十几毫秒,不值得去抢。
    device: str = "cpu"
    # 与训练时一致(tools/lora/train.py)。不一致不会报错,只会让长短句的判定悄悄变样。
    max_length: int = 64
    # 子查询分解最多拆几句。上限防的是"模型把一句话拆成八段"——
    # 句数直接乘在检索耗时上(每句各跑一路召回)。
    max_subqueries: int = 3
    # 单句检索语句的长度上限(字符)。防模型吐一整篇假设答案把嵌入预算吃光。
    max_query_chars: int = 400
    # 低置信兜底阈值。**默认 0 = 不兜底**,这是实测结论不是偷懒:1000 条测试集上把
    # 低置信样本回退到"直接检索",准确率从 0.9700 掉到 0.9590(阈值 0.7)/ 0.9460(0.9),
    # 因为判错的样本真值大多落在第二位,回退没利用这个信息。
    # 读数见 `tools/lora/finetune_report.md`;换分类器时可以再调它。
    confidence_threshold: float = 0.0
    # 分类器批处理:同刻排队的判定合成一批前向(与嵌入/重排同一个 Batcher)。
    # 单条没有额外等待;batch_size = 单次前向条数,batch_max_texts = 单批上限。
    classifier_batch_size: int = 32
    classifier_batch_max_texts: int = 64

    @model_validator(mode="after")
    def _check_knobs(self) -> RewriteConfig:
        """输入:无。行为:拦下会让改写变成"白花钱"或"改坏"的取值。输出:self。

        为什么在配置层拦:这三个旋钮错了都不会当场报错 —— 拆 0 句会让改写产出空表
        (检索直接空转),长度上限太小会把假设答案截成半句。等到评估集上掉点才发现,
        中间隔着好几轮"是不是语料的问题"。
        """
        if self.max_subqueries < 1:
            raise ValueError(f"max_subqueries 至少为 1,收到 {self.max_subqueries}")
        if self.max_query_chars < 16:
            raise ValueError(
                f"max_query_chars 太小({self.max_query_chars}):假设答案会被截成半句,"
                "至少给 16"
            )
        if not 0.0 <= self.confidence_threshold < 1.0:
            raise ValueError(
                f"confidence_threshold 是概率阈值,必须落在 [0, 1)，收到 "
                f"{self.confidence_threshold}"
            )
        for name in ("classifier_batch_size", "classifier_batch_max_texts"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"rewrite.{name} 必须 >= 1,收到 {value}")
        return self


class OssConfig(BaseModel):
    """阿里云 OSS:图片对象的上传与临时 URL 签名。

    **只存 image_key,不存临时 URL**:签名 URL 会过期,把它写进向量库或历史表,
    第二天回看就全是死链。``image_key`` 是对象在桶里的稳定身份(内容 SHA-256),
    每次响应/每次调模型前现签一个短链即可。

    字段名刻意用 ``accesskey_id`` / ``accesskey_secret``(不带下划线),与现有
    ``.env`` 的 ``RAG_OSS__ACCESSKEY_ID`` 对齐;换成 ``ACCESS_KEY_ID`` 会让
    已经写好的环境变量**静默读不到**。
    """

    accesskey_id: SecretStr = SecretStr("")
    accesskey_secret: SecretStr = SecretStr("")
    region: str = "cn-guangzhou"
    bucket: str = ""
    # 后端上传/HEAD 用的 endpoint。本机/WSL 必须是公网地址;
    # 同地域 ECS 才可以用 ``-internal``(内网)。浏览器/模型取图用的是签名 URL,
    # 它不能指向内网地址 —— 那一条由 ``public_endpoint`` 兜住。
    endpoint: str = ""
    # 签名 URL 用的公网 endpoint。留空 = 与 endpoint 相同。
    public_endpoint: str = ""
    # 对象前缀。图片统一放在 ``<prefix>/<hash前2位>/<hash>.<ext>``。
    prefix: str = "rag/images"
    # 当前只实现 signed;proxy 是保留档(需要额外的图片代理路由)。
    url_mode: Literal["signed", "proxy"] = "signed"
    # 临时 URL 的有效期(秒)。太短模型还没取到就过期;太长等于把私有对象变相公开。
    sign_expires_s: int = 300
    connect_timeout_s: float = 5.0
    upload_timeout_s: float = 30.0
    max_retries: int = 3

    @property
    def configured(self) -> bool:
        """输入:无。行为:判断上传所需的最小字段是否齐全。输出:布尔。"""
        return bool(
            self.accesskey_id.get_secret_value()
            and self.accesskey_secret.get_secret_value()
            and self.bucket
            and self.endpoint
        )

    @field_validator("prefix")
    @classmethod
    def _clean_prefix(cls, value: str) -> str:
        """输入:对象前缀。行为:去掉首尾斜杠并拦住空值。输出:规范化前缀。"""
        cleaned = value.strip().strip("/")
        if not cleaned:
            raise ValueError("oss.prefix 不能为空")
        return cleaned

    @field_validator("endpoint", "public_endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        """输入:endpoint。行为:非空时必须带 http(s) 方案。输出:原值。"""
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(
                f"OSS endpoint 必须带 http:// 或 https://,收到 {value!r}"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_knobs(self) -> OssConfig:
        """输入:无。行为:把签名有效期与内网 endpoint 的组合当场拦住。输出:self。"""
        if not 30 <= self.sign_expires_s <= 86400:
            raise ValueError(
                f"oss.sign_expires_s 必须在 [30, 86400] 内,收到 {self.sign_expires_s}"
            )
        if self.connect_timeout_s <= 0 or self.upload_timeout_s <= 0:
            raise ValueError("OSS 的 connect/upload timeout 必须为正")
        if self.max_retries < 0:
            raise ValueError(f"oss.max_retries 不能为负,收到 {self.max_retries}")
        effective_public = self.public_endpoint or self.endpoint
        if self.url_mode == "signed" and "-internal." in effective_public:
            raise ValueError(
                "oss.url_mode=signed 但签名 URL 指向 -internal endpoint —— "
                "浏览器和模型都访问不到。请把 RAG_OSS__PUBLIC_ENDPOINT 设为公网地址,"
                "或者把 url_mode 改成 proxy(需要额外的图片代理路由)"
            )
        return self


class ImagesConfig(BaseModel):
    """图片模态的总开关与资源边界。

    ``allowed_types`` 是逗号分隔的字符串而不是 ``list[str]``:pydantic-settings 对
    ``list`` 字段默认按 **JSON** 解析,``image/jpeg,image/png`` 会被当成非法 JSON
    当场炸。这里在配置边界转成 ``frozenset``,别让环境变量的形状漏进业务代码。
    """

    enabled: bool = False
    max_per_doc: int = 50
    min_bytes: int = 2048
    max_bytes: int = 10 * 1024 * 1024
    allowed_types: str = "image/jpeg,image/png,image/webp"

    @property
    def allowed_type_set(self) -> frozenset[str]:
        """输入:无。行为:把逗号分隔的 MIME 白名单拆开。输出:小写 MIME 集合。"""
        return frozenset(
            item.strip().lower() for item in self.allowed_types.split(",") if item.strip()
        )

    @model_validator(mode="after")
    def _check_bounds(self) -> ImagesConfig:
        """输入:无。行为:校验数量/大小/类型白名单不是无意义的取值。输出:self。"""
        if self.max_per_doc < 1:
            raise ValueError(f"images.max_per_doc 至少为 1,收到 {self.max_per_doc}")
        if self.min_bytes < 0 or self.max_bytes <= 0:
            raise ValueError("images.min_bytes/max_bytes 不能为负")
        if self.min_bytes > self.max_bytes:
            raise ValueError(
                f"images.min_bytes({self.min_bytes}) 不能大于 max_bytes({self.max_bytes})"
            )
        if not self.allowed_type_set:
            raise ValueError("images.allowed_types 不能为空")
        return self


class MinerUConfig(BaseModel):
    """独立 MinerU 解析服务的客户端配置。

    MinerU 不跟着 app 进程走:它吃 CPU/GPU、单份 PDF 可能跑几分钟,而且模型依赖
    与 Web 服务不是一套。app 只提交任务、轮询结果,再把解析产物切成 Document。
    """

    base_url: str = "http://mineru:8000"
    token: SecretStr = SecretStr("")
    timeout_s: int = 3600
    poll_interval_s: float = 2.0
    cache_dir: str = "data/mineru"

    @field_validator("base_url")
    @classmethod
    def _clean_base_url(cls, value: str) -> str:
        """输入:服务地址。行为:去掉尾部斜杠并拦住空值。输出:规范化地址。"""
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            raise ValueError("mineru.base_url 不能为空")
        return cleaned

    @model_validator(mode="after")
    def _check_timing(self) -> MinerUConfig:
        """输入:无。行为:校验超时/轮询间隔。输出:self。"""
        if self.timeout_s <= 0:
            raise ValueError(f"mineru.timeout_s 必须为正,收到 {self.timeout_s}")
        if self.poll_interval_s <= 0:
            raise ValueError(f"mineru.poll_interval_s 必须为正,收到 {self.poll_interval_s}")
        return self


# pydantic的写法,也是配置类的标准写法,比__init__(self.env="dev"....)强大的多,可以设置内部去读取.env和yaml文件中的配置
# 优先级是根据SettingsConfigDict写的关键字顺序定的,越靠前的优先级越高
# 关于字段名的匹配,显式传入的参数字段名必须一一对应,.env中的字段大小写不敏感,但是要对应上.__表示层级
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,  # .env文件路径 ,这是第三优先级,第一优先级是传入的参数,第二优先级是环境变量
        env_prefix="RAG_",  # 环境变量写前缀防止全局命名冲突,加前缀声明去匹配,不匹配.yaml,用于环境变量和.env的匹配
        env_nested_delimiter="__",  # 嵌套配置的分隔符,默认是.是读取环境变量的嵌套结构的时候用的
        extra="ignore",  # 忽略多传入的字段
    )

    # 这些属性当成类属性的时候统一都是默认值,当成实例的时候会使用显示传入的参数,没有传入的才使用默认值
    # 以下数据必须符合类型,否则会报错,但是可以不传入,默认值会自动生效

    # 普通配置,优先传入的,若没有传入则使用下面定义的默认配置
    env: str = "dev"  # 声明当前环境是开发环境
    root_log_level: str = "INFO"  # 日志级别默认设置为正常信息
    console_log_level: str = "INFO"
    file_log_level: str = "INFO"
    log_dir: str = "logs"  # 日志保存目录,这里指项目根目录下的logs目录
    # 日志轮转。原先在 observability/logging_setup.py 里写死 10MB × 5 份 ——
    # 而 log_dir 早就是配置了,同一件事的另外两个参数却没有,属于不一致。
    log_max_bytes: int = 10 * 1024 * 1024  # 单个日志文件上限(写满就滚成 .1)
    log_backup_count: int = 5  # 保留几份旧日志(0 = 不保留)

    # 嵌套配置,通过类型注解声明要把yaml中的层级,转化为LLMConfig类型的对象,自动将字典解包传入这个类中,若没有传入则使用默认值
    llm: LLMConfig = LLMConfig()
    mysql: MySQLConfig = MySQLConfig()
    redis: RedisConfig = RedisConfig()
    faq: FaqConfig = FaqConfig()
    cache: CacheConfig = CacheConfig()
    milvus: MilvusConfig = MilvusConfig()
    keyword: KeywordConfig = KeywordConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    rag: RagConfig = RagConfig()
    # 查询改写(FAQ 未命中 → 向量检索之间那一步)。默认关,理由见 RewriteConfig。
    rewrite: RewriteConfig = RewriteConfig()
    conversation: ConversationConfig = ConversationConfig()
    api: ApiConfig = ApiConfig()
    # 消耗报表的单价表(见 CostConfig)。**与 llm 分开**:llm 说的是"怎么调模型",
    # cost 说的是"这些 token 值多少钱" —— 换代理/换套餐时只动后者。
    cost: CostConfig = CostConfig()
    # 图片模态。总开关默认关:关掉时 PDF 仍走原来的 PyMuPDF,MinerU/OSS 一次都不碰。
    images: ImagesConfig = ImagesConfig()
    oss: OssConfig = OssConfig()
    mineru: MinerUConfig = MinerUConfig()

    @model_validator(mode="after")
    def _check_image_stack(self) -> Settings:
        """输入:无。行为:图片模态开启时,把 OSS 必需字段一次性点齐。输出:self。

        为什么不是每个字段各写一个 validator:缺一个字段时,报错应该是一条能照着填的
        清单,而不是"启动到一半发现 bucket 为空"。关掉 ``images.enabled`` 时一个都不要求 ——
        纯文本部署不该被图片配置拦住。
        """
        if not self.images.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("RAG_OSS__ACCESSKEY_ID", self.oss.accesskey_id.get_secret_value()),
                ("RAG_OSS__ACCESSKEY_SECRET", self.oss.accesskey_secret.get_secret_value()),
                ("RAG_OSS__BUCKET", self.oss.bucket),
                ("RAG_OSS__ENDPOINT", self.oss.endpoint),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "images.enabled=true,但这些 OSS 配置是空的:"
                + "、".join(missing)
                + "。不想接图片就把 RAG_IMAGES__ENABLED 设成 false"
            )
        return self

    # 这个类方法,是用来定义配置的来源和优先级顺序的
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,  # 其他都是内置的,这里是自定义的
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """输入:pydantic-settings 传入的各配置源。行为:按优先级排序,并把 yaml 排在最后。输出:配置源元组。"""
        return (  # 这里是一个元组,元组中的元素顺序就是优先级,越靠前的优先级越高
            init_settings,  # 1. 显式传入
            env_settings,  # 2. 环境变量
            dotenv_settings,  # 3. .env
            file_secret_settings,
            # 4. 优先级第四,并且给他配置文件的路径
            YamlConfigSettingsSource(settings_cls, yaml_file=CONFIG_FILE),
        )


# 这个装饰器缓存了函数的输入输出，下次调用时如果传入的输入不变,直接返回缓存的结果，而不需要再次执行函数
@lru_cache
def get_settings() -> Settings:
    """输入:无。行为:检查配置文件存在后构建 Settings,结果被 lru_cache 缓存复用。输出:全局唯一配置对象。"""
    # Settings类返回最终的配置类
    yaml_file = CONFIG_FILE
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在：{yaml_file}")
    return Settings()


__all__ = [
    "CONFIG_FILE",
    "ENV_FILE",
    "PROJECT_ROOT",
    "project_path",
    "ApiConfig",
    "CacheConfig",
    "ConversationConfig",
    "EmbeddingConfig",
    "FaqConfig",
    "ImagesConfig",
    "LLMConfig",
    "MilvusConfig",
    "MinerUConfig",
    "MySQLConfig",
    "OssConfig",
    "RagConfig",
    "RedisConfig",
    "Settings",
    "get_settings",
]


if __name__ == "__main__":
    settings = get_settings()
    print(settings)
    print(settings.llm.base_url)
    print(settings.mysql.password)
