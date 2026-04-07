<script setup lang="ts">
import type { Runner } from '@/types'
import RunnerCard from './RunnerCard.vue'
import { UiEmptyState } from '@/components/ui'
import { Server } from 'lucide-vue-next'

defineProps<{
  runners: Runner[]
}>()
</script>

<template>
  <div v-if="runners.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
    <RunnerCard v-for="runner in runners" :key="runner.id" :runner="runner" />
  </div>

  <UiEmptyState
    v-else
    title="No runners registered"
    description="Register a runner to start provisioning workspaces for your AI coding agents."
  >
    <template #icon>
      <Server :size="40" />
    </template>
  </UiEmptyState>
</template>
