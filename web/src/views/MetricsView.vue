<script setup lang="ts">
/**
 * 报表页:延迟 + 消耗。**运维与管理员可见**。
 *
 * 两半读的是**同一批账本行**,窗口也是同一个。窗口白名单与分位数的样本量门槛来自
 * `capabilities.metrics` —— 这里一处都不写死(写死的话,后端改了窗口,这个页面会拿着
 * 一个 400 的窗口名去打)。
 *
 * **最容易画错的三处**(画错不会报错,只会骗人):
 *
 * 1. `null` **不是 0**:分位数样本不够时后端给 `null`(门槛见 capabilities),
 *    这里显示 `—` 并解释原因 —— 折算成 0 会让人以为"很快";
 * 2. **冷热各一套数**:分段分位数只算热行;拒答率、截断率、中断率、通道计数也**只数热行**,
 *    冷启动单独列在下面一行 —— 冷启动里用户等的是模型加载,混进来会让这一版的成绩
 *    随"这周重启过几次"漂;
 * 3. **比率一律带分子/分母**:3/4 与 300/400 都是 75%,但一个说明不了任何事。
 *    `null` 金额 = 这个模型没配单价(不是花了 0 元);金额显示到 6 位小数。
 */
import { computed, onMounted } from 'vue'

import MetricCard from '../components/MetricCard.vue'
import Surface from '../components/ui/Surface.vue'
import { capabilitiesState } from '../stores/capabilities'
import { loadMetrics, metricsState, setWindow } from '../stores/metrics'

const metrics = metricsState()
const caps = capabilitiesState()

const windows = computed(() => caps.data?.metrics.windows ?? [])
const minN = computed(() => caps.data?.metrics.min_n ?? { p50: 1, p95: 20, p99: 100 })
const data = computed(() => metrics.data)
const rows = computed(() => data.value?.rows ?? 0)

const cost = computed(() => metrics.cost)

/** 时段名:provider 的价目表叫"高峰/空闲",界面上就照它说。 */
/**
 * 把接口带回来的时段口径说成一句人话。
 *
 * **口径来自后端出参,不是这里写死的一句话**:界面另抄一份的话,配置改了而界面没改,
 * 就会指着一条"空闲时段"说"高峰时段是 09–12"—— 而这种错没人会发现。
 */
const WEEKDAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

function periodRuleText(cost: { peak_weekdays: number[]; peak_windows: string[][] }): string {
  if (cost.peak_windows.length === 0) return '这个部署不分时段(所有调用都按高峰价计)'
  const days =
    cost.peak_weekdays.length === 0
      ? '每天'
      : cost.peak_weekdays.length === 5 && cost.peak_weekdays.every((day) => day <= 5)
        ? '工作日'
        : cost.peak_weekdays.map((day) => WEEKDAY_NAMES[day - 1] ?? `星期${day}`).join('、')
  const windows = cost.peak_windows.map(([start, end]) => `${start}–${end}`).join(' / ')
  return `高峰 ${days} ${windows}`
}

function periodLabel(period: string): string {
  if (period === 'off_peak') return '空闲时段'
  if (period === 'peak') return '高峰时段'
  return period
}

/** 金额:`null` 由调用点显示成「未配置单价」;**保留 6 位小数**(单次调用常常只有 0.000x 元)。 */
function money(value: number): string {
  return `${cost.value?.currency ?? ''} ${value.toFixed(6)}`.trim()
}

