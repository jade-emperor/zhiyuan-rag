# 演示：5 分钟看到界面与一次问答（**不需要下载 4.4G 模型**）

这里的语料是**为演示自写的面试八股**（Python / 数据库 / Linux / 提示词与 Agent / RAG /
Transformer 与微调 / 部署，共 37 道题与自写答案），不涉及任何真实机构、课程或版权材料。
它和作者本机的真实语料（`data/corpus/bagu_ai_data`、`data/corpus/bagu_java_data`、
`data/corpus/pdf_data`）**结构一致**（`### N. 问题` + 参考答案），所以演示跑通的那条链，
换成真实语料是同一套流程 —— 只是真实语料不进仓库（版权）。

它的唯一作用是让「入库 → 检索 → 带引用的回答」这条链在**没有 BGE-M3 模型**的机器上也能跑通。

> ⚠️ 本文里凡是 `scripts/tools/xxx.py` 的命令都是**作者本机的离线工具**，它们跑在 `data/` 与本地表上，**不随仓库发布**（见 `.gitignore`）——
> 公开 clone 上敲不了。演示本身不依赖它们：下面「三步」用的全是仓库里的命令。

## 三步（下面每条命令都实测过）

```bash
./scripts/dev.sh up            # ① 起 Milvus / MySQL / Redis（已经起着就跳过）
./scripts/dev.sh demo          # ② 灌 33 条演示 FAQ + 6 篇语料（哈希嵌入，零模型）
./scripts/dev.sh demo-serve    # ③ 起服务 → 浏览器打开 http://127.0.0.1:18080/
```

登录账号：用你自己 `project-rag user add` 建的账号（演示数据不涉及账号）。

### 问这两句，分别演示两条通道

| 问题 | 走哪条路 | 期望 |
|---|---|---|
| `深拷贝与浅拷贝的区别?` | **FAQ 直答**（标准答案，**不调 LLM**，毫秒级） | `浅拷贝只复制最外层容器…`（与 `demo/faq.csv` 里那行逐字对应） |
| `混合检索与 RRF 融合是怎么回事` | **知识库**（向量+BM25 融合 → LLM，答案带 `[1]` 引用） | 提到 RRF 只看排名、`1/(k+rank)`、k 常取 60…`[1]`，引用指向 `04-RAG与向量检索.md` |

> 为什么 FAQ 那问要**照抄原句**：FAQ 通道的阈值是 `faq.threshold`（代码默认 `0.85`、
> `config.yaml` 里现在 `0.95`，演示吃的也是这个值）—— 近乎一字不差才命中，换一种说法会落到
> 知识库通道，这是刻意的分流，不是失灵。
> 演示 CSV **故意**没有收录 `混合检索与 RRF 融合是怎么回事?` 这一条：那句留给知识库通道，
> 否则它会象刚才那样被 FAQ 直答截胡（实测踩过一次：`channel=faq score=1.0000`，没走检索）。

命令行版（不用开浏览器，也不需登录）：

```bash
# FAQ 通道（不花钱）
RAG_EMBEDDING__BACKEND=hashing RAG_MILVUS__COLLECTION=rag_demo RAG_RAG__RERANK=false \
  uv run project-rag faq -q "深拷贝与浅拷贝的区别?"

# RAG 通道（会真调一次 LLM，花几分钱）
RAG_EMBEDDING__BACKEND=hashing RAG_MILVUS__COLLECTION=rag_demo RAG_RAG__RERANK=false \
  uv run project-rag qa -q "混合检索与 RRF 融合是怎么回事"
```

## 用你**自己的**语料跑同一套演示

演示模式的两个开关（集合名、哈希嵌入）与语料无关，所以换成真语料只改 `--path`：

```bash
# 真实语料 + 真实嵌入（需要 4.4G 模型），进独立集合，不碰你的 rag_chunks
RAG_MILVUS__COLLECTION=rag_bagu uv run project-rag ingest --path data/corpus/bagu_ai_data
RAG_MILVUS__COLLECTION=rag_bagu uv run project-rag qa -q "装饰器是什么"
```

本机现有的真实问答对（八股 120 条 + PDF 教材 80 条，其中 150 条已进 `faq` 表）与
评估集在 `data/eval/qa_bagu_pdf_v1.json`、`data/eval/eval_set_bagu_pdf_v1.json`，
生成方式见 `scripts/tools/build_qa_dataset.py` 的头部说明。

## 演示模式只差三个环境变量

| 变量 | 演示值 | 为什么 |
|---|---|---|
| `RAG_EMBEDDING__BACKEND` | `hashing` | 用字符 bigram 哈希代替 BGE-M3：零模型、确定。**它不是语义模型**，只按字面相似度检索（启动日志里必有一条 WARNING 说明这件事） |
| `RAG_MILVUS__COLLECTION` | `rag_demo` | 独立集合，**不碰**你真实的 `rag_chunks`（两者维度不同：哈希 256 / BGE-M3 1024） |
| `RAG_RAG__RERANK` | `false` | 不加载 2.2G 的重排模型 |

产品代码里**没有"演示分支"**：只有 `embedding.backend` 这一个显式开关，默认值被契约钉在 `bge_m3`
（见 `check_m3_rag` 的「演示后端必须显式开启」+ 反向 RT-6）—— 因为"没模型就悄悄退回哈希"
会让检索看起来正常、实际全是噪声，而那种坏法**不报错**。

## 它在你的库里留下了什么、怎么清

| 落在哪 | 留下什么 | 清理 |
|---|---|---|
| Milvus | 新集合 `rag_demo`（实际块数看 `project-rag collections`） | `uv run project-rag drop --collection rag_demo --yes` |
| MySQL `faq` 表（**共享**） | 新增 33 条演示问答 | 用真实语料那份 CSV 加 `--prune` 撤回：<br>`uv run python scripts/tools/load_faq_csv.py --csv data/faq/qa_bagu_pdf_faq.csv --prune`（把 faq 表拉回真实语料的 150 条） |
| MySQL `documents` / 账本表 | 6 条文档登记行 + 若干延迟账本行 | 不提供一键清理：它们是"这台机器跑过什么"的记录（在开发库里，无副作用）；要整库重来见 `scripts/tools/reset_data.py` |
| 磁盘 | `data/uploads/` 下的 6 份演示文档副本 | 直接删文件即可 |

## 想要真语义检索

演示语料本身就是好材料：把模型准备好、开关设回去就行（不用重新切分，但要**重新入库**，
因为维度从 256 变成 1024，得换个集合）：

```bash
uv run python scripts/tools/fetch_models.py --with-reranker      # 4.4G，需网络
RAG_MILVUS__COLLECTION=rag_demo_bge uv run project-rag ingest --path demo/corpus
RAG_MILVUS__COLLECTION=rag_demo_bge uv run project-rag qa -q "为什么要除以根号 d"
```

这时候改写问法（"为啥要除以根号 d" vs 语料里的"为什么要除以根号 d"）才应该召回得到 ——
哈希嵌入下这类改写**不保证**命中，那正是"它不是语义模型"的意思。
