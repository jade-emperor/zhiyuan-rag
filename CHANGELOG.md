# 变更记录

本文件记录所有**对外可见**的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 约定：`0.x` 期间次要版本可能有破坏性改动；每条变更都尽量带**证据指针**
>（契约脚本名），因为这个项目的规矩是"能验证的才算数"。

## [Unreleased]
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