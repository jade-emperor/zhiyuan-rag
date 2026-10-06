# project_rag

一个 RAG（检索增强生成）问答系统：**FAQ 标准答案优先、知识库检索兜底**，答案带 `[1][2]` 引用，
支持多轮会话、逐字流式输出、HTTP/WebSocket 接口，并带一套「一行日志说清慢在哪」的延迟观测。

**技术栈**：Python 3.13 · LangChain · Milvus 2.4 · MySQL 8 · Redis 7 · FastAPI。
代码按**端口-适配器**分层：`ports/` 只声明「我需要什么能力」，`adapters/` 是全项目唯一碰
第三方 SDK 的地方，`domain/` 只用标准库。换模型厂商、换向量库、换缓存都只换一个适配器文件。

---

## 一、跑起来：仓库里只有代码

**版本库里只有代码**：`src/`（产品代码）、`config/config.yaml`、`docker-compose.yaml`、
`Dockerfile`、`pyproject.toml` + `uv.lock`、`.env.example`，加上两份文档
（`README.md` 与本仓的 [`API.md`](API.md)，后者是 HTTP / WebSocket / CLI 接口文档）。
下面这几样**不在**仓库里，得自己准备 —— 缺哪一样，
对应的功能就跑不起来：

| 缺什么 | 症状 |
|---|---|
| `.env`（API key、数据库密码） | 启动即报错（密码字段空值校验 fail-fast） |
| 三个中间件容器 | Milvus/MySQL/Redis 一律 `Connection refused` |
| 模型权重（各 2.2G） | 知识库检索与重排起不来 |
| 语料（FAQ CSV + 知识库文档） | 没有可匹配/可检索的数据 |
| 三处一次性初始化 | 表/集合不存在 → 抛错（不是「查不到」） |

### 0. 前置条件

