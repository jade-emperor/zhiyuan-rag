<script setup lang="ts">
/**
 * 知识库**一级页面**：目录清单。点一格进二级页面（那一格里的文档）。
 *
 * 两条口径：
 *
 * - **读数来自后端一次对账**（`GET /api/v1/folders`）：份数 / 库内块数 / 需要处理的份数
 *   都是登记表、磁盘、向量库对完之后算的 —— 前端不拿列表长度自己数（那会漏掉
 *   语料目录里还没入库的文件）；
 * - **未分类恒定出现**：磁盘上刚放进来、还没登记的文件只能归它。它是兜底格子，
 *   不是错误状态，所以画成虚线边框而不是告警色。
 *
 * 页面本身不轮询：目录的计数只在"文档有增删/入库完成"时变，而那两件事都发生在二级页面，
 * 它会自己轮询并在回来时重拉。这一页在窗口重新可见 / 拿到焦点时刷一次就够了。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import FolderCard from '../components/FolderCard.vue'
import FolderDialog from '../components/FolderDialog.vue'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import PageShell from '../components/ui/PageShell.vue'
import { foldersState, refreshFolders } from '../stores/folders'
import { humanBytes, humanTime } from '../utils/format'

const router = useRouter()
const folders = foldersState()

const creating = ref(false)
const query = ref('')

/** 这份清单的读数（含未分类那一格）。 */
const totals = computed(() => {
  const all = [...folders.folders, ...(folders.unfiled ? [folders.unfiled] : [])]
  return {
    documents: all.reduce((sum, folder) => sum + folder.documents, 0),
    chunks: all.reduce((sum, folder) => sum + folder.chunks, 0),
    size: all.reduce((sum, folder) => sum + folder.size_bytes, 0),
    issues: all.reduce((sum, folder) => sum + folder.issues, 0),
  }
})

/** 搜索只过滤**真实目录**：未分类是兜底格子，不该因为搜不到关键词就消失。 */
const visible = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return folders.folders
  return folders.folders.filter(
    (folder) =>
      folder.name.toLowerCase().includes(needle) ||
      folder.description.toLowerCase().includes(needle),
  )
})

const recent = computed(() =>
  folders.folders
    .filter((folder) => folder.last_activity !== '')
    .slice()
    .sort((left, right) => right.last_activity.localeCompare(left.last_activity))
    .slice(0, 4),
)

const distribution = computed(() => {
  const all = [
    ...folders.folders,
    ...(folders.unfiled && folders.unfiled.documents > 0 ? [folders.unfiled] : []),
  ]
  return all
    .slice()
    .sort((left, right) => right.documents - left.documents)
    .slice(0, 6)
})

const distributionMax = computed(() =>
  Math.max(1, ...distribution.value.map((folder) => folder.documents)),
)

const empty = computed(
  () => folders.folders.length === 0 && (folders.unfiled?.documents ?? 0) === 0,
)

const hasCatalog = computed(
  () => visible.value.length > 0 || (folders.unfiled?.documents ?? 0) > 0,
)

const attention = computed(() => folders.folders.filter((folder) => folder.issues > 0))

function open(folderId: string): void {
  void router.push({ name: 'folder', params: { folder: folderId } })
}

function onVisible(): void {
  if (document.visibilityState === 'visible') void refreshFolders()
}

onMounted(() => {
  void refreshFolders()
  window.addEventListener('focus', onVisible)
  document.addEventListener('visibilitychange', onVisible)
})

