/**
 * 把前端自己的产物（web/dist）同步一份到 Python 包里的兼容目录。
 *
 * 为什么还要留这一份：
 *   - 桌面壳的本地模式从 Python 包旁边读 `api/static/app/`，不能依赖用户先装 Node；
 *   - 旧的“一体化”启动方式（`uv run project-rag-api` 直接打开 18080）仍然可用；
 *   - 现有契约与浏览器验收都按 `/static/app/` 检查这份产物。
 *
 * 分离部署（Docker / Nginx / 静态服务器）**不读**这个目录，只读 `web/dist`。
 * 这份副本是兼容层，不是新的真源：真源永远是 `web/dist` 与 `web/src`。
 */

import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(here, '..')
const source = resolve(webRoot, 'dist')
const target = resolve(webRoot, '../src/project_rag/api/static/app')

if (!existsSync(source)) {
  console.error(`sync-python-bundle: 找不到 ${source}，先跑 npm run build`)
  process.exit(1)
}

rmSync(target, { recursive: true, force: true })
mkdirSync(target, { recursive: true })
cpSync(source, target, { recursive: true })
console.log(
  `兼容副本: ${relative(webRoot, source)} -> ${relative(webRoot, target)}`,
)