| 需要 | 说明 |
|---|---|
| Python **3.13** | 版本钉在 `.python-version`，依赖只保证 3.13（`requires-python = ">=3.13,<3.14"`） |
| [uv](https://docs.astral.sh/uv/) | 装依赖、跑命令都用它（`uv sync` 会按 `uv.lock` 建 `.venv`） |
| Docker + Compose | 起 Milvus / MySQL / Redis |
| GPU（可选） | 有就留 `cuda`；没有就用 `RAG_EMBEDDING__DEVICE=cpu`（慢，但不改代码） |
| DeepSeek API Key | 生成回答用（[申请](https://platform.deepseek.com/)） |

### 1. 装依赖

```bash
git clone <repo-url> && cd project_rag
uv sync                       # 建 .venv + 装依赖（含 torch，首次下载较久）
```

依赖分两组：上面这条装的是**运行时**；另有一档可选的 `unstructured`（预留给 HTML / Excel / 扫描件
OCR 那条路，见文末「已知边界」）。要它得显式点名：`uv sync --extra unstructured`。
**它不进默认环境、也不进镜像** —— 那个包链上的 `en-core-web-sm` 只有 GitHub 一个下载源，
留在运行时依赖里会让 `uv sync` / `docker build` 每次去 github.com 取一个产品路径根本不用的模型
（`src/` 里 0 处 import）。理由与实测数字写在 `pyproject.toml` 的注释里。

### 2. 写 `.env`（密钥只在这里，不在仓库）

```bash
cat > .env <<'EOF'
RAG_LLM__DEEPSEEK_API_KEY=sk-xxxxxxxx
RAG_MYSQL__PASSWORD=1234
RAG_REDIS__PASSWORD=1234
EOF
```

配置优先级：**环境变量 > `.env` > `config/config.yaml`**（前缀 `RAG_`，嵌套层级用 `__`，
例如 `RAG_FAQ__THRESHOLD=0.9`）。`config.yaml` 里不写任何密钥。

### 3. 起中间件

```bash
docker compose up -d && docker compose ps
```

| 宿主端口 | 服务 | 用途 |
|---|---|---|
| 10086 | Milvus gRPC | 向量库（`standalone` 模式） |
| 10087 | Redis 7.2（密码 `1234`） | FAQ 答案缓存 |
| 10093 | MySQL 8.0（库/用户 `rag`，密码 `1234`） | FAQ 表 + 会话历史表 |
| 10088 / 10092 | Milvus HTTP / Attu | 可选：图形界面看数据 |

> 其余端口（10089/10090/10091）是 Milvus 自带的 MinIO 与 etcd，不用管。
> **连不上容器先确认 Docker 在跑** —— 那是环境问题，不是代码问题；`check_env_services.py` 会把它
> 报成 FAIL（而不是 SKIP），就是为了不让它被误读成「代码回归」。

### 4. 放模型权重（不进版本库，各约 2.2G）

```bash
uv run python -c "from modelscope import snapshot_download as d; \
  d('BAAI/bge-m3', local_dir='models/bge-m3'); \
  d('BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3')"
```

- 目录名必须与 `config/config.yaml` 里的 `embedding.model_dir` / `embedding.rerank_model_dir` 一致。
- 只想跑通、暂时不要重排：`rag.rerank: false`（检索会退回纯融合排序）。
- 没有 GPU：临时 `RAG_EMBEDDING__DEVICE=cpu`，或直接把 `config.yaml` 的 `device` 改成 `cpu`。

### 5. 放语料（不进版本库）

```text
data/
├── faq/<任意名>.csv                    # 两列：问题,答案（UTF-8，带 BOM 也认）
└── corpus/<名字>_data/                 # 知识库语料（可放子目录，source 只看顶层目录名）
    ├── *.md  *.txt                     # 直接读原文
    ├── *.docx  *.pptx                  # Word 的 Heading 样式 / 每页标题
    └── *.pdf                           # 书签目录（没有书签就没有标题层级）
```

- **`source` 就是目录名去掉 `_data`**：`bagu_ai_data/` → `--source bagu_ai` 用它过滤。
- 认识五种后缀：`.md` / `.txt`（直接读原文）、`.docx`（Word 的 Heading 样式）、
  `.pptx`（每页标题）、`.pdf`（书签目录）。不认识的格式、以及认了但抽不出文字的
  （扫描件）会**在 stderr 里各报一行** —— 少入库一份文档必须让人当场看见。
- 还没接 HTML / Excel / 扫描件 OCR，见 [`docs/deferred.md`](docs/deferred.md) §1.4。
- 语料按**标题层级**切父块、父块再切子块；标题层级丢了父块就会碎，所以 Markdown 的结构别省
  （Word 与 PDF 那边，标题来自文档自己的样式 / 书签，不用另做）。

### 6. 三处一次性初始化（都幂等，可重跑）

**6.1 建 FAQ 表并灌数据**

```bash
uv run python - <<'PY'
import csv, pymysql
from project_rag.config import get_settings

CSV = "data/faq/faq.csv"          # ← 换成你的文件（文件名随意）
cfg = get_settings()
table = cfg.faq.table             # 表名跟 config.yaml 的 faq.table 走（默认 faq）
DDL = f"""CREATE TABLE IF NOT EXISTS {table} (
  id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  question VARCHAR(512) NOT NULL,
  answer   TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
conn = pymysql.connect(host=cfg.mysql.host, port=cfg.mysql.port, user=cfg.mysql.user,
                       password=cfg.mysql.password.get_secret_value(),
                       database=cfg.mysql.database, charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute(DDL)
    cur.execute(f"TRUNCATE TABLE {table}")
    # 列名换成你自己 CSV 的表头（这里假设是 问题 / 答案）
    cur.executemany(f"INSERT INTO {table}(question, answer) VALUES(%s, %s)",
                    # 问题里的换行折叠成空格，答案里的换行原样保留（代码块换了就废了）
                    [(" ".join(r["问题"].split()), r["答案"]) for r in rows])
conn.commit()
print(f"装入 {len(rows)} 条")
PY
```

> 用 `pymysql` 而不是 `mysql` 命令行：**不用额外装客户端**（`pymysql` 是项目依赖）。
> CSV 必须用 `csv` 模块读（答案里有换行和逗号，按行 `split(",")` 一定切坏）。

**6.2 建会话表**（多轮问答用）

```bash
uv run python -c "
from project_rag.adapters import MySQLConversationStore
from project_rag.config import get_settings
s = get_settings(); MySQLConversationStore(s.mysql, s.conversation).ensure_table()"
```

**6.3 切块入库**（先看切分质量，再真写）

```bash
CORPUS=data/corpus/bagu_ai_data        # ← 换成你的语料目录
uv run project-rag ingest --path "$CORPUS" --dry-run   # 只打印父块/子块数与块长分布
uv run project-rag ingest --path "$CORPUS"             # 真写 Milvus（幂等：按 doc_id 清旧块重灌）
```

集合不存在会自动建；`--batch-size` 控制每次嵌入的块数（CPU 上大一点更划算）。

**6.4 看/换/删集合**（P0-5 之后每个集合多了一列 `page`，老集合不能写）

```bash
uv run project-rag collections                          # 有哪些集合、各多少块、有没有 page 列、当前配置指向哪个
uv run project-rag drop --collection rag_chunks          # 默认只说不做：打印会删掉什么，然后退出码 1
uv run project-rag drop --collection rag_chunks --yes     # 真删（不可恢复；语料还在磁盘上，重灌即可回来）
```

切集合只改 config 里 `milvus.collection` 一行（或用 `RAG_MILVUS__COLLECTION` 临时覆盖）。
**往没有 `page` 列的集合写会被当场拒收**（静默丢掉页码比报错糟得多），报错里会点名这三条路。

**6.4 看/换/删集合**（P0-5 之后每个集合多了一列 `page`，老集合不能写）

```bash
uv run project-rag collections                          # 有哪些集合、各多少块、有没有 page 列、当前配置指向哪个
uv run project-rag drop --collection rag_chunks          # 默认只说不做：打印会删掉什么，然后退出码 1
uv run project-rag drop --collection rag_chunks --yes     # 真删（不可恢复；语料还在磁盘上，重灌即可回来）
```

切集合只改 config 里 `milvus.collection` 一行（或用 `RAG_MILVUS__COLLECTION` 临时覆盖）。
**往没有 `page` 列的集合写会被当场拒收**（静默丢掉页码比报错糟得多），报错里会点名这三条路。

**6.4 看/换/删集合**（P0-5 之后每个集合多了一列 `page`，老集合不能写）

```bash
uv run project-rag collections                          # 有哪些集合、各多少块、有没有 page 列、当前配置指向哪个
uv run project-rag drop --collection rag_chunks          # 默认只说不做：打印会删掉什么，然后退出码 1
uv run project-rag drop --collection rag_chunks --yes     # 真删（不可恢复；语料还在磁盘上，重灌即可回来）
```

切集合只改 config 里 `milvus.collection` 一行（或用 `RAG_MILVUS__COLLECTION` 临时覆盖）。
**往没有 `page` 列的集合写会被当场拒收**（静默丢掉页码比报错糟得多），报错里会点名这三条路。

### 7. 跑起来

```bash
uv run project-rag chat -p "用一句话解释什么是向量数据库"   # 裸模型，不检索
uv run project-rag faq  -q "mac电脑 打不开乌班图 虚拟机"     # 只走 FAQ 通道：命中直答（问法要近乎一字不差）
uv run project-rag rag  -q "装饰器有什么用？"               # 只走知识库通道：检索 + 生成 + [1][2] 引用
uv run project-rag qa   -q "mac电脑 打不开乌班图 虚拟机"     # 合流：FAQ 优先 → 知识库兜底（日常用的就是它）
uv run project-rag qa   -q "装饰器有什么用？"               # 同一入口，FAQ 未命中时走知识库
uv run project-rag qa   -q "它有什么用？" --session u1       # 多轮：最近几轮历史进提示词，这一轮也记下来
uv run project-rag qa   -q "装饰器有什么用？" --stream       # 逐字流式打印（边生成边出）
uv run project-rag search -q "课程版本" --route bm25         # 只跑关键字那一路（不加载嵌入模型，最快）
uv run project-rag-api                                      # HTTP + WebSocket 服务（0.0.0.0:8080）

uv run project-rag <子命令> --help                           # 每个子命令的参数见这里
```

> **FAQ 命中要求「近乎一字不差」**（阈值 `faq.threshold: 0.85`，口径是「top1 原始分 ÷ 该条问自己
> 的分」）：把库里的原句照抄会命中并打印 `FAQ 直答(标准答案):1.0000`；换个说法通常落到知识库通道
> —— 这是刻意的分流，改写问法交给 RAG，标准答案只留给原句。

### 8. 把服务装进容器（compose 里已经编好了）

`docker-compose.yaml` 的 `app` 服务就是它：镜像由仓库根的 `Dockerfile` 构建（两阶段 —— builder 用 uv
按 `uv.lock` 装依赖，runtime 只带 `.venv` + 代码；非 root 跑，带 `/health` 探活）。**中间件不进这个镜像。**

```bash
docker compose up -d --build        # 一次起全套：中间件 + app（首次要构建镜像，最久）
docker compose ps                   # app 那行要是 healthy（首次最多等 3 分钟：启动预热要加载 BGE-M3）
curl http://localhost:8080/health
# 浏览器打开 http://localhost:8080/ —— 就是个能问话的页面（api/static/index.html）
```

- **地址用服务名**：`standalone:19530` / `mysql:3306` / `redis:6379`。同一个 compose 网络里，
  既不需要 `host.docker.internal`，也不经过 1008x/1009x 那些宿主映射。
- **只想起中间件**（在宿主上 `uv run` 开发）：`docker compose up -d standalone mysql redis`。
- **模型与语料挂卷、不进镜像**：`./models` 与 `./data`。配置里的相对路径是按**项目根**解析的，
  所以容器里的路径与宿主一致（`/app/models/bge-m3`、`/app/data/corpus/...`）。
- 容器里默认 `RAG_EMBEDDING__DEVICE=cpu`（看不见宿主 GPU）。要用 GPU 得换基础镜像、
  再把该变量改回 `cuda`，见 `Dockerfile` 顶部第 2 条。
- 一次性任务用 `run`，不必手写一长串挂载：
  `docker compose run --rm app project-rag ingest --path data/corpus/doc_data`
- 镜像偏大：`uv.lock` 里的 torch 是 CUDA 版，会拖进 `nvidia-*`（本地 `.venv` 实测 6.7G，
  其中 nvidia 3.2G + triton 0.9G）。CPU-only 的瘦身办法写在 `Dockerfile` 末尾注释里，
  **没在真机验证过**，验过再改默认。
- **构建期不需要 GitHub**：`unstructured` 那一档（37 个包）已经挪到可选依赖，运行时用不到，
  `uv sync --locked --no-dev` 也就不会去拉那个只有 GitHub 一个源的 `en-core-web-sm`（见 §1）。
- **国内网络两道坎**（2026-09-30 真机踩过，报错原文在 `Dockerfile` 头部）：
  ① **别给 `Dockerfile` 加 `# syntax=`** —— 那会让 BuildKit 先去 registry 拉一个 frontend 镜像，
  它是整个构建的第一个网络请求；② Docker Desktop 的**手工代理**（Settings → Resources → Proxies）
  如果指着本机的 Clash/V2Ray，代理没开时连 registry mirror 都连不上
  （报 `dial tcp 127.0.0.1:7897: connectex: … refused`，**代理对 mirror 一样生效**）——
  要么把代理开起来，要么关掉手工代理、只留 registry mirror。
  现在构建期只需要 **docker 镜像源 + pip 源（清华）**，不碰 `github.com` / `ghcr.io`。

**等价的手工 `docker run`**（不想用 compose 时；注意这时中间件在宿主上，要走 `host.docker.internal`）：

```bash
docker build -t project-rag:0.1.0 .
docker run --rm -p 8080:8080 \
  -e RAG_LLM__DEEPSEEK_API_KEY=sk-xxxx \
  -e RAG_MYSQL__HOST=host.docker.internal -e RAG_MYSQL__PORT=10093 \
  -e RAG_REDIS__HOST=host.docker.internal -e RAG_REDIS__PORT=10087 \
  -e RAG_MILVUS__HOST=host.docker.internal -e RAG_MILVUS__PORT=10086 \
  -v "$PWD/models:/app/models:ro" -v "$PWD/data:/app/data:ro" -v "$PWD/logs:/app/logs" \
  project-rag:0.1.0
```


**可选：评估链路**（三份 CSV 落盘，从不覆盖历史）

```bash
uv run python scripts/tools/build_eval_set.py                     # 造评估集（确定性，可反复跑）
uv run python scripts/tools/build_eval_dataset.py --in data/eval/eval_set_bagu_v1.json --name bagu_v1
uv run python scripts/tools/run_ragas_eval.py --dataset data/eval/datasets/bagu_v1_dataset.json --tag bagu_v1
```

顺序不能颠倒：先造题 → 再跑本系统产出答案与上下文（同时记下当时的检索旋钮指纹，
否则「改了配置再跑评估」会把旧数据标成新旋钮）→ 最后才打分。judge 用 DeepSeek、
embedding 用本地 BGE-M3（不需要 OpenAI key）。

---

## 二、这个项目做什么

### 两条通道 + 一条路由

| 通道 | 数据来源 | 命中条件 | 代价 | 入口 |
|---|---|---|---|---|
| **FAQ** | MySQL 里人工维护的问答对 | BM25 置信度 ≥ `faq.threshold`（默认 0.85，近乎一字不差） | 一次全表读 + 一次分词匹配，毫秒级 | `faq` / `qa` |
| **知识库** | Milvus 里的语料子块 | 向量 + 关键字两路召回 → 融合 → 折叠父块 → 重排 | 嵌入 + 检索 + 重排 + 生成 | `rag` / `qa` |
| **裸模型** | 无 | 不检索，直接问模型 | 一次生成 | `chat`、`faq` 未命中时的兜底 |

`qa`（合流）的路由规则：**FAQ 命中就直答**（人写的标准答案最可信，顺带省掉检索与生成的钱）；
未命中才走知识库；知识库检索为空则**直接拒答，不调模型**（不让模型拿不到资料硬编）。
FAQ 那半边做成「要用时才构造的零参函数」，所以 FAQ 命中的问题**连嵌入/重排模型都不会加载**。

### 一次知识库问答的完整链路

```text
ingest（一次性）:  文档 → 按标题切父块 → 父块切子块 → BGE-M3 嵌入 → Milvus
                   主键 = sha256(来源|文档|位置|内容) → 重灌幂等

每次提问:   问题 ─┬─ 向量路: BGE-M3 嵌入 → Milvus 检索 ─┐
                  └─ 关键字路: jieba 分词 → BM25 ────────┴→ 融合(RRF/加权)
                  → 折叠父块(子块命中、父块喂模型) → CrossEncoder 重排
                  → 拼提示词 → 模型流式生成 → 答案 + [1][2] 引用(含来源/分数/命中路径)
```

几个刻意的选择：融合**在用例层做**（两路分数量纲不可比，余弦 0.59 vs BM25 14.7，
直接相加等于把 BM25 放大 25 倍）；重排放在**折叠之后、截断之前**（分数要对应模型真正读到的那段
正文，引用清单上的分数才能逐字核对）；结束原因（`finish_reason`）一路透传到用户 ——
半截答案**照原样返回但会提示「被截断」**，不悄悄换成拒答话术。

### 功能清单

| 能力 | 说明 | 代码 |
|---|---|---|
| 类型化配置 | `RAG_` 前缀 + `__` 嵌套 + env > `.env` > yaml；密钥用 `SecretStr`，空值 fail-fast | `config.py` |
| 日志 | 每次重建 handler + 10MB 轮转 + 第三方库降噪；控制台 INFO / 文件 DEBUG 三级阀门 | `observability/logging_setup.py` |
| LLM 端口 | 流式帧 `LlmDelta`（增量文本 + 结束原因），换厂商只换适配器 | `ports/llm.py` / `adapters/llm_deepseek.py` |
| 思考模式开关 | 一个配置变量同时管**开关**与**强度**（`auto/off/low/high/max`） | `adapters/llm_deepseek.py` + `config.yaml` |
| 中文两级切分 | 父块 1200 字 / 子块 300 字 / 重叠 50；分隔符按语义强度递归降级 | `adapters/chunker_recursive.py` |
| 幂等入库 | 主键含内容哈希，同份输入跑两次得到同一批 id；按 `doc_id` 清旧块重灌 | `application/ingest_documents.py` |
| 多格式入库 | 五种后缀（md/txt/docx/pptx/pdf）各一个来源适配器；命名、排序、分派只在装载层一处；没读进来的文件逐个报出 | `ports/document_source.py` / `adapters/document_loader.py` |
| 两路召回 + 融合 | 进程内 BM25 + Milvus 稠密向量；RRF 或加权（min-max 归一化） | `application/fusion.py` / `adapters/bm25_index.py` |
| 重排 | BGE-Reranker-v2-m3 对**父块正文**精排；可关（消融开关） | `adapters/reranker_bge.py` |
| 引用与可核对 | 每条引用带来源、文档、分数、命中路径（单路/两路） | `domain/models.py` |
| FAQ 缓存 | Redis 短路 + TTL + 装配期/调用期双重降级；兜底答案也缓存 | `adapters/cache_redis.py` / `application/answer_faq.py` |
| 多轮会话 | `conversations` 表 + 最近 5 轮裁剪（写入与裁剪同事务）+ 参数化查询 | `adapters/conversation_mysql.py` |
| 流式帧协议 | `sources` / `token` / `done` 三类帧；**非流式入口就是流式的收拢器**（逐字一致） | `application/streaming.py` |
| HTTP / WebSocket | 9 个端点（问答、会话增删查、来源、健康检查、静态挂载）+ 问候拦截 + CORS | `api/app.py` |
| 延迟观测 | 一次回答一行账：检索六段 + TTFT + 吐字速率 + **思考三字段** | `observability/latency.py` |
| 评估闭环 | 评估集 → 跑本系统产出答案与上下文 → RAGAS 四指标落盘（带元数据，从不覆盖历史） | `scripts/tools/`（本地） |
| 验收体系 | 每个能力配一份可执行契约，PASS/FAIL/SKIP 三态；另有反向测试证明检查真的会红 | `scripts/acceptance/`（本地） |

### 慢在哪：一行日志

```bash
grep -h " | latency " logs/app.log | tail -20
# label=rag channel=rag thinking=off faq_ms=- sources_ms=6308.4 retrieve_ms=6308.4 embed_ms=176.4 \
#   vector_ms=112.8 bm25_ms=1095.5 fuse_ms=0.0 collapse_ms=0.0 rerank_ms=4923.6 \
#   llm_think_ms=- llm_ttft_ms=422.7 think_tokens=0 ttft_ms=6731.2 tokens=54 gen_ms=345.4 \
#   tps=156.4 total_ms=7077.9 refused=false truncated=false aborted=false
```

> **这一行里的 `bm25_ms` 已经是"旧数字"了**（P0-1，2026-10-06）：BM25 那一路原本**每查询**都向
> Milvus 要一次语料指纹（3938 块实测 **397.6ms**，而真正的 BM25 打分只要 ~1ms），现在指纹先进
> Redis、未命中才回源。真环境同一进程连查 4 次的对照：**2016 / 3.5 / 2.2 / 1.9 ms**（旧实现
> 1517 / 397.2 / 398.2 / 200.8）→ 稳态 **~400ms → ~2ms**。上面这条日志是加缓存之前留下的。

字段口径（拿不到的一律 `-`，「没量到」与「量到 0」分得开）：

| 字段 | 含义 |
|---|---|
| `retrieve_ms` | 检索总时长；拆成 `embed_ms` / `vector_ms` / `bm25_ms` / `fuse_ms` / `collapse_ms` / `rerank_ms` 六段 |
| `llm_ttft_ms` / `ttft_ms` | 调模型到**第一个答案字** / 从请求开始到第一个字 |
| `tokens` / `gen_ms` / `tps` | 正文帧数 / 首帧到末帧的墙钟 / 帧到达速率（不足两帧给 `-`，不编数） |
| `thinking` / `llm_think_ms` / `think_tokens` | 配置档位 / 思考窗口（到最后一帧思考）/ 思考帧数 |

思考这三列怎么读（最容易读错的是把「没调模型」当成「没想」）：

```text
thinking=auto llm_think_ms=2852.0 think_tokens=459   # 想了 2.85s，首字也等到 2.85s
thinking=off  llm_think_ms=-      think_tokens=0     # 调了模型、但没看到思考
llm_ttft_ms=- think_tokens=0                         # 压根没调模型（FAQ 命中 / 检索为空拒答）
```

### 换一个方向改代码之前，先看这三条硬约束

1. **依赖方向**：`cli/`、`application/`、`adapters/` → `ports/` → `domain/`，箭头永远朝内。
   AST 静态扫描强制：`domain/` 只准标准库；第三方 SDK 只准出现在 `adapters/`；
   `application/` 不准 import `adapters/`。
2. **契约在 `scripts/acceptance/`**：每份脚本的 docstring 就是那一块的权威契约（签名、默认值、
   口径、语义）。改公共签名要连着改契约，否则验收会红。
3. **改了就要能证明**：新增检查必须做**反向测试**（把产品改坏一次，确认检查真的会红），
   否则那条检查等于没有；配置类断言要同时钉「类默认值」和「配置文件的有效值」。

### 目录

```text
src/project_rag/
├── domain/          纯数据模型（只用标准库）
├── ports/           能力声明：llm / embedder / vector_store / keyword_index / reranker /
│                    chunker / faq / matcher / cache / conversation / document_source
├── adapters/        唯一碰第三方 SDK 的地方：DeepSeek / BGE-M3 / BGE-Reranker /
│                    Milvus / BM25 / MySQL / Redis / jieba 分词 / 文档解析与装载
├── application/     用例：合流路由、RAG 检索与生成、FAQ、缓存、帧协议、切分入库、问候
├── observability/   日志装配 + 延迟账本
├── cli/             命令行入口与组合根（Deps：全是惰性工厂）
└── api/             FastAPI 接口层 + 静态页面
config/config.yaml   全部可调项（检索旋钮、模型路径、设备、思考档位、阈值、表名）
docker-compose.yaml  中间件：Milvus(etcd+minio) / MySQL / Redis / Attu ＋ `app`(服务本体)
Dockerfile           服务本体的镜像（中间件不进它）
API.md               HTTP / WebSocket / CLI 接口文档
```

### 已知边界

- **还没接的格式**：HTML / Excel / 扫描件 OCR，以及页码、工作表这类元数据到引用清单的透传；
  优先级与代价见 [`docs/deferred.md`](docs/deferred.md) §1.4。真要做时先
  `uv sync --extra unstructured`（那一档默认不装，见 §1）。
- **应用已编进 compose，但镜像还没在真机构建过**（开发环境里没有 `docker` 命令）：
  `docker compose up -d --build` 起得来、`/health` 会变 healthy 是**纸面验收**；第一次真机构建
  仍可能暴露构建期问题（体积 ~9G、CUDA torch 怎么换成 CPU 轮子），见 §8 与 `Dockerfile` 末尾注释。
- 意图识别 / 检索策略选择、粗排后的多路并行、观测聚合（p50/p95）等未做 ——
  「暂不做」的条目与**触发条件**都写在 [`docs/deferred.md`](docs/deferred.md)。
- 当前状态、验收基线、每条踩过的坑记在 [`docs/STATUS.md`](docs/STATUS.md)。
