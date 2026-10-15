# project_rag

一个 RAG（检索增强生成）问答系统：**FAQ 标准答案优先、知识库检索兜底**，答案带 `[1][2]` 引用，
支持多轮会话、逐字流式输出、HTTP/WebSocket 接口，并带一套「一行日志说清慢在哪」的延迟观测。

**技术栈**：Python 3.13 · LangChain · Milvus 2.4 · MySQL 8 · Redis 7 · FastAPI。
代码按**端口-适配器**分层：`ports/` 只声明「我需要什么能力」，`adapters/` 是全项目唯一碰
第三方 SDK 的地方，`domain/` 只用标准库。换模型厂商、换向量库、换缓存都只换一个适配器文件。

**English**: [`README.en.md`](README.en.md)（短版：能力、5 分钟演示、验证方式）。

---

## 零、先看效果？5 分钟，不需要模型

不想先下 4.4G 模型、也不想准备语料，只想看看界面与一次问答长什么样：

```bash
./scripts/dev.sh up            # 起 Milvus / MySQL / Redis（已经起着就跳过）
./scripts/dev.sh demo          # 灌演示语料（自写的面试八股，哈希嵌入 → 零模型）
./scripts/dev.sh demo-serve    # 起服务 → http://127.0.0.1:18080/
```

演示语料在 [`demo/`](demo/README.md)：FAQ 通道问「深拷贝与浅拷贝的区别?」（**不调 LLM**，毫秒级），
知识库通道问「混合检索与 RRF 融合是怎么回事」（会调一次 LLM，答案带 `[1]` 引用）。
演示模式与生产配置只差三个环境变量（`EMBEDDING__BACKEND=hashing` / 独立集合 `rag_demo` /
关重排），**产品代码里没有"演示分支"**；它在你的库里加了什么、怎么撤回，`demo/README.md` 里逐条写了。

## 一、跑起来：仓库里只有代码（有什么、没有什么见文末两节）

**版本库里只有代码**：`src/`（产品代码）、`config/config.yaml`、`docker-compose.yaml`、
`Dockerfile`、`pyproject.toml` + `uv.lock`、`.env.example`，加上两份文档
（`README.md` 与本仓的 [`API.md`](API.md)，后者是 HTTP / WebSocket / CLI 接口文档），
以及验收脚本（`scripts/acceptance/`）。
（维护者本地的决策与状态文档放在 `docs/`，**不随仓库发布** —— 下文偶尔引用它只作溯源；
克隆者能跑、能读的判据都在 `scripts/acceptance/` 与其 docstring 里。）
下面这几样**不在**仓库里，得自己准备 —— 缺哪一样，
对应的功能就跑不起来：

| 缺什么 | 症状 |
|---|---|
| `.env`（API key、数据库密码） | 启动即报错（密码字段空值校验 fail-fast） |
| 三个中间件容器 | Milvus/MySQL/Redis 一律 `Connection refused` |
| 模型权重（各 2.2G） | 知识库检索与重排起不来（**想跳过就用上面的演示模式**） |
| 语料（FAQ CSV + 知识库文档） | 没有可匹配/可检索的数据（演示语料可先用） |
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
- 还没接 HTML / Excel / 扫描件 OCR（能验证的三种格式见 `scripts/acceptance/check_doc_formats.py`）。
- 语料按**标题层级**切父块、父块再切子块；标题层级丢了父块就会碎，所以 Markdown 的结构别省
  （Word 与 PDF 那边，标题来自文档自己的样式 / 书签，不用另做）。

### 6. 初始化与集合管理（都幂等，可重跑）

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
**灌完它会把这一批登记回来**（状态 / 块数 / 磁盘字节数），所以知识库页上立刻看得到它们 ——
以前 `ingest` 不写登记表，后端灌的语料在界面上永远是「未登记」。

**6.4 看/换/删集合**（P0-5 之后每个集合多了一列 `page`，老集合不能写）

```bash
uv run project-rag collections                          # 有哪些集合、块数 / page 列 / 稀疏列 / 索引类型 / 当前指向哪个
uv run project-rag collections --reindex --yes           # 按 milvus.index.* 重建索引（FLAT ⇄ IVF_FLAT，数据不动）
uv run project-rag sparse --yes                          # 按当前语料重算稀疏向量（keyword.backend=milvus 那一档）
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
uv run project-rag qa   -q "它有什么用？" --session u1       # 多轮：滑动窗口(最近 N 轮且不超过
                                                       #   字符预算)进提示词，这一轮也记下来
uv run project-rag qa   -q "装饰器有什么用？" --stream       # 逐字流式打印（边生成边出）
uv run project-rag search -q "装饰器" --route bm25           # 只跑关键字那一路（不加载嵌入模型，最快）
uv run project-rag-api                                      # HTTP + WebSocket 服务（config.yaml 的 0.0.0.0:18080）
./scripts/dev.sh serve                                      # 同一件事的短命令（18080）+ 端口占用守卫

uv run project-rag <子命令> --help                           # 每个子命令的参数见这里
```

