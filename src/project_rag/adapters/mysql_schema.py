"""MySQL 建表共用的一条口径:字符集与排序规则只在这里定义一次。

**为什么值得单独一个模块(不是洁癖,是一次真故障)**：建表语句散在五个适配器与一个工具里,
而"要不要写 ``COLLATE``"看着像细节 —— 直到两张表 JOIN 起来。

MySQL 8 的规则是:``DEFAULT CHARSET=utf8mb4`` **不带** ``COLLATE`` 时,用的是**字符集自己的**
默认排序规则(``utf8mb4_0900_ai_ci``),**不是**服务端/库的默认。本部署 compose 里钉的是
``--collation-server=utf8mb4_unicode_ci``,于是同一个库里长出**两族表**:

* 写了 ``COLLATE=utf8mb4_unicode_ci`` 的(``conversations`` / ``faq``);
* 只写 ``DEFAULT CHARSET=utf8mb4`` 的(其余全部 → ``utf8mb4_0900_ai_ci``)。

两族表按字符串列 JOIN 就是 ``1267 Illegal mix of collations``。而报错现场在**接口层**
(用户看到一个 500),离"某张表的 DDL 少写了 ``COLLATE``"隔着好几层 —— 会话列表那次
就是这么整页读不出来的。所以:**所有建表语句的尾巴都从这个常量来**,
``align_tables()`` 再把历史遗留的老表就地扳回来。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# 与部署的 ``--collation-server`` / 库默认一致(见 docker-compose.yaml 的 mysql 服务)
CHARSET = "utf8mb4"
COLLATION = "utf8mb4_unicode_ci"

# 所有 CREATE TABLE 的尾巴。**只写 CHARSET 不写 COLLATE 就会掉进上面那个坑**。
TABLE_OPTIONS = f"ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def align_tables(cur, tables: Sequence[str]) -> tuple[str, ...]:  # noqa: ANN001 - pymysql 游标
    """输入:游标 + **自己这几张**表名。行为:把排序规则不是 ``COLLATION`` 的表就地转换。

    输出:真的被改过的表名(没改就是空元组)。表不存在时跳过 —— 调用点不保证表已经建好。

    为什么要它:``CREATE TABLE IF NOT EXISTS`` **不会**给已存在的老表改排序规则。
    旧库里那些"只写了 CHARSET"的表要是不扳回来,任何跨族 JOIN 都还是 1267。
    转换只动排序规则(utf8mb4 家族内部),不动字符集与数据;幂等,且一次 information_schema
    查询就能判断要不要动手 —— 新库上是空转,不写日志。
    """
    names = [name for name in tables if _SAFE_TABLE.match(name)]
    if not names:
        return ()
    placeholders = ", ".join(["%s"] * len(names))
    cur.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = DATABASE() AND table_name IN (" + placeholders + ")"
        " AND table_collation IS NOT NULL AND table_collation <> %s",
        (*names, COLLATION),
    )
    stale = [str(row[0]) for row in cur.fetchall()]
    for table in stale:
        # 表名过了白名单(且来自我们自己的 list),拼进 DDL 是安全的
        cur.execute(f"ALTER TABLE {table} CONVERT TO CHARACTER SET {CHARSET} COLLATE {COLLATION}")
    return tuple(stale)
