# project_rag

RAG（检索增强生成）学习项目：**Python 3.13 + LangChain + Milvus + MySQL + Redis**，按端口-适配器（ports & adapters）分层，
把 EduRAG V7.5 讲义里的每一块功能重写成可测试、可替换的工程结构。

## 目标与取舍

**目标**：用当前端口-适配器框架，做完讲义代码的**全部功能**以及讲义里出现的**优化**。
讲义要求实现什么、做到哪了，逐项列在 [`docs/lecture-map.md`](docs/lecture-map.md)。

**施工顺序**：讲义原序 **④ → ⑤ → ⑥ → ⑦**。**⑤ 评估排在 ⑥ 融合之前** ——
6.2 自称「优化版」，没有评估基线，做完融合也无法证明它比单通道更好。

**取舍**：讲义之外的其他优化集中在 [`docs/deferred.md`](docs/deferred.md) §1（分三梯队），
冷门的放 §2 并写明「为什么不做」。每个"暂不做"都必须写触发条件。

> 已判定「问题特别明显」的例子：讲义 3.3 把 BM25 原始分做 softmax 后再比 `0.85`，
> 而 softmax 的最高值取决于**语料条数**而非相似度（N≥5 就永不命中，即「FAQ 库越大命中率越低」），
> 所以**不照搬**。同类修正共 8 条，见 [`docs/STATUS.md`](docs/STATUS.md) §3。

## 快速开始

```bash
uv sync
uv run project-rag chat -p "用一句话解释什么是向量数据库"
uv run project-rag ask -q "退款流程怎么走？"     # FAQ 通道：命中直答，未命中 LLM 兜底
```

## 验收（回归门槛）

```bash
uv run pyright src                                         # 0 errors
uv run python scripts/acceptance/check_m1_1_config.py      # 11 通过 / 0 失败
uv run python scripts/acceptance/check_m1_2_logging.py     #  8 通过 / 0 失败
uv run python scripts/acceptance/check_m1_3_cli.py         # 11 通过 / 0 失败
uv run python scripts/acceptance/check_m2_faq.py           # 27 通过 / 0 失败 / 0 跳过（容器运行时）
uv run python scripts/acceptance/check_m3_rag.py           # 37 通过 / 0 失败 / 1 跳过（容器运行时）
uv run python scripts/acceptance/check_env_services.py     #  5 通过 / 0 失败（需先起容器）
```

> `check_m2_faq.py` 里有 4 项依赖真实容器（MySQL 落库、Redis 降级、2 条中文端到端）：容器在跑时
> 全部 PASS，MySQL 连不上时报 **SKIP** 并写明连接目标，**不算通过**。
> 契约维护约定：**正常的代码优化由 AI 同步契约**（分数口径、阈值默认值、缓存 value 编码、`score` 语义等
> 已在 2026-09-18 对齐），只有引入致命缺点才反向改代码。详见 [`docs/STATUS.md`](docs/STATUS.md) §0/§7。
>
> 容器没起时 `check_env_services.py` 会报 **0 通过 / 5 失败** —— 那是环境没就绪，不是代码回归；
> `check_m2_faq.py` 里依赖真实 MySQL 的那几项则报 **SKIP** 并写明原因。先 `docker compose up -d`
> （注意：当前 WSL 发行版里没有 `docker` 命令，要在 Windows 侧起 Docker Desktop 并打开 WSL Integration）。

`check_m2_faq.py` 的 docstring 是 M2 契约的权威来源（`docs/STATUS.md` §8 只是索引）。

## 讲义 → 里程碑对照

