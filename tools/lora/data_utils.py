"""数据读取与切分：只吃 ``{"query": "...", "label": 0..3}`` 的 jsonl。

数据来自 ``data/intent/strategy_v1/``（4 类各 1250，train 4000 / test 1000），
口径与质检结论见该目录的 ``README.md``。本模块只做四件事：读、校验、分层切 dev、分词。
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

LABELS: dict[int, str] = {0: "direct", 1: "hypothetical", 2: "subquery", 3: "denoise"}
NUM_LABELS = len(LABELS)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """输入:jsonl 路径。行为:逐行解析并校验 schema。输出:[{"query","label"}]。

    校验失败会**直接抛错**而不是跳过 —— 训练数据静默丢样本是最难查的一类问题。
    """
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
            if set(obj) != {"query", "label"}:
                raise ValueError(f"{path}:{lineno} 键必须是 query+label，实际 {sorted(obj)}")
            if not isinstance(obj["query"], str) or not obj["query"].strip():
                raise ValueError(f"{path}:{lineno} query 为空")
            if not isinstance(obj["label"], int) or obj["label"] not in LABELS:
                raise ValueError(f"{path}:{lineno} label 必须是 {sorted(LABELS)}，实际 {obj['label']!r}")
            rows.append(obj)
    if not rows:
        raise ValueError(f"{path} 一条数据都没有")
    return rows


def describe(rows: list[dict[str, Any]], name: str = "") -> str:
    """输入:样本与名字。行为:统计类别/长度分布。输出:多行文本。"""
    counts = Counter(r["label"] for r in rows)
    lengths = sorted(len(r["query"]) for r in rows)
    dist = Counter("≤15" if n <= 15 else "16-30" if n <= 30 else "31+" for n in lengths)
    head = f"{name}：" if name else ""
    return (
        f"{head}{len(rows)} 条 | 类别 "
        + " ".join(f"{lab}:{counts.get(lab, 0)}" for lab in sorted(LABELS))
        + f" | 长度 {lengths[0]}/{lengths[len(lengths) // 2]}/{lengths[-1]}"
        + f" | 长度分布 ≤15:{dist['≤15']} 16-30:{dist['16-30']} 31+:{dist['31+']}"
    )


def stratified_dev_split(
    rows: list[dict[str, Any]], dev_size: int, seed: int = 42
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """输入:训练样本、dev 目标条数、随机种子。行为:**按类别分层**切出 dev。输出:(train, dev)。

    为什么必须分层：4 类均衡是本数据集的核心约束，随机切会让 dev 的类别比例漂移，
    早停依据（macro-F1）就会跟着抖。
    """
    if dev_size <= 0:
        return rows, []
    by_label: dict[int, list[dict[str, Any]]] = {lab: [] for lab in LABELS}
    for row in rows:
        by_label[row["label"]].append(row)
    per_class = dev_size // NUM_LABELS
    remainder = dev_size - per_class * NUM_LABELS
    rng = random.Random(seed)
    dev: list[dict[str, Any]] = []
    train: list[dict[str, Any]] = []
    for idx, lab in enumerate(sorted(LABELS)):
        bucket = by_label[lab][:]
        rng.shuffle(bucket)
        take = per_class + (1 if idx < remainder else 0)
        if take > len(bucket):
            raise ValueError(f"label {lab} 只有 {len(bucket)} 条，切不出 {take} 条 dev")
        dev.extend(bucket[:take])
        train.extend(bucket[take:])
    rng.shuffle(train)
    rng.shuffle(dev)
    return train, dev


def build_tokenize_fn(tokenizer, max_length: int):  # noqa: ANN001, ANN201
    """输入:分词器与最大长度。行为:造一个 batched map 用的函数。输出:callable。"""

    def tokenize(batch: dict[str, list]) -> dict[str, list]:
        return tokenizer(batch["query"], truncation=True, max_length=max_length)

    return tokenize


def to_datasets(tokenizer, train_rows, dev_rows, max_length: int):  # noqa: ANN001, ANN201
    """输入:分词器、train/dev 样本。行为:转成 datasets.DatasetDict（已去掉 query 列）。输出:DatasetDict。"""
    from datasets import Dataset, DatasetDict

    fn = build_tokenize_fn(tokenizer, max_length)

    def convert(rows: list[dict[str, Any]]) -> Dataset:
        ds = Dataset.from_list([{"query": r["query"], "label": r["label"]} for r in rows])
        return ds.map(fn, batched=True, remove_columns=["query"], desc="分词")

    splits = {"train": convert(train_rows)}
    if dev_rows:
        splits["validation"] = convert(dev_rows)
    return DatasetDict(splits)
