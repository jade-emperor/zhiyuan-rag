#!/usr/bin/env python
"""M7.3 契约：管理员可观测看板 + 它依赖的 `capabilities.metrics`。

用法::

    uv run python scripts/acceptance/check_m7_3_metrics.py

**这一份在堵什么洞**：M7.1/M7.2 之前，「可观测」也是一张路线图页 —— 报表接口
（`GET /api/v1/reports/metrics`，P0-2）早就有了，但**没有界面**；而界面要画对，
得先知道后端的三条口径（`application/metrics.py` 的 docstring）：

* **`null` 不是 0**：`p95` 要 `n ≥ 20`、`p99` 要 `n ≥ 100`，不够就给 `None`。
  画成 0 会让人以为"很快" —— 这个项目已经吃过一次「指标名字会骗人」的亏（§5 第 24 条）；
* **分段只算热行，最慢榜含冷启动**：冷启动是用户真实等到的，不能从"最慢"里藏掉，
  也不能混进分位数（那会让 p95 变成"冷启动有多慢"）；
* **比率要带分子分母**：3/4 与 300/400 都是 75%，但只给百分比读不出样本量。

**前端不许写死白名单**：窗口（`1h/6h/24h/7d/30d`）与那三个样本量门槛现在由
`GET /api/v1/capabilities` 的 `metrics` 段报出来 —— 写死在前端的后果是后端改了窗口，
界面拿着一个 `400` 的窗口名去打（与 M7.1 的 `limits` 同一条规矩）。

公开契约::

    GET /api/v1/capabilities -> {…, metrics: {windows, default_window, top_max,
                                              min_n: {p50, p95, p99}}}
    GET /api/v1/reports/metrics?window=…&top=…
        # window 必须落在上面的白名单里（否则 400）;top ∈ [1, top_max]（否则 422）
        # 报表本身的口径由 check_m5_9_metrics.py 钉着（10 条），这里只钉"报给前端的那些数"

前端部分是静态契约（读源码）：`—` 不折算成 0、比率带分子分母、最慢榜标冷启动、
空窗口有话术、分段保持后端顺序、**能力自述的取数时机**（守卫补拉 / 「刷新」自救 /
空白名单的话术）。不需要容器 / 模型 / 网络。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"


def _read(relative: str) -> str:
    """输入:相对 `web/src/` 的路径。行为:读它。输出:正文(不在就报错,不静默给空串)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— M7.3 的看板还没落地?")
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """输入:一份前端源码。行为:去掉块注释与整行注释。输出:剩下的文本。

    只去整行 `//`：**解释口径的注释里一定会出现 `0`/`null` 这些字样**，
    不剥掉的话「注释」会把断言判红（§5 第 18 条那个坑）。
    """
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", without_block)


def _client():
    """输入:无。行为:建一个注入了假账本的 TestClient。输出:(client, 假账本)。"""
    from fastapi.testclient import TestClient

    from project_rag.api import create_app
    from project_rag.cli import Deps

    class _Ledger:
        def __init__(self) -> None:
            self.rows: list[dict[str, str]] = []

        def init_schema(self) -> None: ...

        def record(self, row) -> None:
            self.rows.append(dict(row))

        def load_since(self, since):
            return list(self.rows)

    ledger = _Ledger()
    # suffixes_factory 也要给:`capabilities` 会读它(上传那半边的能力),
    # 少一个工厂整条接口就 503 —— 那时 json() 里只有 detail,断言会以 KeyError 的样子出现
    deps = Deps(ledger_store_factory=lambda: ledger, suffixes_factory=lambda: (".pdf",))
    return TestClient(create_app(deps)), ledger


def _row(**over: str) -> dict[str, str]:
    """输入:要覆盖的字段。行为:在样板行上覆盖。输出:一行账本(值一律是文本)。"""
    base = {
        "label": "rag",
        "request_id": "r",
        "channel": "rag",
        "thinking": "-",
        "cold": "false",
        "builds": "0",
        "total_ms": "100.0",
        "ttft_ms": "50.0",
        "refused": "false",
        "truncated": "false",
        "aborted": "false",
        "rerank_ms": "20.0",
    }
    return {**base, **over}


