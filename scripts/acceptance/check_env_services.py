#!/usr/bin/env python
"""环境自检：Milvus / MySQL / Redis（FAQ / 知识库验收的公共前置）。

用法：

    uv run python scripts/acceptance/check_env_services.py

做三件事：
  1. Milvus：连接 + 读服务端版本 + **走一次完整往返**（建集合 → 插入 → 建索引 →
     load → 检索 → 删除集合）。往返是最关键的：它能证明「客户端版本 + 服务端版本」
     这个搭配在你的机器上真的能跑，而不只是 TCP 连得上。
  2. MySQL：连接 + 版本 + 字符集 + 建临时表 / 插入 / 查询 / 删除表。
  3. Redis：连接 + PING。

连接参数从环境变量读（默认值与 ``config/config.yaml``、``docker-compose.yaml`` 保持一致）；
改这三处的默认值时要一起看。
"""

from __future__ import annotations

import os
import sys
import uuid

from _harness import check, report

MILVUS_URI = os.environ.get("MILVUS_URI", "http://127.0.0.1:10086")
MYSQL = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "10093")),
    "user": os.environ.get("MYSQL_USER", "rag"),
    "password": os.environ.get("MYSQL_PASSWORD", "1234"),
    "database": os.environ.get("MYSQL_DATABASE", "rag"),
    "charset": "utf8mb4",
}
REDIS = {
    "host": os.environ.get("REDIS_HOST", "127.0.0.1"),
    "port": int(os.environ.get("REDIS_PORT", "10087")),
    "password": os.environ.get("REDIS_PASSWORD", "1234"),
    "db": 0,
}


# ── Milvus ────────────────────────────────────────────────────────────────────
@check(f"Milvus 连接 + 服务端版本（{MILVUS_URI}）")
def _milvus_version() -> str:
    from pymilvus import MilvusClient, __version__

    client = MilvusClient(uri=MILVUS_URI, timeout=15)
    try:
        server = client.get_server_version()
    except Exception:  # noqa: BLE001 - 老客户端可能没有这个方法
        server = "(该客户端不支持查询服务端版本)"
    client.close()
    return f"客户端 pymilvus {__version__} ↔ 服务端 {server}"


@check("Milvus 完整往返（建集合 → 插入 → 建索引 → load → 检索 → 删集合）")
def _milvus_roundtrip() -> str:
    from pymilvus import DataType, MilvusClient

    name = f"env_check_{uuid.uuid4().hex[:8]}"
    client = MilvusClient(uri=MILVUS_URI, timeout=15)
    try:
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
        schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=4)
        client.create_collection(collection_name=name, schema=schema)

        client.insert(
            collection_name=name,
            data=[
                {"id": 1, "vector": [1.0, 0.0, 0.0, 0.0]},
                {"id": 2, "vector": [0.0, 1.0, 0.0, 0.0]},
                {"id": 3, "vector": [0.0, 0.0, 1.0, 0.0]},
            ],
        )

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="L2",
            params={"M": 8, "efConstruction": 64},
        )
        client.create_index(collection_name=name, index_params=index_params)
        client.load_collection(collection_name=name)

        hits = client.search(
            collection_name=name, data=[[1.0, 0.0, 0.0, 0.0]], limit=2
        )
        found = [hit["id"] for hit in hits[0]] if hits else []
        if not found:
            raise AssertionError("检索返回空结果")
        return f"检索 top-2 = {found}（期望含 1）"
    finally:
        try:
            client.drop_collection(collection_name=name)
        finally:
            client.close()


# ── MySQL ─────────────────────────────────────────────────────────────────────
@check("MySQL 连接 + 版本 + 字符集")
def _mysql_basic() -> str:
    import pymysql

    conn = pymysql.connect(connect_timeout=10, **MYSQL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            cur.execute("SELECT @@character_set_database, @@collation_database")
            charset, collation = cur.fetchone()
        if charset != "utf8mb4":
            raise AssertionError(
                f"库 {MYSQL['database']} 的字符集是 {charset}，中文会出问题；应为 utf8mb4"
            )
        return f"MySQL {version} | {charset}/{collation}"
    finally:
        conn.close()


@check("MySQL 建表 / 写入 / 查询（中文往返）")
def _mysql_roundtrip() -> str:
    import pymysql

    table = f"env_check_{uuid.uuid4().hex[:8]}"
    text = "退款流程怎么走？"
    conn = pymysql.connect(connect_timeout=10, autocommit=True, **MYSQL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {table} (id INT PRIMARY KEY, question VARCHAR(255)) "
                "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cur.execute(f"INSERT INTO {table} (id, question) VALUES (1, %s)", (text,))
            cur.execute(f"SELECT question FROM {table} WHERE id = 1")
            got = cur.fetchone()[0]
        if got != text:
            raise AssertionError(f"中文往返不一致：写入 {text!r}，读回 {got!r}")
        return f"中文往返 OK：{got}"
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            conn.close()


# ── Redis ─────────────────────────────────────────────────────────────────────
@check("Redis 连接 + PING")
def _redis_basic() -> str:
    import redis as redis_lib

    client = redis_lib.Redis(socket_connect_timeout=10, **REDIS)
    try:
        key = f"env_check:{uuid.uuid4().hex[:8]}"
        client.set(key, "1", ex=60)
        value = client.get(key)
        client.delete(key)
        if value != b"1":
            raise AssertionError("写入后读回不对")
        # redis-py 6.x 的 CoreCommands.info 返回泛型 ResponseT,pyright 把它推成
        # 「Awaitable[Any] | Any」的联合(同步/异步两个客户端共用同一份 mixin),
        # 而 Awaitable 那一支没有 .get。用 isinstance 收窄成 dict 即可 ——
        # 版本号只是报告里的装饰,连得上已经由上面的 set/get/delete 证明了。
        raw = client.info("server")
        info = raw if isinstance(raw, dict) else {}
        return f"Redis {info.get('redis_version')} | 读写 OK"
    finally:
        client.close()


def main() -> int:
    return report(
        "环境自检：Milvus / MySQL / Redis",
        hint=(
            "提示：连不上容器先看 Windows 侧 Docker Desktop 是否开着"
            "；再用 docker compose ps 确认状态，没起就 docker compose up -d。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())