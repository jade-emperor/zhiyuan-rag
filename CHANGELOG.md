# 变更记录

本文件记录所有**对外可见**的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 约定：`0.x` 期间次要版本可能有破坏性改动；每条变更都尽量带**证据指针**
>（契约脚本名），因为这个项目的规矩是"能验证的才算数"。

## [Unreleased]
### 变更（部署形态：前端产物与容器）
- **构建产物两处**：`npm run build` 先产出 `web/dist`（可独立交给 Nginx / 静态服务器，**不入库**），
  末尾再把一份**兼容副本**同步进 `src/project_rag/api/static/app/`（桌面壳与一体化模式用）；
  两条路径都带 `source-digest`。
- **新开关 `api.serve_frontend`**（默认 `true`，兼容旧部署）：关掉时后端不挂 `/static`，根路径回一段
  JSON（`{"mode":"api-only",…}`）而不是 307 —— 前后端分离时"打开 8080 只看到 404"的坑由这条消掉。
  `docker-compose.yaml` 的 `app` 服务显式设为 `false`：**容器只发 API**，页面由 Vite / Nginx 单独发。
- `scripts/dev.sh` 短命令重写（`backend` / `web` / `frontend` / `status` / `ports`），端口分层写进脚本头部；
  容器 smoke 与 `check_m6_deploy` 跟着服务集调整。

### 新增（答案反馈）
- **接口**：`POST /api/v1/feedback`（`up` / `down` / `none`=撤销，Bearer+归属）、
  `GET /api/v1/reports/feedback`（运维/管理员；`rating` 过滤、`days=0` = 全部历史）。
- **存储**：`answer_feedback` 表 —— `turn_key`（会话+问题+答案的 SHA-256）唯一，
  同一轮重复点是**覆盖写**；问题、答案、通道、可选一句话备注作为**快照**一起落库，
  会话后来被清空也能复盘；`LEFT JOIN users`，删号不吞反馈。
- **前端**：回答下方两个按钮**落到后端**（乐观更新 + 失败回滚、连点置灰、再点同一个 = 撤销）；
  新增 `#/feedback`「答案反馈」看板（运维 + 管理员）：有帮助 / 没帮助 / 负反馈率（带分子分母，
  没有样本画 `—` 而不是 0）/ 条数，7·30·90 天与「仅没帮助」筛选，逐条显示问答快照、备注、
  归属人与通道，可跳到那段会话。
- 验收：`check_m8_5_feedback.py`（8 条，含真 MySQL 的覆盖写与"只删自己"）、
  `reverse_m8_5_feedback.py`（9 条全红）。
### 新增（会话历史页）
- **接口**：`GET /api/v1/sessions` 多一个 `scope=mine|all`。`all` 仅管理员 —— 跨账号列会话，
  每行带 `owner`（登录名）与 `mine`（是不是当前账号的），`scope` 原样回显。账号被删除时
  `owner` 给空串（适配器 `LEFT JOIN users`），**会话不因删号而从视野里消失**。
- **正文读取**：`GET/DELETE /api/v1/history/{id}` 对**管理员**放行任意一段有归属的会话；
  员工的跨账号读取仍然 `403`。**没有归属记录的会话谁都不放行**（包括管理员）。
  HTTP 与 WebSocket 共用同一份 `owner_violation`，两边一起改、一起测。
- **前端**：`#/history/:session?`「会话历史」页（员工看自己的，管理员可切「全部」）——
  目录搜索、按轮次渲染问答、**当轮引用快照**（页码未知照实说、老账没引用也照实说）、
  「继续对话」回聊天页、行内两步确认清空。
- 验收：`check_m5_10_history.py`（7 条，含真 MySQL 的 `list_all`）、
  `reverse_m5_10_history.py`（8 条全红）；`check_p0_3_auth` 的归属口径改为
  「员工 403 / 管理员 200 / 没归属谁都 403」，反向用例同步。
