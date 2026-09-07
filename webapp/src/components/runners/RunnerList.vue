<script setup lang="ts">
import type { Runner } from '@/types'
import RunnerCard from './RunnerCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { Server } from '@lucide/vue'

defineProps<{
  runners: Runner[]
}>()
</script>

<template>
  <div
    v-if="runners.length"
    class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
  >
    <RunnerCard v-for="runner in runners" :key="runner.id" :runner="runner" />
  </div>

  <div v-else class="overflow-hidden rounded-lg border border-border bg-card">
    <EmptyState
      :icon="Server"
      title="No runners registered"
      description="Register a runner to start provisioning workspaces for your AI coding agents."
    />
  </div>
</template>
