/**
 * 主题:跟随系统 / 浅色 / 深色。选择持久化在 localStorage,写到 `<html data-theme>`。
 *
 * 三态而不是布尔:用户在浅色系统上仍可能想固定深色,此时「跟随系统」会把它翻回去。
 */

import { computed, reactive } from 'vue'

export type ThemeMode = 'system' | 'light' | 'dark'

const STORAGE_KEY = 'project-rag.theme'

function stored(): ThemeMode {
  const value = localStorage.getItem(STORAGE_KEY)
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system'
}

const state = reactive({ mode: stored() })

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolve(): 'light' | 'dark' {
  if (state.mode === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return state.mode
}

function apply(): void {
  document.documentElement.dataset.theme = resolve()
}

export const currentTheme = computed<'light' | 'dark'>(() => resolve())

export function themeMode(): ThemeMode {
  return state.mode
}

export function setTheme(mode: ThemeMode): void {
  state.mode = mode
  localStorage.setItem(STORAGE_KEY, mode)
  apply()
}

/** 快捷键与按钮共用:在两档之间切换(从 system 切走时按当前实际外观落定)。 */
export function toggleTheme(): void {
  setTheme(resolve() === 'dark' ? 'light' : 'dark')
}

/** 在挂载应用前调用一次,避免首帧闪白。 */
export function initTheme(): void {
  apply()
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (state.mode === 'system') apply()
  })
}
