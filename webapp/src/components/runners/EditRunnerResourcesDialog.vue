<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Runner } from '@/types'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { useRunnerStore } from '@/stores/runners'
import { getRunnerMetricsLatest } from '@/services/runners.api'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'

const props = defineProps<{
  runner: Runner
}>()

const runnerStore = useRunnerStore()
const open = ref(false)
const submitting = ref(false)
const loadingMetrics = ref(false)

const minVcpus = ref(1)
const maxVcpus = ref(8)
const defaultVcpus = ref(2)
const minMemoryMb = ref(1024)
const maxMemoryMb = ref(16384)
const defaultMemoryMb = ref(4096)
const minDiskSizeGb = ref(20)
const maxDiskSizeGb = ref(200)
const defaultDiskSizeGb = ref(50)
const maxActiveVcpus = ref(8)
const maxActiveMemoryMb = ref(4096)
const maxActiveDiskSizeGb = ref(50)
const unlimitedActiveVcpus = ref(false)
const unlimitedActiveMemoryMb = ref(false)
const unlimitedActiveDiskSizeGb = ref(false)
const hostVcpus = ref<number | null>(null)
const hostMemoryMb = ref<number | null>(null)
const hostDiskSizeGb = ref<number | null>(null)

const RAM_STEP = 256
const DISK_STEP = 5
const supportsQemu = computed(() => runnerSupportsRuntime(props.runner, 'qemu'))

function clamp(
  value: number,
  min: number,
  max: number,
): number {
  return Math.min(Math.max(value, min), max)
}

function snap(value: number, step: number): number {
  return Math.max(step, Math.round(value / step) * step)
}

function toMiB(bytes: number | null | undefined): number | null {
  if (!bytes || bytes <= 0) return null
  return Math.floor(bytes / (1024 * 1024))
}

function toGiB(bytes: number | null | undefined): number | null {
  if (!bytes || bytes <= 0) return null
  return Math.floor(bytes / (1024 * 1024 * 1024))
}

async function loadHostMetrics(): Promise<void> {
  loadingMetrics.value = true
  try {
    const metrics = await getRunnerMetricsLatest(props.runner.id)
    hostMemoryMb.value = toMiB(metrics.ram_total_bytes)
    hostDiskSizeGb.value = toGiB(metrics.disk_total_bytes)
    // CPU core count is not reported by current metrics; we keep a practical marker.
    hostVcpus.value = props.runner.qemu_max_vcpus
  } catch {
    hostVcpus.value = props.runner.qemu_max_vcpus
    hostMemoryMb.value = null
    hostDiskSizeGb.value = null
  } finally {
    loadingMetrics.value = false
  }
}

watch(
  () => props.runner,
  (runner) => {
    if (open.value) return
    minVcpus.value = runner.qemu_min_vcpus
    maxVcpus.value = runner.qemu_max_vcpus
    defaultVcpus.value = runner.qemu_default_vcpus
    minMemoryMb.value = runner.qemu_min_memory_mb
    maxMemoryMb.value = runner.qemu_max_memory_mb
    defaultMemoryMb.value = runner.qemu_default_memory_mb
    minDiskSizeGb.value = runner.qemu_min_disk_size_gb
    maxDiskSizeGb.value = runner.qemu_max_disk_size_gb
    defaultDiskSizeGb.value = runner.qemu_default_disk_size_gb
    unlimitedActiveVcpus.value = runner.qemu_max_active_vcpus == null
    unlimitedActiveMemoryMb.value = runner.qemu_max_active_memory_mb == null
    unlimitedActiveDiskSizeGb.value = runner.qemu_max_active_disk_size_gb == null
    maxActiveVcpus.value = runner.qemu_max_active_vcpus ?? Math.max(runner.qemu_default_vcpus, 1)
    maxActiveMemoryMb.value = runner.qemu_max_active_memory_mb ?? Math.max(runner.qemu_default_memory_mb, RAM_STEP)
    maxActiveDiskSizeGb.value = runner.qemu_max_active_disk_size_gb ?? Math.max(runner.qemu_default_disk_size_gb, DISK_STEP)
  },
  { immediate: true, deep: true },
)

watch(minVcpus, (value) => {
  if (defaultVcpus.value < value) defaultVcpus.value = value
  if (maxVcpus.value < value) maxVcpus.value = value
})
watch(maxVcpus, (value) => {
  if (defaultVcpus.value > value) defaultVcpus.value = value
  if (minVcpus.value > value) minVcpus.value = value
})
watch(defaultVcpus, (value) => {
  defaultVcpus.value = clamp(value, minVcpus.value, maxVcpus.value)
})

