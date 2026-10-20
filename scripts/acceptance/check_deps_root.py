#!/usr/bin/env python
"""组合根 `Deps` 的**形状契约**（字段名、默认值、冻结方式）。

用法：

    uv run python scripts/acceptance/check_deps_root.py

**这份为什么单独一个文件**：这段契约原来住在 `check_m2_faq.py` 里，而那份脚本属于
`--set local`（要真 FAQ 语料）—— 于是**常规回归根本不跑它**，同一个洞连咬两次：
稀疏列与磁盘索引那次加字段没同步、查询改写那次又没同步（`query_rewriter_factory`），
两次都是"红的契约躺了很久"（`check_m2_faq.py` 里原来那句注释已经写了第一次）。
按项目规矩，同一条教训第二次出现就该做成机制，而不是再改一遍数字 —— 机制就是
**把它放进 `--set ci` 会跑的文件里**（`run_all.py` 按 `check_*.py` 通配，所以这个文件
自动进 ci 档）。它零容器、零模型、零网络：只做 dataclass 自省 + 读一次配置。

契约：

* ``Deps`` 必须是 ``frozen=True, slots=True`` 的 dataclass（装配完不许再改）；
* 字段集 = **工厂**（默认 ``None``，「不注入就不造」）+ **静态标量/值对象**
  （默认从配置读，命令层负责交下去）；
* **加一个字段 = 改这份文件**，否则 ci 档当场红。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]


def _require(module_name: str, names: tuple[str, ...]):
    import importlib

    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(f"{module_name} 缺少公开成员 {missing}")
    return module


def _deps_cls():
    module = _require("project_rag.cli", ("Deps",))
    return module.Deps


@check("组合根：Deps 是 frozen+slots dataclass，字段名与默认值按契约")
def _() -> str:
    """字段集、默认值、冻结方式。

    **标题里刻意不写「N 个工厂」**：那两个数原来是手抄的，已经漂过两次
    （加字段时只改列表、忘了改标题；`cost_rule` 那次也是）。真正会红的是下面这张
    列表与 ``Deps`` 的差集，标题里那个数谁也读不到 —— 要报数就让**结果行**现算。
    """
    Deps = _deps_cls()
    if not dataclasses.is_dataclass(Deps):
        raise AssertionError("Deps 不是 dataclass；见本脚本头部契约第 1 条")
    if not getattr(Deps, "__dataclass_params__").frozen:
        raise AssertionError("Deps 应为 frozen=True（组合根一旦装配好就不该被子命令改）")
    if not getattr(Deps, "__slots__", None):
        raise AssertionError("Deps 应为 slots=True（跟领域模型风格一致）")

    fields = {f.name: f for f in dataclasses.fields(Deps)}
    want_factories = (
        "llm_factory",
        "faq_store_factory",
        "matcher_factory",
        "cache_factory",
        # RAG 通道的四个工厂。字段名也是契约 ——
        # 往 Deps 加字段就得同步这里（漏一个是 m2 先红,不是 m3）。
        "embedder_factory",
        "vector_store_factory",
        "keyword_index_factory",
        "reranker_factory",
        # 会话历史的工厂。字段名同样是契约 —— 加字段就得同步这里。
        "conversation_store_factory",
        # 延迟账本落表。默认 None = 不落表(只记日志),
        # 连不上 MySQL 时 `attach_ledger` 会退到这一档 —— 所以它**必须**允许为 None。
        "ledger_store_factory",
        # 认证与授权(用户/令牌/会话归属)。默认 None = **无认证模式**
        # —— 只可能来自显式注入(验收/本机),`build_real_deps` 永远接上。
        "auth_store_factory",
        "captcha_store_factory",
        "captcha_renderer_factory",
        # 文档登记表。默认 None = 不接(纯检索路径与老契约用不上)。
        "document_store_factory",
        # 上传入库要用的三样(切分器/装载函数/后缀表)。
        # 走工厂而不是让 api/ 直接 import 适配器 —— 分层法律有 AST 检查钉着。
        "chunker_factory",
        "loader_factory",
        "suffixes_factory",
        # 图片对象存储:入库上传、出参签临时 URL。默认 None = 图片功能没开
        # (纯文本部署与注入式验收不该被 OSS 配置拦住)。
        "image_store_factory",
        # 稀疏那一路（keyword.backend=milvus）与知识库清单的磁盘索引 —— 各自那次改动
        # 都往 Deps 加了字段，而这条契约没跟着改（check_m2_faq 只在 local 档跑，
        # 于是"红的契约"躺了很久）。加字段时**必须**同步这张表。
        "sparse_encoder_factory",
        "disk_files_factory",
        # 集合级运维 —— 列出集合、删掉一个集合。
        # Milvus 特有,所以包成两个函数放在组合根;命令层不 import pymilvus。
        "collections_factory",
        "drop_collection_factory",
        # 会话目录(前端左侧「我有哪些会话」)。
        # 与 conversation_store_factory 是**两张不同的表**(归属表 + 对话表),所以是两个工厂。
        "session_catalog_factory",
        # 限流计数器(默认 Redis,见 cli/deps.py 的 `_rate_limiter`)。
        "rate_limiter_factory",
        # 可见范围(组 / 组员 / 每份文档授权给谁)。默认 None = **不做可见范围**
        # (一条规则都没有时本来就人人可见,所以"不接"与"接了但没规则"行为一致);
        # `build_real_deps` 永远接上 —— 生产路径上没有"忘了配权限"这一档。
        "access_store_factory",
        # 查询改写(FAQ 未命中 → 向量检索之间那一步)。默认 None = **不改写** ——
        # 与其它工厂同一条口径,但这里的默认是**真的默认**:`build_real_deps` 只在
        # `rewrite.enabled=true` 时才接上(那一步要多花一次 LLM 调用,见 config.RewriteConfig)。
        # ⚠️ 这一行就是漏掉过一次的那个:契约当时住在 check_m2_faq.py(local 档),
        #    常规回归不跑,于是加了这个字段而契约没跟着改。
        "query_rewriter_factory",
    )
    # 思考档位。它**不是工厂** —— 是个静态标签,唯一用途是让延迟账里能看出
    # 「这一轮按哪一档跑的」。命令层把它转交出去,用例与账本都不认识 config。
    # cache_ttl 同理:缓存过期秒数也是「命令层从配置读出来、交给用例」的静态标量,
    # 它不该是工厂(不需要惰性构造),也不该由用例自己去读 config。
    # 多一个静态标量:消耗报表的两个值(单价表 + 币种)。同样由命令层从配置读出来
    # 交给用例/接口,理由与 cache_ttl 一样 —— 用例层不认识 config,验收也能注入自己的价目表。
    labels = (
        "faq_threshold",
        "cache_ttl",
        "thinking",
        "cost_prices",
        "cost_currency",
        # 多一个:时段规则(分时段计价靠它;窗口为空的规则 = 不分时段)。
        # 它是**解析好的值对象**,不是三个原始列表 —— 解析只发生在组合根。
        "cost_rule",
    )
    missing = [n for n in want_factories + labels if n not in fields]
    extra = sorted(set(fields) - set(want_factories) - set(labels))
    if missing or extra:
        raise AssertionError(
            f"Deps 字段不对：缺 {missing}，多 {extra}；应为 {want_factories + labels}"
        )
    for name in want_factories:
        if fields[name].default is not None:
            raise AssertionError(f"Deps.{name} 默认值应为 None（不注入就不造），实际 {fields[name].default!r}")
    if fields["thinking"].default is not None:
        raise AssertionError(
            f"Deps.thinking 默认应为 None（注入式调用不给账本编档位），实际 {fields['thinking'].default!r}"
        )
    # 两个静态标量（阈值 / TTL）的默认值是 **default_factory**：构造时才去读配置，
    # 所以这里直接构造一个裸 Deps，验「没注入时确实取自配置」。
    # 之前是两个字面量 + 一条"与 config 比对"的检查 —— 同一个值两个真源，
    # 而它防的开销（import config）实测只有 79~179ms，比真正的大头（import adapters = 3.5s）小 40 倍。
    from project_rag.application.cost import PeriodRule
    from project_rag.cli.deps import Deps as DepsClass
    from project_rag.config import get_settings

    settings = get_settings()
    for name, want in (
        ("faq_threshold", settings.faq.threshold),
        ("cache_ttl", settings.cache.ttl_s),
        ("cost_prices", settings.cost.prices()),
        ("cost_currency", settings.cost.currency),
        (
            "cost_rule",
            PeriodRule.of(
                weekdays=settings.cost.peak.weekdays,
                windows=settings.cost.peak.windows,
                holidays=settings.cost.holidays,
            ),
        ),
    ):
        got = getattr(DepsClass(), name)
        if got != want:
            raise AssertionError(
                f"裸 Deps().{name} 应取自配置（{want!r}），实际 {got!r}；"
                "默认值别写成字面量 —— 那会让同一个值有两个真源"
            )
    return (
        f"字段与默认值一致（{len(want_factories)} 个工厂 + {len(labels)} 个静态标量，"
        f"共 {len(fields)} 个字段；静态标量取自配置）"
    )



def main() -> int:
    return report(
        "组合根契约：Deps 的字段、默认值与冻结方式",
        hint="这份进 --set ci；加 Deps 字段时改这里（原来住在 check_m2_faq.py，只在 local 档跑，漏过两次）。",
    )


if __name__ == "__main__":
    sys.exit(main())
