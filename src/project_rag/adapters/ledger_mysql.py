"""
MySQL 账本适配器:一行账本写进 ``latency_ledger``,按时间窗读回来做聚合。

**为什么整行再存一份 JSON**(``payload``):账本的字段是会长的 —— 2026-10-06 这轮就一次加了
三个(``request_id`` / ``cold`` / ``builds``)。表结构不该每加一个指标就改一次,所以:
**查询用得到的**几列单独拎出来建索引(时间、追踪号、冷热、总数、三个布尔),
**全部字段**进 ``payload``。聚合在 Python 里做(一天几百行的量级,不值得写 SQL 聚合),
于是加指标 = 只改账本,这里一行不用动。

**为什么每次写各连一次库**:一次回答写一行,频率是"每次提问"而不是"每毫秒";
换连接池要多一份生命周期管理(超时、重连、线程安全),而收益在毫秒级 —— 不值。
真到高频再说(记在 `docs/deferred.md` 第 57 条)。**这条路上的失败必须抛出去**:
降级是 ``LedgerHandler`` 的事,它吞掉并计数;在这里偷偷吞掉,"表里少了一半数据"就没人发现。

**为什么 ts 由这里盖**:账本用的是 ``time.perf_counter()``(单调时钟,量间隔用的,
没有"绝对时刻"的含义)。落表要的是墙上时间,由存储层盖戳,**账本本体不必知道日历**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

import pymysql

from project_rag.config import MySQLConfig
from project_rag.ports import LedgerStore

# 表名白名单:与 conversation_mysql 同一套做法(表名会拼进 SQL,而它是配置来的)
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_TABLE = "latency_ledger"

# 建表语句。**加索引列 = 改这里 + 改 _indexed()**,payload 里的字段不用动。
_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id VARCHAR(64) NOT NULL,
  ts DATETIME(3) NOT NULL,
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
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


class MySQLLedgerStore(LedgerStore):
    """``latency_ledger`` 的读写。构造不连库,第一次用到才连(与其它适配器一致)。"""

    def __init__(self, config: MySQLConfig, table: str = _DEFAULT_TABLE) -> None:
        """输入:MySQL 配置、表名(过白名单)。行为:记下参数,不建连接。输出:无。"""
        if not _SAFE_TABLE.match(table):
            raise ValueError(f"表名不合法:{table!r}(只允许字母数字下划线,且不以数字开头)")
        self._config = config
        self._table = table

    def init_schema(self) -> None:
        """输入:无。行为:建表(幂等,``IF NOT EXISTS``)。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_DDL.format(table=self._table))
        finally:
            conn.close()

    def record(self, row: Mapping[str, str]) -> None:
        """输入:一行账本。行为:索引列拆出来 + 整行进 payload,插一条。输出:无。"""
        ts = datetime.now()
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
            f"(ts, {', '.join(_COLUMNS)}, payload) VALUES (%s, "
            f"{', '.join(['%s'] * len(_COLUMNS))}, %s)"
        )
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (ts, *values, json.dumps(dict(row), ensure_ascii=False)))
        finally:
            conn.close()

    def load_since(self, since: datetime) -> Sequence[Mapping[str, str]]:
        """输入:起始时刻(含)。行为:按 ts 升序取回 payload。输出:行字典序列。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT payload FROM {self._table} WHERE ts >= %s ORDER BY ts ASC", (since,)
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        out: list[Mapping[str, str]] = []
        for (payload,) in rows:
            # pymysql 对 JSON 列返回的是 str(它不替我们解析);解析失败的行**跳过而不是炸** ——
            # 一行坏数据不该让整张报表打不开。
            loaded = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
            if isinstance(loaded, dict):
                out.append(loaded)
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