浏览器打开 **`http://127.0.0.1:18080/`**（307 → `/static/app/`）就是那份 Vue 界面：上面这些能力在界面里
是「对话 / 知识库 / 报表 / 账号与权限」四页（按角色显示：运维只看到「报表」，员工看到「对话 + 知识库」）。
账号没有注册入口，先建一个：

```bash
uv run project-rag user add -u 你的名字 --role admin     # 命令行建号；员工 --role employee、运维 --role ops
# 之后也可以由管理员在界面「账号与权限」页建号 / 删号 / 分组(见下面「界面与角色」)
```

> **FAQ 命中要求「近乎一字不差」**（口径是「top1 原始分 ÷ 该条问自己
> 的分」）：把库里的原句照抄会命中并打印 `FAQ 直答(标准答案):1.0000`；换个说法通常落到知识库通道
> —— 这是刻意的分流，改写问法交给 RAG，标准答案只留给原句。
> 阈值 `faq.threshold` 是**部署旋钮**（代码默认 `0.85`，改 `config/config.yaml` 的 `faq.threshold`；
> 别在文档里抄生效值 —— 它会变，想知道它现在落在可用区间的哪里就跑 `uv run python scripts/acceptance/calibrate_threshold.py`）：
> 它必须落在"实测可用区间"里 —— 调低会让无关问法误命中 FAQ 通道，调高会让加了礼貌语的原句失效，
> 验收脚本 `check_p0_4_ab.py` 会用同一套探针量一遍区间再断言（区间与探针口径写在那个脚本的 docstring 里）。

### 8. 把服务装进容器（compose 里已经编好了）

`docker-compose.yaml` 的 `app` 服务就是它：镜像由仓库根的 `Dockerfile` 构建（两阶段 —— builder 用 uv
按 `uv.lock` 装依赖，runtime 只带 `.venv` + 代码；非 root 跑，带 `/health` 探活）。**中间件不进这个镜像。**

```bash
mkdir -p logs data/uploads          # 容器要写的两个目录（缺了 Docker 会建成 root 属主 → 写不进）
docker compose up -d --build        # 一次起全套：中间件 + app（首次要构建镜像，最久）
docker compose ps                   # app 那行要是 healthy（首次最多等 3 分钟：启动预热要加载 BGE-M3）
curl http://localhost:8080/health
# 浏览器打开 http://localhost:8080/ —— 307 → /static/app/，就是那个 Vue 前端
```

- **地址用服务名**：`standalone:19530` / `mysql:3306` / `redis:6379`。同一个 compose 网络里，
  既不需要 `host.docker.internal`，也不经过 1008x/1009x 那些宿主映射。
- **端口有三个角色，别混**：`18080` = 宿主上的**代码后端**（`uv run project-rag-api` 或
  `./scripts/dev.sh serve`，改完源码直接跑、不必重建镜像），`8080` = 容器里的 app（上面这条命令；
  一个进程同时发页面和接口），`5173` = vite 开发服务器（只发页面）。当前谁占着：`./scripts/dev.sh ports`。
  **两个后端可以同时跑**（会话/向量/限流配额都在中间件里，同源不撕裂），代价是模型显存各占一份。
- **只想起中间件**（在宿主上 `uv run` 开发）：`docker compose up -d standalone mysql redis`。
- **⚠️「启动」有三个入口，起的服务集合不一样 —— 前端在不在，先看你在哪个入口**：

  | 入口 | 起了什么 | 前端在 |
  |---|---|---|
  | `./scripts/dev.sh up` | 只起中间件三件（standalone / mysql / redis） | **没有前端** |
  | `docker compose up -d` | compose 里全部服务（含 `app`，也含 Attu 那个 Milvus 管理界面） | `http://localhost:8080/` |
  | `./scripts/dev.sh app` | 中间件 + `app` 容器（等价于只把容器那份补起来） | `http://localhost:8080/`（镜像里的那份） |
  | `./scripts/dev.sh serve` | 什么容器都不起，只跑工作区源码 | `http://localhost:18080/`（你正在改的那份） |

  这就是"有时候启动 docker 会自动起前端、有时候不会"的全部原因 —— 不是随机，是入口不同。
  **谁现在在发前端、发的是哪一份**：`./scripts/dev.sh status`（它会把容器/镜像里的产物摘要与工作区
  的并排打出来）。
