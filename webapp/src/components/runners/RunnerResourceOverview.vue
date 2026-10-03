<script setup lang="ts">
import { computed, toRef, watch } from 'vue'
import { Cpu, MemoryStick, HardDrive, ChevronDown, TriangleAlert } from '@lucide/vue'
import type { Runner, RunnerSystemMetrics } from '@/types'
import { RunnerStatus } from '@/types'
import type { StorageRuntime } from '@/types/runnerStorage'
import { useRunnerMetrics, resourcePercent, sampleOutdated } from '@/composables/useRunnerMetrics'
import { storageBytes } from '@/composables/useRunnerStorage'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import Sparkline from '@/components/charts/Sparkline.vue'

const props = defineProps<{ runner: Runner; runtimes: StorageRuntime[] }>()
const emit = defineEmits<{ sample: [sample: RunnerSystemMetrics | null] }>()
const runnerId = computed(() => props.runner.id)
const { metrics, history, loading, error, latestError, now, load } = useRunnerMetrics(
  toRef(runnerId),
)
const offline = computed(() => props.runner.status !== RunnerStatus.ONLINE)
const outdated = computed(
  () => offline.value || !!latestError.value || sampleOutdated(metrics.value?.timestamp, now.value),
)
watch(
  [metrics, latestError, outdated],
  () => {
    emit('sample', outdated.value ? null : metrics.value)
  },
  { immediate: true },
)
function age(timestamp: string | null | undefined): string {
  const time = timestamp ? Date.parse(timestamp) : NaN
  if (!Number.isFinite(time) || time > now.value) return 'age unknown'
  const seconds = Math.floor((now.value - time) / 1000)
  return seconds < 60
    ? `${seconds}s ago`
    : seconds < 3600
      ? `${Math.floor(seconds / 60)}m ago`
      : `${Math.floor(seconds / 3600)}h ago`
}
function bytes(value: number | null | undefined): string {
  return storageBytes(value != null && value >= 0 ? value : null)
}
function free(used: number | undefined, total: number | undefined): string {
  if (
    used == null ||
    total == null ||
    !Number.isFinite(used) ||
    !Number.isFinite(total) ||
    total <= 0 ||
    used < 0
  )
    return 'Unknown'
  return bytes(Math.max(0, total - used))
}
function color(percent: number | null, muted: boolean): string {
  if (percent == null || muted) return 'bg-muted-foreground/30'
  return percent >= 90 ? 'bg-destructive' : percent >= 70 ? 'bg-warning' : 'bg-success'
}
const resources = computed(() => [
  {
    name: 'CPU',
    icon: Cpu,
    percent: resourcePercent(metrics.value?.cpu_usage_percent),
    detail: null,
    free: null,
  },
  {
    name: 'RAM',
    icon: MemoryStick,
    percent: resourcePercent(metrics.value?.ram_used_bytes, metrics.value?.ram_total_bytes ?? 0),
    detail: `${bytes(metrics.value?.ram_used_bytes)} / ${bytes(metrics.value?.ram_total_bytes)} used`,
    free: `${free(metrics.value?.ram_used_bytes, metrics.value?.ram_total_bytes)} free`,
  },
  {
    name: 'Storage',
    icon: HardDrive,
    percent: resourcePercent(metrics.value?.disk_used_bytes, metrics.value?.disk_total_bytes ?? 0),
    detail: `${bytes(metrics.value?.disk_used_bytes)} / ${bytes(metrics.value?.disk_total_bytes)} used`,
    free: `${free(metrics.value?.disk_used_bytes, metrics.value?.disk_total_bytes)} host disk free`,
  },
])
const high = computed(
  () => !outdated.value && resources.value.some((r) => r.percent != null && r.percent >= 90),
)
const cpuHistory = computed(() =>
  history.value
    .filter((sample) => {
      const time = Date.parse(sample.timestamp)
      return (
        Number.isFinite(time) &&
        time <= now.value &&
        time >= now.value - 86_400_000 &&
        resourcePercent(sample.cpu_usage_percent) != null
      )
    })
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
    .map((sample) => resourcePercent(sample.cpu_usage_percent)!),
)
const filesystems = computed(() =>
  props.runtimes.flatMap((runtime, runtimeIndex) =>
    runtime.filesystems.map((fs, index) => ({
      ...fs,
      key: `${runtimeIndex}-${index}`,
      runtime: runtime.runtime_type,
      timestamp: runtime.collected_at,
      outdated:
        offline.value ||
        !runtime.fresh ||
        !runtime.collected_at ||
        !Number.isFinite(Date.parse(runtime.collected_at)) ||
        now.value - Date.parse(runtime.collected_at) >= 300_000 ||
        Date.parse(runtime.collected_at) > now.value,
      percent: resourcePercent(fs.used_bytes, fs.capacity_bytes ?? 0),
    })),
  ),
)
const policy = computed(() => [
  {
    name: 'vCPU',
    cap: props.runner.qemu_max_active_vcpus,
    min: props.runner.qemu_min_vcpus,
    max: props.runner.qemu_max_vcpus,
    default: props.runner.qemu_default_vcpus,
    unit: 'vCPU',
  },
  {
    name: 'RAM',
    cap: props.runner.qemu_max_active_memory_mb,
    min: props.runner.qemu_min_memory_mb,
    max: props.runner.qemu_max_memory_mb,
    default: props.runner.qemu_default_memory_mb,
    unit: 'MiB',
  },
  {
    name: 'Disk',
    cap: props.runner.qemu_max_active_disk_size_gb,
    min: props.runner.qemu_min_disk_size_gb,
    max: props.runner.qemu_max_disk_size_gb,
    default: props.runner.qemu_default_disk_size_gb,
    unit: 'GiB',
  },
])
</script>

