# `scripts/` 是干什么的

> **一句话**：`acceptance/` 是**契约**（规格的唯一权威），`tools/` 是**离线工具**（造数据、装模型、跑评估）。

| 目录 | 放什么 | 判断标准 |
|---|---|---|
| `acceptance/` | `check_m*.py`、`calibrate_threshold.py`、`run_all.py`（分组跑全部） | 它回答"**应该是什么样**"。跑一次就知道当前代码离契约有多远 |
| `tools/` | 造评估集、灌 FAQ、下模型、跑 RAGAS、比多次运行 | 它回答"**把数据变成什么样**"。一次性或按需重复，产出落在 `data/` |

> ⚠️ **`tools/` 整目录不公开**（见 `.gitignore`）：里面基本是一次性的个人工具，
> 命令只有这台机器上跑得通。**公开 clone 上下面提到 `scripts/tools/*.py` 的命令你敲不了** ——
> 那几条验收也会相应变成 `SKIP` 并点名 owner（不会红）。恢复某个工具就往 `.gitignore`
> 里单独加一条 `!/scripts/tools/<名字>`。

**先记三个入口**（一条命令，别背下面那些长命令）：

```bash
./scripts/dev.sh            # 列出所有短命令
./scripts/dev.sh ci         # 零容器零网络：pyright + 28 套契约 + 28 份反向（CI 跑的就是这条）
./scripts/dev.sh all        # 全套：37 套契约 + 28 份反向（要中间件 + 语料 + key）
```

> **这几个数同时写在五个地方**：这里、`scripts/dev.sh` 的用法注释、
> `.github/workflows/ci.yml`（顶部注释 + 那个 step 的 name）、`CONTRIBUTING.md` 第三节、
> 以及维护者本地的状态文档。**它们已经漂过好几次**（17/6 → 26/17 → 28/21 → 现在），
> 因为 `run_all.py` 是**动态扫目录**的、这些注释是**手抄**的 —— 后者才是会错的那个。
> 加/删一份 `check_*.py` 之后，用 `ls scripts/acceptance/check_*.py | wc -l` 和
> `ls scripts/acceptance/reverse_*.py | wc -l` 数一遍再改这五处（`--set ci` 的数 =
> 总数 − `run_all.py` 里 `SERVICES` 与 `LOCAL_ONLY` 那两份名单，别自己减）。

**「启动」有四个入口，前端在不在取决于你走哪个**（踩过一次"有时候起前端、有时候不起"）：

```bash
./scripts/dev.sh up         # 只起中间件三件（standalone / mysql / redis）—— 没有前端
./scripts/dev.sh app        # 起容器那份（8080，一个进程同时发页面与接口）
./scripts/dev.sh app --build# 同上，但先构建前端再重建镜像（改了 web/ 之后必须走这条）
./scripts/dev.sh serve      # 只起代码后端（18080，跑工作区源码，零镜像，页面也是工作区那份）
./scripts/dev.sh status     # 谁在发前端、发的是哪一份（对比容器/镜像与工作区的产物摘要）
```

**为什么需要 `status`**：镜像里的前端产物是**构建那一刻**的，`npm run build` 只改工作区 ——
于是"前端在"也可能"不是你改的那份"。`status` 把两份摘要并排打出来（读的是
`src/project_rag/api/static/app/source-digest.txt`，与 `check_m7_frontend` 同一个判据）。

`run_all.py` 把"每套脚本需要什么外部条件"**变成了可执行的清单**（`--set ci|services|local|all`，
分组依据是**一次实测探测**：把中间件端口指向 1、模型目录指向不存在、key 换成假值，
再跑全部 37 套看谁 PASS/FAIL/SKIP）。**为什么不用 Makefile**：这台开发机与维护者的 WSL
都没装 `make` —— 跑不起来的入口比没有入口更糟。CI 里是等价的 uv/python 命令，
三边（本地 / `dev.sh` / CI）指向同一份清单，没有第二处定义。

> 说明：`scripts/` 是**公开**的（契约与反向测试可验证性的一部分），而 `docs/` **不随仓库发布** ——
> 维护者的本地文档（`docs/`，**不随仓库发布**）
> 对克隆者不可见、也不影响运行。要结论时看脚本自己的 docstring 与 `--help`。

**两份公共模块（抽的）** —— 每个 `check_*.py` 只剩下面这几行壳子：

```python
from _harness import check, report          # 三态 / @check 装饰器 / env / 报告打印
from _fakes import FakeEmbedder, FakeLLM    # 端口的内存替身
```

| 模块 | 放什么 | 不放什么 |
|---|---|---|
| `_harness.py` | `PASS/FAIL/SKIP`、`SkipCheck`、`check` 装饰器、`env`、`report`、**反向测试改源码的三件套**（`patched_source` / `restore_stale_patches` / `drop_bytecode`） | 具体假件；任何"只有一个脚本用"的东西 |
| `_fakes.py` | 8 个共享假件（embedder / faq_store / matcher / keyword_index / reranker / conversation_store / vector_store / llm） | **刻意残缺的假件留在脚本里**（例如 `check_m5_qa` 的假向量库只有 `search` —— "越权调 `upsert` 就 AttributeError"本身就是那条检查的证据）；`FakeLLM`（调用即发生）与 `PacedFakeLLM`（生成器，帧在被迭代时才产出）**不能合并**，延迟观测靠这个差别钉"没有预取" |

**反向测试的两条安全规矩**（都是事故换来的）：

