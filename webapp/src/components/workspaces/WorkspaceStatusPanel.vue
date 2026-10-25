<script setup lang="ts">
/**
 * WorkspaceStatusPanel — compact resource overview of one workspace.
 *
 * Storage is the last observation reported by the runner (kept while the
 * workspace is stopped). Memory and CPU are live VM metrics, loaded only
 * while the panel is mounted (i.e. while its popover is open).
 */
import { computed, onMounted, ref } from 'vue'
import { HardDrive } from '@lucide/vue'
import type { VmSystemMetrics, Workspace } from '@/types'
import { WorkspaceStatus } from '@/types'
import { Button } from '@/components/ui/button'
import UsageMeter from '@/components/common/UsageMeter.vue'
import { usePolling } from '@/composables/usePolling'
import { storageBytes } from '@/composables/useRunnerStorage'
import { getRunnerMetricsLatest } from '@/services/runners.api'
import { useRunnerStore } from '@/stores/runners'
import { formatRelativeTime } from '@/lib/utils'
import {
  USAGE_LEVEL_CLASSES,
  describeDiskUsage,
  isDiskUsageStale,
  usageLevel,
} from '@/lib/workspaceDisk'

const props = defineProps<{
  workspace: Workspace
  runnerOffline: boolean
}>()

const emit = defineEmits<{
  'increase-disk': []
}>()

/** Live samples older than this no longer describe the current state. */
const LIVE_METRICS_MAX_AGE_MS = 3 * 60 * 1000

const runnerStore = useRunnerStore()
const vmMetrics = ref<VmSystemMetrics | null>(null)

const isRunning = computed(
  () => props.workspace.status === WorkspaceStatus.RUNNING && !props.runnerOffline,
)
const disk = computed(() => {
  if (!props.workspace.disk_usage) return null
  const view = describeDiskUsage(props.workspace.disk_usage)
  return {
    ...view,
    used: storageBytes(view.usedBytes),
    free: storageBytes(view.freeBytes),
    capacity: storageBytes(view.capacityBytes),
  }
})
const diskAgeLabel = computed(() => {
  const usage = props.workspace.disk_usage
  if (!usage || (isRunning.value && !isDiskUsageStale(usage))) return null
  return `Storage measured ${formatRelativeTime(usage.observed_at)}`
})

const memory = computed(() => {
  const metric = vmMetrics.value
  if (!metric || metric.ram_total_bytes <= 0) return null
  const percent = (metric.ram_used_bytes / metric.ram_total_bytes) * 100
  return {
    percent,
    level: usageLevel(percent),
    label: `${storageBytes(metric.ram_used_bytes)} of ${storageBytes(metric.ram_total_bytes)}`,
  }
})
const cpuPercent = computed(() =>
  vmMetrics.value ? Math.round(vmMetrics.value.cpu_usage_percent) : null,
)

const allocation = computed(() => {
  const runner = runnerStore.runnerById(props.workspace.runner_id)
  const vcpus = props.workspace.qemu_vcpus ?? runner?.qemu_default_vcpus
  const memoryMb = props.workspace.qemu_memory_mb ?? runner?.qemu_default_memory_mb
  const diskGb = props.workspace.qemu_disk_size_gb ?? runner?.qemu_default_disk_size_gb
  return [
    vcpus ? `${vcpus} vCPU` : null,
    memoryMb ? `${storageBytes(memoryMb * 1024 ** 2)} RAM` : null,
    diskGb ? `${storageBytes(diskGb * 1024 ** 3)} disk` : null,
  ]
    .filter(Boolean)
    .join(' · ')
})

async function loadLiveMetrics(): Promise<void> {
  if (!isRunning.value) {
    vmMetrics.value = null
    return
  }
  try {
    const metrics = await getRunnerMetricsLatest(props.workspace.runner_id)
    const fresh = Date.now() - new Date(metrics.timestamp).getTime() <= LIVE_METRICS_MAX_AGE_MS
    vmMetrics.value = fresh ? (metrics.vm_metrics?.[props.workspace.id] ?? null) : null
  } catch {
    vmMetrics.value = null
  }
}

const { start } = usePolling(loadLiveMetrics, 15_000)

onMounted(start)
</script>

<template>
  <div class="flex flex-col gap-4 text-xs" data-testid="workspace-status-panel">
    <section class="flex flex-col gap-1.5" data-testid="workspace-status-storage">
      <div class="flex items-baseline justify-between gap-3">
        <span class="font-medium text-foreground">Storage</span>
        <span
          v-if="disk"
          :class="USAGE_LEVEL_CLASSES[disk.level].text"
          data-testid="workspace-status-storage-free"
        >
          {{ disk.free }} free
        </span>
      </div>
      <template v-if="disk">
        <UsageMeter :percent="disk.percent" :level="disk.level" label="Storage used" />
        <div class="flex items-baseline justify-between gap-3 text-muted-foreground">
          <span>{{ disk.used }} of {{ disk.capacity }} used</span>
          <span class="tabular-nums">{{ disk.percent }}%</span>
        </div>
      </template>
      <p v-else class="text-muted-foreground">Measured while the workspace is running.</p>
    </section>

    <div
      v-if="disk && disk.level !== 'ok'"
      class="flex gap-2.5 rounded-lg border px-3 py-2.5"
      :class="
        disk.level === 'critical'
          ? 'border-destructive/25 bg-destructive/5'
          : 'border-amber-500/25 bg-amber-500/5'
      "
      data-testid="workspace-status-disk-warning"
    >
      <HardDrive :size="14" class="mt-px shrink-0" :class="USAGE_LEVEL_CLASSES[disk.level].text" />
      <div class="min-w-0 flex-1">
        <p class="font-medium" :class="USAGE_LEVEL_CLASSES[disk.level].text">
          {{ disk.level === 'critical' ? 'Disk is full' : 'Disk almost full' }}
        </p>
        <p class="mt-0.5 text-muted-foreground">
          Agents may fail to install packages or write files. Free up space or increase the disk.
        </p>
        <Button
          variant="outline"
          size="sm"
          class="mt-2 h-7 text-xs"
          data-testid="workspace-status-increase-disk"
          @click="emit('increase-disk')"
        >
          Increase disk
        </Button>
      </div>
    </div>

    <section v-if="memory" class="flex flex-col gap-1.5" data-testid="workspace-status-memory">
      <div class="flex items-baseline justify-between gap-3">
        <span class="font-medium text-foreground">Memory</span>
        <span class="text-muted-foreground">{{ memory.label }}</span>
      </div>
      <UsageMeter :percent="memory.percent" :level="memory.level" label="Memory used" />
    </section>

    <section
      v-if="cpuPercent !== null"
      class="flex flex-col gap-1.5"
      data-testid="workspace-status-cpu"
    >
      <div class="flex items-baseline justify-between gap-3">
        <span class="font-medium text-foreground">CPU</span>
        <span class="tabular-nums text-muted-foreground">{{ cpuPercent }}%</span>
      </div>
      <UsageMeter :percent="cpuPercent" label="CPU used" />
    </section>

    <div
      v-if="allocation || diskAgeLabel"
      class="flex flex-col gap-0.5 border-t border-border pt-3 text-[11px] text-muted-foreground"
    >
      <span v-if="allocation" data-testid="workspace-status-allocation">{{ allocation }}</span>
      <span v-if="diskAgeLabel" data-testid="workspace-status-age">{{ diskAgeLabel }}</span>
    </div>
  </div>
</template>
