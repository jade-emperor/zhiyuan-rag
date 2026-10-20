<script setup lang="ts">
/**
 * 知识库**二级页面**:一个目录里的文档。
 *
 * 页面结构与数据口径:
 *
 * 1. **面包屑 + 标题 + 说明**在最上面,统计条紧跟着 —— 进来先回答"这是哪一格、里面有多少";
 * 2. **搜索 / 状态筛选 / 排序**在工具栏,批量操作在选中之后才浮出来(没选东西时不占位置);
 * 3. **主体是表格**(不是卡片):一屏能看到更多行、能多选、能横向比较块数与大小;
 * 4. **右侧是这一格的操作区**:上传(落点就是当前目录)+ 目录信息 + 删除目录。
 *
 * 三条后端口径:
 * - 清单是**实时视图**(登记表 + 磁盘 + 向量库对账),所以 `state` 才是显示的依据;
 * - 还有 pending/processing 就**自动轮询**(3 秒),全落定就停 —— 见 `stores/documents.ts`;
 * - **删目录不删文档**:目录里还有文档时,删除按钮是禁用的;空的才让删。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import DocumentAclDialog from '../components/DocumentAclDialog.vue'
import DocumentTable from '../components/DocumentTable.vue'
import FolderDialog from '../components/FolderDialog.vue'
import Icon from '../components/Icon.vue'
import MoveDocumentDialog from '../components/MoveDocumentDialog.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import UploadPanel from '../components/UploadPanel.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { capabilitiesState } from '../stores/capabilities'
import { humanBytes } from '../utils/format'
import { deleteFolder, foldersState, refreshFolders } from '../stores/folders'
import {
  documentsState,
  openPreview,
  refreshDocuments,
  removeDocument,
  removeDocuments,
  setScope,
  stopPolling,
} from '../stores/documents'

type StateFilter = 'all' | 'attention' | 'ok'
type SortKey = 'name' | 'updated' | 'size' | 'chunks'

const route = useRoute()
const router = useRouter()
const docs = documentsState()
const folders = foldersState()
const caps = capabilitiesState()

const folderId = computed(() => String(route.params.folder ?? ''))
const uploadPanel = ref<InstanceType<typeof UploadPanel> | null>(null)

const query = ref('')
const filter = ref<StateFilter>('all')
const sort = ref<SortKey>('name')
const selected = ref<string[]>([])

const renaming = ref(false)
const moving = ref('')
const aclFor = ref('')
const confirmingFolder = ref(false)
const confirmingBulk = ref(false)

/** 目录本身(名字/说明/计数)。清单还没回来时是 null —— 标题先占位,不编一个空名字。 */
const folder = computed(() => docs.folder)
const unfiled = computed(() => folderId.value === 'unfiled')
/** 目录读不到(典型是 URL 里的 ID 被删掉了):这时不能拿"还没有文档"搪塞过去。 */
const missing = computed(
  () => docs.error !== '' && docs.folder === null && !docs.loading && !unfiled.value,
)
const title = computed(() => folder.value?.name ?? (unfiled.value ? '未分类' : '目录'))
const description = computed(() => {
  if (folder.value?.description) return folder.value.description
  if (unfiled.value) {
    return '尚未归入目录的文档会集中在这里。可在此补齐资料，或移动到更合适的知识目录。'
  }
  return `统一管理「${title.value}」目录中的文档、索引与访问权限，让这一主题下的知识随时可用。`
})
const suffixList = computed(() => caps.data?.suffixes ?? [])
const canDeleteFolder = computed(() => !unfiled.value && (folder.value?.documents ?? 1) === 0)

const active = computed(
  () => docs.docs.filter((doc) => doc.status === 'pending' || doc.status === 'processing').length,
)

const visibleDocs = computed(() => {
  const needle = query.value.trim().toLowerCase()
  const rows = docs.docs.filter((doc) => {
    if (filter.value === 'attention' && doc.state === 'ok') return false
    if (filter.value === 'ok' && doc.state !== 'ok') return false
    if (!needle) return true
    return (
      doc.filename.toLowerCase().includes(needle) || doc.source.toLowerCase().includes(needle)
    )
  })
  const sorted = [...rows]
  sorted.sort((left, right) => {
    if (sort.value === 'name') return left.filename.localeCompare(right.filename, 'zh-CN')
    if (sort.value === 'updated') return right.created_at.localeCompare(left.created_at)
    if (sort.value === 'chunks') return right.indexed_chunks - left.indexed_chunks
    return right.disk_size_bytes - left.disk_size_bytes
  })
  return sorted
})

