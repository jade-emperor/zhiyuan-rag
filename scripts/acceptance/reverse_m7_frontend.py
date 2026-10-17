#!/usr/bin/env python
"""的反向测试：把前端契约与检索旋钮改回坏样子，确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m7_frontend.py

**为什么专门有一份**：`check_m7_frontend.py` 18 条里有好几条是"静态契约"——
读源码与构建产物做正则/AST 断言。静态契约的老毛病是**它可能永远为真**
(写错了正则、锚点漂了、只检查了一个不可能失败的东西)，而"没红过的检查 = 没有检查"
这里每条都真的把代码改坏一次,再看那一行变没变红。

十二条各自的坏法,共同点是**都不会报错**:

* RT-1 拆掉「mode 与权重不能同时给」那条校验（两个真值来源并存）;
* RT-2 `mode` 不再翻译成权重（"只用关键字"变成两路都跑，结果对、延迟账错）;
* RT-3 根路径直接回产物文件、不再跳 app（URL 不以 `/static/app/` 结尾）;
* RT-4 会话目录不再按账号过滤（谁的会话都能列出来）;
* RT-5 改了前端源码但不重新构建（仓库里跑的界面与源码不是一份东西）;
* RT-6 前端把后缀表写死（后端加格式前端不知道）;
* RT-7 WS 不再带令牌（开了认证后前端一条也问不出来）;
* RT-8 管理页签不再按角色渲染（员工看到必然 403 的按钮）;
* RT-9 `rerank=true` 但没装重排器时静默降级（用户勾了重排拿到没重排的结果）;
* RT-10 capabilities 报的上限与出参边界漂开（前端夹取夹到一个 422 的值）;
* RT-11 `endpoints.ts` 里绕开 `API_PREFIX` 写死老路径（前缀就有两处真值来源）;
* RT-12 前端清单把 WS 指到一条 **HTTP** 路由（路径都在 `/api/v1` 下，页面却连不上）。

RT-11 / RT-12 是 补的：内置调试页删掉时，它原来守着的那两条性质
（"页面走的是服务端那条 WS"、"令牌进 WS 的 query"）不能跟着文件一起消失 ——
后一条由 RT-7 接着，前一条搬到了「前端清单↔后端路由表」那条契约上，这两条反向测试
就是它的反向证明。

改源码一律走 ``_harness.patched_source``（先落备份、退出时还原，被打断也能自愈）;
RT-5 改的是前端源码**内容**（摘要只认内容），所以还原之后契约会自己变绿。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "project_rag" / "api" / "app.py"
SCHEMAS = ROOT / "src" / "project_rag" / "api" / "schemas.py"
ENDPOINTS_TS = ROOT / "web" / "src" / "api" / "endpoints.ts"
CLIENT_TS = ROOT / "web" / "src" / "api" / "client.ts"
HEADER_VUE = ROOT / "web" / "src" / "components" / "AppHeader.vue"
CONFIG_PANEL = ROOT / "web" / "src" / "components" / "ConfigPanel.vue"
SCRIPT = "check_m7_frontend.py"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  拆掉「mode 与权重不能同时给」（两个真值来源并存）",
        target=SCHEMAS,
        old="""        if self.mode is not None and (
            self.bm25_weight is not None or self.vector_weight is not None
        ):
            raise ValueError(
                "mode 与 bm25_weight/vector_weight 不能同时给:模式已经决定跑哪几路"
            )