### 新增（FAQ 管理台）
- **接口**：`GET /api/v1/admin/faq?query=`（服务端对问题与答案做包含匹配；`total` 是过滤前总数）、
  `POST`（`201`）、`PATCH`（字段可省）、`DELETE`（幂等，`deleted=0`）—— 四个都是 `admin`。
  在此之前问答对只能由**不随仓库发布的**装载脚本灌一次，改一条就得连库手写 SQL。
- **口径**：读端口 `FaqStore` 与写端口 `FaqEditor` 分开（答题路径不该有写能力，只读假件也不会被
  逼着多长三个方法）；问题 512 字 / 答案 8000 字（与老表 `VARCHAR(512)` / `TEXT` 同形）；查重 =
  压空白 + 忽略大小写后相同 → `409` 并点出已有条目的 ID；删除不存在给 `deleted=0`；
  **改完下一次提问就生效**（Web 每个请求都读库，只有 `project-rag faq` 吃 `cache.ttl_s` 的 5 分钟缓存）。
- **建表**：应用启动时 `ensure_table`（幂等 + 把老表排序规则扳回当前口径），空库不必先跑装载脚本。
- **前端**：`#/faq`「FAQ 管理」页（仅管理员）—— 搜索 / 分页 / 新建与编辑弹层（512 / 8000 上限、
  Markdown 答案）/ 行内两步确认删除；页头写明"只答近乎一字不差的问法"。
- 验收：`check_m2_5_faq_admin.py`（13 条，含真 MySQL 增改删往返与前端静态契约）；
  `reverse_m2_5_faq_admin.py`（9 条全红）。
### 新增（知识库两级目录）
- **一级目录**：`documents` 加 `folder` 列 + `document_folders` 表（名字唯一、ID 形如 `f_`+12 位十六进制、
  **改名不改 ID**）；空串 = **未分类**，是一个合法归属而不是错误状态（磁盘上还没登记的文件只能是它）。
- **接口**：`GET /api/v1/folders`（每格带对账后的份数 / 库内块数 / 占用 / 需要处理，含"未分类"与汇总）、
  `GET /api/v1/folders/{id}`（目录 + 里面的文档 + 汇总，一次往返）、`POST` / `PATCH` / `DELETE`
  （建 / 改名 / 删**空**目录；非空 `409` 并点出份数 —— 删目录不删文档）、
  `PATCH /api/v1/documents/{id}`（挪目录，只改归属）；清单与上传各多一个 `folder` 参数。
- **目录来自语料目录**：`data/corpus` 的每个一级子目录同步成一格（启动时同步、幂等；名字去掉
  `_data`），语料根本身不是一格；语料之外的来源（`uploads`、临时目录）**不再**凭空建目录。
- **入库落点**：默认 = 文件在语料根下的一级目录，`--folder 公司制度` 可显式指定；语料之外的路径落未分类。
  从语料根灌与从子目录灌给出同一批 `doc_id`/`source`（`registration_anchor`），两种入口互相幂等。
  同名文件**不能换目录**（`document_id` 就是文件名，两格会互相删块）：同一目录重传是覆盖，换目录 `409`。
- **老库一次性回填**：加 `folder` 列那一次按 `source` 把老行归进同名目录；**之后不再回填**（移回未分类不会被重启塞回去）。
- **前端两级**：一级是目录卡（`#/knowledge`）；二级 `#/knowledge/:folder` 是面包屑 + 统计条 +
  搜索 / 状态筛选 / 排序 + 文档表格（多选、批量删除、预览 / 权限 / 移动 / 两步确认删除）+ 右侧上传与目录信息 + 空状态。
  上传落点 = 当前目录。`DocumentCard` 换成 `DocumentTable`，"两步确认"这条口径不变。
- 验收：`check_m7_5_folders.py`（23 条，含真 MySQL 的迁移与唯一约束、同名换目录 409、语料目录同步与登记根归一化）；`reverse_m7_5_folders.py`（9 条全红）；
  `check_m7_2_kb` / `check_m7_2_live_kb` 改指两级页面后复跑通过。