/** 表头那一行读数:与表格里看得见的行是同一批(筛选之后不重算,免得两个数打架)。 */
const totals = computed(() => ({
  documents: docs.summary.total,
  chunks: docs.docs.reduce((sum, doc) => sum + doc.indexed_chunks, 0),
  size: docs.docs.reduce(
    (sum, doc) => sum + (doc.on_disk ? doc.disk_size_bytes : doc.size_bytes),
    0,
  ),
  issues:
    docs.summary.changed +
    docs.summary.not_indexed +
    docs.summary.file_missing +
    docs.summary.unregistered +
    docs.summary.in_flight,
  updated: docs.docs.reduce(
    (latest, doc) => (doc.created_at > latest ? doc.created_at : latest),
    '',
  ),
}))

function load(): void {
  setScope(folderId.value)
  selected.value = []
  query.value = ''
  filter.value = 'all'
  confirmingBulk.value = false
  confirmingFolder.value = false
  void refreshDocuments()
  void refreshFolders()
}

watch(folderId, load)

async function removeFolder(): Promise<void> {
  await deleteFolder(folderId.value)
  if (folders.error) return
  confirmingFolder.value = false
  void router.push({ name: 'knowledge' })
}

async function removeSelected(): Promise<void> {
  const ids = [...selected.value]
  selected.value = []
  confirmingBulk.value = false
  await removeDocuments(ids)
}

function onMove(documentId: string): void {
  moving.value = documentId
}

function openUpload(): void {
  uploadPanel.value?.openPicker()
}

/** 弹层关掉之后重拉:挪动改了归属,目录的份数与表格里的行都得跟着变。 */
function afterMove(): void {
  moving.value = ''
  void refreshDocuments()
  void refreshFolders()
}

function afterRename(): void {
  renaming.value = false
  void refreshDocuments()
}

function onVisible(): void {
  if (document.visibilityState === 'visible') void refreshDocuments()
}

onMounted(() => {
  load()
  window.addEventListener('focus', onVisible)
  document.addEventListener('visibilitychange', onVisible)
})

