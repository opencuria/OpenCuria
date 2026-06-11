<script setup lang="ts">
import type { Runner } from '@/types'
import RunnerCard from './RunnerCard.vue'
import { Server } from '@lucide/vue'

defineProps<{
  runners: Runner[]
}>()
</script>

<template>
  <div v-if="runners.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
    <RunnerCard v-for="runner in runners" :key="runner.id" :runner="runner" />
  </div>

  <div
    v-else
    class="flex flex-col items-center justify-center py-12 px-6 text-center"
  >
    <div class="mb-4 text-muted-foreground">
      <Server :size="40" />
    </div>
    <h3 class="text-lg font-medium text-foreground mb-1">No runners registered</h3>
    <p class="text-sm text-muted-foreground max-w-sm">
      Register a runner to start provisioning workspaces for your AI coding agents.
    </p>
  </div>
</template>