# ══ 一、能力自述（行为契约） ═════════════════════════════════════════════════
@check("capabilities.metrics：窗口白名单与路由真正接受的是同一份")
def _() -> str:
    client, _ = _client()
    caps = client.get("/api/v1/capabilities").json()["metrics"]
    windows = caps["windows"]
    assert windows, "窗口白名单是空的"
    assert caps["default_window"] in windows, f"默认窗口 {caps['default_window']} 不在白名单里"

    from project_rag.api.app import _WINDOWS

    assert windows == list(_WINDOWS), f"capabilities 报 {windows},路由认 {list(_WINDOWS)}"

    for window in windows:
        response = client.get(f"/api/v1/reports/metrics?window={window}")
        assert response.status_code == 200, f"白名单里的 {window} 竟然不是 200:{response.text}"
    rejected = client.get("/api/v1/reports/metrics?window=42h")
    assert rejected.status_code == 400, rejected.text
    return f"白名单 {windows} 逐个 200;42h → 400;默认 {caps['default_window']}"


@check("capabilities.metrics.min_n：三个样本量门槛与聚合层常量同源")
def _() -> str:
    from project_rag.application.metrics import MIN_N_P50, MIN_N_P95, MIN_N_P99

    min_n = _client()[0].get("/api/v1/capabilities").json()["metrics"]["min_n"]
    assert min_n == {"p50": MIN_N_P50, "p95": MIN_N_P95, "p99": MIN_N_P99}, min_n
    assert MIN_N_P95 < MIN_N_P99, "p95 的门槛不该比 p99 还高"
    return f"p50≥{MIN_N_P50} / p95≥{MIN_N_P95} / p99≥{MIN_N_P99}(与聚合层同源)"


@check("capabilities.metrics.top_max：与出参边界同源（前端拖不到一个 422 的值）")
def _() -> str:
    from project_rag.api.schemas import TOP_MAX

    client, _ = _client()
    assert client.get("/api/v1/capabilities").json()["metrics"]["top_max"] == TOP_MAX
    assert client.get(f"/api/v1/reports/metrics?top={TOP_MAX}").status_code == 200
    for bad in ("0", str(TOP_MAX + 1)):
        response = client.get(f"/api/v1/reports/metrics?top={bad}")
        assert response.status_code == 422, f"top={bad} 应该 422,实际 {response.status_code}"
    return f"top_max={TOP_MAX};0 与 {TOP_MAX + 1} 都 422"


@check("报表口径在前端被如实画出来：null 给 —、比率给分子分母、最慢榜标冷启动")
def _() -> str:
    view = _read("views/MetricsView.vue")
    code = _strip_comments(view)
    assert re.search(r"if \(value === null \|\| value === undefined\) return '—'", code), (
        "分位数的 null 没有单独分支 —— 落到 toFixed 上会变成 0,那是这张表最会骗人的地方"
    )
    assert "minN.value[kind]" in code or "min_n" in code, "没有用 capabilities 报的门槛解释「—」"
    assert re.search(r"function whyMissing|样本不够", code), "「—」没有解释(要说出 n≥多少)"
    assert "share(" in code, "比率没有分子分母(只给百分比读不出样本量)"
    # 断言**显示出来的那两个字**跟着行数据走:只断言"源码里出现过 row.cold"太弱 ——
    # 把文案换成写死的 '热' 之后 `:class="row.cold ? …"` 还在,检查照样绿(RT-8 抓到的)
    assert re.search(r"\{\{\s*row\.cold \? '冷启动' : '热'\s*\}\}", code), (
        "最慢榜的冷热标签没有跟着 row.cold 走"
    )
    assert "含冷启动" in view, "没有说明最慢榜含冷启动(而分段只算热行)"
    return "— / 分子分母 / 冷启动标记 / 含冷启动说明 四处都在"