onUnmounted(() => {
  // 离开页面就停轮询:不然切到聊天页还在每 3 秒打一次清单
  stopPolling()
  window.removeEventListener('focus', onVisible)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<template>
  <main class="page">
    <div class="page-inner">
      <nav class="crumbs" aria-label="面包屑">
        <RouterLink :to="{ name: 'knowledge' }" class="crumb-link">
          <Icon name="folder" :size="13" /> 知识库
        </RouterLink>
        <Icon name="chevron-right" :size="13" class="crumb-sep" />
        <span class="crumb-here">{{ title }}</span>
      </nav>

      <header class="page-head">
        <div class="page-heading">
          <p class="page-eyebrow">KNOWLEDGE CATALOG</p>
          <div class="page-title">
            <h1>{{ title }}</h1>
            <span v-if="unfiled" class="tag soft">未归档</span>
            <span v-else-if="docs.folder" class="page-meta">{{ docs.folder.documents }} 份</span>
          </div>
          <p class="page-desc">{{ description }}</p>
        </div>
        <div class="page-actions">
          <span v-if="docs.polling" class="faint">
            <span class="spin"></span> {{ active }} 份处理中
          </span>
          <span v-if="docs.refreshedAt" class="faint">上次刷新 {{ docs.refreshedAt }}</span>
          <button class="ghost" type="button" :disabled="docs.loading" @click="refreshDocuments">
            <Icon name="refresh" :size="14" />
            {{ docs.loading ? '刷新中…' : '刷新' }}
          </button>
          <template v-if="!unfiled">
            <button class="ghost" type="button" :disabled="!docs.folder" @click="renaming = true">
              <Icon name="edit" :size="14" /> 重命名
            </button>
            <button
              v-if="!confirmingFolder"
              class="ghost danger-text"
              type="button"
              :disabled="!canDeleteFolder"
              :title="canDeleteFolder ? '删掉这个空目录(不动任何文档)' : '目录里还有文档,先把它们移走或删掉'"
              @click="confirmingFolder = true"
            >
              <Icon name="trash" :size="14" /> 删除目录
            </button>
            <template v-else>
              <button class="danger solid" type="button" @click="removeFolder">确认删除?</button>
              <button class="ghost" type="button" @click="confirmingFolder = false">取消</button>
            </template>
          </template>
        </div>
      </header>

      <p v-if="docs.error" class="alert error">{{ docs.error }}</p>
      <p v-if="folders.error" class="alert error">{{ folders.error }}</p>
      <p v-if="docs.lastDeleted" class="alert info">
        已删除 <code>{{ docs.lastDeleted.document_id }}</code> · 向量
        {{ docs.lastDeleted.deleted_chunks }} 块 · 原文件
        {{ docs.lastDeleted.removed_file ? '已删' : docs.lastDeleted.kept_source ? '保留' : '不在' }} ·
        登记 {{ docs.lastDeleted.removed_record ? '已删' : '不在' }}
      </p>
      <p v-if="docs.lastBatch" class="alert info">
        批量删除 {{ docs.lastBatch.documents }} 份 · 清掉向量 {{ docs.lastBatch.chunks }} 块
      </p>

      <div class="layout">
        <section class="main-pane">
          <div class="stat-strip">
            <span><strong>{{ totals.documents }}</strong> 份文档</span>
            <span><strong>{{ totals.chunks }}</strong> 块</span>
            <span><strong>{{ humanBytes(totals.size) }}</strong></span>
            <span v-if="totals.issues > 0" class="warn-text">
              <strong>{{ totals.issues }}</strong> 份需要处理
            </span>
            <span v-if="totals.updated" class="faint">最近 {{ totals.updated.slice(0, 10) }}</span>
          </div>

          <div class="toolbar">
            <label class="search">
              <Icon name="search" :size="14" />
              <input v-model="query" type="search" placeholder="搜索文档名或来源" aria-label="搜索文档" />
            </label>

            <select v-model="filter" aria-label="按状态筛选">
              <option value="all">全部状态</option>
              <option value="attention">只看需要处理</option>
              <option value="ok">只看已入库</option>
            </select>

            <select v-model="sort" aria-label="排序">
              <option value="name">按名称</option>
              <option value="updated">按更新时间</option>
              <option value="chunks">按分块数</option>
              <option value="size">按大小</option>
            </select>

            <span class="faint">{{ visibleDocs.length }} / {{ docs.docs.length }}</span>
          </div>

          <div v-if="selected.length > 0" class="bulk">
            <span>已选 <strong>{{ selected.length }}</strong> 份</span>
            <template v-if="!confirmingBulk">
              <button class="ghost small" type="button" @click="selected = []">清除选择</button>
              <button class="danger small" type="button" @click="confirmingBulk = true">
                删除所选
              </button>
            </template>
            <template v-else>
              <span class="faint">删除会同时清掉向量与原文件,不可撤销</span>
              <button class="ghost small" type="button" @click="confirmingBulk = false">取消</button>
              <button class="danger solid small" type="button" @click="removeSelected">
                确认删除 {{ selected.length }} 份?
              </button>
            </template>
          </div>

          <EmptyState
            v-if="missing"
            title="没有这个目录"
            hint="它可能已经被删掉了。回知识库看现在的目录列表;目录的 ID 在改名后不变,所以对不上多半是被删了。"
          >
            <RouterLink :to="{ name: 'knowledge' }" class="crumb-link">回知识库</RouterLink>
          </EmptyState>

          <div
            v-else-if="docs.docs.length === 0 && !docs.loading"
            class="empty-panel"
          >
            <span class="empty-visual"><Icon name="folder" :size="26" /></span>
            <p class="empty-kicker">{{ unfiled ? 'TO BE ORGANIZED' : 'EMPTY CATALOG' }}</p>
            <h2>{{ unfiled ? '尚未归档的文档会出现在这里' : '这个目录还没有文档' }}</h2>
            <p class="empty-lede">
              {{
                unfiled
                  ? '磁盘上已有、但尚未登记的资料会归入此处。上传或导入后，即可在问答中被检索。'
                  : '导入第一批资料，让这一主题下的知识可被检索、引用与追溯。'
              }}
            </p>
            <ol class="empty-steps">
              <li><span>01</span><div><strong>上传文档</strong><p>支持 {{ suffixList.join(' / ') || 'PDF / Word / Markdown' }}</p></div></li>
              <li><span>02</span><div><strong>自动解析</strong><p>完成切分、向量化与索引</p></div></li>
              <li><span>03</span><div><strong>开始问答</strong><p>回答自带原文引用</p></div></li>
            </ol>
            <div class="empty-actions">
              <button class="primary" type="button" @click="openUpload">
                <Icon name="plus" :size="14" /> 上传文档
              </button>
              <span class="faint">
                单份不超过 {{ caps.data?.max_upload_mb ?? '—' }} MB
              </span>
            </div>
          </div>

          <template v-else-if="!missing">
            <DocumentTable
              v-if="visibleDocs.length > 0"
              :docs="visibleDocs"
              :deleting="docs.deleting"
              :selected="selected"
              @update:selected="selected = $event"
              @preview="openPreview"
              @remove="removeDocument"
              @acl="aclFor = $event"
              @move="onMove"
            />
            <p v-else class="faint empty-filter">没有符合当前筛选的文档。</p>
          </template>

          <section v-if="!missing && docs.docs.length > 0" class="folder-note">
            <div class="note-copy">
              <span class="note-kicker">PARSING NOTES</span>
              <h2>解析与引用说明</h2>
              <p>
                PDF 按页提取并保留页码，Office 文档按段落切分；图片会随 PDF 一并归档。
                问答中的引用可直接回到对应原文段落。
              </p>
            </div>
            <div class="note-formats">
              <span v-for="suffix in suffixList.slice(0, 6)" :key="suffix">{{ suffix }}</span>
              <span v-if="suffixList.length === 0">PDF / DOCX / PPTX / MD</span>
            </div>
          </section>
        </section>

        <aside class="side-pane">
          <UploadPanel ref="uploadPanel" :folder="folderId" :folder-name="title" />

          <section class="panel">
            <h2>目录信息</h2>
            <dl>
              <div>
                <dt>目录 ID</dt>
                <dd class="mono">{{ unfiled ? 'unfiled(虚拟)' : folderId }}</dd>
              </div>
              <div>
                <dt>文档</dt>
                <dd>{{ docs.folder?.documents ?? 0 }} 份</dd>
              </div>
              <div v-if="!unfiled">
                <dt>创建</dt>
                <dd>{{ docs.folder?.created_at?.slice(0, 10) || '—' }}</dd>
              </div>
              <div v-if="!unfiled">
                <dt>最近更新</dt>
                <dd>{{ docs.folder?.updated_at?.slice(0, 10) || '—' }}</dd>
              </div>
            </dl>
          </section>

          <section class="panel tips">
            <h2>管理提示</h2>
            <ul>
              <li>文档更新后重新上传，保持引用与原文一致。</li>
              <li>目录只负责组织，不承载权限；权限在文档层单独配置。</li>
              <li>删除目录前需先移出全部文档，目录与文档互不牵连。</li>
            </ul>
          </section>
        </aside>
      </div>
    </div>

    <PreviewDialog v-if="docs.previewFor" />
    <DocumentAclDialog v-if="aclFor !== ''" :document-id="aclFor" @close="aclFor = ''" />
    <FolderDialog
      v-if="renaming && docs.folder"
      :folder="docs.folder"
      @close="renaming = false"
      @done="afterRename"
    />
    <MoveDocumentDialog
      v-if="moving !== ''"
      :document-id="moving"
      @close="afterMove"
    />
  </main>
</template>

<style scoped>
.crumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: -8px;
  font-size: 12px;
  color: var(--text-faint);
}

.crumb-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-muted);
}

