#!/usr/bin/env python
"""P0-7 反向测试:把"算钱"改回坏样子,确认契约真的会红。

用法::

    uv run python scripts/acceptance/reverse_p0_7.py

这一批坏版本的共同点:**报表上会出现一个看起来合理的数字**,而它其实是错的 ——
少算一半、双倍计入、把"没量到"当 0、把"没配单价"当免费。改的是:

* ``application/cost.py`` —— 口径本身(哪些行算调用、单价缺失、四舍五入);
* ``adapters/llm_deepseek.py`` —— 采集(用量形状、模型名、开关接线);
* ``api/app.py`` —— 出参(`all_priced` / `unpriced_models` 如实)与权限;
* ``cli/commands/metrics.py`` —— 命令行那一份报表。

锚点是**逐字符照抄**源码里的那几行(含缩进);``main`` 会先核对"锚点在文件里恰好出现 1 次"——
不核对的话,一个对不上的锚点会让什么都没改的源码跑出一片绿,然后被读成"这个检查是摆设"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_p0_7_cost.py"

SRC = ROOT / "src" / "project_rag"
APP = SRC / "application"
API = SRC / "api"
ADAPTERS = SRC / "adapters"
CLI = SRC / "cli" / "commands"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)
    script: str = SCRIPT  # 这条坏版本该由哪个检查脚本抓住


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  把『没量到 token』折成 0（少报的钱从报表上看不出来）",
        target=APP / "cost.py",
        old="""    value = row.get(key)
    if value is None or value in ("", _NONE):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None""",
        new="""    value = row.get(key)
    if value is None or value in ("", _NONE):
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0""",
        needle="缺口 没量到 token",
    ),
    Case(
        title="RT-2  没配单价就算 0 元（把『不知道多少钱』画成一个数字）",
        target=APP / "cost.py",
        old="""    if price is None:
        return None""",
        new="""    if price is None:
        return 0.0""",
        needle="缺口 未配单价",
    ),
    Case(
        title="RT-3  单价 0 元被当成『未配』（本地免费模型被报成缺配置）",
        target=APP / "cost.py",
        # 锚点带上后两行:``if price is None:`` 在 ``rates_of`` 里也有一处,只写一行会撞两次
        old="""    if price is None:
        return None
    return tokens / 1_000_000.0 * price""",
        new="""    if not price:
        return None
    return tokens / 1_000_000.0 * price""",
        needle="缺口 单价 0 元 ≠ 未配",
    ),
    Case(
        title="RT-4  金额只留两位小数（单次调用被抹成 0.00）",
        target=APP / "cost.py",
        old="_MONEY_DIGITS = 6",
        new="_MONEY_DIGITS = 2",
        needle="算钱 先汇总再乘",
    ),
    Case(
        title="RT-5  用外层 qa 行判『调没调模型』（一次请求算两次钱）",
        target=APP / "cost.py",
        old="""    if any(
        _number(row, key) is not None
        for key in ("llm_ttft_ms", "llm_think_ms", "llm_input_tokens", "llm_output_tokens")
    ):""",
        new="""    if any(
        _number(row, key) is not None
        for key in ("ttft_ms", "llm_think_ms", "llm_input_tokens", "llm_output_tokens")
    ):""",
        needle="缺口 不双计",
    ),
    Case(
        title="RT-12 老账本(有调用、没 token)被报成『未配单价的模型 -』",
        target=APP / "cost.py",
        old="""            unmetered += 1
            continue""",
        new="""            unmetered += 1
            calls.setdefault(model, 0)
            per_model.setdefault(model, [0, 0])
            continue""",
        needle="缺口 老账本",
    ),
    Case(
        title="RT-13 缓存命中数不参与计价（0.8M 命中被按未命中价算，金额虚高 40 倍）",
        target=APP / "cost.py",
        old="""        hit_price = (
            rates.input_cache_hit if rates.input_cache_hit is not None else rates.input_cache_miss
        )""",
        new="        hit_price = rates.input_cache_miss",
        needle="算钱 高峰",
    ),
    Case(
        title="RT-14 空闲时段不生效（窗口恒判高峰，一整晚的账按贵的那档算）",
        target=APP / "cost.py",
        old="""    if moment is None or not windows:
        return False""",
        new="""    if True:
        return False""",
        needle="算钱 空闲",
    ),
    Case(
        title="RT-15 存储层不再把时间戳附在行里（分时段计价整段失效，且没人报『时段不明』）",
        target=ADAPTERS / "ledger_mysql.py",
        old="                out.append({**loaded, LEDGER_TS: ts.isoformat()})",
        new="                out.append(loaded)",
        needle="真实链路：账本带 token(含缓存命中)",
        script="check_m5_history.py",
    ),
    Case(
        title="RT-6  模型名用配置里的那个（provider 实际用的另一个 → 单价查错表）",
        target=ADAPTERS / "llm_deepseek.py",
        old='    model = str(response.get("model_name") or response.get("model") or fallback_model)',
        new="    model = fallback_model",
        needle="采集 模型名取 provider 说的那个",
    ),
    Case(
        title="RT-7  stream_usage 不传给客户端（用量永远量不到）",
        target=ADAPTERS / "llm_deepseek.py",
        old="            stream_usage=config.stream_usage,",
        new="            stream_usage=False,",
        needle="采集 开关接线",
    ),
    Case(
        title="RT-8  只认标准形状的 usage（换一个 provider 就静默量不到）",
        target=ADAPTERS / "llm_deepseek.py",
        old='    raw: dict = dict(metadata) if metadata else dict(response.get("token_usage") or {})',
        new="    raw: dict = dict(metadata) if metadata else {}",
        needle="采集 DeepSeek 原始形状",
    ),
    Case(
        title="RT-9  消耗报表不要求权限（任何登录账号都能看到花了多少钱）",
        target=API / "app.py",
        old="""        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window 只支持 {'/'.join(_WINDOWS)}")
        auth.require_reports(user)
        return await _call(_cost, deps, window)""",
        new="""        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window 只支持 {'/'.join(_WINDOWS)}")
        return await _call(_cost, deps, window)""",
        needle="入口 HTTP：/api/v1/reports/cost 形状",
    ),
    Case(
        title="RT-10 出参把 unpriced_models 吞掉（界面以为金额是完整的账）",
        target=API / "app.py",
        old="        all_priced=report.all_priced,",
        new="        all_priced=True,",
        needle="入口 HTTP：没配单价 / 缺口计数都如实报",
    ),
    Case(
        title="RT-11 命令行报表不打消耗段（只有网页看得到，而运维常看命令行）",
        target=CLI / "metrics.py",
        old="    _print_cost(args, rows)",
        new="    return 0",
        needle="入口 CLI",
    ),
)


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:检查脚本名、期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{script}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    """输入:无。行为:逐个改坏→跑契约→还原,统计哪几条没红。输出:退出码。"""
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")

    originals = {case.target: case.target.read_text(encoding="utf-8") for case in CASES}
    bad = 0
    try:
        for case in CASES:
            if only and not any(case.title.startswith(tag) for tag in only):
                continue
            original = originals[case.target]
            hits = original.count(case.old)
            if hits != 1:
                print(
                    f"⚠️  {case.title}\n      锚点出现 {hits} 次(必须恰好 1 次) —— 源码变了,先更新锚点"
                )
                bad += 1
                continue
            with patched_source(case.target, case.old, case.new):
                red, line = run(case.script, case.needle)
            if not red:
                bad += 1
                if "[SKIP]" in line:
                    line += "\n      ⚠️ 这一条是 SKIP(环境不满足)—— 反向测试证明不了它"
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    finally:
        for target, original in originals.items():
            if target.read_text(encoding="utf-8") != original:
                target.write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