- **镜像里的前端是"构建那一刻"的**：`npm run build` 只改工作区，容器照旧发旧产物（于是会出现
  "前端在，但不是我刚改的那份"）。改完 `web/` 要让容器跟上，只有一条路：
  `./scripts/dev.sh app --build`（先构建前端、再重建镜像、再起容器 —— 顺序在契约里钉着）。
- **模型与语料挂卷、不进镜像**：`./models` 与 `./data`。配置里的相对路径是按**项目根**解析的，
  所以容器里的路径与宿主一致（`/app/models/bge-m3`、`/app/data/corpus/...`）。
- **容器用 GPU**：`docker-compose.yaml` 的 app 服务里这两行**已经打开**：
  ```yaml
      gpus: all                        # 把宿主 GPU 交给容器（等价 deploy.resources.reservations.devices）
      environment:
        RAG_EMBEDDING__DEVICE: cuda     # 告诉应用用哪块设备；与上面那行**必须成对**
  ```
  **不用换基础镜像、也不用重建镜像** —— 镜像里的 torch 本来就是 CUDA 版（容器内实测
  `torch 2.14.0+cu130` + 一整套 `nvidia-*`，这也是镜像 11G 的主要来源），缺的只是上面这两件事；
  改完 `docker compose up -d app` 即可（只重建容器）。**这两行必须成对**：只写 `cuda` 不申请 GPU 的
  后果是**预热静默失败 + `/health` 照样绿**，直到第一次真提问才炸（`docs/STATUS.md` §0.3 第 39 条），
  契约 `check_m6_deploy.py`「推理设备与 GPU 预约一致」+ 反向 RT-18 就盯着这个配对。
  实测收益（真容器、真模型）：预热 **18240ms（CPU）→ 7757ms（GPU）**；嵌入 8 条 **CPU 1164ms → GPU 38.2ms**，重排 8 候选 **CPU 527.7ms → GPU 30.7ms**（开 fp16 再快一倍）。
  想开 fp16 用 `embedding.use_f16`（嵌入）与 `embedding.rerank_use_f16`（重排）**两个开关**：
  重排实测 98 → 31ms（3.2×）、嵌入 8 条 22.5 → 9.2ms（2.4×），两个默认都是 `false`。
  ⚠️ **别用自己 shell 里的 `ls /dev/dxg` / `nvidia-smi` 判断"宿主能不能用 GPU"** ——
  这个开发沙箱的 `/dev` 是过滤过的 tmpfs（只有 `null/zero/random/tty/pts…`，**没有 `dxg` 节点**），
  在里面看到的口径一概不算数；容器这条路走 dockerd 的 **nvidia runtime**，不受沙箱影响。
  验 GPU 一律用这条（注意容器里**没有** `nvidia-smi`，`python:slim` 基础镜像不带）：
  ```bash
  docker run --rm --gpus all --entrypoint python project-rag:0.1.0 \
    -c "import torch;print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
  # → True NVIDIA GeForce RTX 5080 Laptop GPU
  ```
  改完再看一眼启动日志：`docker logs rag_platform-app-1 | grep -E "预热|NVIDIA"` ——
  **不该**再出现 `Found no NVIDIA driver`（那条失败是静默的，`/health` 不会变红）。
- 一次性任务用 `run`，不必手写一长串挂载：
  `docker compose run --rm app project-rag ingest --path data/corpus/doc_data`
- 镜像偏大：`uv.lock` 里的 torch 是 CUDA 版，会拖进 `nvidia-*`（本地 `.venv` 实测 6.7G，
  其中 nvidia 3.2G + triton 0.9G）。CPU-only 的瘦身办法写在 `Dockerfile` 末尾注释里，
  **没在真机验证过**，验过再改默认。