.crumb-link:hover {
  color: var(--accent);
}

.crumb-sep {
  color: var(--text-faint);
}

.crumb-here {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.desc {
  margin: -10px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
}

.stat-strip {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  padding: 10px 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.stat-strip strong {
  color: var(--text);
  font-weight: 600;
}

.warn-text {
  color: var(--warn);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.search {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 200px;
  max-width: 320px;
  padding: 0 9px;
  height: 32px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-faint);
}

.search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-ring);
}

.search input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 0;
  height: 100%;
  font-size: 12.5px;
  color: var(--text);
}

.search input:focus {
  outline: none;
}

select {
  /* base.css 给 input/select/textarea 统一 `width: 100%` —— 工具栏里它们必须按内容宽,
     否则三个控件会各占一行(第一版就是这样:搜索框一行、两个下拉各一行)。 */
  width: auto;
  height: 32px;
  padding: 0 8px;
  font-size: 12px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.bulk {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 7px 10px;
  font-size: 12px;
  background: var(--accent-soft);
  border: 1px solid var(--accent-ring);
  border-radius: var(--radius-sm);
}

.small {
  height: 26px;
  padding: 0 8px;
  font-size: 11.5px;
}

.empty-filter {
  margin: 0;
  padding: 18px 0;
  text-align: center;
}

.empty-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 9px;
  min-height: 420px;
  padding: 42px 32px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-xl);
  background:
    radial-gradient(560px 300px at 50% 0%, color-mix(in srgb, var(--accent) 8%, transparent), transparent 70%),
    color-mix(in srgb, var(--bg) 74%, transparent);
  text-align: center;
}