1. **改源码必须用 `with patched_source(path, old, new):`** —— 它先落备份（`.cache/reverse_backup/`，**镜像源码路径**）、退出时还原，**启动时 `restore_stale_patches()` 会把上次被打断留下的坏源码自愈回来**。不用它的 runner 在"改坏 → 跑检查 → 还原"那个窗口里被 SIGTERM/SIGPIPE 杀掉，`src/**` 就会**留在坏版本上**（语法没错、能 import、只是行为变了）。
2. **别把反向测试的输出管进 `head`** —— `head` 读够行数就关管道，Python 收到 SIGPIPE 当场死，正好死在窗口里（真踩过）。要看短的输出就 `> .cache/xxx.log` 再 `grep`。每个用例都要先核对**锚点恰好出现 1 次**并把它算成失败：锚点漂了的那条反向测试是「安静地没跑」，而它看起来只是报告里的一句「1 条没红」。
3. **改了产品代码就重跑那一块的反向 runner** —— 锚点是逐字匹配的，重构（改名、把一段逻辑抽成函数、给函数体加两行）会让锚点**悄悄失效**；runner 会把它报成「锚点出现 0 次」并**计为失败**（设计如此：改坏窗口里最危险的不是红，而是「看起来跑过了」）。
4. **同一时刻只跑一份改源码的 runner** —— 反向测试改的是**同一份源码**，两个 runner 并行时「读原件 → 改坏 → 还原」会互相踩：B 在 A 的坏窗口里读到了坏内容，还原时把**坏版本**写回去，于是 `src/**` 永久留在坏版本上（而备份目录也被对方清掉了，`restore_stale_patches()` 无从还原）。真踩过：`--set ci`（里面 20 份 runner 串行改源码）与手工跑的 `reverse_m7_2_live_kb.py` 同时进行，`application/documents.py` 里留下一句 `if False:  # 反向测试` —— 症状是下一轮**一批**检查莫名其妙地红，而且看着像产品代码坏了。回归要跑就独占跑；要并行就只并行**不碰源码**的那些。**这条被踩过三次** —— 最近一次是 FAQ 那轮：`--set ci` 在跑的同时手工跑了一个反向 runner，两个 runner 共用 `.cache/reverse_backup/`，`restore_stale_patches()` 互相把对方的坏版本还原回去，结果 `reverse_m7_4_security.py` 有一条用例没红（单跑立刻全红）。症状永远是「一批看起来不相干的检查莫名地红 / 不红」。

抽之前（当时 13 份脚本）：同一段壳子各抄一遍（≈687 行）、`_FakeLLM` 有 5 个版本、`_FakeConversationStore`
有 3 个版本；抽之后那 13 份合计 **12562 → 11326 行**。**这几个数字不再维护**（现在 37 份检查 + 28 份反向），
要的是那条结论：壳子只有一处，改一次口径只改一处。

> **评估的范围（定）**：只跑 RAGAS 四指标
> （`faithfulness` / `answer_relevancy` / `context_precision` / `context_recall`）。
> **一轮 A/B = 改一次配置 + 跑一次**（112 条约 15 分钟 + 一次 judge 费用），
> **不为"量噪声"重复跑** —— 噪声只量过一次（`bagu_v1` 的 `mean_of_four` 极差 0.0090），
> 当**读数口径**用：**差 <0.01 按打平读**。
> `scripts/tools/compare_runs.py` 还留着（只读已有 CSV，不花钱），但不再是流程的一环。

## 评估流水线（四步，**顺序不可颠倒**）

```bash
# ① 造评估集（确定性，可反复跑；改规则才需要重跑）
uv run python scripts/tools/build_eval_set.py
#   → data/eval/eval_set_bagu_v1.json          {meta, rows[112]}，题目 + ground_truth

# ② 跑本系统产出 answer + contexts（这一步最慢，CPU 上 ~20 分钟）
uv run python scripts/tools/build_eval_dataset.py \
      --in data/eval/eval_set_bagu_v1.json --name bagu_v1
#   → data/eval/datasets/bagu_v1_dataset.json  ragas 四字段（**数据集按检索旋钮命名**）
#   → data/eval/datasets/bagu_v1_raw.json      排障用：citations / 拒答 / 耗时 + 旋钮 provenance

# ③ 跑四指标并落盘（112 条约 12~19 分钟；30 条约 3 分钟）
uv run python scripts/tools/run_ragas_eval.py \
      --dataset data/eval/datasets/bagu_v1_dataset.json --tag bagu_v1_k5m2
#   → data/eval/ragas_runs/run_<时间戳>_<tag>.{summary,rows,groups}.csv

# 验收（规格在这里；改评估口径先改它，再改实现）
uv run python scripts/acceptance/check_m4_eval.py
```

### 之后：真实数据的问答对与评估集

上面那套（`bagu_v1`，112 条、只有八股）是历史读数，仍然成立。新一轮换成了
**八股（`###` 标题→问题、正文→答案，确定性、零成本）+ PDF 教材（DeepSeek 逐块生成，
带 `doc_id` + 页码）**共 200 条，按行拆成「150 条进 `faq` 表做演示直答 / 50 条做 RAGAS 评估」：

```bash
# 造问答对（八股零成本；PDF 那半要 --yes，约 ¥0.3，实测 88 次调用）
uv run python scripts/tools/build_qa_dataset.py --dry-run     # 先看抽样
uv run python scripts/tools/build_qa_dataset.py --yes         # 生成并逐条校验溯源
#   → data/eval/qa_bagu_pdf_v1.json        200 条（含 bucket / 溯源 / 生成器）
#   → data/faq/qa_bagu_pdf_faq.csv         150 条（进 faq 表）
#   → data/eval/eval_set_bagu_pdf_v1.json  50 条（评估集）

# 灌 FAQ + 跑评估（②③ 与上面同一条流水线，只换文件名）
uv run python scripts/tools/load_faq_csv.py --csv data/faq/qa_bagu_pdf_faq.csv --prune
uv run python scripts/tools/build_eval_dataset.py \
      --in data/eval/eval_set_bagu_pdf_v1.json --name bagu_pdf_v1
uv run python scripts/tools/run_ragas_eval.py \
      --dataset data/eval/datasets/bagu_pdf_v1_dataset.json --tag bagu_pdf_v1_k5m2
```

