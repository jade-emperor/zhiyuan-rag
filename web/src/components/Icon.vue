<script setup lang="ts">
/**
 * 单色线性图标（内联 SVG，零依赖）。
 *
 * 为什么不用 emoji：企业界面里 emoji 的字形随系统变、大小不齐、无法用 currentColor
 * 跟随主题；而图标库（lucide 等）在这个环境装不上（npm registry 不可达，CI 用 npm ci）。
 * 这里的路径只覆盖本页用到的十几个，全部 `stroke="currentColor"`。
 */

export type IconName =
  | 'plus'
  | 'search'
  | 'trash'
  | 'edit'
  | 'close'
  | 'send'
  | 'stop'
  | 'paperclip'
  | 'chevron-down'
  | 'chevron-right'
  | 'copy'
  | 'refresh'
  | 'thumb-up'
  | 'thumb-down'
  | 'panel'
  | 'menu'
  | 'file'
  | 'folder'
  | 'image'
  | 'alert'
  | 'check'
  | 'spark'
  | 'book'
  | 'chart'
  | 'users'
  | 'message'
  | 'sidebar'
  | 'external'
  | 'quote'
  | 'settings'
  | 'arrow-down'
  | 'sparkles'
  | 'sun'
  | 'moon'

withDefaults(defineProps<{ name: IconName; size?: number; stroke?: number }>(), {
  size: 16,
  stroke: 1.8,
})

const PATHS: Record<IconName, string[]> = {
  plus: ['M12 5v14', 'M5 12h14'],
  search: ['M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z', 'm21 21-4.35-4.35'],
  trash: ['M3 6h18', 'M8 6V4h8v2', 'M19 6l-1 14H6L5 6', 'M10 11v6', 'M14 11v6'],
  edit: ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z'],
  close: ['M18 6 6 18', 'M6 6l12 12'],
  send: ['M22 2 11 13', 'M22 2 15 22l-4-9-9-4Z'],
  stop: ['M6 6h12v12H6z'],
  paperclip: [
    'M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5',
  ],
  'chevron-down': ['m6 9 6 6 6-6'],
  'chevron-right': ['m9 6 6 6-6 6'],
  copy: ['M9 9h10v10H9z', 'M5 15H4V4h11v1'],
  refresh: ['M21 12a9 9 0 1 1-2.6-6.4', 'M21 3v6h-6'],
  'thumb-up': ['M7 10v10H4V10h3Z', 'M7 10l4-8a2 2 0 0 1 2 2v4h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7'],
  'thumb-down': ['M7 14V4H4v10h3Z', 'M7 14l4 8a2 2 0 0 0 2-2v-4h5a2 2 0 0 0 2-2.3l-1.2-6A2 2 0 0 0 16.8 4H7'],
  panel: ['M3 4h18v16H3z', 'M15 4v16'],
  menu: ['M4 6h16', 'M4 12h16', 'M4 18h16'],
  file: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z', 'M14 2v6h6'],
  folder: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z'],
  image: ['M3 3h18v18H3z', 'm3 16 5-5 4 4 3-3 6 6', 'M9 9h.01'],
  alert: ['M12 9v4', 'M12 17h.01', 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z'],
  check: ['m20 6-11 11-5-5'],
  spark: ['M12 3v4', 'M12 17v4', 'M3 12h4', 'M17 12h4', 'm5.6 5.6 2.8 2.8', 'm15.6 15.6 2.8 2.8', 'm18.4 5.6-2.8 2.8', 'm8.4 15.6-2.8 2.8'],
  book: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z'],
  chart: ['M3 3v18h18', 'M7 15v3', 'M12 10v8', 'M17 6v12'],
  users: ['M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2', 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M23 21v-2a4 4 0 0 0-3-3.87', 'M16 3.13a4 4 0 0 1 0 7.75'],
  message: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z'],
  sidebar: ['M3 4h18v16H3z', 'M9 4v16'],
  external: ['M15 3h6v6', 'M10 14 21 3', 'M21 14v7H3V3h7'],
  quote: ['M3 21c3-2 5-5 5-9V6H3v6h3', 'M14 21c3-2 5-5 5-9V6h-5v6h3'],
  settings: [
    'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z',
    'M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.5 1Z',
  ],
  'arrow-down': ['M12 5v14', 'm19 12-7 7-7-7'],
  sun: [
    'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
    'M12 2v2',
    'M12 20v2',
    'm4.93 4.93 1.41 1.41',
    'm17.66 17.66 1.41 1.41',
    'M2 12h2',
    'M20 12h2',
    'm6.34 17.66-1.41 1.41',
    'm19.07 4.93-1.41 1.41',
  ],
  moon: ['M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z'],
  sparkles: ['m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z', 'M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8Z'],
}
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <path v-for="(d, index) in PATHS[name]" :key="index" :d="d" />
  </svg>
</template>