### 新增（图片模态）
- **图片入库**：MinerU `content_list.json` → 正文 + 页码区间 + 图片锚点；图片按**字节 SHA-256**
  内容寻址上传阿里云 OSS（`x-oss-forbid-overwrite` 处理并发，同一张图不会产生第二个对象）；
  父块图片以 `image_keys` 随子块落 Milvus（新集合 `rag_chunks_img_v1`），**不把临时 URL 写进库**。
- **引用出参**：`CitationOut.images[]` 给 `key`（稳定身份）与 `url`（现签、默认 300s）；
  WebSocket `sources`/`done` 与历史接口同一形状。历史只存 key，回看时重新签。
- **多模态**：`ChatMessage.images`；RAG 只把前 3 张图的签名 URL 附在用户消息里，
  提示词给出 `[n]` 对应关系；纯文本路径的提示词逐字不变。实测 `deepseek-flash` 接受
  HTTPS URL 与 `data:image/...;base64` 两种形式。
- **CSP**：`img-src` 允许 `https:`（OSS/CDN 签名 URL），脚本仍只许 `'self'`。
- **MinerU 独立服务**：`services/mineru/`（任务队列、PDF 哈希缓存、zip 结果）+ 主 app 的
  `MinerUClient`；compose 里带 `profiles: ["images"]`，不开图片时不构建/不启动。
  zip 里附带 `full.md`/`input.md` 只做 sidecar（审计/人工排查），切块仍只用
  `content_list`（它才有 page_idx 与图片锚点）；图片按 `images.min/max_bytes` 与
  `max_per_doc` 过滤。MinerU 默认 `MINERU_MODEL_SOURCE=modelscope`（HuggingFace 在不少
  国内网络不通），首次解析下载约 1.9GB 模型；真机已用一份带图 PDF 跑通（CPU 约 14 分钟）。
- 验收：`check_image_modal.py`（7 条：内容哈希、OSS 幂等、content_list 解析、父块归属、引用签名、
  历史兼容、多模态前 3 张）；`check_m3_rag` / `check_p0_5_pages` / `check_m3_sparse` / `check_doc_formats` /
  `check_m7_frontend` / `check_m6_deploy` / `check_m7_4_security` 复跑通过。


### 性能（两路召回并行）

- **知识库检索的两路召回改成并行**：稠密向量（Milvus）与关键字（Milvus 稀疏 / 进程内 BM25）
  此前是串着跑的，现在关键字那一路在子线程里跑、汇齐后再融合。真栈 A/B（真语料、真关键字编码，
  各 5 次取中位）：串行 稠密 **398ms** + 关键字 **399ms = 797ms** → 并行墙钟 **400ms**；
  省下的正好是"较快那一路"的时间，所以随当次的一致性等待落在 **200–400ms**（真问答墙钟的 10–20%）。
  只有一个检索进行时不建线程；前端「只用向量 / 只用关键字」两档行为逐字不变。
- **分段口径跟着改**（`StreamTiming.span()`）：`vector_ms` / `bm25_ms` 由**各路自己报自己的时长**，
  不再用"相邻两个打点之差" —— 并行下那个算法会算出负数、或者把"等另一路"的时间算进融合段。
  因此「六段之和 ≈ `retrieve_ms`」改成「六段各自是自己的时长，两路重叠时和**可以大于**
  `retrieve_ms`、绝不会小于它」。顺带修掉一个假 `-`：只开关键字那一路时 `bm25_ms` 以前报不出来。
- 子线程里的异常**带回来由主线程重抛**（`Thread.join` 不传异常，否则会变成"关键字那一路
  静默消失"：少召回一半而账上一切正常）；两路都抛时抛先查的那一路，另一路记 WARNING。
- 验收：`check_m5_5_latency.py` **28 → 30 条**（并行墙钟 < 0.7×两段之和、融合段不吸等待、
  只开关键字时报自己的时长）；反向 `reverse_m5_5_latency.py` **2 → 4 例全红**。