## 改了什么

<!-- 一句话。关联的 issue 写 `fixes #12`。 -->

## 证明（这个项目的 PR 必须回答）

1. **哪条契约能证明它**：脚本名 + 检查名，例如
   `scripts/acceptance/check_m3_bm25_cache.py` → 「BM25 指纹缓存：TTL 过期后语料变了仍会被发现并重建」
2. **反向测试红过没有**：改了/加了契约的话，把产品代码改坏一次，贴出那条检查 FAIL 的那一行
3. **跑过什么**（勾选并贴结果）：

- [ ] `uv run pyright src scripts` → 0 error
- [ ] 相关契约脚本 → `0 失败`
- [ ] 相关反向 runner → `全红`
- [ ] 改了前端：`cd web && npm run typecheck && npm run build`（产物必须一起提交）
- [ ] 跨模块改动：跑了全量回归（权威清单 = `scripts/README.md`；`./scripts/dev.sh all`）

## 自检（勾选）

- [ ] 没有把 `data/`、`models/`、`.env`、`web/node_modules/` 加进提交
- [ ] 注释写的是「为什么 / 当初踩了什么」，不是「这行在做什么」
- [ ] 新增的检查对缺失的外部依赖用 `SkipCheck`（**点名 owner**），没让它 FAIL
- [ ] 文档跟着改了（`README.md` / `API.md` / `CHANGELOG.md` / `scripts/README.md` 至少一处）

## 备注

<!-- 有取舍、有没做的部分、有已知副作用，都写在这里。宁可写"这条我没验"，也别假装验过。 -->
