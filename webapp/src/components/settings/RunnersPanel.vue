<!--
  RunnersPanel — runner list plus create dialog. Polls every 10s. Admin-only tab.
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { usePolling } from '@/composables/usePolling'
import RunnerList from '@/components/runners/RunnerList.vue'
import CreateRunnerDialog from '@/components/runners/CreateRunnerDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'

import RunnerStorageDetail from '@/components/runners/RunnerStorageDetail.vue'
const props = defineProps<{ runnerId?: string; contextVersion?: number }>()
const emit = defineEmits<{ 'detail-change': [open: boolean] }>()
const selectedId = ref<string | null>(null)
const runnerStore = useRunnerStore()
const selected = computed(() => runnerStore.runners.find((r) => r.id === selectedId.value) ?? null)

// Only navigation context changes select a runner. Polling must never undo Back.
watch(
  () => [props.runnerId, props.contextVersion] as const,
  () => {
    selectedId.value = props.runnerId ?? null
  },
  { immediate: true },
)
watch(
  () => Boolean(selected.value),
  (open) => emit('detail-change', open),
  { immediate: true },
)
onUnmounted(() => emit('detail-change', false))

const { start } = usePolling(() => runnerStore.fetchRunners(), 10000)

onMounted(() => {
  start()
})
</script>

<template>
  <div class="min-w-0 space-y-6">
    <RunnerStorageDetail
      v-if="selected"
      :key="selected.id"
      :runner="selected"
      @back="selectedId = null"
    />
    <SettingsSection v-else description="Manage runner instances that execute AI coding agents.">
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

      <RunnerList v-else :runners="runnerStore.runners" @select="selectedId = $event.id" />
    </SettingsSection>
  </div>
</template>