- **那个 `en-core-web-sm` 已经不会再让构建/启动随机失败了**（两轮实测 + 根治）：
  它在可选依赖档 `unstructured` 里、运行时用不到，但过去 `uv sync --locked` 仍会去 github 取它的
  **元数据**来校验锁 —— 取一次实测宿主 **172s** / 容器 **186s**，而 uv 默认 HTTP 超时 30s × 3 次重试
  → **46s 就失败**。于是 `docker build` 与**日常 `uv run project-rag-api`** 都随机死在这一步
  （同一天各一次）。根治是**静态声明它的元数据**：那个 wheel 的 METADATA 里只有 `Name`/`Version`
  （纯数据包，没有一条 `Requires-Dist`），所以 `pyproject.toml` 写了 `[tool.uv] dependency-metadata`，
  `uv lock` 又把它带进 `uv.lock` 的 `[manifest]` 段 —— 之后冷缓存 `--locked` 校验**零网络**（实测 0 秒）。
  ⚠️ **但这只对带 `--locked` / `--frozen` / `--offline` 的调用成立**：不带这些开关的**普通 `uv run`**
  在**冷缓存**（`uv cache clean`、换机器、新克隆、容器首次构建）下**仍会去 GitHub 取那个 wheel**，
  实测日志是 `No cache entry for: …/en_core_web_sm-3.8.0-py3-none-any.whl` → 3 次重试 → 超时
  （就是那句 `Failed to generate package metadata`）。所以日常跑命令用 `uv run --frozen …`
  （或 `export UV_FROZEN=1`），要装东西的那一次才需要网络。
  还留着 `UV_HTTP_TIMEOUT=300` 兜底：首次 sync 时 uv 仍要下载一次那个 wheel 做**哈希校验**（12MB，
  进缓存后不再下），这一次可能被慢网络拖死。契约：`check_m6_deploy.py`「静态元数据」+ 反向 RT-19。
  想连这一次都省掉：把 wheel 放进仓库、`[tool.uv.sources]` 指本地文件（12MB 入库的取舍，
  `Dockerfile` 末尾第 1 条另有一条 `uv export` 的路）——**那才是唯一不依赖"某台机器上的缓存"的做法**。
- **国内网络两道坎**（真机踩过，报错原文在 `Dockerfile` 头部）：
  ① **别给 `Dockerfile` 加 `# syntax=`** —— 那会让 BuildKit 先去 registry 拉一个 frontend 镜像，
  它是整个构建的第一个网络请求；② Docker Desktop 的**手工代理**（Settings → Resources → Proxies）
  如果指着本机的 Clash/V2Ray，代理没开时连 registry mirror 都连不上
  （报 `dial tcp 127.0.0.1:7897: connectex: … refused`，**代理对 mirror 一样生效**）——
  要么把代理开起来，要么关掉手工代理、只留 registry mirror。
  现在构建期需要 **docker 镜像源 + pip 源（清华）**；冷缓存首次构建还会为哈希校验下载一次那个 wheel（12MB，见上一条），不碰 `ghcr.io`。

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
| **FAQ** | MySQL 里人工维护的问答对 | BM25 置信度（0~1）≥ `faq.threshold` **且**提问/条目覆盖率都过门槛（默认 0.85 / 0.5 / 0.8） | 一次全表读 + 一次分词匹配，毫秒级 | `faq` / `qa` |
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

### 查询改写：FAQ 未命中之后、向量检索之前那一步（默认关）

`qa` 在 FAQ 未命中、转知识库**之前**可以插一步**查询改写**：先用微调过的 4 分类器
（`chinese-roberta-wwm-ext` + LoRA，见 `tools/lora/`）判断这句话属于哪一类，
再按那一类把用户原话变成**真正拿去检索的句子**：

| 标签 | 策略 | 什么时候用 | 检索什么 |
|---|---|---|---|
| 0 | 直接检索 | 问题标准、术语明确 | 原话，**一个字都不改**（这一类不调模型） |
| 1 | 假设答案 | 问题空泛、缺可检索的实体 | 先编一段假设答案（HyDE），拿它去检索 |
| 2 | 子查询分解 | 一句话里塞了两个独立问题 | 拆成 2~3 个子问题，**各检索一遍再合并** |
| 3 | 去噪改写 | 混了寒暄/情绪/元指令 | 去掉噪声后的干净语句 |

```bash
# 先看它会把一句话判成哪一类、改写成什么（不检索、不生成，只跑这一步）
project-rag rewrite -q "我是刚转行的特别焦虑，麻烦详细说，Python的GIL是什么？"
#   → denoise（去噪改写）0.9935  →  "Python的GIL是什么"

# 真正打开它：config.yaml 里 rewrite.enabled: true（要 multi-agent 的 key 与适配器目录）
```

三条刻意的设计：

1. **默认关**。除"直接检索"外每一类都要**多花一次 LLM 调用**，收益取决于语料 ——
   它是消融开关，与 `rag.rerank` 同一套规矩，开之前先在评估集上量一次。
