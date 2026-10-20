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
 * 3. **模型与温度是只读的**:它们是服务端启动期配置,这里如实显示"现在用的是什么"。
 */
import { computed, onMounted } from 'vue'

import type { FusionMode, RetrievalMode } from '../api/types'
import { capabilitiesState } from '../stores/capabilities'
import { limits, paramsState, persist, restoreDefaults, weightsOk } from '../stores/params'
import { loadSources, sourcesState } from '../stores/sources'

/** embedded = 放进右侧抽屉时用自己的容器样式（不再占 320px 固定栏）。 */
const props = withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

const params = paramsState()
const caps = capabilitiesState()
const sources = sourcesState()

onMounted(() => {
  // 来源读的是**向量库里的真实来源**(不是配置白名单):要回答"我能检索哪几个库"
  void loadSources()
})

const retrieval = computed(() => caps.data?.retrieval ?? null)
const models = computed(() => caps.data?.models ?? [])
const modelSettings = computed(() => caps.data?.model_settings ?? null)

const MODE_LABELS: Record<RetrievalMode, string> = {
  vector: '向量',
  keyword: 'BM25',
  hybrid: '混合',
}
const FUSION_LABELS: Record<FusionMode, string> = {
  rrf: 'RRF',
  weighted: '加权',
}


function touch(): void {
  persist()
}
</script>

<template>
  <aside class="panel" :class="{ embedded: props.embedded }">
    <h3 v-if="!props.embedded">检索</h3>

    <p v-if="caps.error" class="alert error">{{ caps.error }}</p>

    <section>
      <h4>知识库</h4>
      <p v-if="sources.error" class="alert error small">{{ sources.error }}</p>
      <label class="option">
        <input v-model="params.source_filter" type="radio" :value="null" @change="touch" />
        <span>全部</span>
      </label>
      <label v-for="name in sources.items" :key="name" class="option">
        <input v-model="params.source_filter" type="radio" :value="name" @change="touch" />
        <span>{{ name }}</span>
      </label>
      <p v-if="sources.items.length === 0 && !sources.error" class="faint">暂无来源</p>
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
          <span>权重 <span class="faint">BM25 {{ params.bm25_weight.toFixed(2) }} / 向量 {{ params.vector_weight.toFixed(2) }}</span></span>
          <input v-model.number="params.bm25_weight" type="range" min="0" max="1" step="0.05" @change="touch" />
          <input v-model.number="params.vector_weight" type="range" min="0" max="1" step="0.05" @change="touch" />

          <p v-if="!weightsOk()" class="alert error">权重不能同时为 0</p>
        </div>
      </template>

      <label class="field">
        <span>k</span>
        <input
          v-model.number="params.k"
          type="number"
          min="1"
          :max="limits().k_max"
          @change="touch"
        />
      </label>

      <label class="field">
        <span>上下文</span>
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
        <span :title="retrieval?.rerank_available === false ? '未安装重排器' : ''">重排</span>
      </label>

      <button class="ghost small" type="button" @click="restoreDefaults(); touch()">恢复默认</button>
    </section>

    <section>
      <h4>模型</h4>
      <label class="field">
        <span>模型</span>
        <select :value="models.find((m) => m.default)?.id ?? ''" disabled>
          <option v-for="model in models" :key="model.id" :value="model.id">{{ model.label }}</option>
        </select>
      </label>

      <div class="field">
        <span>Temperature <span class="faint">{{ modelSettings?.temperature ?? '—' }}</span></span>
        <div class="readonly-bar"><div class="fill" :style="{ width: `${(modelSettings?.temperature ?? 0) * 100}%` }"></div></div>
      </div>
      <div class="field">
        <span>思考模式 <span class="faint">{{ modelSettings?.thinking ?? '—' }}</span></span>
      </div>
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

.panel.embedded {
  width: 100%;
  border-left: none;
  padding: 0;
  background: transparent;
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
