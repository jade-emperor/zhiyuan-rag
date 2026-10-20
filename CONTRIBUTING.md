# 贡献指南

先说清这个项目的**脾气**：它把「可验证」看得比「写得快」重 —— 每条能力都配一份可执行契约，
每条契约都要有一条**反向测试**证明它真的会红（把代码改坏一次，看那一条检查是不是真 FAIL）。
所以这里的贡献流程可能比一般项目多两步，但那两步正是这个项目最值钱的地方。

> 说明：可执行的判据一律在 `scripts/acceptance/` 的 docstring 与 `scripts/README.md` 里。

## 一、能贡献什么

| 类型 | 说明 | 需要的证据 |
|---|---|---|
| **Bug 报告** | 用 issue 模板，附「期望 / 实际 / 最小复现 / 日志一行」 | 能贴上 `label=… channel=…` 那行延迟账最好 |
| **文档修正** | 尤其是「文档与代码不一致」—— 本项目的规矩是**先假定代码是错的**去核一遍（这条来自一次真事故：文档比代码严，而没人盯着） | 指到具体行 |
| **新的验收检查** | 见下方「加一条检查」 | **必须**配反向测试 |
| **新的适配器 / 端口实现** | 换模型厂商、换向量库、换缓存……只该动 `adapters/` 下一个文件 | 契约 + 反向 |
| **前端改动** | `web/` 是 Vite + Vue3 + TS | 必须 `npm run build`（产物入库，有摘要契约盯着） |
| **性能优化** | **先量再改**：本项目的每条性能结论都带实测数字（延迟账本那一行 31 个字段就是干这个的） | 前后对照读数 |

## 二、开发环境

```bash
# 1) Python 依赖（Python 3.13，uv 管理）
uv sync

# 2) 中间件（Milvus / MySQL / Redis，跑在容器里）
mkdir -p logs data/uploads        # 容器要写的两个目录（缺了 Docker 会建成 root 属主 → 写不进）
docker compose up -d standalone mysql redis

# 3) 密钥
cp .env.example .env      # 填 RAG_LLM__DEEPSEEK_API_KEY / MySQL / Redis 密码

# 4) 模型（4.4G，不进仓库；不下载也能跑大部分验收，见下）
uv run python -c "from modelscope import snapshot_download as d; \
  d('BAAI/bge-m3', local_dir='models/bge-m3'); \
  d('BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3')"

# 5) 起服务（两个角色别混：18080 = 宿主上的代码后端；8080 = 容器里的 app）
uv run project-rag-api            # 裸跑读 config.yaml，绑 0.0.0.0:18080
./scripts/dev.sh serve            # 同一件事的短命令 + 端口占用守卫（带端口参数可换口）
docker compose up -d app          # 或者整份用容器跑（宿主 8080，页面与接口同一个进程）
```

**没有模型/语料也能跑**：验收脚本对缺失的外部依赖是 **SKIP 并点名 owner**，不是 FAIL。
`scripts/README.md` 每套脚本后面都标了"要不要容器/模型"。

## 三、提交前自检（和 CI 跑的一样）

```bash
./scripts/dev.sh ci        # pyright + 27 套契约 + 27 份反向 runner（= CI 的 contracts 任务）
./scripts/dev.sh frontend  # 改了 web/ 才需要：typecheck + build（产物要一起提交）

# 只想跑你改到的那一块时：
uv run python scripts/acceptance/check_<相关>.py
uv run python scripts/acceptance/reverse_<同族>.py     # 反向：全红才算过
```

`./scripts/dev.sh` 不带参数会列出全部短命令；它只是 `uv run …` 的壳子，命令本身在
`scripts/acceptance/run_all.py`（分组依据写在它的 docstring 里）。

改到跨模块的东西（端口签名、领域模型、帧协议）请跑**全量回归** ——
`scripts/README.md` 与 `run_all.py` 的分组是权威清单，口径是「全部检查 0 失败 · 反向 runner 全红 · pyright 0 error · 无残留」。

### 加一条检查时

1. 写在 `scripts/acceptance/check_*.py` 里，用 `@check("...")`，**检查名要说清它保的是什么**；
2. 缺外部依赖时用 `SkipCheck`（**点名 owner**），别 FAIL —— 否则别人机器上会天天假红；
3. **加一条反向用例**（`reverse_*.py`）：把产品代码改坏一次，确认这条检查真的 FAIL。
   锚点必须**恰好出现一次**，否则这条反向测试等于没跑；
4. 在 `scripts/README.md` 里登记（维护者会同步到本地的基线表）。

## 四、代码规范

- **分层由机器强制**（AST 扫描，违反即验收失败）：`domain/` 只用标准库、`ports/` 只声明能力、
  `adapters/` 是全项目**唯一** import 第三方 SDK 的地方、`api/` 与 `cli/` 只经 `Deps` 工厂组装。
- **注释即契约**：写「为什么这么做」和「当初踩了什么」，不写「这行在做什么」。
  反例：`# 增加计数`；正例：`# 计数只在这里加:另一处加会让 p95 少算一次冷启动`。
- **命名按机制**，不用架构术语。
- **中文注释、中文提交信息**（项目现状如此）。
- 提交信息建议 `<范围>: <做了什么>`，例如 `限流: Redis 滑动窗口 + 兜底话术`；
  一次提交只说一件事，别把「修 bug + 重构 + 格式化」混在一起。

## 五、Pull Request

PR 描述里请回答三个问题（模板里有）：

1. **改了什么**（一句话）；
2. **哪条契约能证明它**（脚本名 + 检查名）；
3. **反向测试红过没有**（新增/改动契约时必须）。

## 六、许可

本项目是 **MIT**（见 `LICENSE`）。提交贡献即表示你同意以同样的许可分发你的代码。
