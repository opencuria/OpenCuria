<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { usePolling } from '@/composables/usePolling'
import { WorkspaceStatus } from '@/types'
import type { VmSystemMetrics } from '@/types'
import WorkspaceList from '@/components/workspaces/WorkspaceList.vue'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import { UiSpinner, UiInput } from '@/components/ui'
import { Search, Filter } from 'lucide-vue-next'
import { getRunnerMetricsLatest } from '@/services/runners.api'

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()

const searchQuery = ref('')
const showRemoved = ref(false)
const warningWorkspaceIds = ref<Record<string, boolean>>({})

const VM_WARNING_THRESHOLD_PERCENT = 80

function toPercent(used: number, total: number): number {
  if (total <= 0) return 0
  return (used / total) * 100
}

function isVmMetricWarning(metric: VmSystemMetrics): boolean {
  return (
    metric.cpu_usage_percent >= VM_WARNING_THRESHOLD_PERCENT ||
    toPercent(metric.ram_used_bytes, metric.ram_total_bytes) >= VM_WARNING_THRESHOLD_PERCENT ||
    toPercent(metric.disk_used_bytes, metric.disk_total_bytes) >= VM_WARNING_THRESHOLD_PERCENT
  )
}

async function fetchWarningWorkspaceIds(): Promise<void> {
  const warnings: Record<string, boolean> = {}
  const metricsPerRunner = await Promise.all(
    runnerStore.runners.map(async (runner) => {
      try {
        return await getRunnerMetricsLatest(runner.id)
      } catch {
        return null
      }
    }),
  )

  for (const metrics of metricsPerRunner) {
    if (!metrics?.vm_metrics) continue
    for (const [workspaceId, vmMetric] of Object.entries(metrics.vm_metrics)) {
      if (isVmMetricWarning(vmMetric)) warnings[workspaceId] = true
    }
  }

  warningWorkspaceIds.value = warnings
}

async function fetchWorkspacesAndWarnings(): Promise<void> {
  await workspaceStore.fetchWorkspaces()
  await fetchWarningWorkspaceIds()
}

const { start } = usePolling(fetchWorkspacesAndWarnings, 10000)

const filteredWorkspaces = computed(() => {
  let workspaces = workspaceStore.workspaces

  // Filter out removed workspaces by default
  if (!showRemoved.value) {
    workspaces = workspaces.filter((w) => w.status !== WorkspaceStatus.REMOVED)
  }

  // Apply search filter
  if (searchQuery.value.trim()) {
    const query = searchQuery.value.toLowerCase()
    workspaces = workspaces.filter(
      (w) =>
        w.name.toLowerCase().includes(query) ||
        w.id.toLowerCase().includes(query),
    )
  }

  return workspaces
})

onMounted(async () => {
  // Ensure runners are loaded (for Create dialog dropdown)
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
  start()
})
</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-fg">Workspaces</h2>
        <p class="text-sm text-muted-fg mt-1">
          Manage workspaces running AI coding agents on your repositories.
        </p>
      </div>
      <CreateWorkspaceDialog />
    </div>

    <!-- Search and Filter Bar -->
    <div class="flex flex-col sm:flex-row gap-4 items-start sm:items-center">
      <!-- Search Input -->
      <div class="relative flex-1 w-full">
        <Search
          :size="18"
          class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-fg pointer-events-none"
        />
        <UiInput
          v-model="searchQuery"
          placeholder="Search by name or ID..."
          class="pl-10"
        />
      </div>

      <!-- Filter: Show Removed -->
      <div
        class="flex items-center gap-3 px-4 py-2.5 rounded-[var(--radius-md)] border border-border bg-bg-subtle hover:bg-bg-muted transition-colors cursor-pointer select-none"
        @click="showRemoved = !showRemoved"
      >
        <div
          :class="[
            'w-5 h-5 rounded border-2 flex items-center justify-center transition-all',
            showRemoved
              ? 'bg-primary border-primary'
              : 'bg-bg border-border',
          ]"
        >
          <svg
            v-if="showRemoved"
            class="w-3 h-3 text-white"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <polyline points="20 6 9 17 4 12" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </div>
        <div class="flex items-center gap-2">
          <Filter :size="16" class="text-muted-fg" />
          <span class="text-sm font-medium text-fg">Show removed workspaces</span>
        </div>
      </div>
    </div>

    <!-- Results Count -->
    <div v-if="workspaceStore.workspaces.length" class="text-sm text-muted-fg">
      Showing {{ filteredWorkspaces.length }} of {{ workspaceStore.workspaces.length }} workspaces
    </div>

    <!-- Loading -->
    <div v-if="workspaceStore.loading && !workspaceStore.workspaces.length" class="flex justify-center py-12">
      <UiSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="workspaceStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ workspaceStore.error }}
    </div>

    <!-- Workspace list -->
    <WorkspaceList
      v-else
      :workspaces="filteredWorkspaces"
      :warning-workspace-ids="warningWorkspaceIds"
    />
  </div>
</template>