onUnmounted(() => {
  window.removeEventListener('focus', onVisible)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<template>
  <PageShell
    eyebrow="KNOWLEDGE BASE"
    title="知识库"
    :meta="`${folders.summary.folders} 个目录`"
    description="以目录组织企业知识资产，统一沉淀、持续维护。每一次问答，都从这里的可信内容出发。"
  >
    <template #actions>
      <span v-if="folders.refreshedAt" class="faint">上次刷新 {{ folders.refreshedAt }}</span>
      <button class="ghost" type="button" :disabled="folders.loading" @click="refreshFolders">
        <Icon name="refresh" :size="14" />
        {{ folders.loading ? '刷新中…' : '刷新' }}
      </button>
      <button class="primary" type="button" @click="creating = true">
        <Icon name="plus" :size="14" /> 新建目录
      </button>
    </template>

    <p v-if="folders.error" class="alert error">{{ folders.error }}</p>

    <template v-if="empty">
      <section class="onboarding">
        <div class="onboarding-copy">
          <p class="onboarding-kicker">START HERE</p>
          <h2>搭建第一座企业知识库</h2>
          <p>
            建立目录、导入文档、开始问答。知源助手会自动完成解析、切分与索引，
            并在回答中标注每一段内容的出处。
          </p>
          <button class="primary" type="button" @click="creating = true">
            <Icon name="plus" :size="15" /> 新建第一个目录
          </button>
        </div>
        <ol class="onboarding-steps">
          <li>
            <span>01</span>
            <div><strong>建立目录</strong><p>按业务主题划分知识边界。</p></div>
          </li>
          <li>
            <span>02</span>
            <div><strong>导入文档</strong><p>支持 PDF、Word、Markdown 等常用格式。</p></div>
          </li>
          <li>
            <span>03</span>
            <div><strong>开始问答</strong><p>用自然语言检索，引用可回到原文。</p></div>
          </li>
        </ol>
      </section>
    </template>

    <template v-else>
      <section class="overview" aria-label="知识库概览">
        <article class="kpi">
          <span class="kpi-icon"><Icon name="file" :size="17" /></span>
          <div class="kpi-body">
            <strong>{{ totals.documents }}</strong>
            <span>份文档</span>
            <small>已纳入知识资产</small>
          </div>
        </article>
        <article class="kpi">
          <span class="kpi-icon"><Icon name="book" :size="17" /></span>
          <div class="kpi-body">
            <strong>{{ totals.chunks }}</strong>
            <span>个知识片段</span>
            <small>可被检索与引用</small>
          </div>
        </article>
        <article class="kpi">
          <span class="kpi-icon"><Icon name="folder" :size="17" /></span>
          <div class="kpi-body">
            <strong>{{ humanBytes(totals.size) }}</strong>
            <span>存储占用</span>
            <small>原始文档与索引</small>
          </div>
        </article>
        <article class="kpi" :class="{ attention: totals.issues > 0 }">
          <span class="kpi-icon"><Icon name="alert" :size="17" /></span>
          <div class="kpi-body">
            <strong>{{ totals.issues }}</strong>
            <span>项待处理</span>
            <small>{{ totals.issues > 0 ? '建议及时核对' : '知识状态良好' }}</small>
          </div>
        </article>
      </section>

      <div class="knowledge-layout">
        <section class="catalog">
          <header class="catalog-head">
            <div>
              <h2>知识目录</h2>
              <p>按主题组织文档，让检索范围清晰可控。</p>
            </div>
            <label class="search">
              <Icon name="search" :size="14" />
              <input v-model="query" type="search" placeholder="搜索目录" aria-label="搜索目录" />
            </label>
          </header>

          <div v-if="hasCatalog" class="grid">
            <FolderCard
              v-for="folder in visible"
              :key="folder.folder_id"
              :folder="folder"
              @open="open(folder.folder_id)"
            />
            <!-- 未分类是兜底格子：**空的就不占位置**（"0 份"的一张虚线圈看着像坏了）。
                 一旦有磁盘上的孤儿文件，它会自己出现。 -->
            <FolderCard
              v-if="folders.unfiled && folders.unfiled.documents > 0"
              :folder="folders.unfiled"
              @open="open(folders.unfiled.folder_id)"
            />
          </div>

          <EmptyState
            v-else
            title="没有匹配的目录"
            :hint="`没有找到与「${query.trim()}」相关的目录，换个关键词试试。`"
          >
            <button class="ghost" type="button" @click="query = ''">清除搜索</button>
          </EmptyState>

          <section v-if="hasCatalog" class="distribution" aria-label="知识分布">
            <header class="distribution-head">
              <div>
                <h2>知识分布</h2>
                <p>按目录中的文档数量排列</p>
              </div>
              <span class="faint">{{ folders.summary.used }} 个目录已有内容</span>
            </header>
            <div class="distribution-list">
              <div v-for="folder in distribution" :key="folder.folder_id" class="distribution-row">
                <button class="distribution-name" type="button" @click="open(folder.folder_id)">
                  {{ folder.name }}
                </button>
                <div class="distribution-track" aria-hidden="true">
                  <span :style="{ width: `${(folder.documents / distributionMax) * 100}%` }"></span>
                </div>
                <strong>{{ folder.documents }}</strong>
              </div>
            </div>
          </section>
        </section>

        <aside class="rail">
          <section v-if="attention.length > 0" class="rail-card attention-card">
            <header class="rail-head">
              <span class="rail-kicker">需要关注</span>
              <span class="tag warn">{{ totals.issues }}</span>
            </header>
            <p class="rail-desc">以下目录有文档需要核对或仍在处理中。</p>
            <ul class="attention-list">
              <li v-for="folder in attention.slice(0, 4)" :key="folder.folder_id">
                <button type="button" @click="open(folder.folder_id)">
                  <span>{{ folder.name }}</span>
                  <strong>{{ folder.issues }} 项</strong>
                </button>
              </li>
            </ul>
          </section>

          <section class="rail-card">
            <header class="rail-head">
              <div>
                <span class="rail-kicker">RECENT ACTIVITY</span>
                <h2>最近更新</h2>
              </div>
              <Icon name="refresh" :size="14" class="rail-icon" />
            </header>
            <ul v-if="recent.length > 0" class="recent-list">
              <li v-for="folder in recent" :key="folder.folder_id">
                <button type="button" @click="open(folder.folder_id)">
                  <span class="recent-icon"><Icon name="folder" :size="14" /></span>
                  <span class="recent-copy">
                    <strong>{{ folder.name }}</strong>
                    <small>{{ folder.documents }} 份 · {{ humanTime(folder.last_activity) }}</small>
                  </span>
                  <Icon name="chevron-right" :size="13" />
                </button>
              </li>
            </ul>
            <p v-else class="rail-empty">导入文档后，这里会显示最近维护的目录。</p>
          </section>

          <section class="rail-card guide-card">
            <span class="rail-kicker">MAINTENANCE</span>
            <h2>让知识持续可用</h2>
            <ol>
              <li>为目录撰写清晰说明，帮助团队理解知识边界。</li>
              <li>文档更新后重新导入，保持引用与原文一致。</li>
              <li>按需配置文档权限，让知识在正确的范围内流动。</li>
            </ol>
          </section>
        </aside>
      </div>
    </template>

    <FolderDialog v-if="creating" @close="creating = false" @done="creating = false" />
  </PageShell>
</template>

<style scoped>
.onboarding {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 0.9fr);
  gap: 40px;
  align-items: center;
  padding: 42px 44px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  background:
    radial-gradient(600px 320px at 12% 0%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 68%),
    var(--bg);
  box-shadow: var(--shadow-sm, var(--shadow-xs));
}

.onboarding-copy {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
}

.onboarding-kicker {
  margin: 0;
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.18em;
}

.onboarding h2 {
  margin: 0;
  font-size: 30px;
  font-weight: 650;
  letter-spacing: -0.04em;
}

.onboarding-copy > p:not(.onboarding-kicker) {
  max-width: 480px;
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.8;
}

.onboarding-copy button {
  margin-top: 8px;
  height: 38px;
}

.onboarding-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.onboarding-steps li {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 13px 15px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--bg) 80%, transparent);
}