@check("窗口选项来自 capabilities；空窗口有话术；分段保持后端顺序")
def _() -> str:
    view = _read("views/MetricsView.vue")
    store = _read("stores/metrics.ts")
    assert "metrics.windows" in view or "caps.data?.metrics.windows" in view, (
        "窗口列表不是从 capabilities 来的(写死的话后端加窗口前端不知道)"
    )
    assert not re.search(r"""['"](?:1h|6h|24h|7d|30d)['"]""", _strip_comments(view)), (
        "视图里出现了写死的窗口字面量"
    )
    assert "default_window" in store, "默认窗口不是从 capabilities 取的"
    assert "还没有请求" in view, "空窗口没有话术(空白表格会被读成「坏了」)"
    assert not re.search(r"sort\(.*p95", view), "按 p95 重排了分段 —— 分段顺序是链路顺序,有意义"
    return "窗口/默认窗口都来自 capabilities;空窗口有话术;分段不重排"


@check("能力自述在守卫里补拉，「刷新」也能自救（直接落在 #/metrics 不是死局）")
def _() -> str:
    """能力自述的取数时机 —— 这是用户报过的一个真事故(见 `docs/STATUS.md` 事故表)。

    事故形态:capabilities 原来只由「登录页」与「对话页」挂载时拉一次,于是"直接落在
    `#/metrics`"(在这一页按 F5 / 书签 / 上次关在这一页)时,看板读到一个**空 store**,
    只回一句「还没读到 capabilities,不知道该请求哪个窗口」,而**点「刷新」读的还是同一个
    空 store** —— 后端明明是好的,这一页却自己把自己锁死了。两处都要有:

    1. 路由守卫:业务页入场前补拉(**必须 await**:不 await 的话视图挂载时还是空的;
       且必须在"已登录"之后 —— 登录页自己会拉);
    2. store:窗口名缺失时补拉一次并**转述真正的原因**(连不上后端…),不拿"还没读到
       capabilities"把"连不上"盖掉 —— 两件事的话术不一样。
    """
    router = _strip_comments(_read("router.ts"))
    store = _strip_comments(_read("stores/metrics.ts"))

    assert "beforeEach" in router, "路由守卫不见了?"
    guard = router[router.index("beforeEach") :]
    assert re.search(
        r"if \(capabilitiesState\(\)\.data === null\) await loadCapabilities\(\)", guard
    ), "守卫里没有 await 补拉 capabilities(不条件化的话每次导航都白打一枪)"
    assert guard.index("auth.user === null") < guard.index("await loadCapabilities()"), (
        "补拉放在了登录判断之前 —— 还没登录也会白打一枪(登录页自己会拉)"
    )
    assert "capabilitiesState().error" in store, (
        "窗口名缺失时没有转述能力那一侧的真实错误(连不上后端会被说成「还没读到 capabilities」)"
    )
    retry = store[store.index("export async function loadMetrics") :]
    assert "await loadCapabilities()" in retry[: retry.index("state.loading = true")], (
        "「刷新」不会重拉能力 —— 后端恢复后这一页仍然是死局"
    )
    return "守卫(已登录之后)条件化 await 补拉;store 在窗口名缺失时补拉并转述真错"


@check("白名单还没读到时不冒充「不支持的类型」（两件事的话术不能混）")
def _() -> str:
    """空白的 `suffixes` = "还没读到 capabilities",不是"后端不认这个后缀"。

    以前混成一句话,用户拖一份 `.docx` 上去看到的是「不支持的类型 .docx —— 这个后端
    只吃 」(后面空着)—— 被指到"后端不支持"这个错方向去查。
    """
    panel = _strip_comments(_read("components/UploadPanel.vue"))
    assert "suffixes.value.length === 0" in panel, "空白名单没有单独分支"
    assert panel.index("suffixes.value.length === 0") < panel.index("不支持的类型"), (
        "先判了后缀再看白名单是否为空 —— 空的时候会说成「不支持的类型 …」"
    )
    assert "还没读到后端能力" in panel, "空白名单的话术没说是「没读到能力」"
    return "空白名单单独一支:说「还没读到后端能力」并指路左上角「后端」;非空才判后缀"


