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
> 所以**不照搬**。同类修正共 7 条，见 [`docs/STATUS.md`](docs/STATUS.md) §3。

## 快速开始

```bash
uv sync
uv run project-rag chat -p "用一句话解释什么是向量数据库"
uv run project-rag faq -q "退款流程怎么走？"     # FAQ 单通道（原 ask）：命中直答，未命中 LLM 兜底
uv run project-rag qa -q "退款流程怎么走？"      # 合流（M5.1）：FAQ 命中直答，否则走知识库 + 引用清单
uv run project-rag qa -q "它有什么用？" --session u1     # 带会话（M5.2）：历史进提示词，这一轮也记下来
uv run project-rag qa -q "装饰器有什么用？" --stream       # 逐字流式打印（M5.3）
uv run project-rag-api                                    # 起 HTTP/WebSocket 服务（M5.4，127.0.0.1:8080）
# 会话历史（M5.2）第一次用之前建一次表（幂等）：
#   uv run python scripts/tools/init_conversation.py
```

## 验收（回归门槛）

```bash
uv run pyright                                             # 0 errors（整个工作区，含 scripts/）
uv run python scripts/acceptance/check_m1_1_config.py      # 13 通过 / 0 失败
uv run python scripts/acceptance/check_m1_2_logging.py     #  8 通过 / 0 失败
uv run python scripts/acceptance/check_m1_3_cli.py         # 11 通过 / 0 失败
uv run python scripts/acceptance/check_m2_faq.py           # 28 通过 / 0 失败 / 0 跳过（容器运行时）
uv run python scripts/acceptance/check_m3_rag.py           # 49 通过 / 0 失败 / 4 跳过（容器运行时）
uv run python scripts/acceptance/check_m4_eval.py          # 30 通过 / 0 失败 / 1 跳过
uv run python scripts/acceptance/check_m5_qa.py            # 32 通过 / 0 失败 / 1 跳过（M5.1 合流路由）
uv run python scripts/acceptance/check_m5_history.py       # 33 通过 / 0 失败（M5.2 对话历史；容器不在时真库那几条 SKIP）
uv run python scripts/acceptance/check_m5_stream.py        # 36 通过 / 0 失败（M5.3 截断检测 + 流式入口 + 思考开关；**会真调 3 次极小 DeepSeek**，见下）
uv run python scripts/acceptance/check_m5_api.py           # 25 通过 / 0 失败（M5.4 九个端点 + 事件循环不被占住 + 起服务姿势；不起服务器、不需要容器）
uv run python scripts/acceptance/check_m5_5_latency.py     # 19 通过 / 0 失败（M5.5 延迟观测：TTFT/tps/检索六段 + timed 的三条纪律；不联网）
uv run python scripts/acceptance/check_env_services.py     #  5 通过 / 0 失败（需先起容器）
```

**慢在哪？看这一行账**（M5.5 加的，M5.6 靠它定位到「每请求重载模型」那 4.4s）：

```bash
grep -h " | latency " logs/app.log | tail -20      # 一次回答一行（qa 外层 + rag 内层）
# label=rag ... retrieve_ms=2021.1 embed_ms=124.1 vector_ms=294.1 bm25_ms=203.6 \
#   fuse_ms=0.0 collapse_ms=0.0 rerank_ms=1399.2 llm_ttft_ms=2803.3 ttft_ms=4824.4 tokens=186 tps=349.7
# label=qa  ... faq_ms=13.0 sources_ms=2035.4 ttft_ms=4838.8 total_ms=5374.5
grep -o "rerank_ms=[0-9.-]*\|tps=[0-9.-]*" logs/app.log | tail -20
```

> **M4 的评估链路**（三条命令，顺序不能颠倒）：
>
> ```bash
> uv run python scripts/tools/build_eval_set.py                      # 造评估集（确定性，可反复跑）
> uv run python scripts/tools/build_eval_dataset.py --in data/eval/eval_set_bagu_v1.json --name bagu_v1
> uv run python scripts/tools/run_ragas_eval.py --dataset data/eval/datasets/bagu_v1_dataset.json --tag bagu_v1
> ```
>
> judge 用 **DeepSeek**、embedding 用**本地 BGE-M3**（不需要 OpenAI key）；
> 结果落到 `data/eval/ragas_runs/run_<时间戳>_<tag>.csv`，**从不覆盖历史**。

