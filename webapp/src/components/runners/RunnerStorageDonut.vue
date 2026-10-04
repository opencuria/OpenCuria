<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown, HardDrive, Image, Monitor } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { storageBytes } from '@/composables/useRunnerStorage'
import {
  buildRunnerStorageChart,
  storageRingPath,
  type StorageSlice,
} from '@/lib/runnerStorageChart'
import type { RunnerTopology, RunnerTopologyNode } from '@/lib/runnerTopology'
import type { StorageRuntime } from '@/types/runnerStorage'

const props = defineProps<{
  runtimes: StorageRuntime[]
  topology: RunnerTopology
  selectedKey?: string | null
}>()
const emit = defineEmits<{ select: [node: RunnerTopologyNode] }>()
const chart = computed(() => buildRunnerStorageChart(props.runtimes, props.topology))
const activeKey = ref<string | null>(null)
const groups = computed(() =>
  [
    ...chart.value.groups.map((group, index) => ({
      ...group,
      color: `var(--git-branch-${(index % 8) + 1})`,
      description: 'Image + workspaces',
    })),
    {
      key: 'other',
      label: 'Other',
      bytes: chart.value.other,
      children: [],
      color: 'var(--muted-foreground)',
      description: 'System, unassigned or unmeasured files, shared allocations and reserved space',
    },
    {
      key: 'free',
      label: 'Free',
      bytes: chart.value.free,
      children: [],
      color: 'var(--muted)',
      description: 'Available space on the measured filesystems',
    },
  ].filter((group) => group.bytes > 0 || group.children.length > 0),
)
const segments = computed(() => {
  let start = 0
  return groups.value
    .filter((group) => group.bytes > 0)
    .flatMap((group) => {
      const sweep = (group.bytes / chart.value.capacity) * 360
      let childStart = start
      const children = (group.children.length ? group.children : [group]).filter(
        (child) => child.bytes > 0,
      )
      const outer = children.map((child, index) => {
        const childSweep = (child.bytes / chart.value.capacity) * 360
        const segment = {
          ...child,
          key: `outer-${child.key}`,
          groupKey: group.key,
          ring: 'outer',
          grouped: false,
          color: group.color,
          fill: group.children.length
            ? `color-mix(in srgb, ${group.color} ${Math.max(48, 100 - index * 12)}%, var(--card))`
            : group.color,
          d: storageRingPath(111, 148, childStart, childSweep),
        }
        childStart += childSweep
        return segment
      })
      const inner = {
        ...group,
        key: `inner-${group.key}`,
        groupKey: group.key,
        ring: 'inner',
        grouped: !!group.children.length,
        fill: group.color,
        d: storageRingPath(70, 107, start, sweep),
      }
      start += sweep
      return [inner, ...outer]
    })
})
const active = computed(() => segments.value.find((segment) => segment.key === activeKey.value))
function percent(bytes: number) {
  return chart.value.capacity > 0
    ? `${((bytes / chart.value.capacity) * 100).toFixed(1)}%`
    : 'Unknown'
}
function sliceBytes(slice: StorageSlice) {
  return slice.unknown && !slice.bytes
    ? 'Unknown'
    : `${storageBytes(slice.bytes)}${slice.unknown ? ' · partial' : ''}`
}
function label(slice: StorageSlice, grouped = false) {
  return `${slice.label}${grouped ? ' · Image + workspaces' : ''}: ${sliceBytes(slice)} · ${percent(slice.bytes)}`
}
function select(slice: StorageSlice) {
  if (slice.node) emit('select', slice.node)
}
</script>