2. **改写只改检索，不改回答**。生成答案用的仍然是用户原话：拿假设答案的正文当
   prompt 里的「问题」，模型会开始回答那个假设答案，用户看到的是自问自答。
3. **失败一律退回原话**。分类器权重不在版本库里（换台机器就可能没有）、模型可能不吐
   json —— 这些的正确表现是"原样检索 + 日志里留原因"，而不是让一次问答挂掉。
   空 `queries` 尤其危险：那会让检索召空、伪装成「知识库里没有」。

改写耗时在账上单独一格 `rewrite_ms`，且它之后的 `embed_ms`/`retrieve_ms`
**从改写结束算起** —— 否则一次 LLM 改写的耗时会被算进"检索花了多久"，那个数就废了。

### 索引与关键字那一路：两个旋钮

**稠密索引**（`milvus.index.type`）在 `FLAT`（暴力精确）与 `IVF_FLAT`（分簇近似）之间选。
索引是**建集合那一刻定格**的 —— 改配置不会自动重排已有索引，换完要跑一次
`project-rag collections --reindex --yes`（数据不动，只重建索引；重建期间这一路检索不可用）。
实测（真语料 3938 块 / dim=1024 / 30 条加噪查询；读一致性取 `bounded`，否则那 400ms 会盖住索引差异）：

| 规模 | FLAT | IVF_FLAT | recall@5 |
|---|---|---|---|
| 3938 块（真语料） | 5.5ms | 5.5–6.3ms（nlist 32/128/512 × nprobe 1/4/16/128 全试过） | **1.000**（每一档都一样） |
| 20000 块（合成向量） | 14.5ms | **3.8ms**（nlist=256 / nprobe=16） | 未单独量（IVF 是近似的，上评估集再量） |

结论：**几千块这个量级索引换不出差别**（那 5ms 是 pymilvus 往返，不是扫描），所以默认 `FLAT`
（精确、结果可断言）；**块数上万再换** `IVF_FLAT` —— 那时换来的是 3~4 倍。

**关键字那一路**（`keyword.backend`）决定 BM25 的索引放在哪：

| 档 | 索引在哪 | 代价 | 什么时候用 |
|---|---|---|---|
| `memory`（默认） | 进程内 `rank_bm25`：语料从向量库全量读出后本地建 | 每进程约 111MB 常驻 + 冷启动约 2.1s（3927 块实测） | 单进程部署（现在的形态） |
| `milvus` | 入库时编码成 **BM25 稀疏向量**写进集合的 `sparse_vector` 列，检索走 `SPARSE_INVERTED_INDEX` | 集合必须带稀疏列 ⇒ **换档要重灌** | 多 worker / 多副本（每进程一份索引开始真的疼） |

两档的**排序口径一致**（RRF 用排名、weighted 用 min-max 归一，都与分数的绝对量纲无关）。
`milvus` 那档把 idf 与长度归一烘在**文档侧**权重里、查询侧只带「出现过的词（权重 1）」——
于是**查询路径不需要任何语料统计**，稀疏内积恰好等于 BM25 分数（推导与边界都写在
`src/project_rag/adapters/bm25_sparse.py` 的 docstring 里）。

**换档步骤**（`memory` → `milvus`；稀疏列是 schema 级的，老集合补不上）：

```bash
# ① config.yaml：milvus.collection 换个新名字（可回滚），keyword.backend: milvus
# ② 重灌：会按新 schema 建集合，同时写稠密与稀疏向量
uv run project-rag ingest --path data/corpus/<你的语料目录>
# ③ 重启 API —— 同一个 config，它就按新档检索了
```

⚠️ **`keyword.backend` 必须在"第一次写入建集合"之前改好**：schema 建集合那一刻定格，删了重建也一样 ——
没改就会看到 `这个集合没有 sparse_vector 列(建它的时候 keyword.backend 还不是 milvus)`。
反过来，稀疏编码器**只在 `backend: milvus` 那一档才装配**（一个旋钮同时决定 schema / 写入 / 检索三件事），
所以 `memory` 那档的 ingest 不会白算一遍全库统计、也不会去撞那一列。

