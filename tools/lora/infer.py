#!/usr/bin/env python
"""推理与评测：加载 LoRA 适配器跑预测，并把**推理各板块的显存**量出来。

三件事：
  1. 预测：单条（``--query``）、一批（``--input file.jsonl``）、或测试集（默认 ``--test``）
  2. 评测：有标签就算 accuracy / macro-F1 / 混淆矩阵；并给出**低置信兜底**的效果对比
     （低于阈值就回退到「直接检索」，这是分类器上线时最常用的一道保险）
  3. 显存：加载基座 / 注入适配器 / 合并权重 三个阶段各打点，再按 batch size 扫一遍峰值曲线

用法：

    python tools/lora/infer.py --adapter logs/lora/run1/lora_adapter --test
    python tools/lora/infer.py --adapter logs/lora/run1/lora_adapter --query "AI和Java的课程大纲"
    python tools/lora/infer.py --adapter logs/lora/run1/lora_adapter --bench-batch 1,8,32,128
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_utils import LABELS, NUM_LABELS, load_jsonl  # noqa: E402
from mem_probe import MB, MemProbe, cuda_visible, ledger_table, param_ledger  # noqa: E402

DEFAULT_ADAPTER = "logs/lora/strategy/lora_adapter"


def build_argparser() -> argparse.ArgumentParser:
    """输入:无。行为:定义推理期参数。输出:ArgumentParser。"""
    parser = argparse.ArgumentParser(description="LoRA 策略分类器推理 + 显存统计")
    parser.add_argument("--adapter", default=DEFAULT_ADAPTER, help="train.py 产出的 lora_adapter 目录")
    parser.add_argument("--model-name-or-path", default=None, help="基座；默认读 adapter 目录里的记录")
    parser.add_argument("--query", default=None, help="只预测一条")
    parser.add_argument("--input", default=None, help="预测一个 jsonl（可以只有 query 字段）")
    parser.add_argument("--test", action="store_true", help="在 data-dir/test.jsonl 上跑并算指标")
    parser.add_argument("--data-dir", default="data/intent/strategy_v1")
    parser.add_argument("--test-file", default="test.jsonl")
    parser.add_argument("--out", default=None, help="预测结果写到哪里（jsonl）")
    parser.add_argument("--batch-size", type=int, default=64, help="推理 batch（序列短，64 很快）")
    parser.add_argument("--max-length", type=int, default=64)
    parser.add_argument("--merge", action="store_true", help="把 LoRA 合并进基座（导出独立模型；显存会多一份）")
    parser.add_argument(
        "--fallback-threshold",
        type=float,
        default=0.0,
        help="最大概率低于它就回退（0=不兜底）。⚠️ 实测：一刀切回退到 0 类会让准确率**变差**"
        "（0.9690 → 0.9460 @0.9），因为低置信样本的真值常在 1/3；更好的用法看 top-2",
    )
    parser.add_argument("--fallback-label", type=int, default=0, help="回退到哪个标签（默认 0=直接检索）")
    parser.add_argument("--bench-batch", default="", help="按这些 batch size 扫显存峰值，如 1,8,32,128")
    parser.add_argument("--no-probe", action="store_true", help="关掉显存打点")
    return parser


def _resolve_base(adapter_dir: Path, explicit: str | None) -> str:
    """输入:适配器目录与显式基座。行为:定位基座路径（优先参数，其次 run_meta，再退默认）。输出:路径。"""
    if explicit:
        return explicit
    meta_path = adapter_dir.parent / "run_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("model"):
            return meta["model"]
    return "models/chinese-roberta-wwm-ext"


@torch.no_grad()
def predict_texts(model, tokenizer, texts: list[str], batch_size: int, max_length: int, device: torch.device):  # noqa: ANN001, ANN201
    """输入:模型/分词器/文本。行为:分批前向，返回概率矩阵与耗时。输出:(probs, seconds)。"""
    model.eval()
    chunks: list[np.ndarray] = []
    started = time.perf_counter()
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        encoded = tokenizer(batch, truncation=True, max_length=max_length, padding=True, return_tensors="pt")
        encoded = {k: v.to(device) for k, v in encoded.items()}
        logits = model(**encoded).logits
        chunks.append(torch.softmax(logits.float(), dim=-1).cpu().numpy())
    if cuda_visible():
        torch.cuda.synchronize()
    return np.concatenate(chunks, axis=0), time.perf_counter() - started


def bench_batch_sizes(model, tokenizer, texts: list[str], sizes: list[int], max_length: int, device: torch.device) -> str:
    """输入:模型与待扫的 batch size。行为:逐档测峰值显存与吞吐。输出:文本表。

    每档前 ``reset_peak_memory_stats``，这样读到的峰值只属于这一档 —— 否则前面的档会污染后面。
    """
    lines = [f"  {'batch':>6s} {'峰值已分配MB':>14s} {'峰值预留MB':>13s} {'ms/条':>9s}"]
    if not cuda_visible():
        return "  ⚠️ 本进程看不到 CUDA，跳过 batch 显存扫描（在容器里跑才有数）"
    sample = (texts * ((max(sizes) // max(len(texts), 1)) + 1))[: max(sizes)]
    for size in sizes:
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        probs, seconds = predict_texts(model, tokenizer, sample[:size], size, max_length, device)
        peak = torch.cuda.max_memory_allocated() / MB
        reserved = torch.cuda.max_memory_reserved() / MB
        lines.append(f"  {size:>6d} {peak:>14.1f} {reserved:>13.1f} {seconds * 1000 / max(len(sample[:size]), 1):>9.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """输入:命令行参数。行为:推理/评测/显存扫描。输出:退出码。"""
    args = build_argparser().parse_args(argv)
    from peft import PeftModel
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    adapter_dir = Path(args.adapter)
    if not adapter_dir.is_dir():
        print(f"找不到适配器目录：{adapter_dir}（先跑 train.py）", file=sys.stderr)
        return 2
    base = _resolve_base(adapter_dir, args.model_name_or_path)
    device = torch.device("cuda" if cuda_visible() else "cpu")

    probe = MemProbe("推理显存账本")
    probe.snap("起始（import 完成）")
    tokenizer = AutoTokenizer.from_pretrained(str(adapter_dir))
    base_model = AutoModelForSequenceClassification.from_pretrained(
        base, num_labels=NUM_LABELS, problem_type="single_label_classification"
    )
    probe.snap("基座权重（fp32）")
    model = PeftModel.from_pretrained(base_model, str(adapter_dir))
    probe.snap("注入 LoRA 适配器")
    if args.merge:
        model = model.merge_and_unload()
        probe.snap("合并 LoRA 进基座（多一份权重）")
    model.to(device)
    probe.snap(f"搬到 {device.type}")
    ledger = param_ledger(model)
    print(ledger_table(ledger, mode="infer"))

    # ---- 取输入 -----------------------------------------------------------
    texts: list[str] = []
    labels: list[int] | None = None
    if args.query:
        texts = [args.query]
    elif args.input:
        # 支持两种：带 label 的（顺便算指标）与只有 query 的（纯预测）
        rows: list[dict] = []
        with open(args.input, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{args.input}:{lineno} 不是合法 JSON：{exc}") from exc
                if "query" not in obj:
                    raise SystemExit(f"{args.input}:{lineno} 缺 query 字段")
                rows.append(obj)
        texts = [r["query"] for r in rows]
        if rows and all("label" in r for r in rows):
            labels = [r["label"] for r in rows]
    elif args.test:
        rows = load_jsonl(Path(args.data_dir) / args.test_file)
        texts = [r["query"] for r in rows]
        labels = [r["label"] for r in rows]
    else:
        print("要么给 --query / --input，要么给 --test", file=sys.stderr)
        return 2

    probs, seconds = predict_texts(model, tokenizer, texts, args.batch_size, args.max_length, device)
    probe.snap(f"推理 {len(texts)} 条（batch={args.batch_size}）")
    preds = probs.argmax(axis=-1)
    conf = probs.max(axis=-1)
    probe.note("吞吐(条/秒)", round(len(texts) / max(seconds, 1e-9), 1))

    overridden = 0
    if args.fallback_threshold > 0:
        adjusted = preds.copy()
        for i, p in enumerate(preds):
            if conf[i] < args.fallback_threshold:
                adjusted[i] = args.fallback_label
        overridden = int((adjusted != preds).sum())
        preds = adjusted

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for i, text in enumerate(texts):
                handle.write(
                    json.dumps(
                        {
                            "query": text,
                            "label": int(preds[i]),
                            "label_name": LABELS[int(preds[i])],
                            "confidence": round(float(conf[i]), 4),
                            "probs": [round(float(p), 4) for p in probs[i]],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"预测写入 {args.out}")

    if not args.query:
        print(f"\n  {len(texts)} 条，{seconds:.2f}s，{len(texts) / max(seconds, 1e-9):.1f} 条/秒"
              + (f"，低置信回退 {overridden} 条（阈值 {args.fallback_threshold}）" if args.fallback_threshold else ""))

    if labels is not None:
        from sklearn.metrics import accuracy_score, f1_score

        acc = accuracy_score(labels, preds)
        macro = f1_score(labels, preds, average="macro")
        order = np.argsort(-probs, axis=1)
        top2 = float(np.mean([t in order[i][:2] for i, t in enumerate(labels)]))
        print(f"  accuracy={acc:.4f}  macro_f1={macro:.4f}  top2={top2:.4f}")
        print("  ↑ top2 才是分类器的真实上限：实测误判样本的真值**全部**在第二位，")
        print("    所以低置信时正确的工程做法是「两个候选改写都做」或转人工，而不是一刀切回退到某一类。")
        from train import confusion_text  # 复用同一套混淆矩阵渲染

        print(confusion_text(list(labels), list(preds)))

    if args.bench_batch:
        sizes = [int(s) for s in args.bench_batch.split(",") if s.strip()]
        print("\n== 推理显存随 batch 的变化 ==")
        print(bench_batch_sizes(model, tokenizer, texts, sizes, args.max_length, device))

    if not args.no_probe:
        print(probe.table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
