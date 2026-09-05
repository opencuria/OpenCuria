<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import type { Runner, RunnerSystemMetrics } from '@/types'
import { RunnerStatus } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Server, Wifi, WifiOff, Clock, Cpu, MemoryStick, HardDrive } from '@lucide/vue'
import { formatRelativeTime } from '@/lib/utils'
import { getRunnerMetricsLatest, getRunnerMetricsHistory } from '@/services/runners.api'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'
import { useAuthStore } from '@/stores/auth'
import EditRunnerResourcesDialog from './EditRunnerResourcesDialog.vue'
import Sparkline from '@/components/charts/Sparkline.vue'

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

function barColor(pct: number): string {
  if (pct >= 90) return 'bg-destructive'
  if (pct >= 70) return 'bg-warning'
  return 'bg-success'
}

async function fetchMetrics() {
  try {
    const [latest, history] = await Promise.all([
      getRunnerMetricsLatest(props.runner.id),
      getRunnerMetricsHistory(props.runner.id)
    ])
    metrics.value = latest
    // Extract CPU usage for the sparkline
    metricsHistory.value = history.map(m => m.cpu_usage_percent)
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
  <Card class="hover:border-border transition-colors duration-150">
    <CardContent>
      <div class="flex items-start justify-between mb-3">
        <div class="flex items-center gap-3">
          <div
            :class="[
              'flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)]',
              runner.status === RunnerStatus.ONLINE
                ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                : 'bg-muted text-muted-foreground',
            ]"
          >
            <Server :size="18" />
          </div>
          <div>
            <h3 class="font-medium text-foreground text-sm">
              {{ runner.name || runner.id.slice(0, 8) }}
            </h3>
            <p class="text-xs text-muted-foreground font-mono">
              {{ runner.id.slice(0, 8) }}…
            </p>
          </div>
        </div>

        <Badge
          :variant="runner.status === RunnerStatus.ONLINE ? 'secondary' : 'secondary'"
        >
          <component
            :is="runner.status === RunnerStatus.ONLINE ? Wifi : WifiOff"
            :size="12"
            class="mr-1"
          />
          {{ runner.status }}
        </Badge>
      </div>

      <!-- System metrics -->
      <div v-if="metrics" class="mb-3 space-y-2">
        <p class="text-xs text-muted-foreground mb-1">System</p>

        <!-- CPU -->
        <div class="flex items-center gap-2">
          <Cpu :size="12" class="text-muted-foreground shrink-0" />
          <div class="flex-1">
            <div class="flex justify-between text-xs mb-0.5">
              <span class="text-muted-foreground">CPU</span>
              <span class="text-foreground font-mono">{{ metrics.cpu_usage_percent.toFixed(1) }}%</span>
            </div>
            <div class="w-full h-1.5 rounded-full bg-muted overflow-hidden">
              <div
                :class="['h-full rounded-full transition-all', barColor(metrics.cpu_usage_percent)]"
                :style="{ width: metrics.cpu_usage_percent + '%' }"
              />
            </div>
          </div>
        </div>

        <!-- RAM -->
        <div class="flex items-center gap-2">
          <MemoryStick :size="12" class="text-muted-foreground shrink-0" />
          <div class="flex-1">
            <div class="flex justify-between text-xs mb-0.5">
              <span class="text-muted-foreground">RAM</span>
              <span class="text-foreground font-mono">
                {{ formatBytes(metrics.ram_used_bytes) }} / {{ formatBytes(metrics.ram_total_bytes) }}
              </span>
            </div>
            <div class="w-full h-1.5 rounded-full bg-muted overflow-hidden">
              <div
                :class="['h-full rounded-full transition-all', barColor(ramPercent)]"
                :style="{ width: ramPercent + '%' }"
              />
            </div>
          </div>
        </div>

        <!-- Disk -->
        <div class="flex items-center gap-2">
          <HardDrive :size="12" class="text-muted-foreground shrink-0" />
          <div class="flex-1">
            <div class="flex justify-between text-xs mb-0.5">
              <span class="text-muted-foreground">Disk</span>
              <span class="text-foreground font-mono">
                {{ formatBytes(metrics.disk_used_bytes) }} / {{ formatBytes(metrics.disk_total_bytes) }}
              </span>
            </div>
            <div class="w-full h-1.5 rounded-full bg-muted overflow-hidden">
              <div
                :class="['h-full rounded-full transition-all', barColor(diskPercent)]"
                :style="{ width: diskPercent + '%' }"
              />
            </div>
          </div>
        </div>

        <!-- 24h CPU History -->
        <div v-if="metricsHistory.length > 1" class="pt-2 mt-3 border-t border-border/50">
          <div class="flex justify-between items-end mb-1">
            <p class="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">24h CPU</p>
            <span class="text-[10px] text-muted-foreground font-mono">
              Max: {{ Math.max(...metricsHistory).toFixed(0) }}%
            </span>
          </div>
          <div class="h-8 w-full text-primary/60">
            <Sparkline
              :data="metricsHistory"
              :min="0"
              :max="100"
              :stroke-width="1.5"
            />
          </div>
        </div>

        <p class="text-xs text-muted-foreground/60 text-right">
          Updated {{ formatRelativeTime(metrics.timestamp) }}
        </p>
      </div>

      <!-- Connection time -->
      <div class="flex items-center justify-between gap-2">
        <div class="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock :size="12" />
          <span v-if="runner.status === RunnerStatus.ONLINE">
            Connected {{ formatRelativeTime(runner.connected_at) }}
          </span>
          <span v-else-if="runner.disconnected_at">
            Last seen {{ formatRelativeTime(runner.disconnected_at) }}
          </span>
          <span v-else>Never connected</span>
        </div>
        <EditRunnerResourcesDialog v-if="authStore.isAdmin && supportsQemu" :runner="runner" />
      </div>
    </CardContent>
  </Card>
</template>