""",
        new="",
        needle="mode 与权重不能同时给",
    ),
    Case(
        title="RT-2  mode 不再翻译成权重（「只用关键字」变成两路都跑）",
        target=APP,
        old="""    mode: RetrievalMode = request.mode or "hybrid"
    if mode == "vector":
        return 0.0, vector  # 关掉关键字那一路
    if mode == "keyword":
        return bm25, 0.0  # 关掉向量那一路
    return bm25, vector""",
        new="    return bm25, vector",
        needle="mode 翻译成权重",
    ),
    Case(
        title="RT-3  根路径直接回产物文件、不再跳 app（URL 不以 /static/app/ 结尾 → 相对资源解析错位）",
        target=APP,
        old='        return RedirectResponse(url="/static/app/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)',
        new='        return FileResponse(_STATIC_DIR / "app" / "index.html")  # type: ignore[return-value]',
        needle="根路径跳前端 app",
    ),
    Case(
        title="RT-4  会话目录不再按账号过滤（谁的会话都能列出来）",
        target=APP,
        old="        rows = await _call(catalog.list_for, user.user_id, limit=limit)",
        new='        rows = await _call(catalog.list_for, "u-admin", limit=limit)',
        needle="sessions：无认证给空表",
    ),
    Case(
        title="RT-5  改了前端源码但不重新构建（界面与源码不是一份东西）",
        target=CONFIG_PANEL,
        old="      <h4>检索设置</h4>",
        new="      <h4>检索设置（改过但没构建）</h4>",
        needle="前端产物与源码同源",
    ),
    Case(
        title="RT-6  前端把能力闭集写死（后端加格式前端不知道）",
        target=CONFIG_PANEL,
        old="const params = paramsState()",
        new="const params = paramsState()\nconst HARDCODED_SUFFIXES = ['.pdf', '.docx']",
        needle="能力不写死在前端",
    ),
    Case(
        title="RT-7  WS 不再带令牌（开了认证后前端一条也问不出来）",
        target=CLIENT_TS,
        old="  url.search = token ? `?token=${encodeURIComponent(token)}` : ''",
        new="  url.search = ''",
        needle="WebSocket 带 ?token=",
    ),
    Case(
        # 页签改按**能力**渲染(报表这一页要 can_view_reports —— 运维也看得到)。
        # 坏版本 = 把能力判断去掉,于是没这个能力的人也看到一个必然 403 的入口。
        title="RT-8  页签不再按能力渲染（没权限的人也看到必然 403 的入口）",
        target=HEADER_VUE,
        old='      <RouterLink v-if="canViewReports" class="tab" :to="{ name: \'reports\' }">报表</RouterLink>',
        new='      <RouterLink class="tab" :to="{ name: \'reports\' }">报表</RouterLink>',
        needle="角色分流",
    ),
    Case(
        title="RT-9  rerank=true 没装重排器时静默降级（勾了重排却拿到没重排的结果）",
        target=APP,
        old="""    if request.rerank and deps.reranker_factory is None:
        raise HTTPException(
            status_code=400,
            detail="这个部署没有配重排器(reranker_factory 为空),rerank=true 无法满足;"
            "去掉 rerank 字段就用配置里的默认值",
        )
    return request.rerank""",
        new="    return request.rerank and deps.reranker_factory is not None",
        needle="rerank=true 而没装重排器",
    ),
    Case(
        title="RT-10  capabilities 报的上限与出参边界漂开（前端夹取夹到 422）",
        target=APP,
        old="""                limits=RetrievalLimitsOut(
                    k_max=K_MAX, max_context_max=MAX_CONTEXT_MAX, rrf_k_max=RRF_K_MAX
                ),""",
        new="""                limits=RetrievalLimitsOut(
                    k_max=K_MAX + 10, max_context_max=MAX_CONTEXT_MAX, rrf_k_max=RRF_K_MAX
                ),""",
        needle="capabilities 的 limits 与 QueryRequest 的边界",
    ),
    Case(
        title="RT-11  endpoints.ts 绕开 API_PREFIX 写死老路径（'/api/stream' —— 前缀两处真值来源）",
        target=ENDPOINTS_TS,
        old="export const STREAM_PATH = `${API_PREFIX}/stream`",
        new="export const STREAM_PATH = '/api/stream'",
        needle="接口前缀只有一处",
    ),
    Case(
        title="RT-12  前端清单把 WS 指到一条 HTTP 路由（/api/v1/query —— 页面连不上，服务端一切正常）",
        target=ENDPOINTS_TS,
        old="export const STREAM_PATH = `${API_PREFIX}/stream`",
        new="export const STREAM_PATH = `${API_PREFIX}/query`",
        needle="前端清单与后端路由表",
    ),
    Case(
        title="RT-13  会话列表把存储层的异常直接抛出去（用户只看到「Internal Server Error」）",
        target=APP,
        old=(
            "        try:\n"
            "            rows = await _call(catalog.list_for, user.user_id, limit=limit)\n"
            "        except Exception as exc:  # noqa: BLE001"
        ),
        new=(
            "        if True:\n"
            "            rows = await _call(catalog.list_for, user.user_id, limit=limit)\n"
            "        elif False:  # noqa: BLE001"
        ),
        needle="sessions：无认证给空表",
    ),
)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    for case in CASES:
        original = case.target.read_text(encoding="utf-8")
        hits = original.count(case.old)
        if hits != 1:
            print(
                f"⚠️  {case.title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(case.target, case.old, case.new):
            red, line = run(case.needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
