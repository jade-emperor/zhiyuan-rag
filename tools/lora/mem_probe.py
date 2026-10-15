"""显存分板块统计：训练与推理时「哪一块占了多少」都要看得见。

两类账，缺一不可：

1. **实测账**（``MemProbe``）：在关键节点打点，记录 ``torch.cuda`` 的
   allocated / reserved / peak、设备级 used/total、以及 Python 进程 RSS。
   训练中由 ``MemProbeCallback`` 接到 :class:`transformers.TrainerCallback` 上按步采样；
   前向激活由 ``ActivationHook`` 在 forward 之后打点（那一刻激活还活着）。
2. **理论账**（``param_ledger``）：从 ``model.named_parameters()`` 按板块汇总参数量，
   再按 dtype 折算字节 —— 权重、梯度、AdamW 两阶状态各占多少，用来解释实测数字，
   也能在没有 CUDA 的机器上给出可预期的量级。

⚠️ **本机/沙箱的 GPU 可见性**：agent 的 shell 挂的是私有 ``/dev``，``torch.cuda.is_available()``
会返回 False —— 那是视角问题，不是机器没有 GPU（见仓库根 ``AGENTS.md``）。本模块在没有 CUDA 时
自动降级成「RSS + 理论账」，并在报告首行明确标注，绝不把"我看不到"写成"不存在"。
要看真实 GPU 数字：在容器里跑（``docker run --gpus all``），或直接在宿主终端跑。
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import torch

# ---------------------------------------------------------------------------
# 基础：CUDA 可见性与进程内存
# ---------------------------------------------------------------------------

MB = 1024 * 1024


def cuda_visible() -> bool:
    """输入:无。行为:探测本进程能否用 CUDA（受沙箱视角影响，见模块 docstring）。输出:bool。"""
    return torch.cuda.is_available()


def _rss_mb() -> float:
    """输入:无。行为:读进程 RSS（优先 /proc，回退 ru_maxrss）。输出:MB。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _device_used_mb() -> tuple[float, float]:
    """输入:无。行为:取设备级显存 used/total。输出:(used_mb, total_mb)，取不到返回 (nan, nan)。"""
    if not cuda_visible():
        return float("nan"), float("nan")
    free, total = torch.cuda.mem_get_info()
    return (total - free) / MB, total / MB


@dataclass
class MemSnapshot:
    """一次打点。allocated/reserved 是 PyTorch 缓存分配器的口径，device_used 是整个设备的口径。"""

    label: str
    allocated_mb: float
    reserved_mb: float
    peak_mb: float
    device_used_mb: float
    device_total_mb: float
    rss_mb: float
    delta_allocated_mb: float
    t: float

    def as_row(self, cuda: bool = True) -> str:
        """输入:是否按 CUDA 口径渲染。行为:格式化成表格一行。输出:str。"""
        if cuda:
            return (
                f"  {self.label:<28s} {self.allocated_mb:>10.1f} {self.delta_allocated_mb:>+11.1f} "
                f"{self.reserved_mb:>10.1f} {self.peak_mb:>10.1f} {self.device_used_mb:>11.1f} "
                f"{self.rss_mb:>10.1f}"
            )
        return f"  {self.label:<28s} {self.rss_mb:>12.1f}"


