# tools/lora —— 查询改写策略分类器的 LoRA 微调

给 `data/intent/strategy_v1/`（4 类：0 直接检索 / 1 假设答案 / 2 子查询分解 / 3 去噪改写）做微调与推理。
基座 `hfl/chinese-roberta-wwm-ext`，只训 LoRA 适配器 + 分类头。

| 文件 | 作用 |
|---|---|
| `train.py` | 微调主脚本：Trainer + TrainingArguments + AdamW + 余弦退火 + 早停 + 显存打点 |
| `infer.py` | 推理/评测：加载适配器、算指标与混淆矩阵、top-2 分析、按 batch 扫显存 |
| `make_report.py` | **从产物生成微调报告**（`finetune_report.md`）—— 报告里的数字不手写 |
| `finetune_report.md` | 微调报告：摘要 / 数据 / LoRA 配置 / 超参设计理由 / 显存分板块 / 评估 / 结论 / 附录 |
| `vram_budget.py` | **显存预算实验**：逐阶段分解（激活/梯度/优化器）+ 用分配器上限实测 OOM 边界 |
| `mem_probe.py` | 显存分板块统计：实测打点 + 参数折算的理论账 + TrainerCallback + 前向激活钩子 |
| `data_utils.py` | 读 jsonl、schema 校验、按类别分层切 dev、分词 |
| `requirements.txt` | 依赖清单（本仓库 `.venv` 里已全部具备） |

> **权威读数在 `finetune_report.md`**（由 `make_report.py` 读 `logs/lora/run1/` 的产物生成，
> 所以永远和产物一致）。本文件只讲"怎么用"和"为什么这么设计"，不重复贴数字。

## 三步

```bash
# 0) 依赖（若 .venv 已装好，这步可跳过）
uv pip install -r tools/lora/requirements.txt

# 1) 冒烟：200 条 / 1 轮 / 序列 32，先确认链路通
python tools/lora/train.py --smoke

# 2) 正式训练（4000 训练 / 400 分层 dev；实测 22 秒跑完 5 轮）
python tools/lora/train.py --output-dir logs/lora/run1

# 3) 推理与评测（test.jsonl 只跑一次，别用它调参）
python tools/lora/infer.py --adapter logs/lora/run1/lora_adapter --test --bench-batch 1,8,32,128
```

基座模型不在仓库里（`models/` 是 gitignore），首次需要下：

```bash
HF_ENDPOINT=https://hf-mirror.com python -c "from huggingface_hub import snapshot_download as d; \
  d('hfl/chinese-roberta-wwm-ext', local_dir='models/chinese-roberta-wwm-ext')"
```

**要在 GPU 上跑**（agent 沙箱的 `/dev` 是私有的，`torch.cuda.is_available()` 恒为 False，
但机器确实有 GPU —— 见仓库根 `AGENTS.md`）：

```bash
docker run --rm --gpus all -v "$PWD":/work -w /work --entrypoint python \
  project-rag:0.1.0 tools/lora/train.py --output-dir logs/lora/run1
```

## 超参：取什么值、为什么

| 参数 | 默认 | 理由 |
|---|---|---|
| `lora_r` | **8** | BERT 系分类任务的常见秩；4~16 之间收益差异很小，8 是性价比位 |
| `lora_alpha` | **16** | 主流取 `2×r`；缩放系数 `alpha/r=2` 是社区默认口径 |
| `lora_dropout` | **0.1** | 正则化，小数据集上防过拟合（5000 条属于小） |
| `target_modules` | **query,value** | BERT 注意力里最常见的注入点；全注入收益不明显、参数更多 |
| `modules_to_save` | **classifier** | 分类头随机初始化，不训练它等于白训（最容易踩的坑） |
| `optim` | **adamw_torch** | HF 原生 AdamW；`betas=(0.9,0.999)`、`eps=1e-8` 是 Transformer 常规 |
| `weight_decay` | **0.01** | 只作用在非 bias/LayerNorm 参数上（HF 默认口径），常规正则 |
| `max_grad_norm` | **1.0** | 梯度裁剪，防偶发炸梯度 |
| `lr` | **2e-4** | LoRA 可训练参数少，学习率比全参微调（2e-5）高一个量级是常态；范围 1e-4~3e-4 |
| `lr_scheduler_type` | **cosine** | 余弦退火 + 10% warmup 是微调里最稳的组合（日志里能看到 lr 从 2e-4 余弦降到 ~1e-9） |
| `num_train_epochs` | **5** | 配早停；实测 5 轮都还在涨，macro_f1 到 0.963 |
| `EarlyStoppingCallback` | **patience=2** | 盯 `macro_f1`（类别均衡，macro 才有意义），`load_best_model_at_end` 自动回滚最优 |
| `per_device_train_batch_size` | **32** | 序列最长 53 字，32 在 16G 卡上毫无压力 |
| `max_length` | **64** | 数据最长 53 字，64 足够且最省算力 |
| `precision` | **auto** | 有 bf16 就用 bf16（不需要 GradScaler、数值更稳），否则 fp16，再否则 fp32 |
| `--label-smoothing` | 0.0 | 数据是干净的均衡集，默认不开；若还担心残余标注噪声可设 0.05 |