> `check_m2_faq.py` 里有 4 项依赖真实容器（MySQL 落库、Redis 降级、2 条中文端到端）：容器在跑时
> 全部 PASS，MySQL 连不上时报 **SKIP** 并写明连接目标，**不算通过**。
> `check_m3_rag.py` 的 5 条 SKIP 只是因为跑在沙箱里看不到 GPU（配置 `device=cuda`），
> 在**自己的终端**里会转 PASS；原先那条点名「融合路由（M5）」的 SKIP 已在 M5.1 转成真断言。
> `check_m5_qa.py` 的 1 条 SKIP 同理（沙箱看不到 GPU 时「真语料 + 真嵌入」那条）；
> 临时用 `RAG_EMBEDDING__DEVICE=cpu` 跑这一份可以拿到 **33 通过 / 0 失败 / 0 跳过**（已验证）。
> `check_m5_history.py` 里有 11 条要真 MySQL（建表幂等 / 往返 / 裁剪 / 回滚 / 参数化 / 连不上报错）：
> 容器在跑时全部 PASS，连不上时报 **SKIP** 并写明连接目标。
> ⚠️ `check_m5_stream.py` 里有 **1 条会真调一次 DeepSeek**（一个问题、上限 16 token，成本可忽略）：
> 它钉的是「provider 到底把 `finish_reason` 放在哪个字段」——这件事假件永远证明不了。
> 没配密钥时它 SKIP；**不想花钱就单跑这一份时把 `.env` 临时挪开**。
> `check_m4_eval.py` 的 1 条 SKIP 是「融合方式 / 重排单位 / 阈值 0.85 这三组 A/B 还没跑」，
> 也点名了 owner（容器没起时 Milvus 那项会再多一条 SKIP）—— 评估链路本身全绿。
> **看指标别只看总值**：`groups.csv` 里 `paraphrase` 组（加口语噪声问）在 6/6 次比较里
> 都低于 `verbatim` 组 —— 那个方向才说明「换个说法还找不找得到」。
> **读数口径**：差量小于 **0.01** 按"打平"读（单次运行的噪声就有这个量级）；
> 评估范围只保留这四个指标，一轮 A/B = 跑一次、不重复跑（见 [`docs/STATUS.md`](docs/STATUS.md) §0.5）。
> 契约维护约定：**正常的代码优化由 AI 同步契约**（分数口径、阈值默认值、缓存 value 编码、`score` 语义、
> `Deps` 字段清单等已多次对齐），只有引入致命缺点才反向改代码。详见 [`docs/STATUS.md`](docs/STATUS.md) §0/§7。
>
> 容器没起时 `check_env_services.py` 会报 **0 通过 / 5 失败** —— 那是环境没就绪，不是代码回归；
> `check_m2_faq.py` 里依赖真实 MySQL 的那几项则报 **SKIP** 并写明原因。先 `docker compose up -d`
> （注意：当前 WSL 发行版里没有 `docker` 命令，要在 Windows 侧起 Docker Desktop 并打开 WSL Integration）。

`check_m2_faq.py` 的 docstring 是 M2 契约的权威来源（`docs/STATUS.md` §8 只是索引）。

## 讲义 → 里程碑对照

| 讲义 | 内容 | 本项目落点 | 状态 |
|---|---|---|---|
| ① 项目概述 | 架构 / 环境 | M1 工程地基 | ✅ |
| ② 项目工具 | Ollama、LangChain、Milvus | `ports/llm.py` + `adapters/llm_deepseek.py`（LLM 走 DeepSeek API，不用 Ollama）；Milvus 已在 M3 用上 | 🔶 |
| ③-3.1 日志 | `logging` 六节 | `observability/logging_setup.py` | ✅ |
| ③-3.2 Redis | Redis 客户端 | `ports/cache.py` + `adapters/cache_redis.py` | ✅ |
| ③-3.3 MySQL 问答 | Config/logger/MySQL/Redis/预处理/main | M2：`faq_mysql.py` + `matcher_bm25.py` + `answer_faq.py` + `faq`（原 `ask`） | ✅ |
| ③-3.3 优化·答案缓存 | Redis 缓存短路（同一问题第二次不再检索） | `answer_faq.py`：短路 + TTL + 双重降级 + 兜底答案也缓存 | ✅ |
| ③-3.3 优化·阈值分流 | 够像直答，否则 LLM 兜底 | `answer_faq(threshold=...)` ← `RAG_FAQ__THRESHOLD` | ✅ |
| ③-3.3 优化·语料缓存 | 问题列表 + 分词结果进 Redis（省全表扫描 + N 次分词） | 未做 | ⬜ |
| ③-3.4 BM25 | `BM25L` 最小演示 | `adapters/matcher_bm25.py`（`BM25Plus` + **自身分归一化**口径，阈值 0.85） | ✅ |
| ④ 基于 Milvus 的问答系统 | 文档处理 / 向量存储 / 两路召回+融合 / 意图识别 / 策略选择 | **M3** | ✅ M3.1–M3.9 已落地（两路召回+融合+重排全通；意图识别/策略选择如实标注未做） |
| ⑤ RAG 系统评估 | RAGAS 四指标与评估代理 | **M4** | ✅ 四指标链路已跑通（judge=DeepSeek / embedding=BGE-M3）；5.3 三个评估代理未做 |
| ⑥ 融合 Mysql 的 RAG 系统 | 双通道融合 + 对话历史 + 流式 + FastAPI | **M5** | ✅ M5.1–M5.4 全部落地（`qa` / `qa --session` / `qa --stream` / `project-rag-api`） |
| ⑦ 企业级生产环境部署 | Docker / 服务化 / 接口文档 | **M6** | ⬜ |