**评估题绝不能也进 `faq` 表**：FAQ 命中是直答（阈值 0.85、近乎一字不差），
评估题一旦在表里，检索与生成全程不参与，指标再高也评的不是检索系统。
这条有契约（`check_m4_eval` 的「评估题不得与 faq 表那份 CSV 近似」）+ 反向
（`reverse_m4_eval.py` 的 RT-1）。

数据换代（清掉与本项目无关的旧语料/FAQ/会话账本，**先备份再删**）：

```bash
uv run python scripts/tools/reset_data.py          # dry-run：打印要删什么
uv run python scripts/tools/reset_data.py --yes    # 备份（mysqldump + 待删块 JSON）→ 删 → 自校验
```

**为什么顺序不能颠倒**：

- ② 的 `contexts` 必须来自**本系统**（原脚本只读一份别人算好的 JSON，等于评别人的系统）；
  所以它必须跑在 ① 之后、③ 之前。
- ③ 的元数据（`rag_k` / `rag_candidate_m` / `fusion` / `rerank`）取自 ② 留下的 `raw.json`，
  **不是**读实时配置 —— 否则"改了配置再跑评估"会把旧数据标成新旋钮（实测踩过）。
- 跳过 ② 直接改 `*_dataset.json` 手工造数据 = 评估结果再也说不清来源（③ 会记指纹，但指纹只证明"文件没变"）。

## 合流路由与对话历史

```bash
uv run python scripts/acceptance/check_m5_qa.py          # 33 通过 / 0 失败 / 0 跳过（6.1 路由）
uv run python scripts/acceptance/check_m5_history.py     # 40 通过 / 0 失败 / 0 跳过（6.2 会话历史 + 建表排序规则口径 + 引用快照）
uv run python scripts/acceptance/check_m5_stream.py      # 37 通过 / 0 失败 / 0 跳过（6.2：截断可见 + 流式入口 + 思考开关/思考与正文分家）
uv run python scripts/acceptance/check_m5_api.py         # 26 通过 / 0 失败 / 0 跳过（6.3：最早那九条路由 + 命名空间 + 事件循环不被占住 + 起服务姿势，无需容器）
uv run python scripts/acceptance/reverse_m5_api.py       # 反向测试：3 条全红（漏带 /api/v1 前缀的新端点 / 静态挂载漏掉 html=True / 删掉路由装饰器）
uv run python scripts/acceptance/check_m5_5_latency.py   # 30 通过 / 0 失败 / 0 跳过（TTFT/tps/检索六段/两路召回并行/思考三字段/改写一段，不联网、无需容器）
uv run python scripts/acceptance/reverse_m5_5_latency.py # 反向测试：4 条全红（一次请求只有一条账 / 帧数只数一遍 / 两路又串起来 / 分段退回打点之差）
uv run python scripts/acceptance/reverse_m5_8.py         # 反向测试：7 条全红（临时改 src/，finally 里还原；--only RT-1 可单选）
#   ⚠️ check_m5_stream 里有 3 次极小真调（各一个问题、上限 16/32 token）：钉「provider 把
#      finish_reason 放在哪个字段」「thinking=off 真的不思考（0 个 reasoning 帧）」以及
#      「SDK 有没有把思考也塞进 chunk.content」—— 都是 provider 行为,假件证明不了。
#      不想花钱就把 .env 临时挪开（这几条会转 SKIP）。
uv run python -m project_rag qa -q "退款流程怎么走？"      # FAQ 命中 → 直返标准答案
uv run python -m project_rag qa -q "装饰器有什么用？"      # 未命中 → 知识库通道 + 引用清单
uv run python -m project_rag qa -q "它有什么用？" --session u1   # 带会话：历史进提示词 + 这一轮入库
uv run python scripts/tools/init_conversation.py         # 会话表建一次（幂等；首次用 --session 前跑）
RAG_EMBEDDING__DEVICE=cpu uv run python scripts/acceptance/check_m5_qa.py   # 33/0/0（沙箱里也能跑满）
```

**权威 = `check_m5_qa.py` 的 docstring**：`QaAnswer` 的不变式、路由规则（含阈值边界与拒答）、
「命中判定只有一处」怎么被钉住、故障语义、`qa` 的输出契约，以及
**「知识库那半边为什么是零参函数」**（函数参数先求值 → FAQ 命中的问题也会加载重模型）。

**权威 = `check_m5_history.py` 的 docstring**：端口的三个动作与承诺（时间正序 / 裁剪与写入同事务）、
`Session` 的不变式、**哪些时候读历史、哪些时候写、为什么拒答也写**、
以及**四处刻意的偏离**（排序用 `id`、列名 `created_at`、裁剪的派生表、表名白名单）。

**权威 = `check_m5_stream.py` 的 docstring**：`LlmDelta` 的语义、**帧协议**（`sources`/`token`/`done` 与那条恒等式）、
**适配器对 provider 元数据的三条实测约定**
（`finish_reason` 在 `response_metadata` 里 / 收尾后的空帧要丢 / `content=None` 不能变 `"None"`）、
「截断保留半截 + 只加提示」的口径、以及 `[注意]` 这个用户提示标记（**断言必须认它**，只搜「截断」
会被用例层的日志满足 —— 那条假检查是反向测试揪出来的）。