## 实测读数

**全部数字见 [`finetune_report.md`](finetune_report.md)**（含逐轮 dev、逐类指标、混淆矩阵、显存分板块）。
重新生成报告：

```bash
python tools/lora/make_report.py --run-dir logs/lora/run1
```

一句话版（RTX 5080 Laptop，容器内跑）：test accuracy / macro-F1 = **0.9700 / 0.9700**，top-2 = **1.0000**，
误差 30 条全在 `3→1`(27)、`1→0`(2)、`0→1`(1)；训练峰值 allocated **1086.5 MB**、设备级 **2505.6 MB**。

## 显存报告怎么读

**实测账**（`MemProbe`，训练时逐板块打点）：

| 板块 | 含义（具体数值见报告第五节） |
|---|---|
| 起始 | CUDA context + 库句柄 + 其它进程；**`allocated` 不含这部分，所以必须看「设备已用」列** |
| 基座权重上卡 | 102.27M 参数 × 4 B ≈ 391 MB（磁盘上 `pytorch_model.bin` 411 MB） |
| 注入 LoRA + 分类头 | 只多 1 MB 级（294,912 + 3,076 个可训练参数） |
| AdamW 状态 | **懒分配**：`on_train_begin` 时还是 0，第一步之后才出现 |
| 前向激活（钩子打点） | 激活最占显存的一刻，随 batch×seq 线性增长 |
| 训练稳态 / 全程峰值 | 稳态 = 权重 + 梯度 + 优化器 + 库 workspace；峰值看 `max_memory_allocated` |

**理论账**（`param_ledger`）：按板块汇总参数量，权重按 dtype、梯度 4 B/可训练参数、
AdamW 8 B/可训练参数折算 —— 所以**全参微调 ≈ 16 B/参数，LoRA 只有适配器+分类头走这 16 B**。

**推理侧**（`--bench-batch`，每档前 `reset_peak_memory_stats()`，所以峰值只属于那一档）：

| batch | 峰值已分配 MB | 峰值预留 MB | ms/条 |
|---|---|---|---|
| 1 | 424.4 | 478.0 | 23.59 |
| 8 | 431.9 | 484.0 | 1.32 |
| 32 | 459.1 | 526.0 | 0.55 |
| 128 | 566.8 | 652.0 | 0.66 |

1000 条测试集：0.85 秒（约 1180 条/秒，batch=64）。**推理不需要梯度与 AdamW 状态**，
391 MB 权重 + 激活就是全部。

**显存不够时按性价比调**：① 降 `--batch-size`（激活是主项）→ ② `--max-length` 降到 48
→ ③ `--grad-accum` 顶上等效 batch → ④ `--gradient-checkpointing`（用时间换显存，本模型没必要）。

**最少要多少显存**（`vram_budget.py` 用分配器上限实测出来的，详见报告 §5.2）：

| 口径 | 分配器下限 | 加上 CUDA context(≈1.3 GB) | 建议留 20% 余量 |
|---|---|---|---|
| 动态 padding（真实训练就是这样） | 1076~1109 MB | 2374~2406 MB | **≈2.8 GB** |
| pad 到 64（最坏情况） | 1467~1549 MB | 2765~2846 MB | **≈3.3 GB** |

也就是说：**2.4 GB 以下必 OOM**（实测 1076 MB 分配器 + 1.3 GB context 就崩），
**3 GB 以上随便跑**。注意那个 1.3 GB 的 context 是固定开销（Blackwell + 这套 torch 构建），
模型本身只占 0.4 GB —— 卡越小、这套固定开销越低（老卡常见 300~600 MB）。

## 两个实测出来的结论（别照抄别的项目）

1. **别用 `test.jsonl` 调参**：dev 是从 `train.jsonl` **按类别分层**切出来的（默认 400 条），与训练互斥。
2. **不要用"低置信就回退到 0 类"**：这条我原本写在建议里，实测**是错的** ——
   阈值 0.7 / 0.9 / 0.99 时准确率单调下降，因为低置信样本的真值多在 1/3 类，
   一刀切回退只是把错误换个位置（具体数字见报告第六节的兜底表）。
   正确的用法是 **top-2**：误判样本的真值 **100% 落在第二位**，低置信样本（<0.9）的 top-1 只有一半左右
   但 top-2 是 100%。所以低置信时应 **把两个候选改写都执行**（或转人工），而不是退到某一类。
   `infer.py` 已经把 top-2 打进指标输出。

## 产物

`logs/lora/run1/` 下：`lora_adapter/`（LoRA 权重 + 分类头 + 分词器，**几百 KB 到几 MB**）、
`run_meta.json`（超参 + 数据规模 + dev/test 指标 + 参数量）、`vram_train.json`（完整显存账本）、
`test_predictions.jsonl`（逐条预测 + 置信度 + 四类概率，供错误分析）。