.empty-visual {
  width: 54px;
  height: 54px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-lg);
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 16%, transparent);
}

.empty-kicker {
  margin: 8px 0 0;
  color: var(--accent);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.17em;
}

.empty-panel h2 {
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.03em;
}

.empty-lede {
  max-width: 500px;
  margin: 0;
  color: var(--text-muted);
  font-size: 12.5px;
  line-height: 1.75;
}

.empty-steps {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 9px;
  width: min(680px, 100%);
  margin: 14px 0 4px;
  padding: 0;
  list-style: none;
}

.empty-steps li {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 11px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 84%, transparent);
  text-align: left;
}

.empty-steps li > span {
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.empty-steps strong {
  display: block;
  font-size: 11.5px;
  font-weight: 600;
}

.empty-steps p {
  margin: 2px 0 0;
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.5;
}

.empty-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  flex-wrap: wrap;
  justify-content: center;
}

.empty-actions button {
  height: 36px;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 296px;
  gap: 20px;
  align-items: start;
}

.folder-note {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-top: 2px;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background:
    linear-gradient(120deg, color-mix(in srgb, var(--accent) 5%, transparent), transparent 62%),
    var(--bg);
}

.note-copy {
  min-width: 0;
}

.note-kicker {
  color: var(--accent);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.note-copy h2 {
  margin: 3px 0 4px;
  font-size: 12.5px;
  font-weight: 650;
}

.note-copy p {
  max-width: 600px;
  margin: 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.65;
}

.note-formats {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 5px;
  flex: none;
  max-width: 190px;
}

.note-formats span {
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--bg-muted);
  color: var(--text-faint);
  font-size: 9.5px;
  font-family: var(--font-mono);
}

.main-pane,
.side-pane {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel {
  padding: 14px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}

.panel h2 {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.panel.tips {
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 5%, transparent), transparent 64%),
    var(--bg);
}

.panel.tips ul {
  margin: 0;
  padding: 0 0 0 16px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.6;
}

dl {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

dl > div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
}

dt {
  color: var(--text-faint);
  flex: none;
}

dd {
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.danger-text {
  color: var(--danger);
}

@media (max-width: 1040px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .side-pane {
    order: -1;
  }
}

@media (max-width: 768px) {
  .page-actions .faint {
    display: none;
  }

  .empty-panel {
    min-height: 0;
    padding: 30px 18px;
  }

  .empty-steps {
    grid-template-columns: minmax(0, 1fr);
  }

  .folder-note {
    flex-direction: column;
    gap: 10px;
  }

  .note-formats {
    justify-content: flex-start;
    max-width: none;
  }
}
</style>
