"""把并发的小批量模型调用攒成一批执行。

**为什么要有它**：100 并发压测里，40 个请求线程各自调一次模型 —— 嵌入是
``embed([一句话])``（批大小 1）、重排是 12–31 对。GPU 在几十个小前向之间切时间片，
单次嵌入从十几毫秒涨到 3 秒、重排从 160 ms 涨到 3.8 秒。把**同时已经在排队**的调用
合成一批，前向次数少了、每次吃得更满，模型侧才不会被小请求切碎。

**不按时间窗口攒**：请求到达率只有几 QPS，几毫秒的窗口里根本等不到第二个调用；
真正在排队的是**执行**。所以 worker 直接把当前队列取走，最多 ``max_items`` 条。

**单个调用没有额外等待**：队列里只有它一个时 worker 立刻开跑，不睡窗口 ——
串行场景（CLI、评估脚本）不会因为批处理多付固定延迟。

**线程模型**：一个后台 daemon 线程 + 条件变量。调用方 ``submit`` 后阻塞在自己的
``Event`` 上；worker 一次取走若干 job，拼成一批调 ``run``，再把结果按原边界切回去。
批内任何异常原样抛给该批所有调用方 —— 不允许「静默少几条」。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class _Job(Generic[T, R]):
    """一次待执行的调用：输入项 + 结果 / 异常 + 完成信号。"""

    __slots__ = ("items", "results", "error", "done")

    def __init__(self, items: list[T]) -> None:
        self.items = items
        self.results: list[R] | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class Batcher(Generic[T, R]):
    """输入项列表 → 结果列表的批处理器。

    用法::

        batcher = Batcher(score_pairs, max_items=128, name="rerank")
        scores = batcher.submit(pairs)   # 阻塞,直到这批算完

    ``run`` 必须满足：**同序等长**（输入几条、输出几条），且只会在 worker 线程里被调用；
    适配器因此不必自己加锁。
    """

    def __init__(
        self,
        run: Callable[[Sequence[T]], Sequence[R]],
        *,
        max_items: int,
        name: str = "batcher",
    ) -> None:
        """输入:批处理函数 + 单批上限 + 线程名。行为:启动后台 worker。输出:无。"""
        if max_items < 1:
            raise ValueError(f"{name}: max_items 必须 >= 1，收到 {max_items}")
        self._run = run
        self._max_items = max_items
        self._name = name
        self._pending: deque[_Job[T, R]] = deque()
        self._closed = False
        self._cv = threading.Condition()
        self._worker = threading.Thread(
            target=self._loop,
            name=f"{name}-batcher",
            # 守护线程:它只是调用链的一环,不该拖住进程退出
            daemon=True,
        )
        self._worker.start()

    def submit(self, items: Sequence[T]) -> list[R]:
        """输入:本调用要处理的一批输入项。行为:入队并等这一批算完。输出:与输入同序的结果。

        **空列表直接返回**,不惊动 worker：``encode([])`` / ``predict([])`` 的行为随
        版本变,而「没有输入」本来就该是空结果。
        """
        if not items:
            return []
        job: _Job[T, R] = _Job(list(items))
        with self._cv:
            if self._closed:
                raise RuntimeError(f"{self._name}: 批处理器已关闭")
            self._pending.append(job)
            self._cv.notify()
        job.done.wait()
        if job.error is not None:
            raise job.error
        if job.results is None:
            raise RuntimeError(f"{self._name}: 批处理器没有返回结果")
        return job.results

    def close(self) -> None:
        """输入:无。行为:停止 worker;还在排队的调用收到 RuntimeError。输出:无。"""
        with self._cv:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
            self._cv.notify()
        for job in pending:
            job.error = RuntimeError(f"{self._name}: 批处理器已关闭")
            job.done.set()
        self._worker.join(timeout=5)

    def _take_locked(self) -> list[_Job[T, R]]:
        """输入:无(调用方持锁)。行为:取走队首若干 job,总量不超过 max_items。

        至少取一个 —— 单个 job 本身超过 ``max_items`` 时也得跑,不能因为超限就永远卡住。
        """
        jobs: list[_Job[T, R]] = []
        total = 0
        while self._pending:
            job = self._pending[0]
            if jobs and total + len(job.items) > self._max_items:
                break
            jobs.append(self._pending.popleft())
            total += len(job.items)
            if total >= self._max_items:
                break
        return jobs

    def _loop(self) -> None:
        """worker 主循环:等活 → 取队列 → 合批跑 → 按原边界切回结果。"""
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                jobs = self._take_locked()
            flat = [item for job in jobs for item in job.items]
            try:
                results = list(self._run(flat))
                if len(results) != len(flat):
                    raise RuntimeError(
                        f"{self._name}: 批处理返回 {len(results)} 条,输入却有 {len(flat)} 条"
                        " —— 这个回调的约定是同序等长"
                    )
                offset = 0
                for job in jobs:
                    size = len(job.items)
                    job.results = results[offset : offset + size]
                    offset += size
            except BaseException as exc:  # noqa: BLE001 —— 原样交给该批所有调用方
                for job in jobs:
                    job.error = exc
            finally:
                for job in jobs:
                    job.done.set()