.onboarding-steps li > span {
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.onboarding-steps strong {
  display: block;
  font-size: 13px;
  font-weight: 600;
}

.onboarding-steps p {
  margin: 3px 0 0;
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.55;
}

.overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.kpi {
  display: flex;
  align-items: flex-start;
  gap: 13px;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 3%, transparent), transparent 64%),
    var(--bg);
  box-shadow: var(--shadow-xs);
}

.kpi-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  flex: none;
  border-radius: var(--radius);
  background: var(--accent-soft);
  color: var(--accent);
}

.kpi-body {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 5px;
  min-width: 0;
}

.kpi-body strong {
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.04em;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.kpi-body > span {
  color: var(--text-muted);
  font-size: 11.5px;
}

.kpi-body small {
  flex-basis: 100%;
  margin-top: 2px;
  color: var(--text-faint);
  font-size: 10.5px;
}

.kpi.attention .kpi-icon {
  background: var(--warn-soft);
  color: var(--warn);
}

.knowledge-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 18px;
  align-items: start;
}

.catalog {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.catalog-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.catalog-head h2 {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.02em;
}

.catalog-head p {
  margin: 3px 0 0;
  color: var(--text-faint);
  font-size: 11.5px;
}

.search {
  display: flex;
  align-items: center;
  gap: 6px;
  width: min(280px, 100%);
  padding: 0 10px;
  height: 34px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
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

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 12px;
}

.distribution {
  margin-top: 4px;
  padding: 17px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 4%, transparent), transparent 54%),
    var(--bg);
  box-shadow: var(--shadow-xs);
}