调过 `keyword.bm25_k1` / `bm25_b` 之后，已有文档的权重还是旧常数烘进去的：
`project-rag sparse --yes` 会按当前语料重算全库的稀疏向量（默认只说不做；稠密向量原样读回写回，
不动嵌入结果）。想换回 `memory`：把 `keyword.backend` 改回去、集合指回原来的那个（数据没动过）。

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
| 两路召回 + 融合 | 稠密（Milvus）+ 关键字（BM25：进程内，或编码成稀疏向量进 Milvus）；RRF 或加权（min-max 归一化） | `application/fusion.py` / `adapters/bm25_index.py` / `adapters/bm25_sparse.py` |
| 索引与关键字后端 | `milvus.index.type`（FLAT / IVF_FLAT + nlist/nprobe）与 `keyword.backend`（memory / milvus）都是配置；换索引一条命令重建，换后端要重灌 | `adapters/milvus_store.py` / `adapters/bm25_sparse.py` |
| 重排 | BGE-Reranker-v2-m3 对**父块正文**精排；可关（消融开关） | `adapters/reranker_bge.py` |
| 引用与可核对 | 每条引用带来源、文档、分数、命中路径（单路/两路） | `domain/models.py` |
| FAQ 缓存 | Redis 短路 + TTL + 装配期/调用期双重降级；兜底答案也缓存 | `adapters/cache_redis.py` / `application/answer_faq.py` |
| 多轮会话 | `conversations` 表 + 最近 5 轮裁剪（写入与裁剪同事务）+ 参数化查询 | `adapters/conversation_mysql.py` |
| 流式帧协议 | `sources` / `token` / `done` 三类帧；**非流式入口就是流式的收拢器**（逐字一致） | `application/streaming.py` |
| HTTP / WebSocket | 27 个业务端点（`/api/v1` 下：问答、流式 WS、会话增删查、来源、认证、能力自述、文档上传/清单/预览/删除、管理报表）+ `/health` 与前端静态入口 | `api/app.py` |
| 认证与角色 | Bearer 令牌（12 小时、库里只存 sha256）、**三种角色**：`employee`（检索问答）/ `ops`（只看报表）/ `admin`（全部 + 管理）；"能干什么"只在 `domain/auth.py` 的三张能力表里，接口各自 `require_knowledge` / `require_reports` / `require_admin`，前端只是"不给点" | `domain/auth.py` / `application/security.py` / `api/auth.py` |
| 账号与可见范围 | 管理员可建号 / 删号 / 改密码 / 建组（一类员工）；**文档可见范围**按"某个人"或"某个组"授权，**没配规则 = 人人可见**（规则只用来收窄）；检索、清单、预览三处同时生效 | `domain/access.py` / `application/access.py` / `adapters/access_mysql.py` |
| 消耗报表 | provider 回报的 token 落进账本（模型 / 输入 / **命中缓存** / 输出四个字段），按 `cost.models` 的**三档 × 两时段**单价折成钱（命中 vs 未命中差 50 倍、高峰 vs 空闲差 2 倍）。**时段 = 北京时间工作日 ∩ 09:00–12:00 / 14:00–18:00 ∩ 非中国法定节假日**（价值表原文；节假日表 `cost.holidays` 每年照国务院通知补一行），账本时间戳**带时区**（容器 UTC / 宿主 CST 都不会判错）；**没配单价 → 报"未配置"**，**没量到 / 无时间戳 / 无时区 / 无日历 / 缓存不明 → 各自单独计数**，都不显示成 0 | `application/cost.py` / `config.yaml` 的 `cost` 段 |
| 文档治理 | 上传（原始 body 流式 → 异步入库）→ 状态轮询 → 预览（父块）→ 删除（连带删向量与原文件，**语料源材料不动**）；知识库清单是**登记表 × 磁盘 × 向量库三处对账**的实时视图 | `application/documents.py` |
| 限流 | Redis 滑动窗口（query 60/min、upload 10/min、login 10/min），429 带 `Retry-After` 与可执行话术；Redis 故障降级放行且只告警一次 | `adapters/ratelimit_redis.py` |
| 前端 | Vue 3 + TS 单页应用（对话 / 知识库 / 报表 / 账号与权限四页，hash 路由）；**能力接口与 `/auth/me` 是唯一的能力来源**，产物入库所以 Python 侧与镜像都不需要 Node | `web/` |
| 桌面壳 | `project-rag-desktop` 一个窗口套在这份产物上；本地模式用回环 HTTP 托管随包产物、`--remote` 直接开后端那份界面 | `src/project_rag/desktop.py` |
| 延迟观测 | 一次回答一行账：检索六段 + TTFT + 吐字速率 + **思考三字段** | `observability/latency.py` |
| 评估闭环 | 评估集 → 跑本系统产出答案与上下文 → RAGAS 四指标落盘（带元数据，从不覆盖历史） | `scripts/tools/`（作者的离线工具，**不随仓库发布**；要复现得自备语料） |
| 验收体系 | 每个能力配一份可执行契约，PASS/FAIL/SKIP 三态；另有反向测试证明检查真的会红 | `scripts/acceptance/`（**在仓库里**，克隆下来就能跑） |

