<script setup lang="ts">
/**
 * 报表页(M7.3 的延迟 + P0-7 的消耗)。**运维与管理员可见**
 * (路由 `needs: 'reports'` + 后端 `require_reports`)。
 *
 * 两半读的是**同一批账本行**:`GET /api/v1/reports/metrics` 给延迟,
 * `GET /api/v1/reports/cost` 给消耗,窗口是同一个。窗口白名单与分位数的样本量门槛来自
 * `capabilities.metrics` —— 前端一处都不写死(写死的话,后端改了窗口这里会拿着一个
 * 400 的窗口名去打)。
 *
 * **消耗那一半最容易画错的三处**(后端 `application/cost.py` 的口径):
 *
 * 1. `null` 金额 = **这个模型没配单价**,不是"花了 0 元" —— 显示「未配置单价」并指路
 *    (config.yaml 的 `cost.models`);
 * 2. `unmetered_calls > 0` = provider 没回报用量(或那一轮断了)—— 那部分**没有**算进金额,
 *    必须显眼地说出来,否则这个总数会被当成完整的账;
 * 3. 金额是"每百万 token"折算出来的,单次调用常常只有 0.000x 元 —— 显示到 6 位小数,
 *    只留两位会让一整天的账变成 0.00。
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
  return `北京时间 ${days} 的 ${windows} 为高峰(法定节假日除外);其余时段半价`
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
  <div class="page">
    <header class="head">
      <div>
        <h1>报表</h1>
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
        <h2>消耗</h2>
        <p class="faint">
          模型调用 <strong>{{ cost?.metered_calls ?? 0 }} 次</strong>(量到 token)
          <span v-if="(cost?.unmetered_calls ?? 0) > 0">
            · <strong>{{ cost?.unmetered_calls }} 次没量到 token</strong>(provider 没回报用量,
            或那一轮中途断了 —— 这部分<strong>没有算进金额</strong>)
          </span>
          ;FAQ 命中与检索为空都不调模型,所以不花钱。
          <span v-if="cost"> · 币种 {{ cost.currency }}</span>
        </p>
        <p v-if="metrics.costError" class="alert error">{{ metrics.costError }}</p>
        <div v-if="cost" class="kpis">
          <MetricCard
            label="输入 token"
            :value="tokens(cost.input_tokens)"
            :detail="`其中命中缓存 ${tokens(cost.cached_input_tokens)}(命中比未命中便宜 50 倍)`"
          />
          <MetricCard
            label="输出 token"
            :value="tokens(cost.output_tokens)"
            detail="含思考内容(provider 的计费口径)"
          />
          <MetricCard
            label="合计消耗"
            :value="money(cost.total_cost)"
            :detail="
              cost.all_priced
                ? `${cost.currency} · 单价来自 config.yaml 的 cost.models`
                : '还有模型没配单价 —— 这个数只是已知的部分'
            "
            :tone="cost.all_priced ? 'plain' : 'warn'"
          />
        </div>

        <!-- 缺口六连:每一种"算不出来"都要有话说,否则那个总数会被当成完整的账 -->
        <p v-if="cost && cost.unmetered_calls > 0" class="alert warn">
          有 <strong>{{ cost.unmetered_calls }}</strong> 次调用没量到 token —— 这部分**没有算进金额**。
        </p>
        <p v-if="cost && cost.cache_unknown_calls > 0" class="alert warn">
          有 <strong>{{ cost.cache_unknown_calls }}</strong> 次没回报缓存命中数,
          整段输入按<strong>未命中价</strong>计 —— 这是**上界**,实际可能更低。
        </p>
        <p v-if="cost && cost.period_unknown_calls > 0" class="alert warn">
          有 <strong>{{ cost.period_unknown_calls }}</strong> 次读不到发生时间,
          按<strong>高峰价</strong>计(偏保守,不会少报)。
        </p>
        <p v-if="cost && cost.zone_unknown_calls > 0" class="alert warn">
          有 <strong>{{ cost.zone_unknown_calls }}</strong> 次的时间戳<strong>没带时区</strong>
          (老账,或后端镜像还没重建),判不出高峰/空闲,一律按<strong>高峰价</strong>计 ——
          这部分是上界。
        </p>
        <p v-if="cost && cost.calendar_unknown_calls > 0" class="alert warn">
          有 <strong>{{ cost.calendar_unknown_calls }}</strong> 次落在那一年
          <strong>没填法定节假日</strong>的日子里,按<strong>高峰价</strong>计 ——
          把那一年的放假通知填进 <code>config.yaml</code> 的 <code>cost.holidays</code> 就能判准。
        </p>
        <p v-if="cost && cost.unpriced_models.length > 0" class="alert info">
          这些模型<strong>没配单价</strong>,所以只报了 token、没报钱:
          <code v-for="model in cost.unpriced_models" :key="model">{{ model }}</code>
          —— 把名字填进 <code>config.yaml</code> 的 <code>cost.models</code> 就有金额了。
        </p>

        <!-- 时段合计:与 provider 价目表一行行对得上(高峰/空闲差 2 倍) -->
        <p v-if="cost" class="faint">时段口径:{{ periodRuleText(cost) }}</p>

        <table v-if="cost && cost.periods.length > 0">
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

        <table v-if="cost && cost.lines.length > 0">
          <thead>
            <tr>
              <th>模型</th>
              <th>时段</th>
              <th class="num">调用</th>
              <th class="num">输入 token</th>
              <th class="num">其中命中缓存</th>
              <th class="num">输出 token</th>
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
              <td class="num" :title="line.total_cost === null ? '这个模型没配单价(不是花了 0 元)' : ''">
                {{ line.total_cost === null ? '未配置单价' : money(line.total_cost) }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="cost" class="faint">
          这个窗口里没有可算的模型调用 —— 走 FAQ 直答、检索为空拒答、寒暄的请求都不调模型。
        </p>
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