| 讲义 | 内容 | 本项目落点 | 状态 |
|---|---|---|---|
| ① 项目概述 | 架构 / 环境 | M1 工程地基 | ✅ |
| ② 项目工具 | Ollama、LangChain、Milvus | `ports/llm.py` + `adapters/llm_deepseek.py`；其余是 M3 的手边工具 | 🔶 |
| ③-3.1 日志 | `logging` 六节 | `observability/logging_setup.py` | ✅ |
| ③-3.2 Redis | Redis 客户端 | `ports/cache.py` + `adapters/cache_redis.py` | ✅ |
| ③-3.3 MySQL 问答 | Config/logger/MySQL/Redis/预处理/main | M2：`faq_mysql.py` + `matcher_bm25.py` + `answer_faq.py` + `ask` | ✅ |
| ③-3.3 优化·答案缓存 | Redis 缓存短路（同一问题第二次不再检索） | `answer_faq.py`：短路 + TTL + 双重降级 + 兜底答案也缓存 | ✅ |
| ③-3.3 优化·阈值分流 | 够像直答，否则 LLM 兜底 | `answer_faq(threshold=...)` ← `RAG_FAQ__THRESHOLD` | ✅ |
| ③-3.3 优化·语料缓存 | 问题列表 + 分词结果进 Redis（省全表扫描 + N 次分词） | 未做 | ⬜ |
| ③-3.4 BM25 | `BM25L` 最小演示 | `adapters/matcher_bm25.py`（`BM25Plus` + **自身分归一化**口径，阈值 0.85） | ✅ |
| ④ 基于 Milvus 的问答系统 | 文档处理 / 向量存储 / 两路召回+融合 / 意图识别 / 策略选择 | **M3** | 🔶 M3.1–M3.8 已落地（检索链路可跑），M3.9 重排待做 |
| ⑤ RAG 系统评估 | RAGAS 四指标与评估代理 | **M4** | ⬜ |
| ⑥ 融合 Mysql 的 RAG 系统 | 双通道融合 + 对话历史 + 流式 + FastAPI | **M5** | ⬜ |
| ⑦ 企业级生产环境部署 | Docker / 服务化 / 接口文档 | **M6** | ⬜ |

> 每一项功能的**出处、依赖、关键代码、讲义自身的坑**，逐条列在
> [`docs/lecture-map.md`](docs/lecture-map.md)（总表）与 [`docs/lecture/`](docs/lecture/)（逐章底稿）。

## 阶段

- [x] **M1 工程地基**：类型化配置、日志、LLM 端口/适配器、CLI、静态检查、验收体系
- [x] **M2 FAQ 通道**：MySQL + BM25 匹配 + Redis 缓存 + `ask` 子命令（契约已同步；真实链路检查等容器起来复跑）
- [ ] **M3 向量知识库与 RAG 问答**（讲义 ④）—— 🔶 完成 M3.1–M3.8：
      配置三段 / domain+ports+内存实现 / BGE-M3 适配器 / 中文两级切分+ingest /
      Milvus 适配器（数据真进库）/ `rag` 带引用问答 / 父子分层 / BM25+RRF 两路融合。
      **待做 M3.9 重排**（`bge-reranker-v2-m3`）
- [ ] **M4 评估闭环**（讲义 ⑤）—— 先有尺子，再谈优化
- [ ] **M5 两通道融合与 FastAPI**（讲义 ⑥）
- [ ] **M6 服务化与部署**（讲义 ⑦）

## 基础设施

```bash
docker compose up -d
docker compose ps
```

> **连不上容器（MySQL/Redis/Milvus 一律 `Connection refused`）先确认 Windows 侧 Docker Desktop 开着**，
> 再看 `docker compose ps` —— 那是环境没就绪，不是代码回归（详见 [`docs/STATUS.md`](docs/STATUS.md) §5 第 10 条）。
> 本 WSL 发行版里没有 `docker` 命令，compose 都在 Windows 侧执行。

| 宿主端口 | 服务 |
|---|---|
| 10086 | Milvus gRPC |
| 10087 | Redis（密码 1234） |
| 10093 | MySQL（库/用户 `rag`，密码 1234） |
| 10092 | Attu（Milvus 图形界面） |

## 文档

- [`docs/lecture-map.md`](docs/lecture-map.md) —— **讲义要求实现什么、做到哪了**（施工总表）
- [`docs/lecture/`](docs/lecture/README.md) —— 逐章功能底稿（做哪个里程碑读哪一份，别通读）
- [`docs/STATUS.md`](docs/STATUS.md) —— 当前状态、验收基线、架构硬约束、环境版本
- [`docs/deferred.md`](docs/deferred.md) —— 暂不做清单 + 备选优化策略（分梯队，写明触发条件）
