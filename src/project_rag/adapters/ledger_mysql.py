"""
MySQL 账本适配器:一行账本写进 ``latency_ledger``,按时间窗读回来做聚合。

**为什么整行再存一份 JSON**(``payload``):账本的字段是会长的(``request_id`` /
``cold`` / ``builds`` 这三个就是这么来的)。表结构不该每加一个指标就改一次,所以:
**查询用得到的**几列单独拎出来建索引(时间、追踪号、冷热、总数、三个布尔),
**全部字段**进 ``payload``。聚合在 Python 里做(一天几百行的量级,不值得写 SQL 聚合),
于是加指标 = 只改账本,这里一行不用动。

**为什么每次写各连一次库**:一次回答写一行,频率是"每次提问"而不是"每毫秒";
换连接池要多一份生命周期管理(超时、重连、线程安全),而收益在毫秒级 —— 不值。
真到高频再说(目前不做)。**这条路上的失败必须抛出去**:
降级是 ``LedgerHandler`` 的事,它吞掉并计数;在这里偷偷吞掉,"表里少了一半数据"就没人发现。

**为什么 ts 由这里盖**:账本用的是 ``time.perf_counter()``(单调时钟,量间隔用的,
没有"绝对时刻"的含义)。落表要的是墙上时间,由存储层盖戳,**账本本体不必知道日历**。

**ts 存 UTC,并单独记下盖戳时的时区偏移**(``ts_offset`` 列) —— 这是分时段计价逼出来的:
价目表按**北京时间**分高峰/空闲(差一倍的价钱),而"14:00"离开时区就不是一个时刻。
实测这个项目里**同一张表有两种写入者**:容器里的 app 进程钟是 UTC、宿主上的脚本是 CST
(容器 ``date`` → UTC,宿主 → CST),只存一个裸钟点的话,两者相差 8 小时 ——
足以把高峰判成空闲,而报表上只会显示一个"合理"的金额。

所以:``ts`` 一律存 UTC(新行),``ts_offset`` 记下盖戳者的 UTC 偏移(新行是 ``+00:00``);
**没有 ``ts_offset`` 的行 = 老账**(那时只存了裸钟点,不知道写它的是哪个钟)——
读回来时**不替它猜**,而还给用例层一个不带时区的时间戳,由报表按"判不出来"处理
(按高峰价计 + 计数,见 ``application/cost.py``)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS, align_tables
from project_rag.observability import get_logger
from project_rag.config import MySQLConfig
from project_rag.ports import LedgerStore
from project_rag.ports.ledger import LEDGER_TS

# 表名白名单:与 conversation_mysql 同一套做法(表名会拼进 SQL,而它是配置来的)
_logger = get_logger("adapters.ledger_mysql")

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_TABLE = "latency_ledger"

# 盖戳时区那一列的宽度:正好装得下 "+08:00"。
_OFFSET_WIDTH = 8

# 建表语句。**加索引列 = 改这里 + 改 _indexed() + 改 _ensure_columns()**,
# payload 里的字段不用动。
_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id VARCHAR(64) NOT NULL,
  ts DATETIME(3) NOT NULL,
  ts_offset VARCHAR(8) NULL,
  label VARCHAR(16) NOT NULL,
  channel VARCHAR(16) NULL,
  cold TINYINT(1) NOT NULL,
  builds INT NOT NULL,
  total_ms DOUBLE NULL,
  ttft_ms DOUBLE NULL,
  refused TINYINT(1) NOT NULL,
  truncated TINYINT(1) NOT NULL,
  aborted TINYINT(1) NOT NULL,
  payload JSON NOT NULL,
  KEY idx_ts (ts),
  KEY idx_request_id (request_id)
) {options}
"""

# 一行账本要按这些字段插进去(其余进 payload)。口径:查得到 = 值得索引。
_COLUMNS = (
    "request_id",
    "label",
    "channel",
    "cold",
    "builds",
    "total_ms",
    "ttft_ms",
    "refused",
    "truncated",
    "aborted",
)


def _flag(value: str) -> int:
    """输入:账本里的 ``true``/``false`` 文本。行为:转成 0/1。输出:整数。"""
    return 1 if value == "true" else 0


def _float_or_none(value: str) -> float | None:
    """输入:账本里的数字文本或 ``-``。行为:``-`` 转 None。输出:浮点或 None。"""
    return None if value in ("", "-") else float(value)


