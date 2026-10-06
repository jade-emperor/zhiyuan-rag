"""
mysql适配器,FAQ问答对的存储实现
"""

# 注解延迟求值(存成字符串),省掉导入时的求值开销
from __future__ import annotations

from collections.abc import Sequence

import pymysql

from project_rag.config import FaqConfig, MySQLConfig
from project_rag.domain import FaqEntry
from project_rag.ports import FaqStore


class MySQLFaqStore(FaqStore):
    def __init__(self, mysql_config: MySQLConfig, faq_config: FaqConfig) -> None:
        """输入:MySQL 配置 + FAQ 配置。行为:只记下连接参数和表名,不建连接。输出:无。"""
        self._config = mysql_config
        self._table = faq_config.table

    # 获得热题表中所有数据
    def load_all(self) -> Sequence[FaqEntry]:
        """输入:无。行为:连库查全表并按 id 排序,连不上抛错点名 host:port。输出:领域问答对序列。"""
        # 目标地址,主要用于错误日志,告诉到底是连不上谁
        target = f"{self._config.host}:{self._config.port}"
        # 建立mysql连接
        try:
            conn = pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),  # 取明文
                database=self._config.database,
                connect_timeout=self._config.connect_timeout_s,
                charset="utf8mb4",
                autocommit=True,  # 自动提交事务
            )
        except Exception as exc:
            raise RuntimeError(
                f"MySQL 连接失败（{target}）：{type(exc).__name__}: {exc}"
            ) from exc  # 保留原始异常信息链

        try:
            with conn.cursor() as cur:
                # 表名只来自配置,不来自用户输入;ORDER BY id 是契约要求的稳定顺序
                cur.execute(
                    f"SELECT id, question, answer FROM {self._table} ORDER BY id ASC"
                )
                rows = cur.fetchall()
        finally:
            conn.close()  # 无论成败都要关连接

        # 列表推导式,将数据库返回的每一行(列表套元组)都转成领域类型(列表套领域对象)
        return [
            FaqEntry(id=int(row[0]), question=str(row[1]), answer=str(row[2]))
            for row in rows
        ]


if __name__ == "__main__":
    from project_rag.config import get_settings

    s = get_settings()
    result = MySQLFaqStore(s.mysql, s.faq).load_all()
    for e in result:
        print(f"id:{e.id}    问题: {e.question}    回答: {e.answer}")
