#!/usr/bin/env python
"""把验收脚本按「需要什么外部条件」分组成三个集合，一次跑完并给出总裁决。

用法：

    uv run python scripts/acceptance/run_all.py --set ci        # CI/新机器：零容器零网络零模型
    uv run python scripts/acceptance/run_all.py --set services  # 本机起了中间件时：多跑依赖真中间件的那几条
    uv run python scripts/acceptance/run_all.py --set local     # 全套：还需要真实语料/真 DeepSeek key
    uv run python scripts/acceptance/run_all.py --set all       # 38 套检查 + 29 份反向，一个不落
    uv run python scripts/acceptance/run_all.py --list          # 只打印分组，不跑

**为什么需要它**：在此之前"全量回归"是一段手写的 shell 循环，只在某个人的终端里存在；
新机器/CI 想知道"我这儿能跑哪些"只能逐份读脚本。这个 runner 把那份知识**变成可执行的清单**。

**分组依据是实测，不是猜的**（做了一次探测：把 Milvus/MySQL/Redis 端口指向 1、
模型目录指向不存在、DeepSeek key 换成假值，然后跑全部 38 套，看谁 PASS/FAIL/SKIP）：

* ``ci``       —— 上面那次探测里 **0 失败**的那些，加上全部反向 runner（26 份）。缺模型/语料时它们
                  **SKIP 并点名 owner**（这是本项目的设计），所以在没有中间件的机器上照样能用。
* ``services`` —— 只需要**真中间件**、不需要真数据的：服务连通性往返、会话表的
                  ``ensure_table``/往返、账本落表、Milvus 的存取往返。CI 的夜间任务用这一组。
* ``local``    —— 还需要**真数据或真 key**：FAQ 表那 463 条（语料有版权、不公开）、
                  真实语料的页码全量核验、真 DeepSeek 流式调用（花钱）。**这三条永远不在公共 CI 里跑**。

裁决口径与单份脚本一致：**检查脚本 exit 0 = 该套 0 失败；反向 runner exit 0 = 全红**。
任一条不合就整份返回非 0，并在末尾把不合格的列出来。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# 需要真中间件（不需要真数据）
SERVICES = [
    "check_env_services.py",  # Milvus/MySQL/Redis 三处往返
    "check_m5_history.py",  # 会话表 ensure_table + record/recent 往返（表由适配器自建）
    "check_m5_9_metrics.py",  # 延迟账本落 MySQL 后按窗口聚合
    "check_p0_5_pages.py",  # Milvus 里页码 7 / None 的存取往返与哨兵
    "check_m3_sparse_milvus.py",  # 稀疏列 + IVF_FLAT 的真 Milvus 往返(零容器那份在 check_m3_sparse.py)
]
# 还需要真数据或真 key —— 公共 CI 里永远不跑
LOCAL_ONLY = [
    "check_m2_faq.py",  # 需要 faq 表里那 463 条真实语料（有版权，不公开）
    "check_m5_qa.py",  # 同上：合流入口的真 FAQ 表
    "check_m5_stream.py",  # 3 次真 DeepSeek 调用（要 key、要花钱）
    # 查询改写 A/B：产物全在 data/eval/ 下（评估集 / 两臂数据集 / 对比报告）—— 都不公开。
    # 它对应的反向测试 reverse_m4_rewrite_ab.py **不在**这一档：数据不在时它会打印
    # 「数据不在本地，跳过」并以 0 退出（与 reverse_m4_eval.py 同一口径），
    # 于是公开 CI 仍能验证「这条检查确实会红」这件事本身没坏。
    "check_m4_rewrite_ab.py",
]
# 其余全部属于 ci 组（含上面 services 里那四份：它们在缺少中间件时会 SKIP 而不是 FAIL）


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    ok: bool
    seconds: float
    detail: str


def _scripts(checks: bool, reverse: bool) -> list[Path]:
    """输入:要哪几类。行为:按文件名排序取脚本。输出:脚本路径列表。"""
    out: list[Path] = []
    if checks:
        out += sorted(HERE.glob("check_*.py"))
    if reverse:
        out += sorted(HERE.glob("reverse_*.py"))
    return out


def _selected(set_name: str, checks: bool, reverse: bool) -> list[Path]:
    """输入:集合名 + 要哪几类。行为:按集合筛脚本。输出:脚本路径列表。"""
    everything = _scripts(checks, reverse)
    if set_name == "all":
        return everything
    if set_name == "ci":
        excluded = set(SERVICES) | set(LOCAL_ONLY)
        return [p for p in everything if p.name not in excluded]
    wanted = SERVICES if set_name == "services" else LOCAL_ONLY
    return [p for p in everything if p.name in set(wanted)]


def _run(path: Path, timeout_s: int) -> Result:
    """输入:脚本、超时。行为:子进程跑它、只看退出码。输出:结果。"""
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return Result(path.name, False, time.perf_counter() - start, f"超时（>{timeout_s}s）")
    seconds = time.perf_counter() - start
    if proc.returncode == 0:
        return Result(path.name, True, seconds, "")
    # 失败时给出最有信息量的一行：检查脚本是「✘ [FAIL] …」，反向 runner 是「✘ 还是绿的…」
    lines = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()]
    detail = next((ln for ln in lines if ln.startswith("✘")), "")
    if not detail:
        detail = next((ln for ln in lines if "FAIL" in ln or "Traceback" in ln), "见完整输出")
    return Result(path.name, False, seconds, detail[:200])


def _lock_or_die() -> object | None:
    """输入:无。行为:抢一把进程锁,防止两份 run_all 同时跑。输出:要一直持有到进程结束的句柄。

    **为什么需要**:反向测试靠「改坏源码 → 跑检查 → 还原」工作,而**两份 run_all 同时跑会互相拆台** ——
    A 的还原会把 B 正在用的补丁撤掉,B 的检查于是报出一些莫名其妙的错(实测:同一个词重复两遍
    的分数报 2.000、索引类型报 FLAG、密码哈希说"盐不是随机的"),
    更糟的是两边都可能在对方还原之后**再写一次坏版本**,最后留下一份没人还原的源码
    (`api/app.py` 上留着 `visible = list(views)`,等于把 ACL 关掉)。

    这不是纪律问题(「记得别同时跑」),是工程问题:**让并发在入口就失败**,
    而不是让它在几十分钟后表现成"某个检查是摆设"。``--force`` 显式跳过
    (调试 runner 本身时用,那时不会有别人在改源码)。
    """
    import fcntl

    lock_path = ROOT / ".cache" / "run_all.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # **a+ 而不是 w**:用 w 会在抢锁之前先把文件截断 —— 而那正是持有者写 pid 的地方,
    # 于是第二条永远只报得出「pid 未知」(实测踩到)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "(pid 未知)"
        print(
            f"已经有一份 run_all 在跑({holder})—— 两份同时跑会互相还原对方正在用的补丁,\n"
            f"结果是「某个检查是摆设」这种假故障。等它跑完,或 --force 明确跳过这道闸。",
            file=sys.stderr,
        )
        handle.close()
        raise SystemExit(2)
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} 启动于 {time.strftime('%H:%M:%S')}\n")
    handle.flush()
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description="按外部依赖分组跑验收")
    parser.add_argument("--set", dest="set_name", default="ci", choices=["ci", "services", "local", "all"])
    parser.add_argument("--checks-only", action="store_true", help="只跑 check_*.py")
    parser.add_argument("--reverse-only", action="store_true", help="只跑 reverse_*.py")
    parser.add_argument("--timeout", type=int, default=900, help="单个脚本的超时秒数")
    parser.add_argument("--list", action="store_true", help="只打印选中哪些脚本")
    parser.add_argument("--force", action="store_true", help="跳过「不许并发」这道闸(调试 runner 时用)")
    args = parser.parse_args()

    checks = not args.reverse_only
    reverse = not args.checks_only
    scripts = _selected(args.set_name, checks, reverse)
    # 反向测试会改源码再还原,所以**必须**独占跑。--list 不改任何东西,不必抢锁
    _lock = None if (args.list or args.force) else _lock_or_die()
    _ = _lock  # 持有到进程结束(局部变量被回收 = 释放) —— 所以这里刻意不让它出作用域
    if args.list:
        for path in scripts:
            print(path.name)
        return 0
    if not scripts:
        print(f"集合 {args.set_name} 里没有脚本 —— 参数写错了？", file=sys.stderr)
        return 2

    print(f"集合 {args.set_name}：{len(scripts)} 份脚本（超时 {args.timeout}s/份）\n" + "─" * 68)
    results: list[Result] = []
    for path in scripts:
        result = _run(path, args.timeout)
        results.append(result)
        mark = "✔" if result.ok else "✘"
        tail = "" if result.ok else f"   ← {result.detail}"
        print(f"  {mark} {result.name:<30} {result.seconds:6.1f}s{tail}", flush=True)

    bad = [r for r in results if not r.ok]
    print("─" * 68)
    print(f"{len(results) - len(bad)}/{len(results)} 合格，用时 {sum(r.seconds for r in results):.0f}s")
    if bad:
        print("\n不合格：")
        for result in bad:
            print(f"  ✘ {result.name} —— {result.detail}")
        print(
            "\n排查提示：反向着看比顺着看快 —— 先确认这条**是不是本机缺外部条件**"
            "（缺模型/语料会 SKIP，不该 FAIL；真 FAIL 就是代码或契约坏了）。"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