<template>
  <section
    aria-label="QEMU storage breakdown"
    class="storage-breakdown min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    data-testid="runner-storage-donut"
  >
    <header
      class="flex flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-3"
    >
      <div>
        <h3 class="flex items-center gap-2 text-sm font-semibold">
          <HardDrive :size="15" />Storage breakdown
        </h3>
        <p class="mt-1 text-xs text-muted-foreground">
          QEMU · Inner: image totals · Outer: image and workspaces
        </p>
      </div>
      <span
        class="text-[11px] text-muted-foreground"
        :class="chart.stale ? 'text-warning' : ''"
        role="status"
      >
        {{
          chart.stale ? 'Last confirmed inventory · Outdated' : 'Allocated file storage · Cached'
        }}
      </span>
    </header>
    <p v-if="chart.unavailable" role="status" class="px-4 py-6 text-sm text-muted-foreground">
      {{ chart.unavailable }}
    </p>
    <template v-else>
      <div class="storage-layout grid min-w-0 items-start gap-4 p-4">
        <div class="min-w-0 space-y-2">
          <svg
            viewBox="0 0 320 320"
            role="group"
            aria-label="Two-ring storage chart"
            class="mx-auto block w-full max-w-80"
            @mouseleave="activeKey = null"
          >
            <path
              v-for="segment in segments"
              :key="segment.key"
              :d="segment.d"
              :fill="segment.fill"
              :stroke="
                segment.ring === 'outer' && segment.node?.key === selectedKey
                  ? 'var(--ring)'
                  : 'var(--card)'
              "
              :stroke-width="
                segment.ring === 'outer' && segment.node?.key === selectedKey ? 3 : 1.5
              "
              :role="segment.ring === 'outer' && segment.node ? 'button' : 'img'"
              :tabindex="0"
              :aria-label="label(segment, segment.grouped)"
              :aria-pressed="
                segment.ring === 'outer' && segment.node
                  ? selectedKey === segment.node.key
                  : undefined
              "
              :data-segment-key="segment.key"
              class="storage-segment outline-none transition-opacity focus:stroke-ring focus:stroke-[3]"
              :class="{
                'cursor-pointer': segment.ring === 'outer' && segment.node,
                'opacity-40': active && active.groupKey !== segment.groupKey,
              }"
              @mouseenter="activeKey = segment.key"
              @focus="activeKey = segment.key"
              @blur="activeKey = null"
              @click="segment.ring === 'outer' && select(segment)"
              @keydown.enter.prevent="segment.ring === 'outer' && select(segment)"
              @keydown.space.prevent="segment.ring === 'outer' && select(segment)"
            >
              <title>{{ label(segment, segment.grouped) }}</title>
            </path>
            <text x="160" y="151" text-anchor="middle" class="fill-muted-foreground text-[11px]">
              Total capacity
            </text>
            <text
              x="160"
              y="175"
              text-anchor="middle"
              class="fill-foreground text-base font-semibold"
            >
              {{ storageBytes(chart.capacity) }}
            </text>
          </svg>
          <div class="min-h-12 text-center text-xs" aria-live="polite" aria-atomic="true">
            <p class="break-words font-medium">{{ active?.label || 'Image + workspaces' }}</p>
            <p class="mt-1 text-muted-foreground tabular-nums">
              {{
                active
                  ? `${sliceBytes(active)} · ${percent(active.bytes)}`
                  : `${chart.filesystemCount} distinct filesystem${chart.filesystemCount === 1 ? '' : 's'}`
              }}
            </p>
          </div>
        </div>
        <div class="min-w-0 divide-y divide-border" aria-label="Storage legend">
          <Collapsible
            v-for="group in groups"
            :key="group.key"
            class="py-1"
            :default-open="!!group.children.some((child) => child.key === selectedKey)"
          >
            <CollapsibleTrigger v-if="group.children.length" as-child>
              <Button
                variant="ghost"
                class="h-auto min-h-10 w-full min-w-0 justify-start gap-2 px-2 py-2 text-left whitespace-normal"
              >
                <span class="size-2.5 shrink-0 rounded-full" :style="{ background: group.color }" />
                <span class="min-w-0 flex-1"
                  ><span class="block break-words text-xs font-medium">{{ group.label }}</span
                  ><span class="block text-[11px] text-muted-foreground font-normal"
                    >Image +
                    {{ group.children.filter((child) => child.node?.workspace).length }}
                    workspaces</span
                  ></span
                >
                <span class="shrink-0 text-right text-xs tabular-nums"
                  ><span class="block">{{ sliceBytes(group) }}</span
                  ><span class="text-[11px] text-muted-foreground">{{
                    group.children.some((child) => child.unknown) ? 'Partial' : percent(group.bytes)
                  }}</span></span
                >
                <ChevronDown class="size-3.5 shrink-0 text-muted-foreground" />
              </Button>
            </CollapsibleTrigger>
            <div
              v-else
              class="flex min-h-10 min-w-0 items-center gap-2 px-2 py-2"
              :title="group.description"
            >
              <span
                class="size-2.5 shrink-0 rounded-full border border-border"
                :style="{ background: group.color }"
              />
              <span class="min-w-0 flex-1 text-xs font-medium">{{ group.label }}</span>
              <span class="shrink-0 text-right text-xs tabular-nums"
                ><span class="block">{{ storageBytes(group.bytes) }}</span
                ><span class="text-[11px] text-muted-foreground">{{
                  percent(group.bytes)
                }}</span></span
              >
            </div>
            <CollapsibleContent v-if="group.children.length" class="pb-2 pl-4">
              <Button
                v-for="child in group.children"
                :key="child.key"
                variant="ghost"
                class="h-auto min-h-9 w-full min-w-0 justify-start gap-2 px-2 py-2 text-left whitespace-normal"
                :aria-label="`Inspect storage ${child.label}`"
                :aria-pressed="selectedKey === child.key"
                :class="selectedKey === child.key ? 'bg-accent' : ''"
                @click="select(child)"
              >
                <component
                  :is="child.node?.workspace ? Monitor : Image"
                  class="size-3.5 shrink-0 text-muted-foreground"
                />
                <span class="min-w-0 flex-1 break-words text-xs">{{
                  child.node?.workspace ? child.label : 'Image itself'
                }}</span>
                <span class="shrink-0 text-xs text-muted-foreground tabular-nums">{{
                  sliceBytes(child)
                }}</span>
              </Button>
            </CollapsibleContent>
          </Collapsible>
        </div>
      </div>
      <footer class="space-y-1 border-t border-border px-4 py-2 text-[11px] text-muted-foreground">
        <p>
          Other includes system files, unassigned storage and reserved space. Free is available
          filesystem space.
        </p>
        <p v-if="chart.unknownCount" role="status" class="text-warning">
          {{ chart.unknownCount }} file allocation{{ chart.unknownCount === 1 ? '' : 's' }}
          unmeasured · group totals are partial.
        </p>
        <p>
          Allocated blocks, not guest disk usage. Filesystem-shared extents may not be exclusive.
        </p>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.storage-breakdown {
  container-type: inline-size;
}
.storage-layout {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
}
@container (max-width: 520px) {
  .storage-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 520px) {
  .storage-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
