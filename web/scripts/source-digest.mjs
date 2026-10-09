/**
 * 把「这份产物是从哪一版源码构建出来的」写成一个摘要文件。
 *
 * 为什么需要它:`src/project_rag/api/static/app/` 里的产物是**入库**的
 * (Python 侧与 Docker 镜像都不装 node),于是"改了前端忘了重新构建"就成了一种
 * **静默**的坏法 —— 仓库里跑的界面和你正在改的源码根本不是一份东西。
 *
 * 为什么不用文件 mtime:`git checkout` / 反向测试还原源码 / 换个机器 clone
 * 都会把 mtime 变新,而内容一个字都没改 —— 那种检查会天天误报,最后被人无视。
 * 摘要只认内容。
 *
 * **算法与 `scripts/acceptance/check_m7_frontend.py` 里的 Python 版一字不差**
 * (两个实现配一对:一边算、一边核。改这里必须同时改那里,否则契约会红 —— 这是刻意的)。
 *
 *   对 web/ 下这些文件按**相对路径字典序**排列:
 *     package.json、vite.config.ts、index.html、src/ 下的全部文件
 *   每个文件算出 sha256,再把 `${相对路径}\0${该文件的 sha256}\n` 依次拼接,
 *   对整串再算一次 sha256 —— 就是摘要。
 */

import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(here, '..')
const outFile = resolve(webRoot, '../src/project_rag/api/static/app/source-digest.txt')

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(full))
    else if (entry.isFile()) out.push(full)
  }
  return out
}

function digest() {
  const files = [
    join(webRoot, 'package.json'),
    join(webRoot, 'vite.config.ts'),
    join(webRoot, 'index.html'),
    ...walk(join(webRoot, 'src')),
  ]
  const entries = files
    .map((full) => ({
      rel: relative(webRoot, full).split('\\').join('/'),
      hash: createHash('sha256').update(readFileSync(full)).digest('hex'),
    }))
    .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0))
  const outer = createHash('sha256')
  for (const { rel, hash } of entries) outer.update(`${rel}\0${hash}\n`)
  return { value: outer.digest('hex'), count: entries.length }
}

const { value, count } = digest()
if (statSync(webRoot).isDirectory()) {
  writeFileSync(outFile, `${value}\n`, 'utf8')
}
console.log(`source-digest: ${value} (${count} 个源文件) -> ${relative(webRoot, outFile)}`)
