#!/usr/bin/env python
"""验收：模型调用的批处理器（``adapters/batching.py``）。

用法：

    uv run python scripts/acceptance/check_m3_batch.py

**为什么单列一份**：嵌入与重排都靠它把并发的「一句话 / 十几对」攒成一批前向。
批处理器自己出错时，表现是「偶发多等几秒 / 某条请求永远不返回」这类最难查的故障；
这里用假 ``run`` 把四件事钉住（零 GPU、零模型、毫秒级）：

1. **真的合批**：20 个线程同时提交，``run`` 的调用次数远少于 20，且总条数不变；
2. **单调用不额外等待**：队列里只有一条时立刻跑，不睡窗口；
3. **异常原样抛给该批所有调用方**：不许静默丢；
4. **单个 job 超过单批上限也能跑**：不能因为超限就永远卡住。

另加两条：返回条数不符当场报错、``close()`` 之后提交报错。
"""

from __future__ import annotations

import sys
import threading
import time

from _harness import check, report


def _batcher(run, *, max_items: int = 64):
    from project_rag.adapters.batching import Batcher

    return Batcher(run, max_items=max_items, name="test")


def _run_threads(count: int, fn) -> list[BaseException | None]:
    """输入:线程数 + 每线程要跑的函数。输出:每线程的异常(没抛就是 None)。"""
    barrier = threading.Barrier(count)

    def wrapped(index: int) -> None:
        barrier.wait()
        try:
            fn(index)
        except BaseException as exc:  # noqa: BLE001 —— 线程里的失败要带回主线程
            errors[index] = exc

    errors: list[BaseException | None] = [None] * count
    threads = [threading.Thread(target=wrapped, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("线程 10 秒没退出 —— 批处理器大概率死锁了")
    return errors


@check("批处理：20 个线程同时提交，调用次数远少于 20 且结果同序")
def _() -> str:
    calls: list[int] = []
    lock = threading.Lock()

    def fake_run(items):
        with lock:
            calls.append(len(items))
        time.sleep(0.02)  # 假装在算,让后来的调用有时间排队
        return [item * 2 for item in items]

    batcher = _batcher(fake_run)
    try:
        results: list[list[int]] = [[] for _ in range(20)]

        def submit(index: int) -> None:
            results[index] = batcher.submit([index, index + 1, index + 2, index + 3, index + 4])

        errors = _run_threads(20, submit)
        raised = [exc for exc in errors if exc is not None]
        if raised:
            raise AssertionError(f"有线程抛了异常:{raised[0]!r}")
        for index in range(20):
            expected = [(index + offset) * 2 for offset in range(5)]
            if results[index] != expected:
                raise AssertionError(f"第 {index} 个调用的结果错位:{results[index]} != {expected}")
        if len(calls) >= 20:
            raise AssertionError(f"没有合批:20 个调用跑了 {len(calls)} 次 run")
        if sum(calls) != 100:
            raise AssertionError(f"总条数不对:{sum(calls)} != 100")
        return f"20 次调用合成 {len(calls)} 批(每批 {sorted(calls)} 条),结果同序"
    finally:
        batcher.close()


@check("批处理：队列里只有一条时立刻执行（不睡固定窗口）")
def _() -> str:
    def fake_run(items):
        return list(items)

    batcher = _batcher(fake_run)
    try:
        started = time.perf_counter()
        out = batcher.submit([1, 2, 3])
        elapsed_ms = (time.perf_counter() - started) * 1000
        if out != [1, 2, 3]:
            raise AssertionError(f"结果不对:{out}")
        if elapsed_ms > 200:
            raise AssertionError(f"单调用等了 {elapsed_ms:.0f} ms —— 不该有固定窗口")
        return f"单调用 {elapsed_ms:.1f} ms（无窗口等待）"
    finally:
        batcher.close()


@check("批处理：run 抛错时原样抛给该批所有调用方")
def _() -> str:
    def broken(items):
        raise ValueError("模型炸了")

    batcher = _batcher(broken)
    caught: list[str] = []
    try:
        for _ in range(3):
            try:
                batcher.submit([1])
            except ValueError as exc:
                caught.append(str(exc))
        if caught != ["模型炸了", "模型炸了", "模型炸了"]:
            raise AssertionError(f"异常没有原样抛出:{caught}")
        return "3 次调用都收到同一个 ValueError"
    finally:
        batcher.close()


@check("批处理：单个 job 超过单批上限也能执行（不能永远卡住）")
def _() -> str:
    seen: list[int] = []

    def fake_run(items):
        seen.append(len(items))
        return [item + 1 for item in items]

    batcher = _batcher(fake_run, max_items=4)
    try:
        out = batcher.submit(list(range(10)))
        if out != list(range(1, 11)):
            raise AssertionError(f"结果不对:{out}")
        if seen != [10]:
            raise AssertionError(f"超长单批应一次跑完,实际调用 {seen}")
        return "10 条 > max_items=4，仍一次跑完"
    finally:
        batcher.close()


@check("批处理：返回条数不符 → 当场报错，不静默少结果")
def _() -> str:
    def short(items):
        return [0] * (len(items) - 1)

    batcher = _batcher(short)
    try:
        try:
            batcher.submit([1, 2, 3])
        except RuntimeError as exc:
            if "同序等长" not in str(exc):
                raise AssertionError(f"报错信息不对:{exc}") from None
            return "少一条 → RuntimeError（点名同序等长）"
        raise AssertionError("返回条数不符却没有报错")
    finally:
        batcher.close()


@check("批处理：close() 之后提交立刻报错")
def _() -> str:
    batcher = _batcher(lambda items: list(items))
    batcher.close()
    try:
        batcher.submit([1])
    except RuntimeError as exc:
        if "已关闭" not in str(exc):
            raise AssertionError(f"报错信息不对:{exc}") from None
        return "close 后 submit → RuntimeError（已关闭）"
    raise AssertionError("close 后 submit 没有报错")


def main() -> int:
    return report(
        "批处理契约：合批、单调用不等待、异常原样抛出、超长批不卡死",
        hint="这份进 --set ci：嵌入/重排的批处理坏了，表现是偶发慢或某条请求永远不返回。",
    )


if __name__ == "__main__":
    sys.exit(main())
