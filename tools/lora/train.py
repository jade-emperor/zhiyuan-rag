#!/usr/bin/env python
"""LoRA 微调：查询改写策略选择器（chinese-roberta-wwm-ext + 4 分类）。

对齐当前主流做法，每个选择都有理由（都在 ``--help`` 里）：

  * **peft.LoraConfig**：``r=8 / lora_alpha=16 / lora_dropout=0.1 / target_modules=["query","value"]``
    —— BERT 系分类任务最常见的秩与注入点；``modules_to_save=["classifier"]`` 是关键，
    分类头是随机初始化的，不训练它等于白训。
  * **Trainer + TrainingArguments**：不再手写训练循环，早停/最优权重回滚/断点续训都由它管。
  * **AdamW**（``optim="adamw_torch"``）：``betas=(0.9, 0.999)``、``eps=1e-8``、``weight_decay=0.01``
    （只作用在非 bias/LayerNorm 上，这是 HF 的默认口径）、``max_grad_norm=1.0``。
  * **余弦退火 + warmup**：``lr_scheduler_type="cosine"``、``warmup_ratio=0.1``、``lr=2e-4``
    （LoRA 的可训练参数少，学习率比全参微调高一个量级是常态）。
  * **早停**：``EarlyStoppingCallback(patience=2)``，盯 ``macro_f1``（类别均衡，macro 才有意义）。
  * **精度**：自动选 bf16 / fp16 / fp32（bf16 不需要 GradScaler，数值更稳，Ampere 以上首选）。
  * **显存**：全程分板块打点（``mem_probe.py``），训练结束打印实测账 + 理论账。

用法::

    # 冒烟（1 分钟，验证链路）
    python tools/lora/train.py --smoke

    # 正式训练（GPU 上 bs=32/len=64 大约几分钟）
    python tools/lora/train.py --output-dir logs/lora/run1

    # 想在容器里用 GPU（agent 沙箱看不到 GPU，见 AGENTS.md）
    docker run --rm --gpus all -v "$PWD":/work -w /work --entrypoint python \\
      project-rag:0.1.0 tools/lora/train.py --output-dir logs/lora/run1
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

import numpy as np
import torch

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_utils import LABELS, NUM_LABELS, describe, load_jsonl, stratified_dev_split, to_datasets  # noqa: E402
from mem_probe import (  # noqa: E402
    ActivationHook,
    MemProbe,
    MemProbeCallback,
    activation_formula,
    cuda_visible,
    ledger_table,
    param_ledger,
    save_report,
)

DEFAULT_DATA_DIR = "data/intent/strategy_v1"
DEFAULT_MODEL = "models/chinese-roberta-wwm-ext"


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    """输入:无。行为:定义全部超参（默认值即推荐值）。输出:ArgumentParser。"""
    parser = argparse.ArgumentParser(description="LoRA 微调查询改写策略分类器")
    data = parser.add_argument_group("数据")
    data.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="放 train.jsonl / test.jsonl 的目录")
    data.add_argument("--train-file", default="train.jsonl", help="训练集文件名")
    data.add_argument("--test-file", default="test.jsonl", help="测试集文件名（只有 --eval-test 时会读）")
    data.add_argument("--dev-size", type=int, default=400, help="从训练集按类别分层切出的 dev 条数（0=不切）")
    data.add_argument("--max-length", type=int, default=64, help="截断长度；本数据集最长 53 字，64 足够")
    data.add_argument("--limit-train", type=int, default=0, help="只取前 N 条训练（调试用，0=全部）")
    data.add_argument("--limit-eval", type=int, default=0, help="只取前 N 条评测（调试用，0=全部）")

    model = parser.add_argument_group("模型与 LoRA")
    model.add_argument("--model-name-or-path", default=DEFAULT_MODEL, help="基座模型路径或 HF id")
    model.add_argument("--lora-r", type=int, default=8, help="LoRA 秩（BERT 分类常用 4~16）")
    model.add_argument("--lora-alpha", type=int, default=16, help="LoRA alpha（主流取 2×r）")
    model.add_argument("--lora-dropout", type=float, default=0.1, help="LoRA dropout（正则化）")
    model.add_argument("--target-modules", default="query,value", help="注入模块，逗号分隔；BERT 常用 query,value")
    model.add_argument("--no-modules-to-save", action="store_true", help="不显式保存分类头（默认保存）")

    train = parser.add_argument_group("训练（主流默认值）")
    train.add_argument("--output-dir", default="logs/lora/strategy", help="产物目录")
    train.add_argument("--epochs", type=float, default=5.0, help="训练轮数（配早停，宁多勿少）")
    train.add_argument("--batch-size", type=int, default=32, help="单卡 batch（序列短，32 很轻松）")
    train.add_argument("--grad-accum", type=int, default=1, help="梯度累积步数（等效放大 batch）")
    train.add_argument("--lr", type=float, default=2e-4, help="学习率（LoRA 常用 1e-4~3e-4）")
    train.add_argument("--weight-decay", type=float, default=0.01, help="AdamW 权重衰减（正则化）")
    train.add_argument("--adam-beta1", type=float, default=0.9)
    train.add_argument("--adam-beta2", type=float, default=0.999)
    train.add_argument("--adam-epsilon", type=float, default=1e-8)
    train.add_argument("--max-grad-norm", type=float, default=1.0, help="梯度裁剪（防炸）")
    train.add_argument("--scheduler", default="cosine", help="学习率调度：cosine / linear / constant_with_warmup")
    train.add_argument("--warmup-ratio", type=float, default=0.1, help="warmup 占总步数比例")
    train.add_argument("--label-smoothing", type=float, default=0.0, help="标签平滑（想抗噪可设 0.05）")
    train.add_argument("--early-stopping-patience", type=int, default=2, help="连续 N 轮 macro_f1 不涨就停（0=关）")
    train.add_argument("--early-stopping-threshold", type=float, default=0.001, help="算作「有提升」的最小增量")
    train.add_argument("--precision", choices=["auto", "bf16", "fp16", "fp32"], default="auto", help="混合精度")
    train.add_argument("--gradient-checkpointing", action="store_true", help="用时间换显存（本模型不必要）")
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--num-workers", type=int, default=2, help="DataLoader 进程数")
    train.add_argument("--eval-test", action="store_true", help="训练结束后在 test.jsonl 上跑一次（只跑一次，别用它调参）")

    probe = parser.add_argument_group("显存统计")
    probe.add_argument("--no-probe", action="store_true", help="关掉显存打点（会略微影响速度）")
    probe.add_argument("--probe-log-every", type=int, default=20, help="训练中每 N 步打一次点")
    probe.add_argument("--probe-forward-limit", type=int, default=20, help="前向激活最多采多少次（钩子自动摘除）")

    parser.add_argument("--smoke", action="store_true", help="冒烟：小样本 + 1 轮 + 短序列，用来验证链路")
    return parser


def _supported_kwargs(cls: type, kwargs: dict) -> dict:
    """输入:类与候选 kwargs。行为:丢掉当前版本不支持的键。输出:过滤后的 kwargs。

    transformers 的 TrainingArguments 在小版本间会改名/删除参数（如 evaluation_strategy → eval_strategy），
    直接传会 TypeError。这里按签名过滤，让同一份脚本跨版本可用。
    """
    params = inspect.signature(cls.__init__).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    dropped = sorted(set(kwargs) - set(params))
    if dropped:
        print(f"  （当前 transformers 不支持这些参数，已忽略：{dropped}）")
    return {k: v for k, v in kwargs.items() if k in params}


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def build_compute_metrics():
    """输入:无。行为:造 compute_metrics（accuracy / macro-F1 / 每类 F1）。输出:callable。"""
    from sklearn.metrics import accuracy_score, f1_score

    def compute_metrics(eval_pred) -> dict[str, float]:  # noqa: ANN001
        logits, labels = eval_pred
        preds = np.argmax(logits, axis=-1)
        out = {
            "accuracy": float(accuracy_score(labels, preds)),
            "macro_f1": float(f1_score(labels, preds, average="macro")),
        }
        per_class = f1_score(labels, preds, average=None, labels=sorted(LABELS))
        for lab, score in zip(sorted(LABELS), per_class, strict=False):
            out[f"f1_{lab}_{LABELS[lab]}"] = float(score)
        return out

    return compute_metrics


def confusion_text(labels: list[int], preds: list[int]) -> str:
    """输入:真值/预测。行为:渲染混淆矩阵（行=真值，列=预测）。输出:多行文本。"""
    from sklearn.metrics import confusion_matrix

    matrix = confusion_matrix(labels, preds, labels=sorted(LABELS))
    names = [f"{lab}:{LABELS[lab][:6]}" for lab in sorted(LABELS)]
    width = max(len(n) for n in names) + 1
    lines = ["  混淆矩阵（行=真值，列=预测）：", " " * (width + 2) + "".join(f"{n:>14s}" for n in names)]
    for i, row in enumerate(matrix):
        lines.append(f"  {names[i]:<{width}s}" + "".join(f"{int(v):>14d}" for v in row))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """输入:命令行参数。行为:跑完一整套 LoRA 微调并打印显存账本。输出:退出码。"""
    args = build_argparser().parse_args(argv)
    if args.smoke:
        args.limit_train = args.limit_train or 200
        args.limit_eval = args.limit_eval or 80
        args.epochs = 1.0
        args.max_length = 32
        args.batch_size = 8
        args.dev_size = min(args.dev_size, 80) if args.dev_size else 0
        args.probe_log_every = 5
        args.early_stopping_patience = 0
        print("== 冒烟模式：小样本 / 1 轮 / 短序列 ==")

    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
        DataCollatorWithPadding,
        EarlyStoppingCallback,
        Trainer,
        TrainingArguments,
        set_seed,
    )
    from peft import LoraConfig, TaskType, get_peft_model

    set_seed(args.seed)
    if cuda_visible():
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.set_float32_matmul_precision("high")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    probe = MemProbe("训练显存账本")
    probe.snap("起始（import 完成）")

    # ---- 数据 -------------------------------------------------------------
    data_dir = Path(args.data_dir)
    train_rows = load_jsonl(data_dir / args.train_file)
    print(describe(train_rows, "训练集"))
    if args.limit_train:
        train_rows = train_rows[: args.limit_train]
    train_rows, dev_rows = stratified_dev_split(train_rows, args.dev_size, seed=args.seed)
    print(describe(train_rows, "训练用"))
    if dev_rows:
        print(describe(dev_rows, "dev（分层切出，与训练互斥）"))

    # ---- 模型 -------------------------------------------------------------
    device = torch.device("cuda" if cuda_visible() else "cpu")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path, num_labels=NUM_LABELS, problem_type="single_label_classification"
    )
    model.config.id2label = dict(LABELS)
    model.config.label2id = {v: k for k, v in LABELS.items()}
    probe.snap("基座权重（刚载入，通常还在 CPU）")
    model.to(device)
    probe.snap(f"基座权重上卡（{device.type}）")

    target_modules = [m.strip() for m in args.target_modules.split(",") if m.strip()]
    lora_kwargs = {
        "task_type": TaskType.SEQ_CLS,
        "r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "lora_dropout": args.lora_dropout,
        "target_modules": target_modules,
        "bias": "none",
    }
    if not args.no_modules_to_save:
        lora_kwargs["modules_to_save"] = ["classifier"]
    lora_config = LoraConfig(**_supported_kwargs(LoraConfig, lora_kwargs))
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    probe.snap("注入 LoRA 适配器 + 分类头")

    ledger = param_ledger(model)
    print(ledger_table(ledger))
    print(activation_formula(args.batch_size, args.max_length, model.config.hidden_size, model.config.num_hidden_layers))

    datasets = to_datasets(tokenizer, train_rows, dev_rows, args.max_length)
    collator = DataCollatorWithPadding(tokenizer=tokenizer)

    # ---- 训练参数 ---------------------------------------------------------
    if args.precision == "auto":
        precision = "bf16" if (cuda_visible() and torch.cuda.is_bf16_supported()) else ("fp16" if cuda_visible() else "fp32")
    else:
        precision = args.precision
    if precision == "fp16" and not cuda_visible():
        precision = "fp32"

    ta_kwargs = {
        "output_dir": str(out_dir / "checkpoints"),
        "per_device_train_batch_size": args.batch_size,
        "per_device_eval_batch_size": args.batch_size * 2,
        "gradient_accumulation_steps": args.grad_accum,
        "learning_rate": args.lr,
        "num_train_epochs": args.epochs,
        "weight_decay": args.weight_decay,
        "optim": "adamw_torch",
        "adam_beta1": args.adam_beta1,
        "adam_beta2": args.adam_beta2,
        "adam_epsilon": args.adam_epsilon,
        "max_grad_norm": args.max_grad_norm,
        "lr_scheduler_type": args.scheduler,
        "warmup_ratio": args.warmup_ratio,
        "label_smoothing_factor": args.label_smoothing,
        "bf16": precision == "bf16",
        "fp16": precision == "fp16",
        "tf32": cuda_visible(),
        "gradient_checkpointing": args.gradient_checkpointing,
        "logging_steps": max(1, args.probe_log_every // 4),
        "eval_strategy": "epoch" if dev_rows else "no",
        "save_strategy": "epoch" if dev_rows else "no",
        "save_total_limit": 2,
        "load_best_model_at_end": bool(dev_rows),
        "metric_for_best_model": "macro_f1",
        "greater_is_better": True,
        "dataloader_num_workers": args.num_workers,
        "dataloader_pin_memory": cuda_visible(),
        "report_to": [],
        "seed": args.seed,
        "data_seed": args.seed,
        "run_name": out_dir.name,
    }
    training_args = TrainingArguments(**_supported_kwargs(TrainingArguments, ta_kwargs))

    callbacks = []
    mem_callback = None
    if not args.no_probe:
        mem_callback = MemProbeCallback(probe, log_every=args.probe_log_every)
        callbacks.append(mem_callback)
    if dev_rows and args.early_stopping_patience > 0:
        callbacks.append(
            EarlyStoppingCallback(
                early_stopping_patience=args.early_stopping_patience,
                early_stopping_threshold=args.early_stopping_threshold,
            )
        )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=datasets["train"],
        eval_dataset=datasets.get("validation"),
        data_collator=collator,
        compute_metrics=build_compute_metrics(),
        callbacks=callbacks,
        processing_class=tokenizer,
    )

    activation_hook = None
    if not args.no_probe:
        activation_hook = ActivationHook(probe, every=1, limit=args.probe_forward_limit)
        activation_hook.attach(model)

    print(f"\n== 开始训练（精度 {precision}，设备 {device.type}）==")
    trainer.train()
    if activation_hook is not None:
        activation_hook.detach()
    # "训练结束"那一点由 MemProbeCallback.on_train_end 打，这里不重复打

    # 派生一行：前向峰值 − 上卡权重 ≈ 激活量级（含 autograd 图与临时张量）
    base_snap = next((s for s in probe.snapshots if s.label.startswith("基座权重上卡")), None)
    fwd_peaks = [s.allocated_mb for s in probe.snapshots if s.label.startswith("前向激活")]
    if base_snap and fwd_peaks and fwd_peaks[0] == fwd_peaks[0]:
        probe.note("派生·激活量级(前向峰值−上卡权重_MB)", round(max(fwd_peaks) - base_snap.allocated_mb, 1))

    # ---- 评测与落盘 -------------------------------------------------------
    dev_metrics = trainer.evaluate() if dev_rows else {}
    if dev_rows:
        pred = trainer.predict(datasets["validation"])
        print(confusion_text(list(pred.label_ids), list(np.argmax(pred.predictions, axis=-1))))

    test_metrics = {}
    if args.eval_test:
        test_rows = load_jsonl(data_dir / args.test_file)
        if args.limit_eval:
            test_rows = test_rows[: args.limit_eval]
        test_ds = to_datasets(tokenizer, test_rows, [], args.max_length)["train"]
        pred = trainer.predict(test_ds)
        preds = np.argmax(pred.predictions, axis=-1)
        from sklearn.metrics import accuracy_score, f1_score

        test_metrics = {
            "accuracy": float(accuracy_score(pred.label_ids, preds)),
            "macro_f1": float(f1_score(pred.label_ids, preds, average="macro")),
            "confusion": confusion_text(list(pred.label_ids), list(preds)),
        }
        print(f"\n== 测试集（只跑一次，别用它调参）==")
        print(f"  accuracy={test_metrics['accuracy']:.4f}  macro_f1={test_metrics['macro_f1']:.4f}")
        print(test_metrics["confusion"])

    trainer.save_model(str(out_dir / "lora_adapter"))
    tokenizer.save_pretrained(str(out_dir / "lora_adapter"))
    save_report(
        probe,
        ledger,
        str(out_dir / "vram_train.json"),
        extra={"dev_metrics": dev_metrics, "test_metrics": {k: v for k, v in test_metrics.items() if k != "confusion"}},
    )
    meta = {
        "model": args.model_name_or_path,
        "data_dir": str(data_dir),
        "labels": LABELS,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout, "targets": target_modules},
        "train": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "grad_accum": args.grad_accum,
            "lr": args.lr,
            "scheduler": args.scheduler,
            "warmup_ratio": args.warmup_ratio,
            "weight_decay": args.weight_decay,
            "max_grad_norm": args.max_grad_norm,
            "precision": precision,
            "max_length": args.max_length,
            "seed": args.seed,
        },
        "sizes": {"train": len(train_rows), "dev": len(dev_rows)},
        "metrics": {"dev": dev_metrics, "test": {k: v for k, v in test_metrics.items() if k != "confusion"}},
        "params": {
            "total": ledger["total_params"],
            "trainable": ledger["trainable_params"],
            "trainable_ratio": ledger["trainable_ratio"],
        },
    }
    with open(out_dir / "run_meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)

    print(probe.table())
    print(f"\n产物：{out_dir}/lora_adapter（LoRA 权重 + 分类头 + 分词器）、run_meta.json、vram_train.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