<template>
  <section
    aria-label="Resource overview"
    class="min-w-0 overflow-hidden rounded-xl border border-border bg-card text-card-foreground"
  >
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
      <h3 class="text-sm font-semibold">Resources</h3>
      <p class="text-xs text-muted-foreground" role="status">
        Host sample · {{ age(metrics?.timestamp) }}<span v-if="offline"> · Offline</span
        ><span v-if="outdated"> · Outdated</span>
      </p>
    </div>
    <div
      class="grid min-w-0 grid-cols-1 divide-y divide-border sm:grid-cols-3 sm:divide-x sm:divide-y-0"
    >
      <div v-for="resource in resources" :key="resource.name" class="min-w-0 space-y-1.5 px-4 py-3">
        <div class="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <component :is="resource.icon" :size="15" />{{ resource.name }}
        </div>
        <p
          class="text-2xl font-semibold tracking-tight tabular-nums"
          :class="
            outdated || resource.percent == null ? 'text-muted-foreground' : 'text-foreground'
          "
        >
          {{ resource.percent == null ? 'Unknown' : `${resource.percent.toFixed(1)}%` }}
        </p>
        <div
          class="h-1.5 overflow-hidden rounded-full bg-muted"
          :aria-label="`${resource.name} utilization${resource.percent == null ? ' unknown' : ''}`"
          :role="resource.percent == null ? 'img' : 'meter'"
          :aria-valuenow="resource.percent ?? undefined"
          :aria-valuemin="resource.percent == null ? undefined : 0"
          :aria-valuemax="resource.percent == null ? undefined : 100"
        >
          <div
            class="h-full rounded-full"
            :class="color(resource.percent, outdated)"
            :style="{ width: `${resource.percent ?? 0}%` }"
          />
        </div>
        <p v-if="resource.detail" class="break-words text-xs text-muted-foreground tabular-nums">
          {{ resource.detail }}
        </p>
        <p v-if="resource.free" class="text-xs text-muted-foreground tabular-nums">
          {{ resource.free }}
        </p>
        <template v-if="resource.name === 'CPU' && cpuHistory.length > 1">
          <div class="h-5 text-primary/60" aria-label="CPU history: available 24h samples">
            <Sparkline :data="cpuHistory" :min="0" :max="100" :stroke-width="1.5" />
          </div>
          <p class="text-[11px] text-muted-foreground">24h samples</p>
        </template>
      </div>
    </div>
    <p
      v-if="high"
      role="status"
      class="flex items-center gap-2 border-t border-border px-4 py-2 text-xs text-warning"
    >
      <TriangleAlert :size="14" />High utilization in the host sample
    </p>
    <div
      v-if="error || !metrics"
      role="status"
      aria-live="polite"
      class="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-muted-foreground"
    >
      <span>{{ error || (loading ? 'Loading host sample…' : 'No host sample available.') }}</span>
      <Button variant="outline" size="sm" :disabled="loading" @click="load">Retry</Button>
    </div>
    <div
      class="grid grid-cols-1 items-start gap-x-3 gap-y-1 border-t border-border px-2 py-1.5 sm:grid-cols-[auto_1fr]"
    >
      <Collapsible class="contents">
        <CollapsibleTrigger as-child
          ><Button variant="ghost" class="order-1 h-7 gap-2 px-2 text-xs"
            >Filesystems ({{ filesystems.length }})<ChevronDown :size="14" /></Button
        ></CollapsibleTrigger>
        <CollapsibleContent class="order-2 col-span-full space-y-3 py-2">
          <p class="text-xs text-muted-foreground">
            Separate runtime paths; capacity is not summed because mounts may overlap.
          </p>
          <p v-if="!filesystems.length" class="text-xs text-muted-foreground">
            No filesystem samples available.
          </p>
          <div
            v-for="fs in filesystems"
            :key="fs.key"
            class="min-w-0 space-y-1.5 rounded-md border border-border p-3"
          >
            <p class="break-all text-xs font-medium">{{ fs.runtime }} · {{ fs.path }}</p>
            <p class="text-xs text-muted-foreground tabular-nums">
              {{ bytes(fs.used_bytes) }} used / {{ bytes(fs.capacity_bytes) }} capacity ·
              {{ bytes(fs.available_bytes) }} available
            </p>
            <div class="h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                class="h-full"
                :class="color(fs.percent, fs.outdated)"
                :style="{ width: `${fs.percent ?? 0}%` }"
              />
            </div>
            <p class="text-[11px] text-muted-foreground">
              Filesystem sample · {{ age(fs.timestamp) }} ·
              {{ fs.outdated ? 'Outdated' : 'Recent' }}
            </p>
          </div>
        </CollapsibleContent>
      </Collapsible>
      <Collapsible v-if="runnerSupportsRuntime(runner, 'qemu')" class="contents">
        <CollapsibleTrigger as-child>
          <Button
            variant="ghost"
            class="order-1 h-auto min-h-7 min-w-0 flex-wrap justify-start gap-x-2 gap-y-1 px-2 py-1 text-xs"
          >
            QEMU caps
            <span class="flex min-w-0 flex-wrap gap-x-2 text-muted-foreground font-normal">
              <span v-for="item in policy" :key="item.name"
                >{{ item.name }}:
                {{ item.cap == null ? 'Unlimited' : `${item.cap} ${item.unit}` }}</span
              >
            </span>
            <ChevronDown :size="12" />
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent class="order-2 col-span-full space-y-1 px-2 py-2">
          <p class="text-xs text-muted-foreground">
            Active allocation caps, not actual usage or host capacity.
          </p>
          <p class="pt-1 text-xs font-medium">Per-workspace defaults and limits</p>
          <p
            v-for="item in policy"
            :key="item.name"
            class="break-words text-xs text-muted-foreground"
          >
            {{ item.name }} · Default {{ item.default }} {{ item.unit }} · Min {{ item.min }} · Max
            {{ item.max }}
          </p>
        </CollapsibleContent>
      </Collapsible>
    </div>
  </section>
</template>
