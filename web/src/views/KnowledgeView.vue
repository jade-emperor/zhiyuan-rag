<script setup lang="ts">
/**
 * 知识库管理(M7.2 交付)。**这是路线图页,不是"做了一半的页面"** ——
 * 它明确写出下一步做什么、后端已经有哪些接口可以用,免得管理员以为功能坏了。
 *
 * 为什么不先放一个只读清单:那会让人以为"上传/删除就是没做",而真正缺的是
 * 「上传 → 轮询状态 → 删除连带删向量 → 预览」这条**完整链路**;一个只读清单
 * 既不能上传也不能删,反而更像坏的。M7.2 一次把它做完。
 */
import { capabilitiesState } from '../stores/capabilities'

const caps = capabilitiesState()
</script>

<template>
  <div class="page">
    <div class="card block">
      <h1>知识库管理</h1>
      <p class="muted">
        这一步(M7.2)交付:上传文档、轮询入库状态、删除(连带删除向量库里的块与磁盘文件)、预览块。
      </p>

      <h3>后端已经就绪的接口</h3>
      <ul>
        <li><code>GET /api/v1/capabilities</code> —— 支持的后缀与大小上限(上传框的 accept 从这里来)</li>
        <li><code>POST /api/v1/documents?filename=…</code> —— 上传并异步入库(202 + pending,后台跑)</li>
        <li><code>GET /api/v1/documents</code> —— 清单:文件名 / 块数 / 状态 / 时间 / 是否可检索</li>
        <li><code>DELETE /api/v1/documents/{id}</code> —— 登记行 + 向量 + 文件三处一起清(仅管理员)</li>
      </ul>

      <h3>当前后端支持的类型</h3>
      <p>
        <span v-for="suffix in caps.data?.suffixes ?? []" :key="suffix" class="tag soft">{{ suffix }}</span>
        <span v-if="(caps.data?.suffixes ?? []).length === 0" class="faint">还没读到 capabilities,请检查后端连接。</span>
      </p>
      <p class="faint">单个文件上限 {{ caps.data?.max_upload_mb ?? '—' }} MB。</p>

      <p class="alert info">
        为什么不做"每个文档一个向量集合":删除一整份文档要能一次删干净,靠的是块里的
        <code>doc_id</code>(登记 ID == 装载器 doc_id == 块里的 doc_id,有契约钉着)。
      </p>
    </div>
  </div>
</template>

<style scoped>
.page {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.block {
  max-width: 46rem;
  margin: 0 auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

h1 {
  font-size: 18px;
}

h3 {
  font-size: 14px;
  margin-top: 6px;
}

ul {
  margin: 0;
  padding-left: 20px;
  color: var(--text-muted);
}

li {
  margin: 2px 0;
}
</style>