### 界面与角色（一份 Vue 单页应用）

| 页 | 谁能看 | 做什么 |
|---|---|---|
| **对话** | `employee` / `admin` | 提问（WS 逐字流式）、引用与页码、左侧会话目录、右侧检索参数面板（知识库 / 检索方式 / 两路权重 / 重排 / 模型） |
| **知识库** | `admin` | 上传（后缀与大小上限来自 `capabilities`）→ 状态轮询 → 预览父块 → 两步确认删除 → **权限**（这份文档给某个人 / 某一类人看）；每张卡片显示**对账后的 `state`**（一致 / 文件已变更 / 库内没有块 / 文件已不在 / 未入库），对不上的写明该怎么办；顶部一句话汇总 + 「上次刷新」时间 |
| **报表** | `ops` / `admin` | 延迟 + **消耗**：六个 KPI（**比率都带分子分母**）、分段 p50/p95/p99（样本不够画 `—`，**不折算成 0**）、最慢榜（标冷启动）、五个时间窗口；消耗区给出模型调用次数、输入（含命中缓存）/输出 token 与金额，按**模型 × 时段**分行，并写出**时段口径本身**（哪几天、哪几个钟点是高峰，来自接口而不是界面写死）（未配单价 / 没量到 token / 无时间戳 / 无时区 / 无日历 / 缓存不明都如实说） |
| **账号与权限** | `admin` | 建号（员工 / 运维 / 管理员）、改密码、删号（**不能删自己、不能删最后一个管理员**）、建组并维护成员；文档给谁看在「知识库」页每张卡片的「权限」按钮上 |

三条与后端对齐的规矩：**能力不写死在前端**（后缀表 / 检索方式 / 上限 / 模型名一律来自
`GET /api/v1/capabilities`；"哪个角色能看哪一页"来自 `GET /api/v1/auth/me` 报的三个能力布尔值，
前端不比较角色字符串）；**页面唯一的环境就是"一个后端地址"**（左上角可改、会被记住 —— 桌面壳
与"连另一台后端"都靠它）；**界面藏起来的不等于权限**（每个接口在后端各自
`require_knowledge` / `require_reports` / `require_admin`，契约里有一条全表扫描盯着）。

### 慢在哪：一行日志

```bash
grep -h " | latency " logs/app.log | tail -20
# label=rag channel=rag thinking=off faq_ms=- rewrite_ms=- sources_ms=6308.4 retrieve_ms=6308.4 embed_ms=176.4 \
#   vector_ms=112.8 bm25_ms=1095.5 fuse_ms=0.0 collapse_ms=0.0 rerank_ms=4923.6 \
#   llm_think_ms=- llm_ttft_ms=422.7 think_tokens=0 ttft_ms=6731.2 tokens=54 gen_ms=345.4 \
#   tps=156.4 total_ms=7077.9 refused=false truncated=false aborted=false
```