设计理由写在各脚本的 docstring 里。

## 查询改写（FAQ 未命中 → 向量检索之间那一步，默认关）

```bash
uv run python scripts/acceptance/check_m8_rewrite.py     # 36 通过 / 0 失败 / 0 跳过（零容器零模型零网络）

# 真模型那条路要 GPU + 适配器目录（logs/ 不在版本库里）:
docker run --rm --gpus all -v "$PWD":/work -w /work --entrypoint python \
  -e RAG_REWRITE__ENABLED=true -e RAG_REWRITE__DEVICE=cuda \
  project-rag:0.1.0 -m project_rag rewrite -q "我是刚转行的特别焦虑，麻烦详细说，Python的GIL是什么？"
#   → denoise（去噪改写）0.9935 → 「Python的GIL是什么」
```

**权威 = `check_m8_rewrite.py` 的 docstring**：两条不变式（`queries` 永不为空、
`direct ⟺ queries == (original,)`）、四种策略各自调不调模型、**五种坏法**都退回原话、
`retrieve_many` 的单句等价与按 `chunk_id` 去重合并、**「改写只改检索、不改回答」**、
账本 `rewrite_ms` 与 `embed_ms`/`retrieve_ms` 的起点口径、三条分层静态扫描。

设计理由与真机读数；判定口径的已知冲突（"宽泛领域词 + 有哪些"
算 0 类还是 1 类）也记在那里 —— **没动数据**。

### 改写的 A/B：开与不开，四指标 + 代价

**一条命令跑完四步**（builder×2 + RAGAS×2），两臂之间**只有改写这一个变量**：

```bash
bash scripts/tools/eval_rewrite_ab.sh all      # 全部（约 1 小时；也可以分步跑）
bash scripts/tools/eval_rewrite_ab.sh report   # 只出对比表，不花钱
```

| 步骤 | 命令 | 产物 |
|---|---|---|
| ① 造评估集 | `build_eval_set_mix.py [--generate-vague]` | `data/eval/eval_set_mix_v1.json`（4 类 × 30 条） |
| ② 两臂建数据集 | `build_eval_dataset.py --name mix_v1_{norw,rw} [--rewrite]` | `datasets/mix_v1_{norw,rw}_{dataset,raw}.json` |
| ③ 两臂评估 | `run_ragas_eval.py --tag mix_v1_{norw,rw}` | `ragas_runs/run_*_mix_v1_{norw,rw}.{summary,rows,groups}.csv` |
| ④ 对比 | `compare_rewrite_ab.py` | `data/eval/rewrite_ab_report.md` |

**两臂的差别就是 `RAG_REWRITE__ENABLED` 与 `--rewrite`**；检索旋钮、语料、模型、提示词、
评估集全部相同 —— 报告里「差的是改写」这句话靠这一点成立。

**评估集为什么按"四类问法"造**：``direct`` / ``vague`` / ``subquery`` / ``denoise``
分别对应四个改写策略，一份评估集就能把四路都测到；四类抽的是**互不相交**的小节，
参考答案**全部是知识库原文**（只有 "假设答案" 那一类的**问题**由 DeepSeek 生成，落盘冻结）。

**验收**：`check_m4_rewrite_ab.py`（LOCAL_ONLY，产物在 `data/` 下）+
`reverse_m4_rewrite_ab.py`（5 条反向：两臂标志、基线不许有改写字段、评估集确定性、
报告数字对账、适配器溯源）。报告里每个数都能从两份 `rows.csv` 重算出来 ——
**手抄一个好看的数进去会当场红**。

## 公开面边界（仓库里只留外人读得懂的东西）

```bash
uv run python scripts/acceptance/check_public_surface.py       # 2 通过 / 0 失败 / 0 跳过（只要有 git）
uv run python scripts/acceptance/reverse_public_surface.py     # 反向测试：3 条全红（往仓库里塞回台账指针 / 决策者口吻 / 裸 docs 路径）
```

**它守的是一条边界，不是功能**：会公开的仓库文件（`git ls-files`）里不许出现
内部台账的路径与章节编号，也不许用决策者口吻（三种具体形状写在脚本 docstring 里）。
判据是机械的（`git ls-files` + 正则），所以它能被反向用例打红 —— 这类"读起来别扭"的坏法
没有报错、测试全绿，只有一条纪律，而靠人记的纪律已经失效过一次（全仓 100+ 处指针）。
仓库里该留的是**自包含的约束**（"改这里会坏什么"）；决策过程住在维护者本地的台账里。

## 多格式入库（装载 / 分派 / 命名）

```bash
uv run python scripts/acceptance/check_doc_formats.py       # 13 通过 / 0 失败 / 0 跳过
uv run python scripts/acceptance/reverse_doc_formats.py     # 反向测试：9 条全红（临时改 src/，finally 里还原）
```

**权威 = `check_doc_formats.py` 的 docstring**：端口（`suffixes` + `read`）、装载口径（`doc_id` 取相对路径、
`source` 取顶层目录名去掉 `_data`、按路径排序）、两份「没读进来」的清单（`skipped` 不认识的扩展名 /
`empty` 认了但没抽出文字），以及**为什么不用 unstructured** —— `partition_docx` 会在**运行期**去 GitHub
下 spacy 模型（离线直接挂在 socket 上），而 Word 自己就有 Heading 样式。夹具是**现造的**四种格式
（python-docx / python-pptx / PyMuPDF 生成），所以不依赖本地语料。

## BM25 语料指纹缓存

