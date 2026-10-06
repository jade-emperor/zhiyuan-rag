<script setup lang="ts">
/**
 * 可观测看板(M7.3 交付)。路线图页,理由与 KnowledgeView 相同。
 *
 * 这里刻意把**口径**先写出来(`-` 不是 0、分段只算热行、p95 要 n≥20)——
 * 那是 M7.3 画表时最容易画错的地方,先把规矩摆在页面上,免得做成一张
 * "看起来很专业但读不出来"的图。
 */
</script>

<template>
  <div class="page">
    <div class="card block">
      <h1>可观测</h1>
      <p class="muted">
        这一步(M7.3)交付:窗口内的 p50/p95/p99、冷热条数、拒答/截断/中断率、最慢几条 ——
        数据来自 <code>GET /api/v1/admin/metrics</code>(已就绪,仅管理员)。
      </p>

      <h3>画之前要遵守的口径(P0-2 定的)</h3>
      <ul>
        <li><strong><code>-</code> 不是 0</strong>:<code>p95</code> 要 n≥20、<code>p99</code> 要 n≥100,否则后端给 <code>null</code>;画成 0 会让人以为"很快"</li>
        <li><strong>分段只算热行</strong>,最慢榜**含冷启动** —— 冷启动是用户真实等到的,不能从"最慢"里藏掉</li>
        <li><strong>样本量要一起显示</strong>:没有 n 的分位数读不出东西</li>
        <li>最近秩不插值:分位数是"真实存在的那一条",不是平均出来的假点</li>
      </ul>

      <h3>现在就能看的两种方式</h3>
      <ul>
        <li>命令行:<code>project-rag metrics --window 24h</code></li>
        <li>接口:<code>curl -H "Authorization: Bearer &lt;token&gt;" http://&lt;host&gt;:&lt;port&gt;/api/v1/admin/metrics?window=24h</code></li>
      </ul>
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
