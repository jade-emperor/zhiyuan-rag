<script setup lang="ts">
/**
 * 可观测看板(M7.3)。**只对管理员可见**(路由守卫 + 后端 `require_admin`)。
 *
 * 数据来自 `GET /api/v1/admin/metrics`;窗口白名单与分位数的样本量门槛来自
 * `capabilities.metrics` —— 前端一处都不写死(写死的话,后端改了窗口这里会拿着一个
 * 400 的窗口名去打)。
 *
 * **这张表最容易画错的三处**(后端 `application/metrics.py` 的口径,画错不会报错,只会骗人):
 *
 * 1. `null` **不是 0**:分位数样本不够时后端给 `null`(p95 要 n≥20、p99 要 n≥100),
 *    这里显示 `—` 并解释门槛 —— 折算成 0 会让人以为"很快";
 * 2. **分段只算热行,最慢榜含冷启动**:冷启动是用户真实等到的,不能从"最慢"里藏掉,
 *    也不能混进分位数(那会让 p95 变成"冷启动有多慢");
 * 3. **比率一律带分子/分母**:3/4 与 300/400 都是 75%,但一个说明不了任何事。
 */
import { computed, onMounted } from 'vue'

import MetricCard from '../components/MetricCard.vue'
import { capabilitiesState } from '../stores/capabilities'
import { loadMetrics, metricsState, setWindow } from '../stores/metrics'

const metrics = metricsState()
const caps = capabilitiesState()

const windows = computed(() => caps.data?.metrics.windows ?? [])
const minN = computed(() => caps.data?.metrics.min_n ?? { p50: 1, p95: 20, p99: 100 })
const data = computed(() => metrics.data)
const rows = computed(() => data.value?.rows ?? 0)

/** `null` → `—`。**不许折算成 0**(那是这张表最容易犯的错)。 */
function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  return `${value.toFixed(1)} ms`
}

function rate(part: number, whole: number): string {
  if (whole <= 0) return '—'
  return `${((part / whole) * 100).toFixed(1)}%`
}

function share(part: number, whole: number): string {
  if (whole <= 0) return '—'
  return `${part}/${whole}`
}

/** 这一格为什么是 `—`:要么样本不够(给出门槛),要么这一轮根本没量到。 */
function whyMissing(kind: 'p50' | 'p95' | 'p99', n: number): string {
  const need = minN.value[kind]
  if (n < need) return `样本不够:这一格要 n≥${need}(当前 n=${n})`
  return '这一轮没量到(没做这一步,或老数据没这个字段)'
}

onMounted(() => {
  void loadMetrics()
})
</script>