@check("报表按窗口取数：1h 与 24h 传给账本的起点真的差 23 小时")
def _() -> str:
    from datetime import datetime, timedelta

    client, ledger = _client()
    seen: list[datetime] = []

    def load_since(since):
        seen.append(since)
        return []

    ledger.load_since = load_since  # type: ignore[method-assign]
    assert client.get("/api/v1/reports/metrics?window=1h").status_code == 200
    assert client.get("/api/v1/reports/metrics?window=24h").status_code == 200
    assert len(seen) == 2, seen
    now = datetime.now()
    for since, hours in zip(seen, (1, 24)):
        drift = abs((now - since) - timedelta(hours=hours))
        assert drift < timedelta(minutes=5), f"窗口 {hours}h 的起点偏了 {drift}"
    # 24h 的起点更早,所以是 1h 的起点减它 —— 反过来会得到 -23 小时(第一版就这么写反了)
    assert abs((seen[0] - seen[1]) - timedelta(hours=23)) < timedelta(minutes=5), seen
    return "1h/24h 的 since 分别落在 now-1h / now-24h(误差 < 5 分钟)"


@check("接口层不把「样本不够」折算成 0：p95 仍是 null，而最大/中位数照常")
def _() -> str:
    client, ledger = _client()
    # 3 行:rerank 只出现 3 次(< p95 门槛 20),total_ms 每行都有
    ledger.load_since = lambda since: [  # type: ignore[method-assign]
        _row(request_id=f"r{i}", total_ms=str(100 + i), rerank_ms="20.0") for i in range(3)
    ]
    body = client.get("/api/v1/reports/metrics?window=24h").json()
    segments = {segment["name"]: segment for segment in body["segments"]}
    rerank = segments["rerank_ms"]
    assert rerank["n"] == 3, rerank
    assert rerank["p95"] is None and rerank["p99"] is None, f"样本不够必须是 null:{rerank}"
    assert rerank["p50"] == 20.0 and rerank["maximum"] == 20.0, rerank
    total = segments["total_ms"]
    assert total["n"] == 3 and total["p95"] is None, "total_ms 的 n 也是 3,同样不该给 p95"
    assert total["maximum"] == 102.0, total
    return "n=3 时 p95/p99 是 null(不是 0),p50/max 照常给"


@check("报表把比率的分子分母都给出来（前端才画得出 3/4 与 300/400 的区别）")
def _() -> str:
    client, ledger = _client()
    ledger.load_since = lambda since: [  # type: ignore[method-assign]
        _row(request_id="a", refused="true"),
        _row(request_id="b", truncated="true"),
        _row(request_id="c", aborted="true", cold="true", total_ms="500.0"),
        _row(request_id="d", label="faq", channel="faq"),
        _row(request_id="e"),
    ]
    body = client.get("/api/v1/reports/metrics?window=24h&top=3").json()
    assert body["rows"] == 5, body
    assert (body["refused"], body["truncated"], body["aborted"]) == (1, 1, 1), body
    assert body["faq_hits"] == 1 and body["rag_answers"] == 4, body
    assert body["cold_rows"] == 1 and body["warm_rows"] == 4, body
    assert len(body["slowest"]) == 3, "top=3 应该只回 3 条最慢"
    assert body["slowest"][0]["cold"] is True, "最慢榜要含冷启动,而且 cold 标记要透出来"
    return "rows/refused/truncated/aborted/faq_hits/cold_rows 齐;top 生效;最慢榜带 cold"


# ══ 二、前端静态 ═════════════════════════════════════════════════════════════
@check("看板不再是路线图页：接的是报表接口，不是「M7.3 交付」那段文案")
def _() -> str:
    view = _read("views/MetricsView.vue")
    assert "M7.3 交付" not in view, "路线图页的文案还在 —— 这一页现在应该是真页面"
    assert "metrics" in _read("api/endpoints.ts"), "endpoints.ts 没有报表函数"
    for call in ("loadMetrics", "setWindow"):
        assert call in view, f"视图没有调 {call}"
    for field in ("p95", "p99", "cold_rows", "refused", "aborted", "slowest"):
        assert field in view, f"看板没有画 {field}(报表里最该看的几项)"
    return "接上报表接口;p95/p99/冷热/拒答/中断/最慢榜都画了"


def main() -> int:
    return report(
        "M7.3 管理员可观测看板 + capabilities.metrics",
        hint="前端改动后先 `cd web && npm run build`；产物要入库（Python 侧与镜像都不装 node）",
    )


if __name__ == "__main__":
    sys.exit(main())