```bash
uv run python scripts/acceptance/check_m3_bm25_cache.py     # 5 通过 / 0 失败 / 0 跳过（1.2 秒，不需容器/模型/网络）
uv run python scripts/acceptance/reverse_m3_bm25.py         # 反向测试：4 条全红（原地改 adapters/bm25_index.py，finally 还原）
```

**权威 = `check_m3_bm25_cache.py` 的 docstring**：3938 块时 `VectorStore.fingerprint()` 要
**397.6ms** 而**每次检索都要调**，真正的 BM25 打分只要 **~1ms** —— 所以指纹先问 Redis（存 sha256
摘要，不是 250KB 的整条指纹），未命中才回源。五条契约分别钉「省了」与「没省出问题」：
TTL 内不再回源 / 过期后变化仍被发现 / 缓存故障只降级（**不许"当作没变"**，那是静默错误）/
存的是摘要 / `cache=None` 时语义与加缓存前一致。TTL 用**假时钟**测，不 sleep。

**为什么单独一个脚本**：它全内存、不要容器，而反向测试要反复改坏源码跑很多次 ——
塞进 2400 行的 `check_m3_rag.py` 会让每次反向测试都等几分钟加载 BGE-M3。

## PDF 页码

```bash
uv run python scripts/acceptance/check_p0_5_pages.py   # 12 通过 / 0 失败（后两条要 Milvus；集合运维那两条用假件）
uv run python scripts/acceptance/reverse_p0_5.py       # 反向测试：10 条全红（跨 7 个文件，含 drop 的两条）
uv run python scripts/acceptance/check_p0_8_account.py   # 13 通过 / 0 失败（注册 / 验证码 / 改密 / 主题；零容器零网络）
uv run python scripts/acceptance/reverse_p0_8_account.py   # 反向测试：5 条全红
```

**权威 = `check_p0_5_pages.py` 的 docstring**：页码错了**不会报错**，只会给出一个看着合法的错页号，
所以整条链路分段钉死。两环十条：**适配器**（每页独有词映射回自己那页、区间首尾相接覆盖全文、
**空白页占 0 字符**、`read()` 与 `read_with_pages()` 逐字符一致、**非 PDF 源没有这个方法**）→
**切分与写入**（切分器两级偏移都切得出自己且与 `split_text` 分段一致、`chunk_documents` 算页码
并在偏移数不符时**当场报错**、不报偏移的切分器给 `None`（**不许猜**）、跨页块取起始页、
`Citation.page` 跟着子块走、Milvus 往返、**老集合读得回来但写入当场报错**）。
⚠️ 页码是「块**开始**的那一页」：真语料里 **22.9% 的块跨页**（2644 块里 606 个），有的块甚至只在一页上占最后 1~2 个字符 —— 判据要按这个约定写，别写成「整块都在这一页里」；
⚠️ 造 PDF 夹具用 **ASCII**；
⚠️ 夹具的**尺度**要让被测行为看得见（子块比一页还大就会把两页并进同一块，第二页永远验不到 ——
）；⚠️ langchain **1.x** 的 `add_start_index` 是**构造参数**。
反向测试要跑它 10 次（每次整套检查;Milvus 那两条与集合运维两条都在内,单次约 11 秒;出参那条跑 `check_m5_api`）——**别用默认的短超时**。

## 检索层 A/B 的裁判 + FAQ 阈值

```bash
uv run python scripts/acceptance/check_p0_4_ab.py      # 6 通过 / 0 失败（除阈值那条要真 FAQ 表外全内存）
uv run python scripts/acceptance/reverse_p0_4.py       # 反向测试：6 条全红
uv run python scripts/tools/ab_retrieval.py --limit 112 --configs rrf,weighted-5-5,weighted-3-7,weighted-7-3
uv run python scripts/acceptance/calibrate_threshold.py  # FAQ 阈值标定（真表 463 条，约 1 分钟；E 组最慢）
```

**权威 = `check_p0_4_ab.py` 的 docstring**：`overlaps()`（reference 的字符 3-gram 被某条 citation
覆盖的比例 ≥ 0.35）是 A/B 的尺子，**它错了读数全废而且不报错**。五条契约钉：identical 必中、
空 reference 一律不中、**覆盖率口径而不是 Jaccard**（长 context 不许被稀释）、空白不敏感、
阈值是 `>=`。⚠️ 写这类断言时**别用重复文本撑长度**。

**第六条是第三组（FAQ 阈值）**：`check_p0_4_ab` 直接 import 标定工具的 `probe` 与
`NOISE_PROBES`（**探针口径只有一处**），只跑最快的 A/B/C 三组（0.8 秒）并断言
`faq.threshold` 落在实测可用区间 `(C 上界, B 下界]` 里。读数：**463 条语料上
区间 (0.303, 1.000]、余量 0.697，0.85 在区间内 → 不改默认**；反向 RT-4 把配置改成 0.20 会让它红。
⚠️ 区间只挡「分数太低」，挡不住「答成了隔壁那条」（近邻混淆）—— 那是排序问题，见
规则见本节上一条。

## 认证 / 角色 / 会话归属

```bash
uv run python scripts/acceptance/check_p0_3_auth.py    # 20 通过 / 0 失败（除真 MySQL 往返外不需容器）
uv run python scripts/acceptance/reverse_p0_3.py       # 反向测试：16 条全红（WS 两条 + 文档四条 + 对账四条 + 认证六条）
uv run project-rag user add -u 你的名字 --role admin   # 建号（**唯一的建号入口**，没有注册接口）
uv run project-rag doctor                              # 对账（默认只读，退出码 1 = 有问题）
uv run project-rag doctor --prune-orphans              # 清孤儿块（库里有、登记表没有）
uv run project-rag doctor --forget-missing             # 清缺块行（登记 processed、库里一块没有）
```

