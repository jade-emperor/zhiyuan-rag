#!/usr/bin/env python
"""显存预算实验：把一次训练步**逐阶段**的峰值量出来，并实测 OOM 边界。

为什么需要这个脚本：训练时 `MemProbe` 只在生命周期节点打点（forward 钩子 + 每 N 步），
能给出"这一时刻驻留多少"，但回答不了两个问题：
  1. **反向传播、优化器 step 那两段各占多少**（那才是峰值所在）；
  2. **到底至少要多少显存才不会 OOM**。
这个脚本用真实模型 + 真实 batch 跑一次完整训练步，每阶段前 `reset_peak_memory_stats()`，
所以每段峰值只属于该段；再用 `torch.cuda.set_per_process_memory_fraction` 把分配器上限压到
不同档位，实测"这一档能不能跑完一步" —— 二分出真实下限。

用法::

    # 逐阶段分解（默认 bs=32、动态 padding，和训练一致）
    python tools/lora/vram_budget.py --json logs/lora/run1/vram_budget.json

    # 顺带把 OOM 边界扫出来（每档一个独立进程，见 --cap-fraction）
    for f in 0.04 0.05 0.06 0.08 0.10; do
      python tools/lora/vram_budget.py --cap-fraction $f --quiet || echo "cap=$f OOM";
    done

⚠️ 与 AGENTS.md 一致：沙箱里 `torch.cuda.is_available()` 恒为 False，要看真实数字必须在容器里跑
（`docker run --rm --gpus all ...`）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

import torch

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_utils import LABELS, NUM_LABELS, load_jsonl  # noqa: E402
from mem_probe import (  # noqa: E402
    MB,
    _device_used_mb,
    cuda_visible,
    ledger_table,
    param_ledger,
)


def _true_device_used_mb() -> float:
    """输入:无。行为:取**真实的**整卡占用（口径与幻影见 ``mem_probe.WSL_PHANTOM_MB``）。输出:MB。

    这里不直接用 ``torch.cuda.mem_get_info()``:在 WSL 上它有一个 1063 MiB 的常数虚报，
    拿它当"显存占用"会把需求多算一倍(这个脚本第一版就是这么错的)。
    """
    return _device_used_mb()[0]

DEFAULT_MODEL = "models/chinese-roberta-wwm-ext"


def build_argparser() -> argparse.ArgumentParser:
    """输入:无。行为:定义实验参数。输出:ArgumentParser。"""
    parser = argparse.ArgumentParser(description="训练步显存分解 + OOM 边界实测")
    parser.add_argument("--model-name-or-path", default=DEFAULT_MODEL)
    parser.add_argument("--data-dir", default="data/intent/strategy_v1")
    parser.add_argument("--batch-size", type=int, default=32, help="与训练一致")
    parser.add_argument("--max-length", type=int, default=64)
    parser.add_argument("--dynamic-padding", action="store_true", default=True,
                        help="按 batch 内最长样本 padding（训练就是这么做的）")
    parser.add_argument("--pad-to-max", action="store_true", help="强制 pad 到 max-length（最坏情况）")
    parser.add_argument("--lora-r", type=int, default=8)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--lora-dropout", type=float, default=0.1)
    parser.add_argument("--target-modules", default="query,value")
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--cap-fraction", type=float, default=0.0,
                        help="把本进程分配器上限压到 设备总量×该比例（>0 时进入 OOM 边界测试模式）")
    parser.add_argument("--host-snapshot", action="store_true",
                        help="只做**宿主侧**探针（不初始化 CUDA）：读 Windows 的 nvidia-smi 与逐进程 GPU 计数器")
    parser.add_argument("--json", default=None, help="把结果写到这个 json")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论（扫边界时用）")
    return parser


class PhaseMeter:
    """逐阶段测峰值：每段前 reset_peak，读到的 max_memory_allocated 只属于该段。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def run(self, label: str, fn):  # noqa: ANN001, ANN201
        """输入:阶段名与可调用。行为:执行并记录该段的峰值/驻留。输出:fn 的返回值。"""
        if cuda_visible():
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            before = torch.cuda.memory_allocated() / MB
        else:
            before = 0.0
        out = fn()
        if cuda_visible():
            torch.cuda.synchronize()
            row = {
                "label": label,
                "peak_mb": torch.cuda.max_memory_allocated() / MB,
                "resident_mb": torch.cuda.memory_allocated() / MB,
                "reserved_mb": torch.cuda.memory_reserved() / MB,
                "before_mb": before,
                "device_used_mb": _true_device_used_mb(),
            }
        else:
            row = {"label": label, "peak_mb": 0.0, "resident_mb": 0.0, "reserved_mb": 0.0,
                   "before_mb": 0.0, "device_used_mb": 0.0}
        row["delta_peak_mb"] = row["peak_mb"] - row["before_mb"]
        self.rows.append(row)
        return out

    def table(self) -> str:
        """输入:无。行为:渲染阶段表。输出:str。"""
        lines = [
            f"\n  {'阶段':<26s} {'段内峰值MB':>11s} {'该段新增峰值MB':>14s} {'结束时驻留MB':>13s} {'整卡MB':>11s}",
        ]
        for r in self.rows:
            lines.append(
                f"  {r['label']:<26s} {r['peak_mb']:>11.1f} {r['delta_peak_mb']:>+14.1f} "
                f"{r['resident_mb']:>13.1f} {r['device_used_mb']:>11.1f}"
            )
        return "\n".join(lines)