<template>
  <div class="page">
    <header class="head">
      <div>
        <h1>可观测</h1>
        <p class="faint">
          延迟账本按请求一行;下面是
          <strong>{{ metrics.window || '—' }}</strong> 窗口里的汇总
          <span v-if="metrics.fetchedAt">· 取数于 {{ metrics.fetchedAt }}</span>
          <span v-if="metrics.loading"> · <span class="spin"></span> 读取中…</span>
        </p>
      </div>
      <div class="controls">
        <div class="windows">
          <button
            v-for="item in windows"
            :key="item"
            type="button"
            :class="{ primary: item === metrics.window }"
            @click="setWindow(item)"
          >
            {{ item }}
          </button>
        </div>
        <button class="ghost" type="button" :disabled="metrics.loading" @click="loadMetrics">
          刷新
        </button>
      </div>
    </header>

    <p v-if="metrics.error" class="alert error">{{ metrics.error }}</p>

    <div v-if="data && rows === 0" class="alert info">
      这个窗口里还没有请求 —— 去「对话」页问几个问题,回来刷新就有了。
      (报表读不到数据与报表是空的不是一回事:没接账本表时接口给 503,不会拿空表冒充。)
    </div>

    <template v-if="data">
      <section class="kpis">
        <MetricCard
          label="请求数"
          :value="String(data.rows)"
          :detail="`冷启动 ${data.cold_rows} · 热 ${data.warm_rows}`"
        />
        <MetricCard
          label="拒答率"
          :value="rate(data.refused, data.rows)"
          :detail="`${share(data.refused, data.rows)} 条拒答(检索为空直接拒答,不调模型)`"
          :tone="data.rows === 0 ? 'plain' : 'plain'"
        />
        <MetricCard
          label="截断率"
          :value="rate(data.truncated, data.rows)"
          :detail="`${share(data.truncated, data.rows)} 条撞到 max_tokens`"
          :tone="data.truncated > 0 ? 'warn' : 'plain'"
        />
        <MetricCard
          label="中断率"
          :value="rate(data.aborted, data.rows)"
          :detail="`${share(data.aborted, data.rows)} 条被客户端断开(用户没耐心了)`"
          :tone="data.aborted > 0 ? 'warn' : 'plain'"
        />
        <MetricCard
          label="FAQ 命中率"
          :value="rate(data.faq_hits, data.rows)"
          :detail="`${share(data.faq_hits, data.rows)} 条直答(这些连模型都没调)`"
        />
        <MetricCard
          label="知识库回答"
          :value="String(data.rag_answers)"
          detail="走 RAG 通道并给出答案的条数"
        />
      </section>

      <section class="card block">
        <h2>分段延迟</h2>
        <p class="faint">
          单位毫秒(≥1 秒显示成秒)。**只统计热行** —— 冷启动那几行被模型加载主导,
          混进来 p95 就变成"冷启动有多慢"。分段顺序就是一次回答里的先后顺序。
          <code>—</code> = 样本不够(<code>p95</code> 要 n≥{{ minN.p95 }}、<code>p99</code> 要
          n≥{{ minN.p99 }})或这一轮没量到,**不折算成 0**。
        </p>
        <table>
          <thead>
            <tr>
              <th>分段</th>
              <th class="num">n</th>
              <th class="num">p50</th>
              <th class="num">p95</th>
              <th class="num">p99</th>
              <th class="num">最大</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="segment in data.segments"
              :key="segment.name"
              :class="{ total: segment.name === 'total_ms' }"
            >
              <td>
                <code>{{ segment.name }}</code>
                <span v-if="segment.name === 'total_ms'" class="tag info">端到端</span>
              </td>
              <td class="num">{{ segment.n }}</td>
              <td class="num" :title="segment.p50 === null ? whyMissing('p50', segment.n) : ''">
                {{ ms(segment.p50) }}
              </td>
              <td class="num" :title="segment.p95 === null ? whyMissing('p95', segment.n) : ''">
                {{ ms(segment.p95) }}
              </td>
              <td class="num" :title="segment.p99 === null ? whyMissing('p99', segment.n) : ''">
                {{ ms(segment.p99) }}
              </td>
              <td class="num">{{ ms(segment.maximum) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="card block">
        <h2>最慢的几条</h2>
        <p class="faint">
          **含冷启动** —— 那是用户真实等到的;行数不够时这一榜会比上面的表更"难看",那是正常的。
        </p>
        <table>
          <thead>
            <tr>
              <th>请求</th>
              <th>渠道</th>
              <th>冷热</th>
              <th class="num">总耗时</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in data.slowest" :key="row.request_id">
              <td><code>{{ row.request_id }}</code></td>
              <td>{{ row.channel }}</td>
              <td>
                <span class="tag" :class="row.cold ? 'warn' : 'soft'">
                  {{ row.cold ? '冷启动' : '热' }}
                </span>
              </td>
              <td class="num">{{ ms(row.total_ms) }}</td>
            </tr>
            <tr v-if="data.slowest.length === 0">
              <td colspan="4" class="faint">这个窗口里没有请求。</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h1 {
  font-size: 18px;
}

h2 {
  font-size: 15px;
}

.controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.windows {
  display: flex;
  gap: 4px;
}

.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.block {
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}

th,
td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}

th {
  color: var(--text-faint);
  font-weight: 500;
  font-size: 12px;
}

.num {
  text-align: right;
}

tr.total td {
  font-weight: 600;
  background: var(--bg-muted);
}

td .tag {
  margin-left: 6px;
}
</style>