> 每一项功能的**出处、依赖、关键代码、讲义自身的坑**，逐条列在
> [`docs/lecture-map.md`](docs/lecture-map.md)（总表）与 [`docs/lecture/`](docs/lecture/)（逐章底稿）。

## 阶段

- [x] **M1 工程地基**：类型化配置、日志、LLM 端口/适配器、CLI、静态检查、验收体系
- [x] **M2 FAQ 通道**：MySQL + BM25 匹配 + Redis 缓存 + `faq` 子命令（M5.1 从 `ask` 改名；契约已同步，容器在跑时 27/0/0 全绿）
- [x] **M3 向量知识库与 RAG 问答**（讲义 ④）—— M3.1–M3.9 全部落地：
      配置三段 / domain+ports+内存实现 / BGE-M3 适配器 / 中文两级切分+ingest /
      Milvus 适配器（数据真进库）/ `rag` 带引用问答 / 父子分层 / BM25+RRF 两路融合 / 重排。
      收尾两件（2026-09-22 均已完成）：**重灌一次 Milvus**（138 → 70 子块，主键换 sha256 + 清掉 68 块旧扁平块）
      与工作区类型检查（`src/` + `scripts/`，0 errors）
- [x] **M4 评估闭环**（讲义 ⑤）—— 先有尺子，再谈优化。语料扩到 3 个 source / 1353 子块；
      评估集 **112 条**（八股文 H3 小节自动成题，含 56 条口语化改写）+ 讲义自带 30 条；
      补上了讲义 ⑤ 缺的 **dataset builder**；四指标落盘带元数据（summary / rows / groups 三份 CSV，从不覆盖）。
      judge = DeepSeek、embedding = 本地 BGE-M3（**不需要 OpenAI key**）；
      `context_relevancy` 在 ragas 0.4.3 已改名 `context_precision`
- [ ] **M5 两通道融合与 FastAPI**（讲义 ⑥）
      —— **M5.1 已落地**：合流入口 `qa`（FAQ 优先 → 知识库兜底，`domain.QaAnswer` 带上「谁答的 + 证据链」）；
      命中判定收进 `match_faq()` 一处；知识库那半边以**零参函数**交给用例，FAQ 命中时连嵌入/重排模型都不构造。
      —— **M5.2 已落地**：对话历史（`conversations` 表 + 最近 5 轮裁剪、参数化查询 + 事务）；
      `qa --session` 把最近几轮喂进**知识库通道的提示词**，FAQ 命中不读历史但这一轮照样记下来。
      —— **M5.3a 已落地**：模型端口改成吐**帧**（`LlmDelta`，带结束原因），适配器不再丢 `finish_reason`，
      半截答案**照原样返回但会告诉用户「被截断了」**（原来它静悄悄）。
      —— **M5.3 已落地**：模型端口吐帧（`LlmDelta`）、流式入口 `stream_question`/`stream_rag`（帧协议
      `sources`/`token`/`done`）、`qa --stream` 逐字打印；**非流式就是流式的收拢器**，两者逐字一致。
      —— **M5.4 已落地**：FastAPI 接口层（9 个端点 + 问候拦截 + CORS + 静态页面），
      HTTP 端点写成同步 `def`（自动进线程池）、`WS /api/stream` 直接消费流式帧。
      **M5 全部完成**
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
