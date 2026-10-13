# AGENTS.md —— 在本仓库里工作的 agent 先读这里

> DSH 每次会话的第一次请求会自动注入本文件（项目根标记 `.git` → 会话工作目录逐层查找 `AGENTS.md`；
> 用户全局的 `~/.dsh/AGENTS.md` 也在链上）。**这里只写"必须知道的事实 + 机制"，不写又一篇会被跳过的长文。**
> 长账在 `docs/STATUS.md`（维护者的决策与踩坑总账），按 `§` 编号可直接检索。

## 一、环境事实：沙箱视角 ≠ 机器能力（本仓库已经因此错过三次）

### 判据规矩（适用于 GPU / 设备 / 内核模块 / 网络 / 端口这类会被沙箱与命名空间裁剪的东西）

1. 判「宿主机有没有 X」必须走**不经本进程沙箱的通道**：`docker exec` / `docker run --gpus all`、
   `docker info`、宿主服务自身、Windows 侧命令。
   **进程内的 `/dev`、`nvidia-smi`、`torch.cuda` 只代表"我这个视角"**，不构成机器事实。
2. 结论是**否定**时，先假设「是不是我看不到」，并至少找一条**机制上正交**的证据；
   四条同源否证 ≈ 一条否证，不算交叉验证。
3. 写进文档 / 配置 / 提交信息的**否定断言**，必须带上**判据命令 + 从哪一侧跑的**。
   「沙箱看不见」≠「机器没有」—— 这个错在本仓库发生过三次：`docs/STATUS.md` §0.3 第 6、40 条，
   §5 第 48 条；其中两次被写进了产物（compose 注释、README）。

### 本机已知事实（2026-10-13 实测）

| 项 | 值 | 判据（哪一侧） |
|---|---|---|
| GPU | **有**：NVIDIA GeForce RTX 5080 Laptop GPU | `docker exec rag_platform-app-1 python -c "import torch;print(torch.cuda.is_available())"` → `True`（经 dockerd） |
| docker GPU runtime | 有 `nvidia` | `docker info --format '{{json .Runtimes}}'`（经 dockerd） |
| agent shell 的 `/dev` | **私有、14 项、无 `/dev/dxg`** | `ls /dev`（沙箱内）；同侧 `nvidia-smi` 只会报 `GPU access blocked by the operating system` |
| compose 的 app 服务 | `gpus: all` + `RAG_EMBEDDING__DEVICE: cuda`，**成对，别单独删** | `docker-compose.yaml` |

### 推论：在 agent shell 里跑宿主侧模型脚本要压到 CPU

`ingest` / `build_eval_dataset.py` / `run_ragas_eval.py` 直接在 agent shell 里跑时看不到 GPU，
会报 `Found no NVIDIA driver on your system`。覆盖成 CPU：

```bash
RAG_EMBEDDING__DEVICE=cpu RAG_EMBEDDING__USE_F16=false RAG_EMBEDDING__RERANK_USE_F16=false \
  .venv/bin/python scripts/tools/build_eval_dataset.py --in data/eval/eval_set_bagu_v1.json --name bagu_v1
```

**别把这三行覆盖写回 `config/config.yaml`**：那份配置是给**容器**用的，容器里 `cuda` 是对的；
要 GPU 就在容器里跑。CPU 上 112 条的代价：builder ≈13 分钟、RAGAS ≈24 分钟。

## 二、动手前要知道的项目口径

- `data/`、`docs/`、`models/`、`.cache/`、`.trash/` 都不进版本库（见 `.gitignore`）；`/scripts/*` 只公开
  `acceptance/`、`tools/` 的一部分。
- 验收在 `scripts/acceptance/`（`check_*.py` / `reverse_*.py`，入口 `run_all.py`，分组见脚本头部）。
  **改了数据或契约必须复跑相关 check**；SKIP 要能点名 owner，FAIL 要能复现。
- **数据是契约的一部分**：`data/eval/datasets/*.json` 的内容哈希被记在 `data/eval/ragas_runs/*.summary.csv`，
  验收有专条盯「summary 记的指纹 == 当前数据集文件」。**改了数据集就必须重跑评估**，
  不要手工修内容后留旧分数（`check_m4_eval.py` 会红，这正是那条检查存在的理由）。
- 变更同步：改数据 → `data/README.md`；改契约与读数 → `docs/STATUS.md`；改用法 → `README.md`。
- 清退/删除类操作：先隔离到 `.trash/<主题>_<日期>/` 并写 `MANIFEST.md`（清单 + 恢复命令 + 没动什么），
  因为 `data/` 不在 git 里，删了找不回来。

## 三、别重复已经写过的教训

动手前先 grep 总账：`grep -n "关键词" docs/STATUS.md`。
**同一条教训第二次出现时，别再写一遍文档** —— 把它做成机制（验收断言 / `SkipCheck` / 放在下一个会踩的人
必然看到的位置的注释），或者写进本文件。
