<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AppHeader from './components/AppHeader.vue'

const route = useRoute()
// 登录页不显示顶部导航:那时候还没有"我是谁",导航上的每一项都可能 403
const showHeader = computed(() => route.name !== 'login')
</script>

<template>
  <div class="shell">
    <AppHeader v-if="showHeader" />
    <RouterView />
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
</style>