**权威 = `check_p0_3_auth.py` 的 docstring**：**登记 ID == 装载器 doc_id**（不然删除静默删不掉）、上传走原始 body、文件名只取 basename、WebSocket 走 `?token=`（浏览器不能给 WS 设头）、无令牌先回 error 帧再关、**越权回帧但不断连接**、`/api/v1/query` 也要查归属；口令哈希串自带参数（换迭代次数不必全员重设）、
盐必须随机、坏串一律 False（不抛也不通过）、令牌只认 `Bearer`、登录失败话术一致（不给用户名枚举）、
**会话没主人 = 拒绝**（`owner_of` 的 `None` 既可能是老会话也可能是瞎编的 ID）。
`auth_store=None` = 无认证模式，**只可能来自显式注入**；`build_real_deps` 永远接上。

**对账的两个清理开关**：`doctor` **默认只读**，`--prune-orphans` / `--forget-missing` 点名才动手；删哪些只由 `reconcile` 判一次，清理函数只删传进来的那些 doc_id；逐条回报，**清完按新状态给退出码**。反向测试钉的是「删得比该删的多」（`RT-15` 顺手全清、`RT-16` 空清单也删）—— 删少了看得见，删多了没人立刻发现。

## 延迟报表与落表

```bash
uv run python scripts/acceptance/check_m5_9_metrics.py      # 10 通过 / 0 失败（除真 MySQL 往返外不需容器）
uv run python scripts/acceptance/reverse_m5_9.py            # 反向测试：7 条全红（改 metrics.py 与 ledger_sink.py，finally 还原）
uv run project-rag metrics --window 24h                     # 人看的报表：分段 p50/p95/p99 + 比率 + 最慢几条
```

**权威 = `check_m5_9_metrics.py` 的 docstring**。账本字段本身（31 个）仍由
`check_m5_5_latency.py` 钉住；这里钉的是**聚合口径**，重点是"数字在什么条件下才有意义"：
最近秩不插值、样本不够就不给数（p95 要 20 条、p99 要 100 条）、`-` 不折算成 0、
**冷热各一套数**：分段只算热行，拒答率/截断率/中断率/通道计数**也只数热行**
（冷启动被模型加载主导；混进来 p95 就变成"冷启动有多慢"，而比率会随"这周重启过几次"漂），
冷启动那一套单独报（含它自己的分母）—— 藏着和混着一样糟。
冷/热由 `note_build()` 判定 —— 四个重资源工厂与 BM25 索引**重建处**各打一次点，
所以预热过的进程第一个真实请求就是热的。

落表链路：`StreamTiming.emit()` → logging handler（认 `extra={"ledger": …}`）→ `LedgerStore` 端口 →
`adapters/ledger_mysql.py`。**三条入口（CLI / HTTP / WS）自动覆盖**，因为出口只有一处。

**一次请求只有一条账**：账本由合流入口（`stream_question`）开，知识库通道拿到的是**同一个**
`timing`（`KnowledgeChannel` 的第二个参数），检索/生成的打点记在同一行上。
两个各开一条的后果不是"多一行日志"—— 报表的请求量、拒答率、通道计数会**全部翻倍**，
而翻倍在报表上只表现为"量比想象的大"（见 `reverse_m5_5_latency.py` 的用例，前两条就是它）。

**两路召回并行**：`retrieve()` 里稠密与关键字那两路互不依赖，现在是叠着跑的（关键字那一路在
子线程），所以 `vector_ms` / `bm25_ms` 那两格由**各路自己报自己的时长**（`StreamTiming.span()`）——
用"相邻打点之差"在并行下会算出负数、或者把"等另一路"的时间算进融合段。

## 部署契约（compose / Dockerfile / 依赖档位）

```bash
uv run python scripts/acceptance/check_m6_deploy.py         # 23 通过 / 0 失败 / 0 跳过
uv run python scripts/acceptance/reverse_m6_deploy.py       # 反向测试：34 条全红（改临时副本，真实仓库一个字节不动）
```

**权威 = `check_m6_deploy.py` 的 docstring**：这几份文件之间必须自洽 —— 地址用服务名 + 容器内端口
（且必须等于该服务公布映射的**右侧**，防两处漂移）、`depends_on` 的 condition 必须与目标服务有没有
`healthcheck` 匹配（给没有 healthcheck 的服务写 `service_healthy`，compose 直接报错）、只读挂载落在镜像的
`WORKDIR` 下、`EXPOSE` / `ENV RAG_API__PORT` / `ports` / `HEALTHCHECK` 四者一致、密钥走 `.env`、
`.dockerignore` 按**整行**匹配、**`unstructured` 那一档不许回到运行时依赖**（`en-core-web-sm` 的 wheel
只有 GitHub 一个源，回去就意味着 `docker build` 要去 github.com 取一个产品路径不用的模型）。

**它证明「配置自洽」，不证明「镜像能构建」** —— 后者只能在装了 docker 的机器上跑
`docker compose up -d --build`（开发机没有 `docker` 命令，见 README 的「仓库里有什么、没有什么」）。

## 前端：源码在 `web/`、产物入库、契约在 `check_m7_frontend.py`

```bash
cd web && npm install            # 只第一次（node 工具链只在这里用得到）
cd web && npm run typecheck      # vue-tsc --noEmit
cd web && npm run build          # ① vite 构建 → src/project_rag/api/static/app/
                                 # ② 写 source-digest.txt（产物「来自哪一版源码」的内容摘要）
cd web && npm run dev            # 开发态 5173;后端地址在页面左上角填（CORS 已开）
```

