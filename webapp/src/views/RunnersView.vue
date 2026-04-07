<script setup lang="ts">
import { onMounted } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { usePolling } from '@/composables/usePolling'
import RunnerList from '@/components/runners/RunnerList.vue'
import CreateRunnerDialog from '@/components/runners/CreateRunnerDialog.vue'
import { UiSpinner } from '@/components/ui'

const runnerStore = useRunnerStore()

const { start } = usePolling(() => runnerStore.fetchRunners(), 10000)

onMounted(() => {
  start()
})
</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-fg">Runners</h2>
        <p class="text-sm text-muted-fg mt-1">
          Manage runner instances that execute AI coding agents.
        </p>
      </div>
      <CreateRunnerDialog />
    </div>

    <!-- Loading state -->
    <div v-if="runnerStore.loading && !runnerStore.runners.length" class="flex justify-center py-12">
      <UiSpinner :size="24" />
    </div>

    <!-- Error state -->
    <div
      v-else-if="runnerStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ runnerStore.error }}
    </div>

    <!-- Runner list -->
    <RunnerList v-else :runners="runnerStore.runners" />
  </div>
</template>
