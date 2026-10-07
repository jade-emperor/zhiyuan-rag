<script setup lang="ts">
/**
 * 知识库管理页(M7.2)。**只对管理员可见**(路由守卫 + 后端各自 `require_admin`)。
 *
 * 数据来源一眼看清:清单 `GET /api/v1/documents`、上传 `POST /api/v1/documents?filename=…`、
 * 删除 `DELETE /api/v1/documents/{id}`、预览 `GET /api/v1/documents/{id}/chunks`;
 * 上传框的 `accept` 与大小上限来自 `GET /api/v1/capabilities`。
 *
 * 三条口径:
 * - **只要还有 pending/processing 就自动轮询**(3 秒),全落定就停 —— 见 `stores/documents.ts`;
 * - **删除两步确认**,删完如实转述后端结果(删了几块 / 文件在不在 / 登记行在不在);
 * - **预览显示的是父块**,与"入库 N 块(子块)"并排显示,免得被读成丢数据。
 */
import { computed, onMounted, onUnmounted } from 'vue'

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

const active = computed(
  () => docs.docs.filter((doc) => doc.status === 'pending' || doc.status === 'processing').length,
)

const lastDeleted = computed(() => docs.lastDeleted)

onMounted(() => {
  void refreshDocuments()
})

onUnmounted(() => {
  // 离开页面就停轮询:不然切到聊天页还在每 3 秒打一次清单
  stopPolling()
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
          <button class="ghost" type="button" :disabled="docs.loading" @click="refreshDocuments">
            {{ docs.loading ? '刷新中…' : '刷新清单' }}
          </button>
        </div>
      </header>

      <p v-if="docs.error" class="alert error">{{ docs.error }}</p>

      <p v-if="lastDeleted" class="alert info">
        已删除 <code>{{ lastDeleted.document_id }}</code>:向量
        {{ lastDeleted.deleted_chunks }} 块 · 原文件
        {{ lastDeleted.removed_file ? '已删' : '本来就不在' }} · 登记行
        {{ lastDeleted.removed_record ? '已删' : '本来就不在' }}。
      </p>

      <div v-if="docs.docs.length === 0 && !docs.loading" class="empty card">
        <p class="muted">知识库里还没有文档。左边上传一份试试。</p>
      </div>

      <div class="grid">
        <DocumentCard
          v-for="doc in docs.docs"
          :key="doc.document_id"
          :doc="doc"
          :deleting="docs.deleting === doc.document_id"
          @preview="openPreview(doc.document_id)"
          @remove="removeDocument(doc.document_id)"
        />
      </div>
    </main>

    <PreviewDialog v-if="docs.previewFor" />
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