| 关注点 | 在哪 |
|---|---|
| **前端每个控件的数据从哪来** | `check_m7_frontend.py` 的「能力不写死在前端」+ `web/src/api/endpoints.ts` |
| 为什么产物入库、为什么 hash 路由 | `check_m7_frontend.py`「产物与源码同源」+「hash 路由 + 相对 base」两条契约 |
| 契约（聊天页） | `check_m7_frontend.py` **18 条**（静态 9 + 行为 9，其中一条盯「存储层坏掉时说人话」）；反向 `reverse_m7_frontend.py` **13 条** |
| 契约（知识库页） | `check_m7_2_kb.py` **12 条**（预览接口行为 7 + 前端静态 5）；反向 `reverse_m7_2_kb.py` **9 条** |
| 契约（索引类型 + 稀疏向量） | `check_m3_sparse.py` **16 条**（索引类型由配置决定 3 + 稀疏列边界 3 + BM25 编码口径 6 + 接线 4，全部用桩客户端，零容器）；`check_m3_sparse_milvus.py` **3 条**（真 Milvus：稀疏列往返 / rebuild_index 换类型 / 页码与来源过滤）；反向 `reverse_m3_sparse.py` **10 条全红** |
| 契约（可观测看板） | `check_m7_3_metrics.py` **12 条**（`capabilities.metrics` + 报表行为 6 + 前端静态 6，其中一条盯"冷热分层读数"）；反向 `reverse_m7_3_metrics.py` **15 条**（多出来的四条盯"能力自述的取数时机"：守卫补拉 /「刷新」自救 / 不把"连不上"说成"还没读到" / 空白名单不冒充"不支持的类型"，另有两条盯"冷热分层被画平"：拿总行数当比率分母 / 冷启动那一套数不画出来） |
| 契约（安全收口 + 桌面壳） | `check_m7_4_security.py` **22 条**（限流 10 + 登出 1 + 路由守卫扫描 1 + CSP/CORS 2 + 桌面壳 3 + 前端静态 2 + …）；反向 `reverse_m7_4_security.py` **21 条** |
| 改了前端忘了构建 | 契约第 1 条就会红：产物摘要与当前源码不一致 |
| 浏览器实测（按需，不进回归） | `serve_fake_backend.py`（假后端，`>>> QUERY` 行会打出后端真正解析出的旋钮、**上传的文档也真的会入到它的假向量库里**）+ `browser_m7_frontend.cjs`（聊天页 19 条）+ `browser_m7_2_kb.cjs`（知识库页 21 条：原 12 + 实时清单 9）+ `browser_m7_3_metrics.cjs`（可观测看板 22 条，含"样本不够要画 `—` 不是 0"、"冷启动那次拒答没被算进拒答率"这两条，以及"直接落在 `#/metrics` 也要自己补拉能力 / 能力打空点「刷新」能自救"两条）+ `browser_m7_4_security.cjs`（**桌面壳场景** 10 条：起壳自己的回环服务、`?backend=`、跨源问答、退出撤销、限流可见）+ `browser_p0_6_access.cjs`（角色与可见范围 23 条：文档侧授权弹层、**组侧「文档权限」三态与勾选保存**、运维页签与接口状态）+ `browser_container_smoke.cjs`（**真容器**冒烟 8 条：`BASE=http://127.0.0.1:8080/` 打开 compose 里那份产物 —— 307 落登录页、登录卡片渲染、能力自述同源 200、控制台干净；登录之后的问答要有真账号口令，归人工）；跑法与结果记在维护者本地的（不随仓库发布） |

**三个不进回归的验证件**（在同一个目录里，但**不会**被 `check_*` / `reverse_*` 的 glob 扫到）:

* `serve_fake_backend.py` —— 真 uvicorn + 全假依赖的假后端（`127.0.0.1:18081`，
  账号 `admin`/`admin-pass`、`employee`/`emp-pass`）。**它是给眼睛看的**：改完界面想确认一下，
  起它比连真库安全（不动 MySQL / Milvus 一行数据），而登录、角色、会话目录、流式、引用页码
  走的都是真接口。每次提问会把后端解析出的旋钮打在 `>>> QUERY` 行上；
* **桌面壳**：`uv run project-rag-desktop --check --print-url`（不开窗,不需要 GUI 栈;
  要装壳本身才用 `uv sync --extra desktop`）。它**本地模式起一个只绑回环的静态服务** ——
  别改回 `file://`：module 脚本在 `file://` 下会被 CORS 挡掉、页面白屏。
* `browser_m7_frontend.cjs` / `browser_m7_2_kb.cjs` / `browser_m7_3_metrics.cjs` / `browser_m7_4_security.cjs` —— 用 Playwright
  驱动真 Chromium 跑一遍真实用户流程（17 条：路由 / 登录 / 角色 / 历史 / 流式 / 引用页码 / 参数
  真的生效；12 条：上传 → 轮询到「已入库」→ 预览两个数与跨页页码 → 两步删除 → 如实转述删了几块；
  15 条：KPI 与假账本对得上、切窗口、**样本不够的分位数画成 `—` 而不是 0**、最慢榜标冷启动。
  三者都断言 0 报错 0 失败请求）。需要一次性装 playwright + 一个 Chromium（国内走 npmmirror 镜像）
  + 缺的 `libnss3` 那三个库 —— 完整命令写在各 `browser_*.cjs` 的文件头注释里。
  ⚠️ 假后端的上传落在 `.cache/fake_uploads/`，**不会**碰 `data/uploads/`。