class MemProbe:
    """分板块显存打点器。

    用法::

        probe = MemProbe("训练")
        probe.snap("起始")
        model = ...            # 加载基座
        probe.snap("基座权重")
        ...
        print(probe.table())
    """

    HEADER = (
        f"  {'板块':<28s} {'已分配MB':>10s} {'本步增量MB':>11s} "
        f"{'预留MB':>10s} {'峰值MB':>10s} {'设备已用MB':>11s} {'进程RSS_MB':>10s}"
    )

    def __init__(self, title: str = "显存账本") -> None:
        self.title = title
        self.snapshots: list[MemSnapshot] = []
        self.marks: dict[str, Any] = {}
        self._last_alloc = 0.0
        self._run_peak = 0.0
        self._t0 = time.perf_counter()
        if cuda_visible():
            torch.cuda.synchronize()
            # 首次 CUDA 调用会把 context / cuBLAS 句柄算进来，先打一枪再打点，
            # 否则"起始"那一行会把上下文开销记到下一个板块头上。
            torch.zeros(1, device="cuda")

    # -- 打点 ---------------------------------------------------------------
    def snap(self, label: str) -> MemSnapshot:
        """输入:板块名。行为:记录当前显存并维护峰值（自己维护，避免 reset 破坏全局峰值）。输出:快照。"""
        if cuda_visible():
            torch.cuda.synchronize()
            allocated = torch.cuda.memory_allocated() / MB
            reserved = torch.cuda.memory_reserved() / MB
            peak = torch.cuda.max_memory_allocated() / MB
        else:
            allocated = reserved = peak = float("nan")
        used, total = _device_used_mb()
        self._run_peak = max(self._run_peak, 0.0 if peak != peak else peak)  # noqa: PLR0124 - nan 判定
        snap = MemSnapshot(
            label=label,
            allocated_mb=allocated,
            reserved_mb=reserved,
            peak_mb=peak,
            device_used_mb=used,
            device_total_mb=total,
            rss_mb=_rss_mb(),
            delta_allocated_mb=(allocated - self._last_alloc) if allocated == allocated else float("nan"),
            t=time.perf_counter() - self._t0,
        )
        if allocated == allocated:
            self._last_alloc = allocated
        self.snapshots.append(snap)
        return snap

    def note(self, key: str, value: Any) -> None:
        """输入:键值。行为:记一条与显存无关的元信息（如参数量、token 长度）。输出:无。"""
        self.marks[key] = value

    # -- 输出 ---------------------------------------------------------------
    def table(self) -> str:
        """输入:无。行为:渲染成表格（没有 CUDA 时只渲染 RSS 一列，不灌 nan）。输出:str。"""
        lines = [f"\n===== {self.title} ====="]
        if cuda_visible():
            name = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / MB
            lines.append(f"  设备: {name}（{total:.0f} MB）")
            lines.append(self.HEADER)
            for snap in self.snapshots:
                lines.append(snap.as_row(cuda=True))
        else:
            lines.append("  ⚠️ 本进程看不到 CUDA（沙箱私有 /dev，见 AGENTS.md）—— 下面只有进程 RSS，")
            lines.append("     它 ≠ 显存；要看真实显存请在容器里跑：docker run --rm --gpus all ...")
            lines.append(f"  {'板块':<28s} {'进程RSS_MB':>12s} {'ΔRSS_MB':>10s}")
            previous = 0.0
            for snap in self.snapshots:
                lines.append(f"{snap.as_row(cuda=False)}{snap.rss_mb - previous:>+10.1f}")
                previous = snap.rss_mb
        if self.marks:
            lines.append("  元信息: " + json.dumps(self.marks, ensure_ascii=False, default=str))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """输入:无。行为:导出可写盘的结构。输出:dict。"""
        return {
            "title": self.title,
            "cuda_visible": cuda_visible(),
            "device": torch.cuda.get_device_name(0) if cuda_visible() else None,
            "snapshots": [asdict(s) for s in self.snapshots],
            "marks": self.marks,
            "run_peak_mb": self._run_peak,
        }

    @contextlib.contextmanager
    def phase(self, label: str):
        """输入:板块名。行为:进入/退出各打一次点，便于给一段代码记账。输出:上下文管理器。"""
        self.snap(f"{label} · 开始")
        try:
            yield self
        finally:
            self.snap(f"{label} · 结束")


# ---------------------------------------------------------------------------
# 理论账本：参数量 → 字节数
# ---------------------------------------------------------------------------

def _group_of(name: str) -> str:
    """输入:参数名。行为:按命名归到板块。输出:板块名。"""
    if "lora_" in name:
        return "LoRA 适配器"
    if name.startswith("classifier") or ".classifier." in name:
        return "分类头"
    return "基座权重（冻结）"