watch(minMemoryMb, (value) => {
  minMemoryMb.value = snap(value, RAM_STEP)
  if (defaultMemoryMb.value < minMemoryMb.value) defaultMemoryMb.value = minMemoryMb.value
  if (maxMemoryMb.value < minMemoryMb.value) maxMemoryMb.value = minMemoryMb.value
})
watch(maxMemoryMb, (value) => {
  maxMemoryMb.value = snap(value, RAM_STEP)
  if (defaultMemoryMb.value > maxMemoryMb.value) defaultMemoryMb.value = maxMemoryMb.value
  if (minMemoryMb.value > maxMemoryMb.value) minMemoryMb.value = maxMemoryMb.value
})
watch(defaultMemoryMb, (value) => {
  defaultMemoryMb.value = snap(clamp(value, minMemoryMb.value, maxMemoryMb.value), RAM_STEP)
})

watch(minDiskSizeGb, (value) => {
  minDiskSizeGb.value = snap(value, DISK_STEP)
  if (defaultDiskSizeGb.value < minDiskSizeGb.value) defaultDiskSizeGb.value = minDiskSizeGb.value
  if (maxDiskSizeGb.value < minDiskSizeGb.value) maxDiskSizeGb.value = minDiskSizeGb.value
})
watch(maxDiskSizeGb, (value) => {
  maxDiskSizeGb.value = snap(value, DISK_STEP)
  if (defaultDiskSizeGb.value > maxDiskSizeGb.value) defaultDiskSizeGb.value = maxDiskSizeGb.value
  if (minDiskSizeGb.value > maxDiskSizeGb.value) minDiskSizeGb.value = maxDiskSizeGb.value
})
watch(defaultDiskSizeGb, (value) => {
  defaultDiskSizeGb.value = snap(clamp(value, minDiskSizeGb.value, maxDiskSizeGb.value), DISK_STEP)
})

const activeVcpusSliderMax = computed(() =>
  Math.max(maxVcpus.value * 4, hostVcpus.value ? hostVcpus.value * 2 : 1, maxActiveVcpus.value),
)
const activeMemorySliderMax = computed(() =>
  Math.max(maxMemoryMb.value * 4, hostMemoryMb.value ? hostMemoryMb.value * 2 : RAM_STEP, maxActiveMemoryMb.value),
)
const activeDiskSliderMax = computed(() =>
  Math.max(maxDiskSizeGb.value * 4, hostDiskSizeGb.value ? hostDiskSizeGb.value * 2 : DISK_STEP, maxActiveDiskSizeGb.value),
)

function markerPosition(hostMax: number | null, sliderMax: number): string {
  if (!hostMax || sliderMax <= 0) return '0%'
  return `${Math.min(100, (hostMax / sliderMax) * 100)}%`
}

const isValid = computed(() =>
  minVcpus.value > 0 &&
  maxVcpus.value >= minVcpus.value &&
  defaultVcpus.value >= minVcpus.value &&
  defaultVcpus.value <= maxVcpus.value &&
  minMemoryMb.value > 0 &&
  maxMemoryMb.value >= minMemoryMb.value &&
  defaultMemoryMb.value >= minMemoryMb.value &&
  defaultMemoryMb.value <= maxMemoryMb.value &&
  minDiskSizeGb.value > 0 &&
  maxDiskSizeGb.value >= minDiskSizeGb.value &&
  defaultDiskSizeGb.value >= minDiskSizeGb.value &&
  defaultDiskSizeGb.value <= maxDiskSizeGb.value,
)

async function handleSubmit(): Promise<void> {
  if (!isValid.value || !supportsQemu.value) return
  submitting.value = true
  const ok = await runnerStore.updateRunner(props.runner.id, {
    qemu_min_vcpus: minVcpus.value,
    qemu_max_vcpus: maxVcpus.value,
    qemu_default_vcpus: defaultVcpus.value,
    qemu_min_memory_mb: minMemoryMb.value,
    qemu_max_memory_mb: maxMemoryMb.value,
    qemu_default_memory_mb: defaultMemoryMb.value,
    qemu_min_disk_size_gb: minDiskSizeGb.value,
    qemu_max_disk_size_gb: maxDiskSizeGb.value,
    qemu_default_disk_size_gb: defaultDiskSizeGb.value,
    qemu_max_active_vcpus: unlimitedActiveVcpus.value ? null : maxActiveVcpus.value,
    qemu_max_active_memory_mb: unlimitedActiveMemoryMb.value ? null : maxActiveMemoryMb.value,
    qemu_max_active_disk_size_gb: unlimitedActiveDiskSizeGb.value ? null : maxActiveDiskSizeGb.value,
  })
  submitting.value = false
  if (ok) open.value = false
}