def build_batch(tokenizer, texts: list[str], batch_size: int, max_length: int, pad_to_max: bool, device):  # noqa: ANN001, ANN201
    """输入:分词器与文本。行为:造一个真实 batch（可选最坏情况 padding）。输出:(inputs, labels, token 数)。"""
    picked = texts[:batch_size]
    encoding = tokenizer(picked, truncation=True, max_length=max_length)
    lengths = [len(x) for x in encoding["input_ids"]]
    pad_len = max_length if pad_to_max else max(lengths)
    input_ids, attention = [], []
    for ids in encoding["input_ids"]:
        pad = pad_len - len(ids)
        input_ids.append(ids + [tokenizer.pad_token_id] * pad)
        attention.append([1] * len(ids) + [0] * pad)
    inputs = {
        "input_ids": torch.tensor(input_ids, device=device),
        "attention_mask": torch.tensor(attention, device=device),
    }
    labels = torch.arange(batch_size, device=device) % NUM_LABELS  # 只用于算 loss，类别均衡即可
    return inputs, labels, lengths, pad_len


def host_snapshot() -> dict:
    """输入:无。行为:走 Windows 侧通道（nvidia-smi.exe + PowerShell GPU 计数器）读整卡与逐进程占用。

    ⚠️ 为什么宿主侧那条路仍然留着：它能看见 **WSL 虚拟机之外的占用** ——
    桌面、浏览器、别的 Windows 进程。那是我这个进程无论怎么问都看不到的一半。
    但**「WSL 里看到的一定比宿主小」这句话是错的**（第一版就是这么写的）：
    实测 `torch.cuda.mem_get_info()` 反而**多报 1063 MiB**（常数幻影，见
    `mem_probe.WSL_PHANTOM_MB` 那张三方对照表）。所以：

    * 想知道**本进程**占了多少 → 容器内 `nvidia-smi`（进程里 subprocess 问一下就有，真值）；
    * 想知道**整张卡还有多少给别人** → 这个 `--host-snapshot`（它才看得见 WSL 之外的占用）。
    """
    import subprocess

    out: dict = {"nvidia_smi": {}, "processes": [], "raw": {}}
    smi = "/mnt/c/Windows/System32/nvidia-smi.exe"
    try:
        text = subprocess.run([smi, "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        out["raw"]["nvidia_smi"] = text
        if "," in text:
            name, used, total = [x.strip() for x in text.split(",")]
            out["nvidia_smi"] = {"name": name, "used_mib": float(used.split()[0]), "total_mib": float(total.split()[0])}
    except Exception as exc:  # noqa: BLE001 - 不在 WSL 或没有 Windows 侧工具时，这条探针就是不可用
        out["nvidia_smi"] = {"error": f"{type(exc).__name__}: {exc}"}

    ps = (
        "Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty CounterSamples | "
        "Where-Object {$_.CookedValue -gt 20MB} | Sort-Object CookedValue -Descending | "
        "ForEach-Object { \"$($_.InstanceName)`t$([math]::Round($_.CookedValue/1MB,1))\" }"
    )
    try:
        text = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=120).stdout
        out["raw"]["powershell"] = text.strip()
        for line in text.splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 2:
                continue
            entry = {"instance": parts[0], "dedicated_mib": float(parts[1])}
            match = re.search(r"pid_(\d+)", entry["instance"])
            if match:
                entry["pid"] = int(match.group(1))
                entry["process_name"] = _pid_name(entry["pid"])
            out["processes"].append(entry)
    except Exception as exc:  # noqa: BLE001
        out["processes"] = [{"error": f"{type(exc).__name__}: {exc}"}]
    return out


def _pid_name(pid: int) -> str:
    """输入:pid。行为:走 Windows 侧 tasklist 查进程名（把 pid_8092 这种匿名句柄翻译成人看得懂的名字）。输出:名字。"""
    import subprocess

    try:
        text = subprocess.run(["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True, timeout=60, errors="replace").stdout
        first = text.strip().splitlines()[0] if text.strip() else ""
        if first.startswith('"'):
            return first.split('","')[0].strip('"')
    except Exception:  # noqa: BLE001
        pass
    return "(未知)"


def main(argv: list[str] | None = None) -> int:
    """输入:命令行参数。行为:逐阶段测显存；带 --cap-fraction 时测 OOM 边界。输出:退出码。"""
    args = build_argparser().parse_args(argv)

    if args.host_snapshot:
        snap = host_snapshot()
        smi = snap["nvidia_smi"]
        print("===== 宿主侧（Windows）显存视图 =====")
        if "error" in smi:
            print(f"  nvidia-smi 不可用：{smi['error']}")
        else:
            print(f"  {smi['name']}：已用 {smi['used_mib']:.0f} MiB / 共 {smi['total_mib']:.0f} MiB")
        print(f"  {'进程/VM':<46s} {'专用显存 MiB':>12s}")
        for row in snap["processes"]:
            if "error" in row:
                print(f"  （逐进程计数器不可用：{row['error']}）")
            else:
                name = row.get("process_name", "")
                label = f"{row['instance']}" + (f"  → {name}" if name else "")
                print(f"  {label:<60s} {row['dedicated_mib']:>10.1f}")
        print("\n  提示：`pid_*_luid_*` 里那个 pid 用 `tasklist /FI \"PID eq <pid>\"` 就能看到是谁；")
        print("       WSL 的 CUDA 占用通常记在 `vmwp` / `vmmemWSL` 名下（整个虚拟机的持有量，")
        print("       进程退出后不会立刻还给 Windows —— 所以它会一直显示在任务管理器里）。")
        if args.json:
            Path(args.json).write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n  已写入 {args.json}")
        return 0

    if not cuda_visible():
        print("本进程看不到 CUDA（沙箱私有 /dev，见 AGENTS.md）——请在容器里跑："
              "docker run --rm --gpus all ...", file=sys.stderr)
        return 3

    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = torch.device("cuda")
    total_mb = torch.cuda.get_device_properties(0).total_memory / MB

    # 先建 context（计入固定开销），再压上限 —— 否则上限会把 context 也算进去，测出来的不是分配器预算
    torch.zeros(1, device=device)
    torch.cuda.synchronize()
    context_mb = torch.cuda.memory_allocated() / MB  # context 不在 allocated 里，仅占位
    device_after_context = _true_device_used_mb()
    if args.cap_fraction > 0:
        torch.cuda.set_per_process_memory_fraction(args.cap_fraction, device=0)
        cap_mb = total_mb * args.cap_fraction
    else:
        cap_mb = None

    meter = PhaseMeter()
    texts = [r["query"] for r in load_jsonl(Path(args.data_dir) / "train.jsonl")]
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    batch, labels, lengths, pad_len = build_batch(
        tokenizer, texts, args.batch_size, args.max_length, args.pad_to_max, device
    )

    ok = True
    failure = None
    try:
        model = meter.run("① 建模型（CPU 载入）", lambda: AutoModelForSequenceClassification.from_pretrained(
            args.model_name_or_path, num_labels=NUM_LABELS, problem_type="single_label_classification"))
        model.config.id2label = dict(LABELS)
        meter.run("② 权重上卡（fp32）", lambda: model.to(device))
        lora = LoraConfig(task_type=TaskType.SEQ_CLS, r=args.lora_r, lora_alpha=args.lora_alpha,
                          lora_dropout=args.lora_dropout, bias="none",
                          target_modules=[m.strip() for m in args.target_modules.split(",") if m.strip()],
                          modules_to_save=["classifier"])
        model = meter.run("③ 注入 LoRA + 分类头", lambda: get_peft_model(model, lora))
        model.train()
        params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(params, lr=args.lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=0.01)
        criterion = torch.nn.CrossEntropyLoss()

        def fwd():
            return model(**batch).logits

        logits = meter.run("④ 前向（激活存活）", fwd)
        loss = criterion(logits.float(), labels)

        def bwd():
            loss.backward()

        meter.run("⑤ 反向（梯度生成）", bwd)

        def step1():
            optimizer.step()

        meter.run("⑥ 优化器 step（AdamW 状态落地）", step1)
        optimizer.zero_grad(set_to_none=True)

        # 第二次完整步 = 稳态（AdamW 状态已存在，梯度重新分配）
        meter.run("⑦ 稳态前向", fwd)
        loss2 = criterion(model(**batch).logits.float(), labels)
        meter.run("⑧ 稳态反向", lambda: loss2.backward())
        meter.run("⑨ 稳态 step", lambda: optimizer.step())
        optimizer.zero_grad(set_to_none=True)

        def eval_fwd():
            model.eval()
            with torch.no_grad():
                return model(**batch).logits

        meter.run("⑩ 评测前向（no_grad）", eval_fwd)
        model.train()
    except torch.cuda.OutOfMemoryError as exc:  # 期待中的失败：这就是边界
        ok = False
        failure = str(exc).splitlines()[0][:160]
        torch.cuda.empty_cache()

    if cap_mb:
        if args.quiet:
            print(f"cap={args.cap_fraction:.2f} ({cap_mb:.0f} MB) -> {'OK' if ok else 'OOM: ' + (failure or '')}")
        else:
            print(f"\n== OOM 边界测试：分配器上限 {cap_mb:.0f} MB（设备 {total_mb:.0f} MB × {args.cap_fraction}）==")
            print(f"  结果：{'能跑完一步 ✔' if ok else 'OOM ✘ —— ' + (failure or '')}")
            print(meter.table())
        if args.json:
            Path(args.json).write_text(json.dumps(
                {"mode": "cap", "cap_fraction": args.cap_fraction, "cap_mb": cap_mb,
                 "ok": ok, "failure": failure, "total_device_mb": total_mb,
                 "context_overhead_mb": device_after_context, "batch_pad_len": pad_len,
                 "phases": meter.rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if ok else 1

    # ---- 正常模式：打印完整分解 ----
    ledger = param_ledger(model)
    print(f"\n===== 设备：{torch.cuda.get_device_name(0)}（{total_mb:.0f} MB）=====")
    print(f"  CUDA context + 库句柄（建 context 后、装权重前）：**{device_after_context:.1f} MB**")
    print(f"  batch：{args.batch_size} 条，真实长度 {min(lengths)}~{max(lengths)}，"
          f"padding 到 {pad_len}（{'强制 max_length' if args.pad_to_max else '动态 padding'}）")
    print(meter.table())
    print(ledger_table(ledger, mode="train"))

    def peak(label: str) -> float:
        row = next((r for r in meter.rows if r["label"].startswith(label)), None)
        return row["peak_mb"] if row else float("nan")

    weights = peak("② 权重上卡")
    fwd_peak = peak("④ 前向")
    bwd_peak = peak("⑤ 反向")
    steady_peak = max(peak("⑧ 稳态反向"), peak("⑨ 稳态 step"))
    eval_peak = peak("⑩ 评测前向")
    overall = max(r["peak_mb"] for r in meter.rows)
    print("\n===== 分解（按「这段时间里同时活着什么」读）=====")
    print(f"  权重（基座+LoRA+分类头）        : {weights:>8.1f} MB")
    print(f"  激活（前向峰值 − 权重）          : {fwd_peak - weights:>8.1f} MB")
    print(f"  反向段峰值 − 权重               : {bwd_peak - weights:>8.1f} MB（激活 + 梯度 + 临时张量）")
    print(f"  稳态段峰值 − 权重               : {steady_peak - weights:>8.1f} MB（激活 + 梯度 + AdamW 状态）")
    print(f"  评测（no_grad）峰值 − 权重       : {eval_peak - weights:>8.1f} MB")
    print(f"  整步最高峰值                     : {overall:>8.1f} MB")
    print(f"  → 最少可用显存 ≈ context {device_after_context:.0f} + 整步峰值 {overall:.0f} "
          f"= **{device_after_context + overall:.0f} MB**，再留 15~20% 余量 ≈ "
          f"**{(device_after_context + overall) * 1.2:.0f} MB**")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"mode": "decompose", "device": torch.cuda.get_device_name(0), "total_device_mb": total_mb,
             "context_overhead_mb": device_after_context, "batch_size": args.batch_size,
             "pad_len": pad_len, "token_lengths": [min(lengths), max(lengths)],
             "phases": meter.rows, "ledger": ledger,
             "summary": {"weights_mb": weights, "activation_mb": fwd_peak - weights,
                         "backward_extra_mb": bwd_peak - weights, "steady_extra_mb": steady_peak - weights,
                         "eval_extra_mb": eval_peak - weights, "step_peak_mb": overall,
                         "min_total_mb": device_after_context + overall,
                         "recommended_total_mb": (device_after_context + overall) * 1.2}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - 扫边界时要把栈打出来，便于判断是 OOM 还是别的错
        traceback.print_exc()
        raise SystemExit(1) from None
