<script setup lang="ts">
/**
 * 知识库管理页。**只对管理员可见**(路由守卫 + 后端各自 `require_admin`)。
 *
 * 每张卡片有一个「权限」按钮:给这份文档选**某个人**或**某一类人**(组)。
 * 没配规则的文档人人可见,配了就只给名单里的人 + 管理员。
 *
 * 数据来源一眼看清:清单 `GET /api/v1/documents`、上传 `POST /api/v1/documents?filename=…`、
 * 删除 `DELETE /api/v1/documents/{id}`、预览 `GET /api/v1/documents/{id}/chunks`;
 * 上传框的 `accept` 与大小上限来自 `GET /api/v1/capabilities`。
 *
 * 四条口径:
 * - **清单是实时视图**:后端每次都把登记表、磁盘、向量库对一遍,每行带 `state` ——
 *   所以"后端删了 / 后端更新了"在这里当场看得出来(卡片显示的是 `state`,不是登记状态);
 * - **只要还有 pending/processing 就自动轮询**(3 秒),全落定就停 —— 见 `stores/documents.ts`;
 * - **窗口重新可见 / 拿到焦点时重拉一次**:页面开着不动时,别处的改动(CLI 灌完、
 *   有人往语料目录里放了文件)要等下一次操作才可见 —— 那正是"前端不是实时"的体感来源;
 *   刷新时间也摆在标题栏上,过期不过期一眼看得出来;
 * - **删除两步确认**,删完如实转述后端结果(删了几块 / 原件在不在 / 源材料留没留 / 登记行在不在)。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import DocumentAclDialog from '../components/DocumentAclDialog.vue'
import DocumentCard from '../components/DocumentCard.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import UploadPanel from '../components/UploadPanel.vue'
import {
  documentsState,
  openPreview,
  refreshDocuments,
  removeDocument,
  stopPolling,
} from '../stores/documents'

const docs = documentsState()

/** 正在改权限的那份文档。一次只开一个弹层。 */
const aclFor = ref('')

const active = computed(
  () => docs.docs.filter((doc) => doc.status === 'pending' || doc.status === 'processing').length,
)

const lastDeleted = computed(() => docs.lastDeleted)

/** 汇总那一行:**只列非零的项** —— 一排 `0` 读不出信息,反而把真问题淹了。 */
const summaryParts = computed(() => {
  const s = docs.summary
  const parts: string[] = [`共 ${s.total} 份`]
  if (s.ok) parts.push(`一致 ${s.ok}`)
  if (s.not_indexed) parts.push(`库内没块 ${s.not_indexed}`)
  if (s.file_missing) parts.push(`文件不在 ${s.file_missing}`)
  if (s.changed) parts.push(`文件已变更 ${s.changed}`)
  if (s.unregistered) parts.push(`未入库 ${s.unregistered}`)
  if (s.in_flight) parts.push(`处理中/失败 ${s.in_flight}`)
  return parts.join(' · ')
})

/** 有"对不上"的项时就把它点出来:一致的时候不该让这行显得像告警。 */
const hasMismatch = computed(
  () =>
    docs.summary.not_indexed +
      docs.summary.file_missing +
      docs.summary.changed +
      docs.summary.unregistered >
    0,
)

function onVisible(): void {
  if (document.visibilityState === 'visible') void refreshDocuments()
}

onMounted(() => {
  void refreshDocuments()
  // 回到这个窗口 / 从别的标签页切回来时重拉:后端那边刚发生的事,不该等用户想起来点刷新
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
  <div class="page">
    <UploadPanel />

    <main class="main">
      <header class="head">
        <h1>知识库文件</h1>
        <div class="head-right">
          <span v-if="docs.polling" class="faint">
            <span class="spin"></span> 有 {{ active }} 份在入库,每 3 秒自动刷新
          </span>
          <span v-if="docs.refreshedAt" class="faint" title="后端每次都拿登记表、磁盘、向量库对一遍">
            上次刷新 {{ docs.refreshedAt }}
          </span>
          <button class="ghost" type="button" :disabled="docs.loading" @click="refreshDocuments">
            {{ docs.loading ? '刷新中…' : '刷新清单' }}
          </button>
        </div>
      </header>

      <p v-if="docs.docs.length > 0" class="summary" :class="{ warn: hasMismatch }">
        {{ summaryParts }}
        <span v-if="hasMismatch" class="faint">
          —— 对不上的那几张卡片下面写了该怎么办(未入库的文件在 {{ docs.corpusDir }})
        </span>
      </p>

      <p v-if="docs.error" class="alert error">{{ docs.error }}</p>

      <p v-if="lastDeleted" class="alert info">
        已删除 <code>{{ lastDeleted.document_id }}</code>:向量
        {{ lastDeleted.deleted_chunks }} 块 · 原文件
        {{
          lastDeleted.removed_file
            ? '已删'
            : lastDeleted.kept_source
              ? '语料源材料,保留'
              : '本来就不在'
        }}
        · 登记行 {{ lastDeleted.removed_record ? '已删' : '本来就不在' }}。
      </p>

      <div v-if="docs.docs.length === 0 && !docs.loading" class="empty card">
        <p class="muted">
          知识库里还没有文档。左边上传一份试试;已经在语料目录里的文件会显示成「未入库」,
          用 `project-rag ingest --path …` 灌进来。
        </p>
      </div>

      <div class="grid">
        <DocumentCard
          v-for="doc in docs.docs"
          :key="`${doc.source}/${doc.document_id}`"
          :doc="doc"
          :deleting="docs.deleting === doc.document_id"
          @preview="openPreview(doc.document_id)"
          @remove="removeDocument(doc.document_id)"
          @acl="aclFor = doc.document_id"
        />
      </div>
    </main>

    <PreviewDialog v-if="docs.previewFor" />
    <DocumentAclDialog v-if="aclFor !== ''" :document-id="aclFor" @close="aclFor = ''" />
  </div>
</template>

<style scoped>
.page {
  flex: 1;
  display: flex;
  gap: 20px;
  padding: 20px;
  min-height: 0;
  overflow-y: auto;
}

.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.head-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

h1 {
  font-size: 18px;
}

.summary {
  margin: 0;
  font-size: 13px;
  color: var(--fg-soft, #9aa4b2);
}

.summary.warn {
  color: var(--warn, #d9a441);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px;
  align-content: start;
}

.empty {
  padding: 24px;
  text-align: center;
}
</style>
