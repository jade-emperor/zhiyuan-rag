<script setup lang="ts">
/**
 * 一级页面的一张目录卡。整张卡是一个入口(点击 / 回车都进二级页面)。
 *
 * 四个读数都来自后端那次对账(份数 / 库内块数 / 占用 / 最近更新)—— 卡片不自己数,
 * 否则它会与点进去看到的列表各说一套。`issues` 只标"需要处理",**不把它写成"错误"**:
 * 在途入库的文档也算在里面。
 */
import { computed } from 'vue'

import type { FolderRecord } from '../api/types'
import { humanBytes, humanTime } from '../utils/format'
import Icon from './Icon.vue'

const props = defineProps<{ folder: FolderRecord }>()
const emit = defineEmits<{ open: [] }>()

const description = computed(() => props.folder.description || '—')
const updated = computed(() =>
  props.folder.last_activity ? humanTime(props.folder.last_activity) : '还没有文档',
)
</script>

<template>
  <article
    class="folder"
    :class="{ unfiled: folder.unfiled }"
    tabindex="0"
    role="link"
    :aria-label="`打开目录 ${folder.name}`"
    @click="emit('open')"
    @keydown.enter.prevent="emit('open')"
    @keydown.space.prevent="emit('open')"
  >
    <header>
      <span class="icon" aria-hidden="true"><Icon name="folder" :size="17" /></span>
      <h3 :title="folder.name">{{ folder.name }}</h3>
      <span v-if="folder.issues > 0" class="tag warn" :title="`${folder.issues} 份需要处理`">
        {{ folder.issues }}
      </span>
      <Icon class="chev" name="chevron-right" :size="15" />
    </header>

    <p class="desc" :title="description">{{ description }}</p>

    <footer>
      <span class="stat"><strong>{{ folder.documents }}</strong> 份</span>
      <span class="stat">{{ folder.chunks }} 块</span>
      <span class="stat">{{ humanBytes(folder.size_bytes) }}</span>
      <span class="time">{{ updated }}</span>
    </footer>
  </article>
</template>

<style scoped>
.folder {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 14px 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color 0.12s ease, box-shadow 0.12s ease, transform 0.12s ease;
}

.folder:hover,
.folder:focus-visible {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-xs);
}

.folder:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-ring);
}

/* 未分类是兜底格子:虚线边框让它一眼看出"这不是你建的目录" */
.folder.unfiled {
  border-style: dashed;
  background: var(--bg-muted);
}

header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.icon {
  width: 28px;
  height: 28px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: var(--radius-xs);
  background: var(--bg-sunken);
  color: var(--text-muted);
}

h3 {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chev {
  flex: none;
  color: var(--text-faint);
}

.folder:hover .chev {
  color: var(--text-muted);
}

.desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

footer {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.stat strong {
  color: var(--text);
  font-weight: 600;
}

.time {
  margin-left: auto;
  white-space: nowrap;
}

@media (max-width: 560px) {
  .time {
    margin-left: 0;
  }
}
</style>