* `browser_container_smoke.cjs`（加）—— 同一个 Chromium，但打的是**真容器**：
  `BASE=http://127.0.0.1:8080/ LD_LIBRARY_PATH=… node scripts/acceptance/browser_container_smoke.cjs`。
  它回答的是另一个问题：**镜像里那份产物在浏览器里真的渲染出来了吗**（假后端那几套证明不了这个 ——
  它们打的是仓库里那份产物 + 壳的回环服务）。8 条断言：307 落 `/static/app/#/login`、登录卡片渲染、
  两个输入框与按钮文案、`/api/v1/capabilities` 同源 200、0 console 报错、0 未捕获异常、截图。
  登录之后的问答要有真账号口令（账号在 MySQL 里、口令是 PBKDF2 摘要），**归人工点**。

**两条容易踩的**:

* **产物是入库的**（`src/project_rag/api/static/app/`）—— Python 侧与 Docker 镜像都不装 node，
  代价是改完前端必须 `npm run build`；判据是**内容摘要**不是 mtime（mtime 会被"反向测试还原 /
  checkout / clone"假红）；
* **预览折叠的是父块**：向量库里写的是子块、父块正文挂在每个子块的 `parent_content` 上，
  所以 `GET /documents/{id}/chunks` 按 `parent_id` **折叠**回去 —— 只按子块列出来会把
  "入库 42 块 / 预览 15 段"显示成 42 段，用户以为我们丢了数据；
* **限流的那份计数在 Redis**（`adapters/ratelimit_redis.py`：滑动窗口 + 一段 Lua 原子判定）：
  契约里那几条要**真 Redis**（连不上会 SKIP 并点名）；离线只跑"内存那份"的语义。
  想关掉限流是 `api.rate_limit.enabled=false`，想退到单进程是 `backend: memory` ——
  **别把 `backend` 写成别的值**，那样会当场抛（不认识的后端名不许悄悄退化成某一种）。
* **单路检索的语义**：`mode=vector|keyword` 在接口层翻译成"另一路权重 = 0"，
  而权重 ≤ 0 的那一路**连检索都不做**（`application/answer_rag.retrieve`）——
  否则延迟账上的 `embed` / `vector` / `bm25` 三段会显示根本没发生的耗时。

## 数据契约在哪

**权威 = `scripts/acceptance/check_m4_eval.py` 的 docstring**（评估集字段、四字段映射、
CSV 列、指标改名、裁判选择、以及为什么评估不进 `src/`）。设计理由只留**设计理由**，两处不重复。

## 与 `src/` 的关系（单向）

```text
scripts/  ──可以──►  src/**          脚本是第二个组合根，允许直接装配适配器
src/**    ──禁止──►  scripts/        验收有一条检查钉住「src/ 里不出现 ragas」
```

**为什么评估不进 `src/`**：离线一次性，没有第二个调用方；而且 ragas 只在这一条链路里出现，
让它进 `adapters/` 等于把"上游依赖再出一次坏导入"变成产品级事故。
**触发重评**：要对外提供在线打分、或 CLI/CI/HTTP 出现第二个调用方时（清单在维护者本地）——。

## 还没做完的（别当成 bug）

| 项 | 状态 |
|---|---|
| 检索调参 A/B | **融合 `rrf` vs `weighted` 已量完**（不改默认）、**FAQ 阈值 0.85 已标定**（在实测区间内，不改）—— 两组都有契约 + 反向（`check_p0_4_ab.py` / `reverse_p0_4.py`）。**只剩两项**：重排父块 vs 子块（仪器已就位、读数未跑）、**ragas 四指标全链路**（要 DeepSeek 裁判费用；`check_m4_eval` 里那条 SKIP 点名了 owner） |
| `retrieval_k`/`candidate_m` 那组 A/B | **已量完并回退到 5/2**（0.9347 vs 0.9313）—— 这是唯一一次完整 A/B，两份数据集的对比|
| 裁判偶发失败 | `IncompleteOutputException`（长 ground_truth 的 claim 枚举撑爆 8192 输出）与偶发超时。当前口径：不当场炸，记进 summary 的 `errors` 列，均值跳过空值。 |
| 三个评估代理（LLM 造题 / 质检 / 1–5 分评分） | 未做 |
| 服务化：Dockerfile / 接口文档 / `.env` 模板 | ✅ 已做（`Dockerfile`、`API.md`、`.env.example`）；镜像约 9G（CPU-only torch 能砍到 ~1.5G，未验证） |
| 编进 compose + 依赖分档 | ✅ 已做（`app` 服务 + `unstructured` 挪进 optional，`.venv` 7.1G → 6.7G，构建期不再需要 GitHub）；**剩**：镜像还没在**真机**构建一次（开发机没有 `docker` 命令）—— 配置自洽由 `check_m6_deploy.py` **23 条**钉住 |
| 会话历史的过期与归档 | 未做：`conversations` 表只按「每会话最近 N 轮」裁，**没有 TTL、没有归档** |
| `qa --source` 的学科白名单校验 | 未做：`--source` 直接透传成检索过滤（与 `rag` 一致）。参考实现有一层 `VALID_SOURCES` 校验 |

## 时间成本（本机实测，供排期参考）

| 步骤 | CPU（agent 沙箱，无 GPU） | 你的终端（`embedding.device: cuda`） |
|---|---|---|
| ② 建 112 条数据集 | ~19 分钟 | 预计 2~3 分钟（重排 6s/问 → 75ms/问） |
| ③ 跑 112 条四指标 | 12~19 分钟 | 同左（瓶颈在 DeepSeek 端点，不在本地） |

> ③ 的耗时大头是**判分调用量**：112 条 × 4 指标 = 448 个判分单元，每个单元内部还有多次 HTTP
> （抽陈述后逐条验证、把 ground_truth 拆 claim 后逐条归因）。所以**先用 `--limit 5` 冒烟**再上全量 ——
> 这个教训的代价是一次白跑的 55 分钟。
