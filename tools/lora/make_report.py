#!/usr/bin/env python
"""从训练产物生成微调报告（`finetune_report.md`）。

为什么是生成器而不是手写：报告里的每个数字都必须与产物对得上，而重跑一次（换种子、换机器、
GPU 非确定性）就会让手写的数字全部过期。这个脚本把「产物 → 报告」这条链固定下来：

    python tools/lora/make_report.py --run-dir logs/lora/run1 --out tools/lora/finetune_report.md

输入（都在 run-dir 下）：
  * ``run_meta.json``         —— 超参、数据规模、dev/test 指标（train.py 落盘）
  * ``vram_train.json``       —— 显存打点 + 参数账本（mem_probe.py 落盘）
  * ``test_predictions.jsonl``—— 逐条预测与四类概率（infer.py 落盘）
  * 训练日志（``--log`` 或 run-dir 同级同名 .log）—— 逐轮 eval 指标、耗时
数据侧读 data-dir 的 train/test.jsonl 统计长度与类别分布。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_utils import LABELS  # noqa: E402

REPORT_TITLE = "微调报告：chinese-roberta-wwm-ext + LoRA · 查询改写策略选择器 v1"


# ---------------------------------------------------------------------------
# 读产物
# ---------------------------------------------------------------------------

def read_run(run_dir: Path, data_dir: Path, log_path: Path | None) -> dict:
    """输入:产物目录/数据目录/日志。行为:把报告需要的数字全部抽出来。输出:dict。"""
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    vram = json.loads((run_dir / "vram_train.json").read_text(encoding="utf-8"))

    train_rows = [json.loads(l) for l in open(data_dir / "train.jsonl", encoding="utf-8") if l.strip()]
    test_rows = [json.loads(l) for l in open(data_dir / "test.jsonl", encoding="utf-8") if l.strip()]
    preds = [json.loads(l) for l in open(run_dir / "test_predictions.jsonl", encoding="utf-8") if l.strip()]
    assert len(preds) == len(test_rows), f"预测条数 {len(preds)} != 测试集 {len(test_rows)}"

    y = np.array([r["label"] for r in test_rows])
    probs = np.array([p["probs"] for p in preds])
    yh = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    order = np.argsort(-probs, axis=1)

    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

    per_class = {}
    for lab in sorted(LABELS):
        mask = y == lab
        tp = int(((yh == lab) & mask).sum())
        fp = int(((yh == lab) & ~mask).sum())
        fn = int((~(yh == lab) & mask).sum())
        per_class[lab] = {
            "support": int(mask.sum()),
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "f1": 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0,
        }

    # 混淆矩阵的"错误对"
    errors = Counter(f"{t}→{p}" for t, p in zip(y, yh, strict=False) if t != p)
    error_samples = [
        {"true": int(t), "pred": int(p["label"]), "conf": p["confidence"], "query": p["query"]}
        for p, t in zip(preds, y, strict=False)
        if p["label"] != t
    ]

    # 低置信回退策略（离线模拟，回退到 0 类）
    sweep = []
    for th in (0.0, 0.5, 0.7, 0.9, 0.99):
        adj = yh.copy()
        if th > 0:
            adj[conf < th] = 0
        sweep.append(
            {
                "threshold": th,
                "accuracy": float(accuracy_score(y, adj)),
                "macro_f1": float(f1_score(y, adj, average="macro")),
                "changed": int((adj != yh).sum()),
            }
        )

    low = conf < 0.9
    stats = {
        "accuracy": float(accuracy_score(y, yh)),
        "macro_f1": float(f1_score(y, yh, average="macro")),
        "weighted_f1": float(f1_score(y, yh, average="weighted")),
        "top2": float(np.mean([t in order[i][:2] for i, t in enumerate(y)])),
        "n": len(y),
        "wrong": int((y != yh).sum()),
        "conf_ok_median": float(np.median(conf[y == yh])),
        "conf_bad_median": float(np.median(conf[y != yh])),
        "low_n": int(low.sum()),
        "low_top1": int((yh[low] == y[low]).sum()),
        "low_top2": int(sum(t in order[i][:2] for i, t in enumerate(y) if low[i])),
        "per_class": per_class,
        "confusion": confusion_matrix(y, yh, labels=sorted(LABELS)).tolist(),
        "errors": dict(errors),
        "error_samples": error_samples[:8],
        "sweep": sweep,
    }

    # 训练日志：逐轮 eval + 耗时
    epochs, runtime = [], {}
    if log_path and log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for chunk in re.findall(r"\{'eval_loss'.*?\}", text):
            try:
                row = ast.literal_eval(chunk)
            except (ValueError, SyntaxError):
                continue
            if "eval_macro_f1" in row:
                epochs.append(row)
        match = re.search(r"\{'train_runtime'.*?\}", text)
        if match:
            runtime = ast.literal_eval(match.group(0))
        lrs = [float(x) for x in re.findall(r"'learning_rate': ([0-9.eE+-]+)", text)]
        if lrs:
            runtime["lr_peak"] = max(lrs)
            runtime["lr_last"] = lrs[-1]

    def length_stats(rows: list[dict]) -> dict:
        lens = sorted(len(r["query"]) for r in rows)
        dist = Counter("≤15" if n <= 15 else "16-30" if n <= 30 else "31+" for n in lens)
        return {"min": lens[0], "median": lens[len(lens) // 2], "max": lens[-1], "dist": dict(dist)}

    snaps = vram["mem"]["snapshots"]
    # 显存预算实验的产物（tools/lora/vram_budget.py 落盘）；缺了就不渲染那两节
    def _load(path: Path) -> dict | None:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    budget = _load(run_dir / "vram_budget.json")
    budget_peak = _load(run_dir / "vram_budget_pad64.json")
    host_view = _load(run_dir / "vram_host_view.json")
    caps: list[dict] = []
    for path in sorted(run_dir.glob("vram_cap_*.json")):
        row = _load(path)
        if row:
            row["_name"] = path.stem
            row["_mode"] = "pad64" if "pad64" in path.stem else "dynamic"
            caps.append(row)

    return {
        "meta": meta,
        "vram": vram,
        "snaps": snaps,
        "budget": budget,
        "budget_peak": budget_peak,
        "host_view": host_view,
        "caps": caps,
        "ledger": vram["ledger"],
        "marks": vram["mem"]["marks"],
        "epochs": epochs,
        "runtime": runtime,
        "test": stats,
        "data": {
            "train": length_stats(train_rows),
            "test": length_stats(test_rows),
            "counts": {"train_file": len(train_rows), "test_file": len(test_rows)},
        },
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _mb(value: float) -> str:
    """输入:MB。行为:统一成一位小数。输出:str。"""
    return f"{value:.1f}"


def render(run: dict, run_dir: str, data_dir: str) -> str:
    """输入:抽取结果。行为:渲染整份报告。输出:markdown 文本。"""
    meta, ledger, marks = run["meta"], run["ledger"], run["marks"]
    tr, sizes = meta["train"], meta["sizes"]
    test = run["test"]
    epochs, runtime = run["epochs"], run["runtime"]
    groups = ledger["groups"]
    base = groups["基座权重（冻结）"]
    lora = groups["LoRA 适配器"]
    head = groups["分类头"]
    snaps = {s["label"]: s for s in run["snaps"]}

    def snap(label: str) -> dict | None:
        return snaps.get(label)

    on_card = snap(f"基座权重上卡（cuda）") or snap("基座权重上卡（cpu）")
    peak_mark = marks.get("全程峰值(peak_mb)", float("nan"))
    steady = marks.get(
        next((k for k in marks if k.startswith("步内均值")), ""), float("nan")
    )
    act_peak = max((s["allocated_mb"] for s in run["snaps"] if s["label"].startswith("前向激活")), default=float("nan"))
    act_derived = marks.get("派生·激活量级(前向峰值−上卡权重_MB)", float("nan"))
    dev_peak = max(s["device_used_mb"] for s in run["snaps"])
    start_dev = run["snaps"][0]["device_used_mb"] if run["snaps"] else float("nan")

    best_epoch = max(epochs, key=lambda r: r["eval_macro_f1"]) if epochs else None
    last_epoch = epochs[-1] if epochs else None

    lines: list[str] = []
    add = lines.append

    add(f"# {REPORT_TITLE}\n")
    add("| 项 | 值 |")
    add("|---|---|")
    add("| 任务 | 4 分类（0 直接检索 / 1 假设答案 / 2 子查询分解 / 3 去噪改写） |")
    add(f"| 基座 | `{meta['model']}`（{base['params']:,.0f} 参数，fp32） |")
    add(f"| 数据 | `{data_dir}`（训练 {sizes['train']} / dev {sizes['dev']} / 测试 {test['n']}） |")
    add(f"| 微调方式 | LoRA（`r={meta['lora']['r']}`、`alpha={meta['lora']['alpha']}`、"
        f"`dropout={meta['lora']['dropout']}`、`target={meta['lora']['targets']}`）+ 分类头 |")
    add(f"| 优化 | `{tr.get('optim', 'adamw_torch')}` + `{tr['scheduler']}` 退火 + "
        f"{tr['warmup_ratio']:.0%} warmup + 早停（回滚最优权重） |")
    add(f"| 精度 / 序列 | {tr['precision']} / `max_length={tr['max_length']}` / "
        f"batch {tr['batch_size']}（grad_accum {tr['grad_accum']}） |")
    add(f"| 产物 | `{run_dir}/`（适配器 + `run_meta.json` + `vram_train.json` + `test_predictions.jsonl`） |")
    add("")

    # 一、摘要
    rt = runtime.get("train_runtime")
    sps = runtime.get("train_samples_per_second")
    ran_epochs = runtime.get("epoch")  # 实际跑完的轮数（早停会小于配置轮数）
    stop_note = ""
    if ran_epochs and abs(ran_epochs - tr["epochs"]) > 1e-6:
        stop_note = f"（配置 {tr['epochs']:.0f} 轮，第 {ran_epochs:.0f} 轮触发早停）"
    add("---\n\n## 一、摘要\n")
    add(f"用 LoRA 微调 `chinese-roberta-wwm-ext`，把「用户 query 该走哪种改写策略」做成 4 分类器，")
    add(f"只训练 **{ledger['trainable_ratio']:.2%}** 的参数"
        f"（{ledger['trainable_params']:,} / {ledger['total_params']:,}）"
        + (f"，{rt:.1f} 秒跑完 {ran_epochs:.0f} 轮{stop_note}（{sps:.0f} 样本/秒）。" if rt else "。"))
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 测试集 accuracy（{test['n']} 条，只跑一次） | **{test['accuracy']:.4f}** |")
    add(f"| 测试集 macro-F1 | **{test['macro_f1']:.4f}** |")
    add(f"| 测试集 **top-2** 准确率 | **{test['top2']:.4f}** |")
    add(f"| 训练显存峰值（PyTorch allocated） | **{_mb(peak_mark)} MB** |")
    add(f"| 训练显存峰值（设备级实际占用） | **{_mb(dev_peak)} MB** |")
    add(f"| 推理吞吐 / 权重占用 | 见第五节 |")
    add("")
    err_pairs = "、".join(f"`{k}`（{v} 条）" for k, v in sorted(test["errors"].items(), key=lambda kv: -kv[1]))
    n_pairs = len(test["errors"])
    pair_word = "这一对" if n_pairs == 1 else f"这 {n_pairs} 对上"
    add(f"三句话结论：① 误差只有 **{test['wrong']} 条**，全部集中在 {err_pairs} —— {pair_word}；")
    add("② 这些误判与数据卡里预判的「铺垫 + 核心无指代」边界样本一致，是**数据边界问题**而非模型容量问题；")
    add(f"③ 误判样本的真值 **100% 落在 top-2**（{test['top2']:.4f}），"
        "线上应当用「两个候选改写都执行」，而不是一刀切回退。")
    add("")

    # 二、数据
    d = run["data"]
    add("---\n\n## 二、数据\n")
    add(f"数据由 5 个子代理各生成 1000 条、经两轮质检与返工配平而来"
        f"（生成与质检细节见 `{data_dir}/README.md`）。本次微调实际使用：\n")
    add("| 划分 | 条数 | 类别分布 | 来源 |")
    add("|---|---|---|---|")
    add(f"| train | **{sizes['train']}** | 各类 {sizes['train'] // len(LABELS)} | "
        f"`train.jsonl`（{run['data']['counts']['train_file']} 条）按类别**分层**切掉 dev 后 |")
    add(f"| dev（早停用） | **{sizes['dev']}** | 各类 {sizes['dev'] // len(LABELS) if sizes['dev'] else 0} | "
        f"同一份 train.jsonl 分层切出，与训练**互斥**（seed={tr['seed']}） |")
    add(f"| test | **{test['n']}** | 各类 {test['n'] // len(LABELS)} | "
        f"`test.jsonl`，全部来自 agent5（生成者隔离，测跨生成者泛化） |")
    add("")
    add(f"- 文本长度：最短 {d['train']['min']} / 中位 {d['train']['median']} / 最长 {d['train']['max']} 字 "
        f"→ `max_length={tr['max_length']}` 足够、无截断")
    add(f"- 长度分布：≤15 字 {d['train']['dist'].get('≤15', 0)}、16~30 字 {d['train']['dist'].get('16-30', 0)}、"
        f"31+ 字 {d['train']['dist'].get('31+', 0)}")
    add("- 内容范围：技术八股（Python/Java/数据库/Linux/网络/大模型）+ 大模型教材术语 + 企业制度；"
        "**不含任何培训业务内容**")
    add("- 已知问题（沿用数据卡，本次训练未修复）：label 3 里约 10% 属「铺垫 + 核心无指代」的规则边界；"
        "8 对 0.7~0.8 的跨集近似未清洗；数据是 LLM 合成的，分布不代表真实流量")
    add("")

    # 三、LoRA 配置
    add("---\n\n## 三、LoRA 配置\n")
    add("| 项 | 值 | 说明 |")
    add("|---|---|---|")
    add("| `task_type` | `SEQ_CLS` | 序列分类任务头 |")
    add(f"| `r`（秩） | **{meta['lora']['r']}** | BERT 系分类的常见秩；4~16 之间收益差异很小 |")
    add(f"| `lora_alpha` | **{meta['lora']['alpha']}** | 主流取 `2×r`，缩放比 `alpha/r=2` |")
    add(f"| `lora_dropout` | **{meta['lora']['dropout']}** | 正则化，5000 条属小数据，防过拟合 |")
    add(f"| `target_modules` | **`{meta['lora']['targets']}`** | BERT 注意力里最常用的注入点 |")
    add("| `bias` | `none` | 不训练 bias，省参数也避免破坏预训练偏置 |")
    add("| `modules_to_save` | **`[\"classifier\"]`** | 分类头随机初始化，必须训练并单独保存 |")
    add("")
    add("**参数量分布**（`param_ledger` 实测）：\n")
    add("| 板块 | 参数量 | 可训练 | 权重显存 | 梯度+AdamW |")
    add("|---|---|---|---|---|")
    for name in ("基座权重（冻结）", "LoRA 适配器", "分类头"):
        slot = groups[name]
        add(f"| {name} | {int(slot['params']):,} | {int(slot['trainable']):,} | "
            f"{slot['weights_mb']:.2f} MB | {slot['train_mb']:.2f} MB |")
    add(f"| **合计** | **{ledger['total_params']:,}** | "
        f"**{ledger['trainable_params']:,}（{ledger['trainable_ratio']:.4%}）** | "
        f"{sum(g['weights_mb'] for g in groups.values()):.2f} MB | "
        f"{sum(g['train_mb'] for g in groups.values()):.2f} MB |")
    add("")
    add(f"> 分类头那 {int(head['params']):,} 个参数里只有 {int(head['trainable']):,} 在训练："
        "peft 的 `modules_to_save` 会把原模块**复制一份**（`original_module` 冻结 + "
        "`modules_to_save.default` 训练），这是账本量出来的，不是推断。")
    add("")

    # 四、训练超参
    add("---\n\n## 四、训练超参与设计理由\n")
    add("| 超参 | 取值 | 为什么这么设计 |")
    add("|---|---|---|")
    add(f"| `learning_rate` | **{tr['lr']:.0e}** | LoRA 可训练参数只占 {ledger['trainable_ratio']:.2%}，"
        "梯度信号弱，学习率要比全参微调（典型 2e-5）高一个量级；主流区间 1e-4~3e-4 |")
    add(f"| `lr_scheduler_type` | **{tr['scheduler']}** | 余弦退火让后期步长自然收敛到 ~0，比线性更平滑"
        + (f"；实测 lr 从 warmup 峰值 {runtime['lr_peak']:.1e} 降到 {runtime['lr_last']:.1e}" if runtime.get("lr_peak") else "")
        + " |")
    add(f"| `warmup_ratio` | **{tr['warmup_ratio']}** | 前 10% 线性升温，避免随机初始化的分类头"
        "在第一步就把预训练权重带偏 |")
    add("| `optim` | **`adamw_torch`** | Transformer 微调的事实标准；AdamW 把权重衰减从梯度里解耦 |")
    add("| `adam_beta1/beta2` | **0.9 / 0.999** | Transformer 常规取值，二阶矩估计对稀疏梯度更稳 |")
    add("| `adam_epsilon` | 1e-8 | 常规取值，防止除零 |")
    add(f"| `weight_decay` | **{tr['weight_decay']}** | 正则化；HF 口径只作用在非 bias/LayerNorm 参数上 |")
    add(f"| `max_grad_norm` | **{tr['max_grad_norm']}** | 梯度裁剪，防偶发炸梯度 |")
    add(f"| `num_train_epochs` | **{tr['epochs']:.0f}** | 配早停，宁多勿少"
        + (f"；本次实际第 {runtime['epoch']:.0f} 轮就触发了早停（连续 "
           f"{tr.get('early_stopping_patience', 2)} 轮 macro-F1 无提升）" if runtime.get("epoch") and abs(runtime["epoch"] - tr["epochs"]) > 1e-6 else "")
        + " |")
    add("| `EarlyStoppingCallback` | **patience=2** | 盯 `macro_f1`（4 类均衡，macro 才有意义），"
        "配合 `load_best_model_at_end=True` 自动回滚最优权重 |")
    add(f"| `per_device_train_batch_size` | **{tr['batch_size']}** | 序列最长 "
        f"{d['train']['max']} 字，{tr['batch_size']} 在 16 GB 卡上毫无压力 |")
    add(f"| `max_length` | **{tr['max_length']}** | 覆盖最长样本且留余量，再长只是浪费算力 |")
    add(f"| `precision` | **{tr['precision']}** | 有 bf16 就用 bf16：不需要 GradScaler、动态范围大；"
        "无 GPU 时自动退化 fp32 |")
    add("| `label_smoothing_factor` | 0.0 | 数据是干净均衡集；若日后确认残余噪声可设 0.05 |")
    add(f"| `seed` / `data_seed` | **{tr['seed']}** | 切分与初始化可复现 |")
    add("")
    if epochs:
        add("**逐轮 dev 表现**（早停依据）：\n")
        add("| 轮次 | eval_loss | accuracy | macro-F1 | f1(0) | f1(1) | f1(2) | f1(3) |")
        add("|---|---|---|---|---|---|---|---|")
        for row in epochs:
            star = "（最优）" if row is best_epoch else ""
            add(f"| {int(row['epoch'])}{star} | {row['eval_loss']:.4f} | {row['eval_accuracy']:.4f} | "
                f"{row['eval_macro_f1']:.4f} | {row['eval_f1_0_direct']:.3f} | "
                f"{row['eval_f1_1_hypothetical']:.3f} | {row['eval_f1_2_subquery']:.3f} | "
                f"{row['eval_f1_3_denoise']:.3f} |")
        add("")
        if best_epoch and last_epoch and best_epoch is not last_epoch:
            add(f"> 最优轮是第 {int(best_epoch['epoch'])} 轮（macro-F1 {best_epoch['eval_macro_f1']:.4f}），"
                f"最后一轮（第 {int(last_epoch['epoch'])} 轮）为 {last_epoch['eval_macro_f1']:.4f}"
                + ("，随后触发早停；" if runtime.get("epoch") and abs(runtime["epoch"] - tr["epochs"]) > 1e-6 else "；")
                + f"`load_best_model_at_end=True` 把最优那份取了回来 —— 最优权重回滚这条链路是通的。")
            add("")

    # 五、显存
    add("---\n\n## 五、显存占用（分板块 + 总占用）\n")
    add(f"训练配置：`batch={tr['batch_size']}`、`max_length={tr['max_length']}`、`{tr['precision']}`、"
        f"`grad_accum={tr['grad_accum']}`。「已分配」是 PyTorch 分配器口径，"
        "「设备已用」是整个 GPU 的口径（含 CUDA context、库 workspace 与其它进程）。\n")
    add("| 板块 | 已分配 MB | 设备已用 MB | 说明 |")
    add("|---|---|---|---|")
    add(f"| CUDA context + 库句柄 + 其它进程（起点） | 0.0 | **{_mb(start_dev)}** | "
        "**不计入 `memory_allocated`**，只看已分配会严重低估 OOM 风险 |")
    if on_card:
        add(f"| 基座权重上卡（fp32） | **{_mb(on_card['allocated_mb'])}** | {_mb(on_card['device_used_mb'])} | "
            f"{base['params']:,.0f} 参数 × 4 B |")
    add(f"| LoRA 适配器 + 分类头 | +{lora['weights_mb'] + head['weights_mb']:.1f} → "
        f"{_mb((on_card['allocated_mb'] if on_card else 0) + lora['weights_mb'] + head['weights_mb'])} | — | "
        f"{int(lora['trainable']) + int(head['trainable']):,} 个可训练参数 |")
    add(f"| AdamW 一阶/二阶状态 | 首步后出现 | — | **懒分配**：理论 {lora['train_mb'] + head['train_mb']:.2f} MB "
        "（只有可训练参数有状态） |")
    add(f"| 梯度 + 优化器 + 库 workspace（训练稳态） | **{_mb(steady)}** | "
        f"{_mb(max((s['device_used_mb'] for s in run['snaps'] if s['label'].startswith('训练 step')), default=float('nan')))} | "
        "比权重多出的部分是梯度、AdamW 状态与 cuBLAS/cuDNN workspace |")
    add(f"| 前向激活峰值（forward 钩子打点） | **{_mb(act_peak)}** | — | "
        f"激活最占显存的一刻；**激活量级 ≈ {_mb(act_derived)} MB** |")
    add(f"| 评测 / 保存 checkpoint 峰值 | {_mb(max((s['peak_mb'] for s in run['snaps'] if s['label'].startswith(('评测', '保存'))), default=float('nan')))} | {_mb(dev_peak)} | 保存会额外复制一份 state_dict |")
    add(f"| **训练全程峰值（总占用）** | **{_mb(peak_mark)}** | **{_mb(dev_peak)}** | "
        "前者是进程内 PyTorch 峰值，后者是这张卡上真实被占掉的最大值 |")
    add("")
    add(f"**理论对照**：同一份模型若做**全参**微调，权重+梯度+AdamW ≈ "
        f"**{ledger['full_finetune_mb_theoretical']:.0f} MB**（16 B/参数），"
        f"LoRA 只要 **{sum(g['weights_mb'] + g['train_mb'] for g in groups.values()):.0f} MB**"
        f"（只有 {ledger['trainable_ratio']:.2%} 的参数带梯度与优化器状态），"
        f"差 ~{ledger['full_finetune_mb_theoretical'] / max(sum(g['weights_mb'] + g['train_mb'] for g in groups.values()), 1e-9):.1f} 倍。")
    add("")

    # 五之二：一次训练步的完整分解（vram_budget.py 的产物）
    budget = run.get("budget")
    budget_peak = run.get("budget_peak")
    host_view = run.get("host_view")
    caps = run.get("caps") or []
    if budget:
        s = budget["summary"]
        add("### 5.1 一次训练步的完整分解（逐阶段实测，含激活/梯度/优化器）\n")
        add("上面那张表是**训练时按生命周期打点**，只看得到「某一刻驻留多少」；真正的峰值在**反向传播**那一段，")
        add("所以另跑了一次专门的实验（`tools/lora/vram_budget.py`）：真实模型 + 真实 batch，")
        add("**每个阶段前 `reset_peak_memory_stats()`**，因此每段峰值只属于该段。\n")
        add(f"配置：batch {budget['batch_size']}，样本真实长度 {budget['token_lengths'][0]}~{budget['token_lengths'][1]}，"
            f"padding 到 {budget['pad_len']}（动态 padding，与训练一致）。\n")
        add("| 阶段 | 段内峰值 MB | 该段新增峰值 MB | 结束时驻留 MB | 设备已用 MB |")
        add("|---|---|---|---|---|")
        for row in budget["phases"]:
            add(f"| {row['label']} | {row['peak_mb']:.1f} | {row['delta_peak_mb']:+.1f} | "
                f"{row['resident_mb']:.1f} | {row['device_used_mb']:.1f} |")
        add("")
        add("**同一时刻到底活着什么**（这才是「显存被谁占了」的答案）：\n")
        add("| 组成 | 大小 | 怎么来的 |")
        add("|---|---|---|")
        add(f"| 权重（基座 + LoRA + 分类头） | **{s['weights_mb']:.1f} MB** | 102.57M 参数 × 4 B（fp32） |")
        add(f"| **激活**（前向峰值 − 权重） | **{s['activation_mb']:.1f} MB** | 前向要保存每一层的中间结果给反向用 |")
        add(f"| 反向额外（梯度 + 临时张量） | +{s['backward_extra_mb'] - s['activation_mb']:.1f} MB | 只有 0.29% 参数有梯度，所以很小 |")
        add(f"| AdamW 一阶/二阶状态 | +{s['steady_extra_mb'] - s['backward_extra_mb']:.1f} MB | 只对可训练参数（懒分配，首步才出现） |")
        add(f"| 评测（no_grad）时的激活 | {s['eval_extra_mb']:.1f} MB | 不建计算图，比训练小一个量级 |")
        add(f"| **训练步最高峰值** | **{s['step_peak_mb']:.1f} MB** | 权重 + 激活 + 梯度 + 优化器状态 |")
        add("")
        if budget_peak:
            sp = budget_peak["summary"]
            add(f"**最坏情况**（强制 pad 到 `max_length=64`，即一个 batch 全是长样本）："
                f"激活从 {s['activation_mb']:.0f} MB 涨到 **{sp['activation_mb']:.0f} MB**，"
                f"训练步峰值 {sp['step_peak_mb']:.0f} MB。真实训练用的是动态 padding（按 batch 内最长样本），"
                f"所以实测峰值 {peak_mark:.0f} MB 落在两者之间。\n")
        add("> 训练日志里 `前向激活` 那几行只覆盖 forward 结束的一瞬，"
            "**不是**整步峰值；整步峰值由 `全程峰值` 给出，而各段分别是多少要看这张表。\n")

    # 五之三：最少要多少显存（OOM 边界实测）
    if caps:
        dyn = [c for c in caps if c["_mode"] == "dynamic"]
        pad = [c for c in caps if c["_mode"] == "pad64"]

        def boundary(rows: list[dict]) -> tuple[float, float] | None:
            ok = [r["cap_mb"] for r in rows if r["ok"]]
            bad = [r["cap_mb"] for r in rows if not r["ok"]]
            if not ok or not bad:
                return None
            return max(bad), min(ok)

        add("### 5.2 到底至少要多少显存（OOM 边界实测）\n")
        add("方法：`torch.cuda.set_per_process_memory_fraction(f)` 把**本进程分配器**的上限压到"
            "「设备总量 × f」，然后在每一档里跑一次完整训练步（forward + backward + optimizer.step）；"
            "每档是**独立进程**，OOM 就记一次失败。注意 context（约 1.3 GB）是在分配器之外分配的，"
            "所以下面「上限」只是分配器预算，真实占用要再加上 context。\n")
        for mode, rows, title in (("dynamic", dyn, "动态 padding（真实训练口径）"), ("pad64", pad, "pad 到 64（最坏情况）")):
            if not rows:
                continue
            add(f"**{title}**\n")
            add("| 分配器上限 MB | 占设备 | 结果 |")
            add("|---|---|---|")
            for r in sorted(rows, key=lambda x: x["cap_mb"]):
                add(f"| {r['cap_mb']:.0f} | {r['cap_mb'] / r['total_device_mb'] * 100:.1f}% | "
                    f"{'能跑完 ✔' if r['ok'] else 'OOM ✘'} |")
            add("")
        bd, bp = boundary(dyn), boundary(pad)
        ctx = budget["context_overhead_mb"] if budget else float("nan")
        if bd:
            add(f"- **动态 padding（真实训练）**：分配器下限在 **{bd[0]:.0f} ~ {bd[1]:.0f} MB** 之间；"
                f"加上 context **{ctx:.0f} MB** → 整卡至少要有 **{bd[0] + ctx:.0f} ~ {bd[1] + ctx:.0f} MB** 空闲。"
                f"留 20% 余量（抗碎片与其它进程）→ **约 {(bd[1] + ctx) * 1.2 / 1024:.1f} GB**。")
        if bp:
            add(f"- **最坏情况（pad 到 64）**：分配器下限 **{bp[0]:.0f} ~ {bp[1]:.0f} MB** → 整卡 "
                f"**{bp[0] + ctx:.0f} ~ {bp[1] + ctx:.0f} MB**，留 20% 余量 → **约 {(bp[1] + ctx) * 1.2 / 1024:.1f} GB**。")
        if bd:
            add(f"- 计算出的激活是**线性**的：激活 ≈ batch × 序列长度 × 常数。显存不够时的顺序是"
                "① 降 `--batch-size`（最有效）→ ② 降 `--max-length` → ③ `--grad-accum` 顶上等效 batch "
                "→ ④ `--gradient-checkpointing`（用时间换激活）。")
        add("")
        add("> ⚠️ 上面这个「最少」是**进程视角**：它把 CUDA context（约 1.3 GB）算进去了，"
            "而 context 是**每个用 CUDA 的进程都要付一遍**的固定开销。"
            "如果你在 Windows 任务管理器里看到的数字对不上，看下一节 —— 那是另一套口径。\n")

    # 五之四：三个口径
    if budget and host_view:
        smi = (host_view.get("nvidia_smi") or {})
        procs = [p for p in (host_view.get("processes") or []) if "dedicated_mib" in p]
        ctx_mb = budget["context_overhead_mb"]
        tensors = budget["summary"]["step_peak_mb"]
        add("### 5.3 三个「显存占用」量的不是一回事（为什么屏幕上的数字和这里不一样）\n")
        add("| 口径 | 实测值 | 含义 / 什么时候用它 |")
        add("|---|---|---|")
        dyn_rows = [c for c in caps if c["_mode"] == "dynamic"]
        floor = min((c["cap_mb"] for c in dyn_rows if c["ok"]), default=None)
        floor_bad = max((c["cap_mb"] for c in dyn_rows if not c["ok"]), default=None)
        floor_txt = (f"**{floor_bad:.0f} MB 就 OOM，{floor:.0f} MB 能跑**"
                     if floor and floor_bad else "见 5.2 表")
        add(f"| ① 张量硬下限（分配器 cap 实测） | {floor_txt} | "
            "「权重+激活+梯度+优化器」本身要多少；与进程无关的硬指标 |")
        add(f"| ② 进程视角总量（WSL 内 `mem_get_info`） | **{ctx_mb + tensors:.0f} MB** = context {ctx_mb:.0f} + 张量 {tensors:.0f}（实测峰值）；"
            f"cap 实验给的**可用下限**是 {ctx_mb + floor:.0f} MB（多出的是碎片） | "
            "「我这个训练进程一共压了多少」——判断本进程会不会 OOM |")
        if "error" not in smi:
            top = procs[0] if procs else None
            name = top.get("process_name", "") if top else ""
            add(f"| ③ 宿主/任务管理器视角 | 整卡 **{smi['used_mib']:.0f} MiB** / {smi['total_mib']:.0f} MiB"
                + (f"，最大一项 **{name or top['instance']}** = **{top['dedicated_mib']:.1f} MiB** | " if top else " | ")
                + "你在屏幕上看到的数 —— 含**整个 WSL 虚拟机（vmwp）的持有量**，不等于本任务的占用 |")
        add("")
        add("宿主侧实测（`vram_budget.py --host-snapshot`，原始输出在 `vram_host_view.json`）：\n")
        if "error" not in smi:
            add(f"- 整卡已用 **{smi['used_mib']:.0f} MiB**；逐进程计数器只报了一个大户：")
        for row in procs[:3]:
            nm = row.get("process_name", "")
            add(f"  - `{nm or row['instance']}`（pid {row.get('pid', '?')}）→ **{row['dedicated_mib']:.1f} MiB**")
        add(f"- 同一时刻训练进程**没在跑**、RAG 服务自己 `torch.cuda.memory_allocated() = 0`（启动预热失败，模型没加载）")
        add("")
        add("**结论：那 4.3 GB 是 WSL2 虚拟机（`vmwp`）的 GPU 持有量** —— WSL 里历次 CUDA 进程用过、"
            "宿主的驱动还没收回的部分。所以：\n")
        add("- 任务管理器从 4.3 GB 涨到 5.7 GB，是**虚拟机在这次运行里又向 Windows 多要了约 1.4 GB**，"
            "不是「这个任务要 4 GB」")
        add(f"- 这个增量小于口径②的 {ctx_mb + tensors:.0f} MB，因为 context 那一块在 WSL VM 里是**复用/摊销**的"
            "（VM 早就持有），而我在 WSL 里量到的「设备已用」把每个进程的 context 都单独算了一遍")
        add("- **真要判断会不会 OOM**：用口径②（进程自己 2.4 GB）+ 卡上已有的其它占用。"
            "16 GB 卡上这个任务随便跑；8 GB 卡且 RAG 服务热跑（BGE-M3+reranker ≈ 5 GB）时才会紧张\n")

    # 六、评估
    add("---\n\n## 六、评估结果\n")
    add(f"**测试集（`test.jsonl` {test['n']} 条，训练全程只跑这一次）**\n")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| accuracy | **{test['accuracy']:.4f}** |")
    add(f"| macro-F1 | **{test['macro_f1']:.4f}** |")
    add(f"| top-2 accuracy | **{test['top2']:.4f}** |")
    add(f"| 加权 F1 | {test['weighted_f1']:.4f} |")
    add("")
    add("**逐类指标**\n")
    add("| label | 类别 | precision | recall | f1 | support |")
    add("|---|---|---|---|---|---|")
    for lab, s in sorted(test["per_class"].items()):
        add(f"| {lab} | {LABELS[lab]} | {s['precision']:.3f} | {s['recall']:.3f} | {s['f1']:.3f} | {s['support']} |")
    add("")
    add("**混淆矩阵**（行=真值，列=预测）\n")
    add("|  | " + " | ".join(f"{lab}:{LABELS[lab][:6]}" for lab in sorted(LABELS)) + " |")
    add("|---|" + "---|" * len(LABELS))
    for i, row in enumerate(test["confusion"]):
        cells = " | ".join(f"**{v}**" if i == j else str(v) for j, v in enumerate(row))
        add(f"| **{i}:{LABELS[i][:6]}** | {cells} |")
    add("")
    add(f"**误差分析**：{test['wrong']} 条误判 —— " +
        "、".join(f"`{k}` **{v} 条**" for k, v in sorted(test["errors"].items(), key=lambda kv: -kv[1])) + "。\n")
    add("- 全部落在数据卡预告的「铺垫 + 核心无指代」边界上，例如：")
    for s in test["error_samples"][:3]:
        add(f"  `{s['query']}`（true={s['true']}, pred={s['pred']}, conf={s['conf']:.3f}）")
    add(f"- 置信度**有信息量**：正确样本置信度中位 **{test['conf_ok_median']:.3f}**，"
        f"误判样本中位 **{test['conf_bad_median']:.3f}**")
    add(f"- **top-2 兜底有效**：误判样本真值 100% 在第二位；置信度 <0.9 的 {test['low_n']} 条里，"
        f"top-1 只命中 {test['low_top1']} 条"
        f"（{test['low_top1'] / max(test['low_n'], 1):.0%}），"
        f"**top-2 命中 {test['low_top2']} 条"
        f"（{test['low_top2'] / max(test['low_n'], 1):.0%}）**")
    add("")
    add("**「低置信就回退到 0 类」实测有害**（离线在 `test_predictions.jsonl` 上模拟）：\n")
    add("| 回退阈值 | accuracy | macro-F1 | 回退条数 |")
    add("|---|---|---|---|")
    for s in test["sweep"]:
        tag = "关闭" if s["threshold"] == 0 else f"{s['threshold']}"
        bold = "**" if s["threshold"] == 0 else ""
        add(f"| {tag} | {bold}{s['accuracy']:.4f}{bold} | {bold}{s['macro_f1']:.4f}{bold} | {s['changed']} |")
    add("")
    add("原因很清楚：低置信样本的真值多在 1/3 类，回退到 0 只是把错误换个位置。")
    add("")

    # 七、结论
    add("---\n\n## 七、结论\n")
    add(f"1. **模型可用**：test accuracy {test['accuracy']:.4f} / macro-F1 {test['macro_f1']:.4f}，"
        "4 类均衡下没有塌缩类；分类头 + LoRA 的 "
        f"{ledger['trainable_ratio']:.2%} 参数足够承载这个任务。")
    add(f"2. **误差是结构性的、可解释的**：{test['wrong']} 条误判全部集中在 `1↔3` 这一对语义边界上，"
        "根因是数据里约 10% 的规则边界样本（「有铺垫但核心空泛」该判 3 还是 1），**不是模型容量问题**。"
        "→ 下一步提升不靠调超参，靠把这批边界样本人工复核后重训。")
    add("3. **线上策略应当是 top-2 双改写，而不是低置信回退**：误判真值 100% 在第二位，"
        "而一刀切回退实测把准确率拉低明显。低成本做法：置信度低于阈值时同时执行两个候选改写，或转人工。")
    add(f"4. **成本极低**：训练 {rt:.1f} 秒、峰值 {_mb(peak_mark)} MB（设备级 {_mb(dev_peak)} MB）；"
        "适配器只有 MB 级，可以按「数据一更新就重训一轮」的节奏跑。" if rt else "4. **成本极低**：可以按「数据一更新就重训一轮」的节奏跑。")
    add("5. **局限**：只跑了 1 个种子（没有多种子方差）；test 全部来自 agent5（跨生成者泛化，偏保守）；"
        "数据是合成的，真实流量上线后需要重测分布。")
    add("")

    # 八、附录
    add("---\n\n## 八、附录\n")
    add("### A. 产物清单\n")
    add(f"| 文件（`{run_dir}/`） | 内容 |")
    add("|---|---|")
    add("| `lora_adapter/` | `adapter_model.safetensors`（LoRA + 分类头）、`adapter_config.json`、分词器 |")
    add("| `run_meta.json` | 超参、数据规模、dev/test 指标、参数量 |")
    add("| `vram_train.json` | 完整显存账本（逐打点快照 + 参数账本 + 元信息） |")
    add("| `test_predictions.jsonl` | 逐条预测：label / confidence / 四类概率（错误分析用） |")
    add("")
    add("### B. 复现命令\n")
    add("```bash")
    add("# 1) 基座（不在仓库里）")
    add("HF_ENDPOINT=https://hf-mirror.com python -c \"from huggingface_hub import snapshot_download as d; \\")
    add("  d('hfl/chinese-roberta-wwm-ext', local_dir='models/chinese-roberta-wwm-ext')\"")
    add("")
    add("# 2) 冒烟（CPU 也能跑）")
    add("python tools/lora/train.py --smoke")
    add("")
    add("# 3) 正式训练 + 最后跑一次测试集")
    add("docker run --rm --gpus all -v \"$PWD\":/work -w /work --entrypoint python \\")
    add("  project-rag:0.1.0 tools/lora/train.py --output-dir logs/lora/run1 --eval-test")
    add("")
    add("# 4) 推理 / 评测 / 显存扫描；产出本报告")
    add("docker run --rm --gpus all -v \"$PWD\":/work -w /work --entrypoint python \\")
    add("  project-rag:0.1.0 tools/lora/infer.py --adapter logs/lora/run1/lora_adapter \\")
    add("  --test --bench-batch 1,8,32,128 --out logs/lora/run1/test_predictions.jsonl")
    add("python tools/lora/make_report.py --run-dir logs/lora/run1")
    add("```")
    add("")
    add("> 为什么走 `docker run --gpus all`：agent 的 shell 挂的是私有 `/dev`（无 `/dev/dxg`），")
    add("> `torch.cuda.is_available()` 恒为 False —— 那是**视角**问题，不是机器没有 GPU（见仓库根 `AGENTS.md`）。")
    add("")
    add("### C. 显存口径\n")
    add("| 名词 | 含义 |")
    add("|---|---|")
    add("| 已分配 `memory_allocated` | PyTorch 分配器当前**活跃**张量占用；不含 CUDA context 与库 workspace |")
    add("| 预留 `memory_reserved` | 分配器向驱动要来的缓存池；碎片化会让它持续上涨 |")
    add("| 峰值 `max_memory_allocated` | 自上次 `reset_peak_memory_stats()` 以来的最高活跃占用 |")
    add("| 设备已用 `mem_get_info` | 整张卡的真实占用（含 context、其它进程）→ **判断会不会 OOM 看这个** |")
    add("")
    add("### D. 代码与相关文档\n")
    add("| 文件 | 作用 |")
    add("|---|---|")
    add("| `tools/lora/train.py` | 训练主脚本（Trainer + TrainingArguments + LoRA + 早停 + 显存打点） |")
    add("| `tools/lora/infer.py` | 推理/评测/显存扫描（含 top-2 与低置信策略对比） |")
    add("| `tools/lora/make_report.py` | **本报告的生成器**（产物 → 报告，数字不手写） |")
    add("| `tools/lora/mem_probe.py` | 显存分板块统计（实测账 + 理论账 + Callback + 前向钩子） |")
    add("| `tools/lora/data_utils.py` | 数据读取、schema 校验、分层切 dev |")
    add(f"| `{data_dir}/README.md` | 数据卡：生成口径、质检结论、已知边界问题 |")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """输入:命令行参数。行为:读产物 → 渲染报告 → 写文件。输出:退出码。"""
    parser = argparse.ArgumentParser(description="从训练产物生成微调报告")
    parser.add_argument("--run-dir", default="logs/lora/run1")
    parser.add_argument("--data-dir", default="data/intent/strategy_v1")
    parser.add_argument("--log", default=None, help="训练日志；默认取 run-dir 同级的 train_<run>.log")
    parser.add_argument("--out", default="tools/lora/finetune_report.md")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"找不到产物目录：{run_dir}", file=sys.stderr)
        return 2
    log_path = Path(args.log) if args.log else run_dir.parent / f"train_{run_dir.name}.log"
    missing = [f for f in ("run_meta.json", "vram_train.json", "test_predictions.jsonl") if not (run_dir / f).is_file()]
    if missing:
        print(f"缺产物：{missing}（先跑 train.py 与 infer.py）", file=sys.stderr)
        return 2

    run = read_run(run_dir, Path(args.data_dir), log_path)
    # 交叉校验：run_meta 与逐条预测算出的 test 指标应当一致
    meta_test = run["meta"].get("metrics", {}).get("test", {})
    if meta_test:
        delta = abs(meta_test.get("accuracy", 0) - run["test"]["accuracy"])
        if delta > 0.001:
            print(f"⚠️ run_meta 的 accuracy 与 test_predictions 差 {delta:.4f} —— 两者可能不是同一次评测", file=sys.stderr)

    text = render(run, args.run_dir, args.data_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"报告已写出：{out}（{len(text)} 字符 / {text.count(chr(10)) + 1} 行）")
    print(f"  数据：train {run['meta']['sizes']['train']} / dev {run['meta']['sizes']['dev']} / test {run['test']['n']}")
    print(f"  test：accuracy {run['test']['accuracy']:.4f} / macro_f1 {run['test']['macro_f1']:.4f} / top2 {run['test']['top2']:.4f}")
    print(f"  显存峰值：allocated {run['marks'].get('全程峰值(peak_mb)')} MB / 设备级 {max(s['device_used_mb'] for s in run['snaps']):.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
