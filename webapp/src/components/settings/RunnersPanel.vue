<!--
  RunnersPanel — runner list plus create dialog. Polls every 10s. Admin-only tab.
-->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { usePolling } from '@/composables/usePolling'
import RunnerList from '@/components/runners/RunnerList.vue'
import CreateRunnerDialog from '@/components/runners/CreateRunnerDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'

const runnerStore = useRunnerStore()

const { start } = usePolling(() => runnerStore.fetchRunners(), 10000)

onMounted(() => {
  start()
})
</script>

<template>
  <div class="space-y-6">
    <SettingsSection description="Manage runner instances that execute AI coding agents.">
      <template #actions>
        <CreateRunnerDialog />
      </template>

      <div
        v-if="runnerStore.loading && !runnerStore.runners.length"
        class="flex justify-center py-12"
      >
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="runnerStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ runnerStore.error }}
      </div>

      <RunnerList v-else :runners="runnerStore.runners" />
    </SettingsSection>
  </div>
</template>