def param_ledger(model: torch.nn.Module) -> dict[str, Any]:
    """输入:模型。行为:按板块统计参数量，并按主流口径折算显存。输出:账本 dict。

    折算口径（**主流做法，不是猜的**）：
      * 权重：按参数自身 dtype 算（fp32 4B / bf16 2B）
      * 梯度：可训练参数各一份，fp32 4B（autocast 不改参数 dtype，梯度仍 fp32）
      * AdamW：exp_avg + exp_avg_sq 两份 fp32 = 8B/可训练参数
    所以「全参微调」≈ 16 B/参数，而 LoRA 冻结基座后只有适配器+分类头走这 16 B。
    """
    groups: dict[str, dict[str, float]] = {}
    for name, param in model.named_parameters():
        key = _group_of(name)
        slot = groups.setdefault(key, {"params": 0.0, "trainable": 0.0, "bytes": 0.0})
        n = param.numel()
        slot["params"] += n
        slot["bytes"] += n * param.element_size()
        if param.requires_grad:
            slot["trainable"] += n
    for slot in groups.values():
        tr = slot["trainable"]
        slot["grad_bytes"] = tr * 4
        slot["adamw_bytes"] = tr * 8
        slot["train_bytes"] = slot["grad_bytes"] + slot["adamw_bytes"]
        slot["weights_mb"] = slot["bytes"] / MB
        slot["train_mb"] = slot["train_bytes"] / MB
    total = sum(s["params"] for s in groups.values())
    trainable = sum(s["trainable"] for s in groups.values())
    return {
        "groups": groups,
        "total_params": int(total),
        "trainable_params": int(trainable),
        "trainable_ratio": (trainable / total) if total else 0.0,
        "weights_only_mb": sum(s["weights_mb"] for s in groups.values()),
        "peak_train_mb_theoretical": sum(s["weights_mb"] + s["train_mb"] for s in groups.values()),
        "full_finetune_mb_theoretical": total * 16 / MB,
    }


def ledger_table(ledger: dict[str, Any], mode: str = "train") -> str:
    """输入:param_ledger 结果与用途（train/infer）。行为:渲染成表。输出:str。"""
    lines = [
        "\n===== 理论账本（由参数量折算，可解释实测数字）=====",
        f"  {'板块':<22s} {'参数量':>13s} {'可训练':>13s} {'权重MB':>9s} {'梯度+AdamW_MB':>14s}",
    ]
    for name, slot in ledger["groups"].items():
        lines.append(
            f"  {name:<22s} {int(slot['params']):>13,d} {int(slot['trainable']):>13,d} "
            f"{slot['weights_mb']:>9.2f} {slot['train_mb']:>14.2f}"
        )
    lines.append(
        f"  {'合计':<22s} {ledger['total_params']:>13,d} {ledger['trainable_params']:>13,d} "
        f"（可训练占比 {ledger['trainable_ratio']:.4%}）"
    )
    if mode == "train":
        lines.append(
            f"  对照：同样这份数据做**全参**微调，权重+梯度+AdamW ≈ "
            f"{ledger['full_finetune_mb_theoretical']:.0f} MB；LoRA 只要 "
            f"{ledger['peak_train_mb_theoretical']:.0f} MB（差 "
            f"{ledger['full_finetune_mb_theoretical'] / max(ledger['peak_train_mb_theoretical'], 1e-9):.0f}×）"
        )
    else:
        lines.append(
            f"  推理**不需要**梯度与 AdamW 状态：权重 {ledger['weights_only_mb']:.0f} MB 就是全部；"
            f"同一模型若做全参微调训练要 ≈ {ledger['full_finetune_mb_theoretical']:.0f} MB"
        )
    lines.append("  注：**激活**不在这张表里（随 batch×seq 走），实测看上一张表的「前向激活」行。")
    return "\n".join(lines)


def activation_formula(batch_size: int, seq_len: int, hidden: int, layers: int, dtype_bytes: int = 2) -> str:
    """输入:训练形状。行为:给出激活量级的估算式（不精确，用来判断趋势）。输出:str。"""
    per_layer = batch_size * seq_len * hidden * dtype_bytes
    return (
        f"  激活量级估算：batch={batch_size} × seq={seq_len} × hidden={hidden} × layers={layers} "
        f"≈ {per_layer * layers / MB:.0f} MB（再乘以注意力矩阵等常数因子，实测通常 2~4×）"
    )


# ---------------------------------------------------------------------------
# 训练期：TrainerCallback + forward 钩子
# ---------------------------------------------------------------------------

try:  # transformers 是硬依赖，但让本模块在没有它时仍可单独 import
    from transformers import TrainerCallback

    _HAS_TF = True
except Exception:  # noqa: BLE001
    _HAS_TF = False

    class TrainerCallback:  # type: ignore[no-redef]
        """transformers 缺席时的占位基类。"""