watch(open, (value) => {
  if (value) {
    loadHostMetrics()
  }
})
</script>

<template>
  <Dialog
    :open="open"
    @update:open="(value) => (open = value)"
  >
    <DialogTrigger as-child>
      <Button variant="outline" size="sm" :disabled="!supportsQemu">QEMU Limits</Button>
    </DialogTrigger>

    <DialogContent class="sm:max-w-3xl">
      <DialogHeader>
        <DialogTitle>QEMU Runner Limits</DialogTitle>
        <DialogDescription>
          Configure per-workspace defaults/min/max and optional active totals for this runner.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <form
          id="runner-limits-form"
          class="grid grid-cols-1 gap-4 md:grid-cols-2"
          @submit.prevent="handleSubmit"
        >
          <!-- Per-workspace limits -->
          <section class="space-y-3 rounded-md border border-border bg-muted/50 p-3">
            <p class="text-sm font-medium text-foreground">Per-workspace limits</p>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <p class="text-sm font-medium text-foreground">vCPU</p>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Min</span>
                <input v-model.number="minVcpus" type="range" class="w-full accent-primary" min="1" :max="Math.max(1, maxVcpus)" step="1" />
                <input v-model.number="minVcpus" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" min="1" :max="Math.max(1, maxVcpus)" step="1" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Default</span>
                <input v-model.number="defaultVcpus" type="range" class="w-full accent-primary" :min="minVcpus" :max="maxVcpus" step="1" />
                <input v-model.number="defaultVcpus" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="minVcpus" :max="maxVcpus" step="1" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Max</span>
                <input v-model.number="maxVcpus" type="range" class="w-full accent-primary" :min="Math.max(1, minVcpus)" max="128" step="1" />
                <input v-model.number="maxVcpus" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="Math.max(1, minVcpus)" max="128" step="1" />
              </div>
            </div>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <p class="text-sm font-medium text-foreground">RAM (MiB)</p>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Min</span>
                <input v-model.number="minMemoryMb" type="range" class="w-full accent-primary" :min="RAM_STEP" :max="Math.max(RAM_STEP, maxMemoryMb)" :step="RAM_STEP" />
                <input v-model.number="minMemoryMb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="RAM_STEP" :max="Math.max(RAM_STEP, maxMemoryMb)" :step="RAM_STEP" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Default</span>
                <input v-model.number="defaultMemoryMb" type="range" class="w-full accent-primary" :min="minMemoryMb" :max="maxMemoryMb" :step="RAM_STEP" />
                <input v-model.number="defaultMemoryMb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="minMemoryMb" :max="maxMemoryMb" :step="RAM_STEP" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Max</span>
                <input v-model.number="maxMemoryMb" type="range" class="w-full accent-primary" :min="Math.max(RAM_STEP, minMemoryMb)" max="262144" :step="RAM_STEP" />
                <input v-model.number="maxMemoryMb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="Math.max(RAM_STEP, minMemoryMb)" max="262144" :step="RAM_STEP" />
              </div>
            </div>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <p class="text-sm font-medium text-foreground">Storage (GiB)</p>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Min</span>
                <input v-model.number="minDiskSizeGb" type="range" class="w-full accent-primary" :min="DISK_STEP" :max="Math.max(DISK_STEP, maxDiskSizeGb)" :step="DISK_STEP" />
                <input v-model.number="minDiskSizeGb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="DISK_STEP" :max="Math.max(DISK_STEP, maxDiskSizeGb)" :step="DISK_STEP" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Default</span>
                <input v-model.number="defaultDiskSizeGb" type="range" class="w-full accent-primary" :min="minDiskSizeGb" :max="maxDiskSizeGb" :step="DISK_STEP" />
                <input v-model.number="defaultDiskSizeGb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="minDiskSizeGb" :max="maxDiskSizeGb" :step="DISK_STEP" />
              </div>
              <div class="grid grid-cols-[3.5rem_1fr_4.5rem] items-center gap-2">
                <span class="text-xs text-muted-foreground">Max</span>
                <input v-model.number="maxDiskSizeGb" type="range" class="w-full accent-primary" :min="Math.max(DISK_STEP, minDiskSizeGb)" max="4096" :step="DISK_STEP" />
                <input v-model.number="maxDiskSizeGb" type="number" class="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="Math.max(DISK_STEP, minDiskSizeGb)" max="4096" :step="DISK_STEP" />
              </div>
            </div>
          </section>

          <!-- Active totals -->
          <section class="space-y-3 rounded-md border border-border bg-muted/50 p-3">
            <div class="flex items-center justify-between">
              <p class="text-sm font-medium text-foreground">Active totals</p>
              <p class="text-xs text-muted-foreground">optional cap, can exceed host marker</p>
            </div>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <div class="flex items-center justify-between">
                <p class="text-sm text-foreground">Total active vCPU</p>
                <label class="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox v-model="unlimitedActiveVcpus" />
                  Unlimited
                </label>
              </div>
              <template v-if="!unlimitedActiveVcpus">
                <div class="flex items-center gap-2">
                  <input v-model.number="maxActiveVcpus" type="range" class="w-full accent-primary" min="1" :max="activeVcpusSliderMax" step="1" />
                  <input v-model.number="maxActiveVcpus" type="number" class="w-18 shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" min="1" :max="activeVcpusSliderMax" step="1" />
                </div>
                <div class="relative h-2 rounded bg-muted">
                  <div v-if="hostVcpus" class="absolute top-0 bottom-0 w-px bg-primary" :style="{ left: markerPosition(hostVcpus, activeVcpusSliderMax) }" />
                </div>
                <div class="flex justify-between text-[11px]">
                  <span class="text-muted-foreground">1</span>
                  <span class="text-primary" v-if="hostVcpus">host {{ hostVcpus }}</span>
                  <span class="text-muted-foreground">{{ activeVcpusSliderMax }}</span>
                </div>
              </template>
            </div>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <div class="flex items-center justify-between">
                <p class="text-sm text-foreground">Total active RAM (MiB)</p>
                <label class="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox v-model="unlimitedActiveMemoryMb" />
                  Unlimited
                </label>
              </div>
              <template v-if="!unlimitedActiveMemoryMb">
                <div class="flex items-center gap-2">
                  <input v-model.number="maxActiveMemoryMb" type="range" class="w-full accent-primary" :min="RAM_STEP" :max="activeMemorySliderMax" :step="RAM_STEP" />
                  <input v-model.number="maxActiveMemoryMb" type="number" class="w-18 shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="RAM_STEP" :max="activeMemorySliderMax" :step="RAM_STEP" />
                </div>
                <div class="relative h-2 rounded bg-muted">
                  <div v-if="hostMemoryMb" class="absolute top-0 bottom-0 w-px bg-primary" :style="{ left: markerPosition(hostMemoryMb, activeMemorySliderMax) }" />
                </div>
                <div class="flex justify-between text-[11px]">
                  <span class="text-muted-foreground">{{ RAM_STEP }}</span>
                  <span class="text-primary" v-if="hostMemoryMb">host {{ hostMemoryMb }}</span>
                  <span class="text-muted-foreground">{{ activeMemorySliderMax }}</span>
                </div>
              </template>
            </div>

            <div class="space-y-2 rounded-sm border border-border bg-background p-3">
              <div class="flex items-center justify-between">
                <p class="text-sm text-foreground">Total active disk (GiB)</p>
                <label class="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox v-model="unlimitedActiveDiskSizeGb" />
                  Unlimited
                </label>
              </div>
              <template v-if="!unlimitedActiveDiskSizeGb">
                <div class="flex items-center gap-2">
                  <input v-model.number="maxActiveDiskSizeGb" type="range" class="w-full accent-primary" :min="DISK_STEP" :max="activeDiskSliderMax" :step="DISK_STEP" />
                  <input v-model.number="maxActiveDiskSizeGb" type="number" class="w-18 shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="DISK_STEP" :max="activeDiskSliderMax" :step="DISK_STEP" />
                </div>
                <div class="relative h-2 rounded bg-muted">
                  <div v-if="hostDiskSizeGb" class="absolute top-0 bottom-0 w-px bg-primary" :style="{ left: markerPosition(hostDiskSizeGb, activeDiskSliderMax) }" />
                </div>
                <div class="flex justify-between text-[11px]">
                  <span class="text-muted-foreground">{{ DISK_STEP }}</span>
                  <span class="text-primary" v-if="hostDiskSizeGb">host {{ hostDiskSizeGb }}</span>
                  <span class="text-muted-foreground">{{ activeDiskSliderMax }}</span>
                </div>
              </template>
            </div>

            <p class="text-xs text-muted-foreground" v-if="loadingMetrics">Loading host limits…</p>
          </section>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="open = false">Cancel</Button>
        <Button type="submit" form="runner-limits-form" :disabled="submitting || !isValid">
          {{ submitting ? 'Saving…' : 'Save' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
