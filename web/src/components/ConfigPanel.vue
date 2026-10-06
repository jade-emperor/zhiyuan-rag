<script setup lang="ts">
/**
 * 右侧参数面板(对齐作者给的参考图:知识库 / 检索设置 / 模型设置)。
 *
 * 三条与后端对齐的口径,写在这里免得后面有人"顺手"改坏:
 *
 * 1. **每个选项都来自 `capabilities`**,不是前端写死的常量 —— 后端加一种融合方式,
 *    这里自动多一个选项;
 * 2. **权重只在混合检索下露出**,因为单路时它没有任何作用(后端那边 mode 与权重
 *    也不能同时给:两个真值来源必然打架)。给用户一个调了不生效的滑条比不给他调更糟;
 * 3. **模型与温度是只读的**:它们是服务端启动期配置,开放按请求覆盖要动后端与账本口径
 *    (触发条件见 docs/frontend.md §5)。这里如实显示"服务端现在用的是什么"。
 */
import { computed, onMounted, ref } from 'vue'

import * as api from '../api/endpoints'
import type { FusionMode, RetrievalMode } from '../api/types'
import { capabilitiesState } from '../stores/capabilities'
import { limits, paramsState, persist, restoreDefaults, weightsOk } from '../stores/params'

const params = paramsState()
const caps = capabilitiesState()

const sources = ref<string[]>([])
const sourcesError = ref('')

onMounted(async () => {
  try {
    // 来源读的是**向量库里的真实来源**(不是配置白名单):要回答"我能检索哪几门课"
    const result = await api.sources()
    sources.value = result.sources
  } catch (error) {
    sourcesError.value = (error as Error).message
  }
})

const retrieval = computed(() => caps.data?.retrieval ?? null)
const models = computed(() => caps.data?.models ?? [])
const modelSettings = computed(() => caps.data?.model_settings ?? null)

const MODE_LABELS: Record<RetrievalMode, string> = {
  vector: '向量检索',
  keyword: '关键字检索(BM25)',
  hybrid: '混合检索(两路)',
}
const FUSION_LABELS: Record<FusionMode, string> = {
  rrf: 'RRF —— 只用排名,免标定',
  weighted: '加权 —— 各自 min-max 归一化后相加',
}

const weightHint = computed(() =>
  params.fusion === 'weighted'
    ? '两路分数先各自 min-max 归一化到 0~1,再按权重相加:能表达“哪一路更可信”。'
    : 'RRF 下这两个是**排名权重**:权重越大,那一路的名次在融合里越占优。',
)

function touch(): void {
  persist()
}
</script>

<template>
  <aside class="panel">
    <h3>Configuration</h3>

    <p v-if="caps.error" class="alert error">{{ caps.error }}</p>

    <section>
      <h4>知识库</h4>
      <p class="faint">按来源过滤。M7.1 只支持单选;多选要改后端端口签名(见 docs/frontend.md §5)。</p>
      <p v-if="sourcesError" class="alert error small">{{ sourcesError }}</p>
      <label class="option">
        <input v-model="params.source_filter" type="radio" :value="null" @change="touch" />
        <span>全部来源</span>
      </label>
      <label v-for="name in sources" :key="name" class="option">
        <input v-model="params.source_filter" type="radio" :value="name" @change="touch" />
        <span>{{ name }}</span>
      </label>
      <p v-if="sources.length === 0 && !sourcesError" class="faint">向量库里还没有来源。</p>
    </section>

    <section>
      <h4>检索设置</h4>

      <label class="field">
        <span>检索方式</span>
        <select v-model="params.mode" @change="touch">
          <option v-for="mode in retrieval?.modes ?? []" :key="mode" :value="mode">
            {{ MODE_LABELS[mode] }}
          </option>
        </select>
      </label>

      <template v-if="params.mode === 'hybrid'">
        <label class="field">
          <span>融合方式</span>
          <select v-model="params.fusion" @change="touch">
            <option v-for="fusion in retrieval?.fusions ?? []" :key="fusion" :value="fusion">
              {{ FUSION_LABELS[fusion] }}
            </option>
          </select>
        </label>

        <div class="field">
          <span>两路权重 <span class="faint">BM25 {{ params.bm25_weight.toFixed(2) }} / 向量 {{ params.vector_weight.toFixed(2) }}</span></span>
          <input v-model.number="params.bm25_weight" type="range" min="0" max="1" step="0.05" @change="touch" />
          <input v-model.number="params.vector_weight" type="range" min="0" max="1" step="0.05" @change="touch" />
          <p class="faint">{{ weightHint }}</p>
          <p v-if="!weightsOk()" class="alert error">两路权重不能同时为 0:那等于什么都不检索。</p>
        </div>
      </template>

      <label class="field">
        <span>召回条数 k <span class="faint">每一路各取几条</span></span>
        <input
          v-model.number="params.k"
          type="number"
          min="1"
          :max="limits().k_max"
          @change="touch"
        />
      </label>

      <label class="field">
        <span>引用块数 <span class="faint">最多几条父块进上下文</span></span>
        <input
          v-model.number="params.max_context"
          type="number"
          min="1"
          :max="limits().max_context_max"
          @change="touch"
        />
      </label>

      <label class="option">
        <input
          v-model="params.rerank"
          type="checkbox"
          :disabled="retrieval !== null && !retrieval.rerank_available"
          @change="touch"
        />
        <span>
          重排
          <span class="faint">
            ({{ retrieval?.rerank_available === false ? '这个部署没装重排器' : 'CrossEncoder 精排;实测中位 0.7s / p95 5.4s' }})
          </span>
        </span>
      </label>

      <button class="ghost small" type="button" @click="restoreDefaults(); touch()">恢复默认</button>
    </section>

    <section>
      <h4>模型设置</h4>
      <label class="field">
        <span>模型</span>
        <select :value="models.find((m) => m.default)?.id ?? ''" disabled>
          <option v-for="model in models" :key="model.id" :value="model.id">{{ model.label }}</option>
        </select>
      </label>
      <p class="faint">预留位:当前只有一个模型,选择走服务端配置(改 config.yaml 后重启)。</p>

      <div class="field">
        <span>Temperature <span class="faint">{{ modelSettings?.temperature ?? '—' }}</span></span>
        <div class="readonly-bar"><div class="fill" :style="{ width: `${(modelSettings?.temperature ?? 0) * 100}%` }"></div></div>
      </div>
      <div class="field">
        <span>思考模式 <span class="faint">{{ modelSettings?.thinking ?? '—' }}</span></span>
      </div>
      <p class="faint">这两项是服务端配置:按请求覆盖会绕过配置与延迟账的口径。</p>
    </section>
  </aside>
</template>

<style scoped>
.panel {
  width: var(--panel);
  flex: none;
  border-left: 1px solid var(--border);
  background: var(--bg);
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
}

h3 {
  font-size: 15px;
}

section {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

h4 {
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin: 0;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--text);
  font-size: 13px;
}

.option input {
  width: auto;
  margin-top: 4px;
}

.small {
  font-size: 12px;
  padding: 4px 8px;
  align-self: flex-start;
}

.readonly-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--bg-sunken);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--border-strong);
}
</style>
