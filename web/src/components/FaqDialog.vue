<script setup lang="ts">
/**
 * 问答对的新建 / 编辑弹层。
 *
 * 长度上限**照抄后端的口径**（问题 512 字、答案 8000 字）只是为了让输入框在越界前
 * 就停住；真正的判定仍在 `application/faq_admin.py` —— 重名那句回执要说出是哪个 ID，
 * 前端拼不出来。
 */
import { computed, ref } from 'vue'

import type { FaqEntry } from '../api/types'
import { createFaq, faqState, updateFaq } from '../stores/faq'
import DialogShell from './ui/DialogShell.vue'

const props = defineProps<{ entry?: FaqEntry | null }>()
const emit = defineEmits<{ close: [] }>()

const faq = faqState()

const question = ref(props.entry?.question ?? '')
const answer = ref(props.entry?.answer ?? '')
const error = ref('')

const editing = computed(() => props.entry != null)
const title = computed(() => (editing.value ? '编辑问答对' : '新建问答对'))
const busy = computed(() =>
  editing.value ? faq.busy === `update:${props.entry?.id}` : faq.busy === 'create',
)

async function submit(): Promise<void> {
  if (!question.value.trim()) {
    error.value = '问题不能为空'
    return
  }
  if (!answer.value.trim()) {
    error.value = '答案不能为空'
    return
  }
  error.value = ''
  const ok = editing.value
    ? await updateFaq(props.entry!.id, {
        question: question.value,
        answer: answer.value,
      })
    : await createFaq(question.value, answer.value)
  if (!ok) {
    error.value = faq.error || '保存失败'
    return
  }
  emit('close')
}
</script>

<template>
  <DialogShell :label="title" :title="title" size="md" @close="emit('close')">
    <template #subtitle>
      命中即直答。问题写得越接近用户的原话，命中率越高。
    </template>

    <label class="field">
      <span>问题</span>
      <input
        v-model="question"
        type="text"
        maxlength="512"
        placeholder="例如：String、StringBuffer 和 StringBuilder 有什么区别？"
        autofocus
        @keydown.enter.prevent="submit"
      />
      <small class="faint">{{ question.length }} / 512</small>
    </label>

    <label class="field">
      <span>答案</span>
      <textarea
        v-model="answer"
        rows="10"
        maxlength="8000"
        placeholder="支持 Markdown（列表、代码块、加粗）"
      ></textarea>
      <small class="faint">{{ answer.length }} / 8000</small>
    </label>

    <p v-if="error" class="alert error">{{ error }}</p>

    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="busy" @click="submit">
        {{ busy ? '保存中…' : editing ? '保存' : '创建' }}
      </button>
    </template>
  </DialogShell>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field > span {
  font-size: 12px;
  color: var(--text-muted);
}

.field small {
  align-self: flex-end;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

textarea {
  resize: vertical;
  min-height: 160px;
  font-family: inherit;
  line-height: 1.6;
}
</style>