/** token 数带千分位:六位数的 token 一眼看不出量级。 */
function tokens(value: number): string {
  return value.toLocaleString('en-US')
}

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
  <main class="page">
    <div class="page-inner">
      <header class="page-head">
        <div class="page-title">
          <h1>报表</h1>
          <span class="page-meta">
            {{ metrics.window || '—' }}
            <template v-if="metrics.fetchedAt"> · {{ metrics.fetchedAt }}</template>
            <template v-if="metrics.loading"> · 读取中…</template>
          </span>
        </div>
        <div class="page-actions">
          <div class="windows segmented">
            <button
              v-for="item in windows"
              :key="item"
              type="button"
              :class="{ active: item === metrics.window }"
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

      <div v-if="data && rows === 0" class="alert info">这个窗口里还没有请求</div>

      <template v-if="data">
        <section class="kpis">
          <MetricCard
            label="请求数"
            :value="String(data.rows)"
            :detail="`冷启动 ${data.cold_rows} · 热 ${data.warm_rows}`"
          />
          <MetricCard
            label="拒答率"
            :value="rate(data.warm.refused, data.warm.rows)"
            :detail="`${share(data.warm.refused, data.warm.rows)} 条`"
          />
          <MetricCard
            label="截断率"
            :value="rate(data.warm.truncated, data.warm.rows)"
            :detail="`${share(data.warm.truncated, data.warm.rows)} 条`"
            :tone="data.warm.truncated > 0 ? 'warn' : 'plain'"
          />
          <MetricCard
            label="中断率"
            :value="rate(data.warm.aborted, data.warm.rows)"
            :detail="`${share(data.warm.aborted, data.warm.rows)} 条`"
            :tone="data.warm.aborted > 0 ? 'warn' : 'plain'"
          />
          <MetricCard
            label="FAQ 命中率"
            :value="rate(data.warm.faq_hits, data.warm.rows)"
            :detail="`${share(data.warm.faq_hits, data.warm.rows)} 条`"
          />
          <MetricCard
            label="知识库回答"
            :value="String(data.warm.rag_answers)"
            detail="热行"
          />
        </section>

        <p v-if="data.cold_rows > 0" class="faint cold-note">
          只数热行 · 冷启动 {{ data.cold_rows }} 次：拒答 {{ data.cold.refused }} ·
          截断 {{ data.cold.truncated }} · 中断 {{ data.cold.aborted }} ·
          FAQ {{ data.cold.faq_hits }} · 知识库 {{ data.cold.rag_answers }}
        </p>

        <Surface title="消耗">
          <template #actions>
            <span class="faint">{{ periodRuleText(cost ?? { peak_weekdays: [], peak_windows: [] }) }}</span>
          </template>

          <p v-if="metrics.costError" class="alert error">{{ metrics.costError }}</p>
          <p v-if="cost" class="cost-line">
            模型调用 <strong>{{ cost.metered_calls }}</strong> 次
            <span v-if="cost.unmetered_calls > 0"> · 未计量 {{ cost.unmetered_calls }} 次</span>
            <span class="faint"> · {{ cost.currency }}</span>
          </p>

          <div v-if="cost" class="kpis">
            <MetricCard
              label="输入 token"
              :value="tokens(cost.input_tokens)"
              :detail="`其中命中缓存 ${tokens(cost.cached_input_tokens)}`"
            />
            <MetricCard label="输出 token" :value="tokens(cost.output_tokens)" detail="含思考" />
            <MetricCard
              label="合计消耗"
              :value="money(cost.total_cost)"
              :detail="cost.all_priced ? '已定价' : '部分未定价'"
              :tone="cost.all_priced ? 'plain' : 'warn'"
            />
          </div>

          <div v-if="cost" class="gaps">
            <span v-if="cost.unmetered_calls > 0" class="tag warn">未计量 {{ cost.unmetered_calls }}</span>
            <span v-if="cost.cache_unknown_calls > 0" class="tag warn">缓存未知 {{ cost.cache_unknown_calls }}</span>
            <span v-if="cost.period_unknown_calls > 0" class="tag warn">时段未知 {{ cost.period_unknown_calls }}</span>
            <span v-if="cost.zone_unknown_calls > 0" class="tag warn">时区未知 {{ cost.zone_unknown_calls }}</span>
            <span v-if="cost.calendar_unknown_calls > 0" class="tag warn">日历未知 {{ cost.calendar_unknown_calls }}</span>
            <span v-if="cost.unpriced_models.length > 0" class="tag info">
              未定价 {{ cost.unpriced_models.join(' / ') }}
            </span>
          </div>

          <table v-if="cost && cost.periods.length > 0" class="data-table">
            <thead>
              <tr>
                <th>时段</th>
                <th class="num">调用</th>
                <th class="num">输入 token</th>
                <th class="num">输出 token</th>
                <th class="num">金额</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in cost.periods" :key="item.period">
                <td>{{ periodLabel(item.period) }}</td>
                <td class="num">{{ item.calls }}</td>
                <td class="num">{{ tokens(item.input_tokens) }}</td>
                <td class="num">{{ tokens(item.output_tokens) }}</td>
                <td class="num">{{ money(item.total_cost) }}</td>
              </tr>
            </tbody>
          </table>

          <table v-if="cost && cost.lines.length > 0" class="data-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>时段</th>
                <th class="num">调用</th>
                <th class="num">输入</th>
                <th class="num">命中缓存</th>
                <th class="num">输出</th>
                <th class="num">金额</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="line in cost.lines" :key="`${line.model}-${line.period}`">
                <td><code>{{ line.model }}</code></td>
                <td>{{ periodLabel(line.period) }}</td>
                <td class="num">{{ line.calls }}</td>
                <td class="num">{{ tokens(line.input_tokens) }}</td>
                <td class="num">{{ tokens(line.cached_input_tokens) }}</td>
                <td class="num">{{ tokens(line.output_tokens) }}</td>
                <td class="num">{{ line.total_cost === null ? '未配置单价' : money(line.total_cost) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else-if="cost" class="faint">暂无调用</p>
        </Surface>

        <Surface title="分段延迟">
          <template #actions>
            <span class="faint">ms · 热行 · — = 样本不够</span>
          </template>
          <table class="data-table">
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
        </Surface>

        <Surface title="最慢的几条">
          <template #actions>
            <span class="faint">含冷启动</span>
          </template>
          <table class="data-table">
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
                <td colspan="4" class="faint">还没有请求</td>
              </tr>
            </tbody>
          </table>
        </Surface>
      </template>
    </div>
  </main>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px;
}

.cost-line {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--text-muted);
}

.gaps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 10px;
}

.cold-note {
  margin: -8px 0 0;
}

:deep(.surface-body) {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

tr.total td {
  font-weight: 600;
  background: var(--bg-muted);
}

td .tag {
  margin-left: 6px;
}

@media (max-width: 768px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .page-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .windows {
    max-width: 100%;
    overflow-x: auto;
  }
}
</style>