def _offset_text(moment: datetime) -> str:
    """输入:一个带时区的时刻。行为:把它相对 UTC 的偏移写成 ``±HH:MM``。输出:文本。

    写成偏移而不是时区名(``Asia/Shanghai``):读的一侧不需要 IANA 时区库就能还原出绝对时刻
    (容器里常常没有 tzdata),而"中国没有夏令时"这件事让固定偏移就是准确的。
    """
    total = int((moment.utcoffset() or timedelta(0)).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _stamp(ts: datetime, offset: str | None) -> str:
    """输入:库里的裸钟点 + 那一行的偏移文本。行为:拼成 ISO-8601。输出:文本。

    **有偏移 = 绝对时刻**,读的一侧换算成北京时间判高峰/空闲;**没有偏移 = 老账**,
    原样返回一个裸钟点,让报表按"时段判不出来"处理(按高峰价计 + 计数)。
    """
    if offset is None:
        return ts.isoformat()
    total = (1 if offset[0] == "+" else -1) * (int(offset[1:3]) * 60 + int(offset[4:6]))
    return ts.replace(tzinfo=timezone(timedelta(minutes=total))).isoformat()


class MySQLLedgerStore(LedgerStore):
    """``latency_ledger`` 的读写。构造不连库,第一次用到才连(与其它适配器一致)。"""

    def __init__(self, config: MySQLConfig, table: str = _DEFAULT_TABLE) -> None:
        """输入:MySQL 配置、表名(过白名单)。行为:记下参数,不建连接。输出:无。"""
        if not _SAFE_TABLE.match(table):
            raise ValueError(f"表名不合法:{table!r}(只允许字母数字下划线,且不以数字开头)")
        self._config = config
        self._table = table

    def init_schema(self) -> None:
        """输入:无。行为:建表 + 补历史表缺的列 + 对齐排序规则(都幂等)。输出:无。

        老表(加 ``ts_offset`` 之前建的)不会因为 ``CREATE TABLE IF NOT EXISTS`` 长出这一列,
        所以这里显式补一次 —— 幂等由 ``information_schema`` 查一下保证。排序规则同理
        (老表只写了 ``CHARSET``,会与另一族表 JOIN 报 1267,见 ``adapters/mysql_schema.py``)。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_DDL.format(table=self._table, options=TABLE_OPTIONS))
                stale = align_tables(cur, (self._table,))
                if stale:
                    _logger.info("排序规则已对齐到 %s(老表):%s", COLLATION, "、".join(stale))
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
                    (self._table, "ts_offset"),
                )
                found = cur.fetchone()
                if not found or not found[0]:
                    cur.execute(
                        f"ALTER TABLE {self._table} "
                        f"ADD COLUMN ts_offset VARCHAR({_OFFSET_WIDTH}) NULL"
                    )
        finally:
            conn.close()

    def record(self, row: Mapping[str, str]) -> None:
        """输入:一行账本。行为:索引列拆出来 + 整行进 payload,插一条。输出:无。

        ``ts`` 存 **UTC**(与库/容器的本地时区无关),另把盖戳时的偏移写进 ``ts_offset``
        —— 于是"这一次调用发生在几点"在任何机器上读出来都是同一个绝对时刻。
        """
        now = datetime.now(UTC)
        ts = now.replace(tzinfo=None)
        values = (
            str(row.get("request_id") or "-"),
            str(row.get("label") or "-"),
            (str(row["channel"]) if row.get("channel") not in (None, "-") else None),
            _flag(str(row.get("cold"))),
            int(row.get("builds") or 0),
            _float_or_none(str(row.get("total_ms", "-"))),
            _float_or_none(str(row.get("ttft_ms", "-"))),
            _flag(str(row.get("refused"))),
            _flag(str(row.get("truncated"))),
            _flag(str(row.get("aborted"))),
        )
        sql = (
            f"INSERT INTO {self._table} "
            f"(ts, ts_offset, {', '.join(_COLUMNS)}, payload) VALUES (%s, %s, "
            f"{', '.join(['%s'] * len(_COLUMNS))}, %s)"
        )
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (ts, _offset_text(now), *values, json.dumps(dict(row), ensure_ascii=False)))
        finally:
            conn.close()

    def load_since(self, since: datetime) -> Sequence[Mapping[str, str]]:
        """输入:起始时刻(含)。行为:按 ts 升序取回 payload(**并把 ts 附在行里**)。输出:行字典序列。

        ``since`` 可以是**带时区的绝对时刻**(最好),也可以是裸钟点 —— 后者按**本进程的本地时区**
        解释(宿主上是 CST、容器里是 UTC,两边都对)。列里存的是 UTC,所以比较前先换算过去。

        **为什么要把 ts 塞回行里**:分时段计价(高峰 / 空闲)要问"这一次调用发生在几点",
        而账本本体只有 ``time.perf_counter()``(单调时钟)—— 墙上时间只在这个表里有。
        键名用 ``ports.ledger.LEDGER_TS``,并且有契约盯着"账本自己的字段表里没有这个名字"。
        """
        moment = since if since.tzinfo is not None else since.astimezone()
        bound = moment.astimezone(UTC).replace(tzinfo=None)
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ts, ts_offset, payload FROM {self._table} "
                    "WHERE ts >= %s ORDER BY ts ASC",
                    (bound,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        out: list[Mapping[str, str]] = []
        for ts, offset, payload in rows:
            # pymysql 对 JSON 列返回的是 str(它不替我们解析);解析失败的行**跳过而不是炸** ——
            # 一行坏数据不该让整张报表打不开。
            loaded = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
            if isinstance(loaded, dict):
                # 不覆盖 payload 里同名键(理论上不会有,真撞上时以库里的列为准并留个痕)
                out.append({**loaded, LEDGER_TS: _stamp(ts, offset)})
        return out

    def _connect(self, *, autocommit: bool):
        """输入:是否自动提交。行为:连库,失败时点名 host:port。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),
                database=self._config.database,
                charset="utf8mb4",
                connect_timeout=self._config.connect_timeout_s,
                autocommit=autocommit,
            )
        except pymysql.MySQLError as exc:
            raise RuntimeError(f"连不上 MySQL {target}:{exc}") from exc