.distribution-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 13px;
}

.distribution-head h2 {
  font-size: 13.5px;
  font-weight: 650;
}

.distribution-head p {
  margin: 2px 0 0;
  color: var(--text-faint);
  font-size: 10.5px;
}

.distribution-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.distribution-row {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr) 34px;
  gap: 10px;
  align-items: center;
}

.distribution-name {
  height: auto;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 11.5px;
  font-weight: 500;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.distribution-name:hover:not(:disabled) {
  background: transparent;
  color: var(--accent);
}

.distribution-track {
  height: 6px;
  overflow: hidden;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
}

.distribution-track span {
  display: block;
  height: 100%;
  min-width: 2px;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 48%, #7dd3fc));
}

.distribution-row strong {
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 600;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.rail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.rail-card {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  box-shadow: var(--shadow-xs);
}

.rail-card.attention-card {
  border-color: color-mix(in srgb, var(--warn) 22%, var(--border));
  background:
    linear-gradient(180deg, var(--warn-soft), transparent 72%),
    var(--bg);
}

.rail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.rail-kicker {
  display: block;
  color: var(--accent);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.rail-card h2 {
  margin-top: 3px;
  font-size: 13.5px;
  font-weight: 650;
}

.rail-icon {
  color: var(--text-faint);
}

.rail-desc {
  margin: 8px 0 9px;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.6;
}

.attention-list,
.recent-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.attention-list button,
.recent-list button {
  width: 100%;
  height: auto;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 4px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
  font-size: 11.5px;
  color: var(--text-muted);
}

.attention-list button:hover,
.recent-list button:hover {
  background: var(--bg-hover);
}

.attention-list strong {
  color: var(--warn);
  font-size: 11px;
}

.recent-list button {
  align-items: center;
  justify-content: flex-start;
}

.recent-icon {
  width: 25px;
  height: 25px;
  display: grid;
  place-items: center;
  flex: none;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--accent);
}

.recent-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.recent-copy strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 11.5px;
  font-weight: 550;
}

.recent-copy small {
  color: var(--text-faint);
  font-size: 10px;
}

.recent-list svg {
  color: var(--text-faint);
}

.rail-empty {
  margin: 8px 0 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.6;
}

.guide-card {
  background:
    radial-gradient(280px 180px at 100% 0%, var(--accent-soft), transparent 70%),
    var(--bg);
}

.guide-card ol {
  margin: 10px 0 0;
  padding: 0 0 0 17px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.65;
}

@media (max-width: 1100px) {
  .knowledge-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .rail {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  }
}

@media (max-width: 860px) {
  .overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .onboarding {
    grid-template-columns: minmax(0, 1fr);
    padding: 28px 24px;
  }
}

@media (max-width: 560px) {
  .overview {
    grid-template-columns: minmax(0, 1fr);
  }

  .grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .catalog-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .search {
    width: 100%;
  }
}
</style>