class MemProbeCallback(TrainerCallback):
    """把显存打点接到 Trainer 的生命周期上。

    打点位置与含义：
      * ``on_train_begin``：优化器状态刚分配完（AdamW 两阶）—— 这是"开始训练前的稳态"
      * ``on_step_end``  ：每 ``log_every`` 步采一次，记录 allocated/reserved/峰值
      * ``on_evaluate``  ：评测峰值（``torch.no_grad`` 下激活更小，但 batch 可能更大）
      * ``on_save``      ：保存 checkpoint 的瞬时峰值（``state_dict`` 会额外复制一份权重）
    """

    def __init__(self, probe: MemProbe, log_every: int = 20, keep_steps: int = 40) -> None:
        self.probe = probe
        self.log_every = max(1, log_every)
        self.keep_steps = keep_steps
        self.step_samples: list[dict[str, float]] = []
        self._finalized = False

    # -- 内部 ---------------------------------------------------------------
    def _sample(self) -> None:
        if not cuda_visible():
            return
        self.step_samples.append(
            {
                "step": float(getattr(self, "_step", 0)),
                "allocated_mb": torch.cuda.memory_allocated() / MB,
                "reserved_mb": torch.cuda.memory_reserved() / MB,
                "peak_mb": torch.cuda.max_memory_allocated() / MB,
            }
        )

    # -- 生命周期 -----------------------------------------------------------
    def on_train_begin(self, args, state, control, **kwargs):  # noqa: ANN001, ANN201
        self.probe.snap("优化器状态就绪")
        self.probe.note("train_batch_size", args.per_device_train_batch_size)
        self.probe.note("grad_accum", args.gradient_accumulation_steps)
        self.probe.note("amp", "bf16" if args.bf16 else ("fp16" if args.fp16 else "fp32"))
        self.probe.note("optim", args.optim)
        return control

    def on_step_end(self, args, state, control, **kwargs):  # noqa: ANN001, ANN201
        self._step = state.global_step
        if state.global_step % self.log_every == 0:
            self.probe.snap(f"训练 step {state.global_step}")
        if len(self.step_samples) < self.keep_steps:
            self._sample()
        return control

    def on_evaluate(self, args, state, control, **kwargs):  # noqa: ANN001, ANN201
        self.probe.snap(f"评测 step {state.global_step}")
        return control

    def on_save(self, args, state, control, **kwargs):  # noqa: ANN001, ANN201
        self.probe.snap(f"保存 checkpoint step {state.global_step}")
        return control

    def on_train_end(self, args, state, control, **kwargs):  # noqa: ANN001, ANN201
        self.probe.snap("训练结束")
        self._finalize()
        return control

    def _finalize(self) -> None:
        if self._finalized or not self.step_samples:
            self._finalized = True
            return
        alloc = [s["allocated_mb"] for s in self.step_samples]
        peak = [s["peak_mb"] for s in self.step_samples]
        self.probe.note("步内均值(allocated_mb)", round(sum(alloc) / len(alloc), 1))
        self.probe.note("步内最大值(allocated_mb)", round(max(alloc), 1))
        self.probe.note("全程峰值(peak_mb)", round(max(peak), 1))
        self._finalized = True


class ActivationHook:
    """在 forward 之后打点，量「激活还活着」那一刻的显存。

    只挂 ``forward`` 钩子：``register_full_backward_hook`` 在输入不含梯度的模块上不保证触发，
    而 forward 钩子对整数输入安全。反向/优化器那一段用 ``on_step_end`` 的快照 + 全局峰值覆盖。

    ``limit`` 是自保：激活峰值每步都一样，没必要全程挂着钩子（每次打点要 ``cuda.synchronize``），
    采够 ``limit`` 次就自动摘掉。
    """

    def __init__(self, probe: MemProbe, every: int = 1, limit: int = 20) -> None:
        self.probe = probe
        self.every = max(1, every)
        self.limit = max(1, limit)
        self.calls = 0
        self.snaps = 0
        self._handle = None

    def attach(self, model: torch.nn.Module) -> None:
        def hook(module, args, output):  # noqa: ANN001, ANN202
            self.calls += 1
            if self.calls % self.every == 0 and self.snaps < self.limit:
                self.probe.snap("前向激活（峰值含激活）")
                self.snaps += 1
            elif self.snaps >= self.limit:
                self.detach()

        self._handle = model.register_forward_hook(hook)

    def detach(self) -> None:
        if self._handle is not None:
            self._handle.remove()
            self._handle = None


def save_report(probe: MemProbe, ledger: dict[str, Any] | None, path: str, extra: dict[str, Any] | None = None) -> None:
    """输入:探针/账本/路径。行为:把显存报告落成 json + 追加可读文本。输出:无。"""
    payload = {"mem": probe.to_dict(), "ledger": ledger, "extra": extra or {}}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