> **这一行里的 `bm25_ms` 已经是"旧数字"了**（P0-1，）：BM25 那一路原本**每查询**都向
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
├── api/             FastAPI 接口层 + 前端产物（static/app/ 由 web/ 构建进来）
└── desktop.py       桌面壳（只开窗：本地回环托管产物，或 --remote 直接开后端那份）
config/config.yaml   全部可调项（检索旋钮、模型路径、设备、思考档位、阈值、表名）
web/                 前端工程（Vite+Vue3+TS）：`npm run build` 把产物写进 src/project_rag/api/static/app/
docker-compose.yaml  中间件：Milvus(etcd+minio) / MySQL / Redis / Attu ＋ `app`(服务本体)
Dockerfile           服务本体的镜像（中间件不进它）
API.md               HTTP / WebSocket / CLI 接口文档
```

### 已知边界

- **还没接的格式**：HTML / Excel / 扫描件 OCR，以及页码、工作表这类元数据到引用清单的透传；
  真要做时先 `uv sync --extra unstructured`（那一档默认不装，见 §1）。
- **镜像已在真机构建并跑起来过**（之前"开发环境里没有 `docker` 命令"那条已作废）：
  `docker compose up -d --build` 构建成功、app 变 healthy、宿主 8080 上打开的就是仓库里这份
  Vue 前端（容器内预热 7649ms、`/static/app/` 200、旧调试页 404）。
  **体积 ~11G 的成因已量清**：venv 7.0G 里 `nvidia` 3.2G + `torch` 1.2G + `triton` 0.9G = 5.3G 是 CUDA 相关；
  想要瘦身版用 `docker build --build-arg TORCH_BACKEND=cpu`（代价：容器里不能再用 GPU；
  ⚠️ 这条路本机**没验成** —— 它要从 `download.pytorch.org` 取 torch/triton，实测卡住 15 分钟无进展，
  换到能连官方源的环境再试，见 `Dockerfile` 的 `ARG TORCH_BACKEND` 那段）。
  桌面壳的**可验证那一半**（产物与引用的资源、回环服务真能发出页面与 JS、`?backend=` 被记住并抹掉）
  有契约与浏览器实测钉着；**开窗那一帧**要图形界面、**打包成可执行文件**（PyInstaller / AppImage / .dmg）
  要额外的打包链路，这两件目前都没验过（如实写在这里，别读成"已经能用"）。
- 意图识别 / 检索策略选择、粗排后的多路并行、观测聚合（p50/p95）等未做 ——
  「暂不做」的条目与各自的**触发条件**记在维护者本地的 `docs/deferred.md`（不随仓库发布）。
- 当前状态、验收基线、每条踩过的坑同样记在维护者本地的 `docs/STATUS.md`；
  仓库里对应的是**可执行的证据**：`scripts/acceptance/` 的 30 套契约 + 21 份反向 runner，
  以及 `CHANGELOG.md`。

---

## 许可证

**MIT**，见 [`LICENSE`](LICENSE) —— 可以自由使用、修改、分发，保留版权声明即可。
与许可有关的三件事写在这里，免得误会：

- **模型权重有各自的许可**：`BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3` 由 ModelScope /
  HuggingFace 提供，使用时请自行确认它们的条款；**本项目不分发权重**（`models/` 不在仓库里）。
- **语料不在仓库里**：`data/` 是语料与评估集，有**第三方版权**（见下）。
- 依赖的许可见 `uv.lock` 与 `web/package-lock.json`（前端依赖只用于构建，产物已入库）。

## 仓库里有什么、没有什么

| | 内容 |
|---|---|
| **在仓库里** | 后端源码 `src/`、前端源码 `web/src/` 与两个锁文件、**前端构建产物** `src/project_rag/api/static/app/`、配置 `config/`、`Dockerfile` / `docker-compose.yaml`、`API.md`、`CHANGELOG.md`、演示语料 `demo/`、验收脚本 `scripts/acceptance/` |
| **不在仓库里** | `docs/`（维护者的决策与状态文档）、`data/`（语料与评估集：第三方版权）、`models/`（4.4G 权重，按上面第一节那一行命令下载）、`.env`（密钥 —— 用 `.env.example` 复制一份填）、`logs/`、`.cache/`、`web/node_modules/`（`cd web && npm ci` 还原）、`scripts/tools/`（作者的**离线工具**：造评估集 / 灌 FAQ / 下模型 / 跑 RAGAS / A-B 读数。它们跑在 `data/` 与本地表上，所以**不随仓库发布** —— 下面提到 `scripts/tools/xxx.py` 的命令，公开 clone 上跑不了；README 里凡是能公开复现的步骤都给了等价的内置命令） |

后两条是**刻意的**，不是忘了：

1. **前端产物反而要入库**：Python 侧与 Docker 镜像都不装 node，`vite build` 的结果就得提交进来；
   代价是"改了前端必须重新构建"，所以有一条契约盯着「产物与源码同源」——
   判据是**内容摘要**而不是 mtime（`scripts/acceptance/check_m7_frontend.py` 的第 1 条契约）。
2. **`web/node_modules` 不入库**：它是 `npm ci` 的产物（和 `.venv` 一个性质），而且里面装着
   **平台相关的二进制**（esbuild / rollup 的 linux-x64 构建）。真值来源是
   `web/package.json` + `web/package-lock.json` —— 有这两个，任何机器都能还原出一样的依赖树。
   （这段是 开源化时补的：它曾经比 `.gitignore` 那条规则更早进仓库 —— 而**忽略规则不会自动
   取消已跟踪的文件**，所以一度真的躺在版本库里；后来连同其它不公开路径一起从提交历史里
   抹掉了，现在 `.git/` 里也不再有它。）
