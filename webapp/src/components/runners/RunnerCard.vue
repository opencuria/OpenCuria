<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import type { Runner, RunnerSystemMetrics } from '@/types'
import { RunnerStatus } from '@/types'
import { Badge } from '@/components/ui/badge'
import { Server, Wifi, WifiOff, Clock, Cpu, MemoryStick, HardDrive } from '@lucide/vue'
import { formatRelativeTime } from '@/lib/utils'
import { getRunnerMetricsLatest, getRunnerMetricsHistory } from '@/services/runners.api'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'
import { useAuthStore } from '@/stores/auth'
import EditRunnerResourcesDialog from './EditRunnerResourcesDialog.vue'
import Sparkline from '@/components/charts/Sparkline.vue'
import SettingsRow from '@/components/settings/SettingsRow.vue'

const props = defineProps<{
  runner: Runner
}>()

const metrics = ref<RunnerSystemMetrics | null>(null)
const metricsHistory = ref<number[]>([])
let pollInterval: ReturnType<typeof setInterval> | null = null
const authStore = useAuthStore()

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1) + ' GB'
  if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(0) + ' MB'
  return (bytes / 1024).toFixed(0) + ' KB'
}

const ramPercent = computed(() => {
  if (!metrics.value) return 0
  return Math.round((metrics.value.ram_used_bytes / metrics.value.ram_total_bytes) * 100)
})

const diskPercent = computed(() => {
  if (!metrics.value) return 0
  return Math.round((metrics.value.disk_used_bytes / metrics.value.disk_total_bytes) * 100)
})

const supportsQemu = computed(() => runnerSupportsRuntime(props.runner, 'qemu'))
const isOnline = computed(() => props.runner.status === RunnerStatus.ONLINE)

function barColor(pct: number): string {
  if (pct >= 90) return 'bg-destructive'
  if (pct >= 70) return 'bg-warning'
  return 'bg-success'
}

async function fetchMetrics() {
  try {
    const [latest, history] = await Promise.all([
      getRunnerMetricsLatest(props.runner.id),
      getRunnerMetricsHistory(props.runner.id),
    ])
    metrics.value = latest
    metricsHistory.value = history.map((m) => m.cpu_usage_percent)
  } catch {
    // No metrics yet — keep null
  }
}

onMounted(() => {
  if (props.runner.status === RunnerStatus.ONLINE) {
    fetchMetrics()
    pollInterval = setInterval(fetchMetrics, 60_000)
  }
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <SettingsRow :icon-class="isOnline ? 'bg-success/10 text-success' : undefined">
    <template #icon>
      <Server :size="16" />
    </template>
    <div class="min-w-0 space-y-1">
      <h3 class="text-sm font-medium text-foreground">
        {{ runner.name || runner.id.slice(0, 8) }}
      </h3>
      <p class="font-mono text-xs text-muted-foreground">
        {{ runner.id.slice(0, 8) }}…
      </p>
      <p class="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Clock :size="12" />
        <span v-if="isOnline">Connected {{ formatRelativeTime(runner.connected_at) }}</span>
        <span v-else-if="runner.disconnected_at">
          Last seen {{ formatRelativeTime(runner.disconnected_at) }}
        </span>
        <span v-else>Never connected</span>
      </p>
    </div>
    <template #badges>
      <Badge variant="secondary">
        <component :is="isOnline ? Wifi : WifiOff" :size="12" />
        {{ runner.status }}
      </Badge>
    </template>
    <template #actions>
      <EditRunnerResourcesDialog v-if="authStore.isAdmin && supportsQemu" :runner="runner" />
    </template>
    <template v-if="metrics" #detail>
      <div class="grid grid-cols-3 gap-3">
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-1 text-xs">
            <span class="inline-flex items-center gap-1 text-muted-foreground">
              <Cpu :size="12" />
              CPU
            </span>
            <span class="font-mono text-foreground">{{ metrics.cpu_usage_percent.toFixed(1) }}%</span>
          </div>
          <div class="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              :class="['h-full rounded-full transition-all', barColor(metrics.cpu_usage_percent)]"
              :style="{ width: metrics.cpu_usage_percent + '%' }"
            />
          </div>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-1 text-xs">
            <span class="inline-flex items-center gap-1 text-muted-foreground">
              <MemoryStick :size="12" />
              RAM
            </span>
            <span class="font-mono text-foreground">{{ ramPercent }}%</span>
          </div>
          <div class="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              :class="['h-full rounded-full transition-all', barColor(ramPercent)]"
              :style="{ width: ramPercent + '%' }"
            />
          </div>
          <p class="text-xs text-muted-foreground">
            {{ formatBytes(metrics.ram_used_bytes) }} / {{ formatBytes(metrics.ram_total_bytes) }}
          </p>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-1 text-xs">
            <span class="inline-flex items-center gap-1 text-muted-foreground">
              <HardDrive :size="12" />
              Disk
            </span>
            <span class="font-mono text-foreground">{{ diskPercent }}%</span>
          </div>
          <div class="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              :class="['h-full rounded-full transition-all', barColor(diskPercent)]"
              :style="{ width: diskPercent + '%' }"
            />
          </div>
          <p class="text-xs text-muted-foreground">
            {{ formatBytes(metrics.disk_used_bytes) }} / {{ formatBytes(metrics.disk_total_bytes) }}
          </p>
        </div>
      </div>
      <div v-if="metricsHistory.length > 1" class="mt-3 hidden border-t border-border pt-3 lg:block">
        <div class="mb-1 flex items-end justify-between">
          <p class="text-xs font-medium text-muted-foreground">24h CPU</p>
          <span class="font-mono text-xs text-muted-foreground">
            Max: {{ Math.max(...metricsHistory).toFixed(0) }}%
          </span>
        </div>
        <div class="h-8 w-full text-primary/60">
          <Sparkline :data="metricsHistory" :min="0" :max="100" :stroke-width="1.5" />
        </div>
      </div>
    </template>
  </SettingsRow>
</template>
