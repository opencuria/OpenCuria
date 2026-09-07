<!--
  RunnersPanel — Extrahierter Runners-Kern aus RunnersView (Schritt 5).

  Enthält Liste + Create-Dialog ohne Page-Header. Pollt alle 10s (wie die View).
  Wird vom Settings-Sheet (Tab "Runners", nur Admin) und weiterhin von
  RunnersView wiederverwendet.
-->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { usePolling } from '@/composables/usePolling'
import RunnerList from '@/components/runners/RunnerList.vue'
import CreateRunnerDialog from '@/components/runners/CreateRunnerDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'

const runnerStore = useRunnerStore()

const { start } = usePolling(() => runnerStore.fetchRunners(), 10000)

onMounted(() => {
  start()
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-start justify-between gap-3">
      <p class="text-sm text-muted-foreground">Manage runner instances that execute AI coding agents.</p>
      <div class="shrink-0">
        <CreateRunnerDialog />
      </div>
    </div>

    <!-- Loading state -->
    <div
      v-if="runnerStore.loading && !runnerStore.runners.length"
      class="flex justify-center py-12"
    >
      <LoadingSpinner :size="24" />
    </div>

    <!-- Error state -->
    <div
      v-else-if="runnerStore.error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ runnerStore.error }}
    </div>

    <!-- Runner list -->
    <RunnerList v-else :runners="runnerStore.runners" />
  </div>
</template>
