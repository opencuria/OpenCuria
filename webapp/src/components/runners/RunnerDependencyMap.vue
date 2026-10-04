<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { ArrowRight, HardDrive, Image, Monitor, Search } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { storageBytes } from '@/composables/useRunnerStorage'
import {
  buildRunnerTopology,
  projectRunnerTopology,
  topologyPath,
  type RunnerTopology,
  type RunnerTopologyNode,
} from '@/lib/runnerTopology'
import { generationLabel, runnerStateClass, runnerStateLabel } from '@/lib/runnerPresentation'
import type { VmSystemMetrics } from '@/types'
import type { StorageGeneration, StorageRuntime } from '@/types/runnerStorage'

const props = defineProps<{
  runtimes: StorageRuntime[]
  generations: StorageGeneration[]
  topology?: RunnerTopology
  selectedKey?: string | null
  vmMetrics?: Record<string, VmSystemMetrics>
  metricsTimestamp?: string
}>()
const emit = defineEmits<{ select: [node: RunnerTopologyNode] }>()
const graph = computed(
  () =>
    props.topology ?? projectRunnerTopology(buildRunnerTopology(props.runtimes, props.generations)),
)
const query = ref('')
const showOther = ref(false)
const showAll = ref(false)
const localSelection = ref<string | null>(null)
const selected = computed(() => {
  const key = props.selectedKey === undefined ? localSelection.value : props.selectedKey
  return key ? (graph.value.physicalToPresentation?.get(key) ?? key) : key
})
const selectedPath = computed(() =>
  topologyPath(selected.value ? [selected.value] : [], graph.value.edges),
)
const matches = computed(() => {
  const term = query.value.trim().toLowerCase()
  return term
    ? graph.value.nodes.filter((n) =>
        [
          n.label,
          n.resource?.physical_id,
          ...(n.resource?.aliases ?? []),
          ...(n.storage?.resources.flatMap((resource) => [
            resource.physical_id,
            ...resource.aliases,
          ]) ?? []),
          n.generation?.id,
          n.generation?.runner_ref,
          n.workspace?.id,
        ].some((value) => value?.toLowerCase().includes(term)),
      )
    : []
})
const matchedPath = computed(() =>
  topologyPath(
    matches.value.map((n) => n.key),
    graph.value.edges,
  ),
)
const isOther = (node: RunnerTopologyNode) => !node.storage && !node.generation
const otherCount = computed(() => graph.value.nodes.filter(isOther).length)
const eligible = computed(() =>
  graph.value.nodes.filter((node) => {
    if (query.value.trim()) return matchedPath.value.has(node.key)
    return (
      !isOther(node) ||
      showOther.value ||
      (node.unresolved && !node.unassigned) ||
      selectedPath.value.has(node.key)
    )
  }),
)
const runtimeScopes = computed(() => [...new Set(graph.value.nodes.map((n) => n.runtime))].sort())
const visible = computed(() => {
  if (showAll.value || query.value.trim()) return eligible.value
  // A focused path remains visible even beyond the initial preview.
  const preview = new Set(
    [0, 1, 2].flatMap((column) =>
      eligible.value
        .filter((n) => displayColumn(n) === column)
        .slice(0, 12)
        .map((n) => n.key),
    ),
  )
  return eligible.value.filter(
    (n) => preview.has(n.key) || selectedPath.value.has(n.key) || (n.unresolved && !n.unassigned),
  )
})
const visibleEdges = computed(() => {
  const keys = new Set(visible.value.map((n) => n.key))
  return graph.value.edges.filter((e) => keys.has(e.from) && keys.has(e.to))
})
const columns = computed(() => [
  { title: 'Workspaces', column: 0, icon: Monitor },
  ...(visible.value.some(isOther)
    ? [{ title: 'Other resources', column: 1, icon: HardDrive }]
    : []),
  { title: 'Images', column: 2, icon: Image },
])
function displayColumn(node: RunnerTopologyNode) {
  return isOther(node) ? 1 : node.column
}
const canvas = ref<HTMLElement | null>(null)
const markerId = `topology-arrow-${useId().replace(/[^a-zA-Z0-9-]/g, '')}`
const lines = ref<{ key: string; d: string; usage: boolean; highlighted: boolean }[]>([])
let observer: ResizeObserver | undefined
let disposed = false
async function measure() {
  await nextTick()
  if (disposed || !canvas.value) return
  const root = canvas.value.getBoundingClientRect()
  const elements = new Map<string, DOMRect>()
  canvas.value.querySelectorAll<HTMLElement>('[data-node-key]').forEach((element) => {
    elements.set(element.dataset.nodeKey!, element.getBoundingClientRect())
    observer?.observe(element)
  })
  lines.value = visibleEdges.value.flatMap((edge) => {
    const from = elements.get(edge.from),
      to = elements.get(edge.to)
    if (!from || !to) return []
    const forward = to.left > from.left
    const sameColumn = Math.abs(to.left - from.left) < 1
    const x1 = (sameColumn ? from.right : forward ? from.right : from.left) - root.left
    const x2 = (sameColumn ? to.right : forward ? to.left : to.right) - root.left
    const y1 = from.top + from.height / 2 - root.top
    const y2 = to.top + to.height / 2 - root.top
    const bend = sameColumn ? 22 : Math.max(20, Math.abs(x2 - x1) / 2)
    const direction = sameColumn || forward ? 1 : -1
    return [
      {
        key: JSON.stringify(edge),
        d: `M ${x1} ${y1} C ${x1 + bend * direction} ${y1}, ${x2 - (sameColumn ? -bend : bend) * direction} ${y2}, ${x2} ${y2}`,
        usage: edge.kind === 'usage',
        highlighted: selectedPath.value.has(edge.from) && selectedPath.value.has(edge.to),
      },
    ]
  })
}
watch(
  [visible, selectedPath],
  () => {
    observer?.disconnect()
    if (canvas.value) observer?.observe(canvas.value)
    void measure()
  },
  { flush: 'post' },
)
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => void measure())
    if (canvas.value) observer.observe(canvas.value)
  }
  window.addEventListener('resize', measure)
  void measure()
})
onBeforeUnmount(() => {
  disposed = true
  observer?.disconnect()
  window.removeEventListener('resize', measure)
})
function select(node: RunnerTopologyNode) {
  localSelection.value = node.key
  emit('select', node)
}
function nodeLabel(key: string) {
  return graph.value.nodes.find((n) => n.key === key)?.label ?? 'Unknown resource'
}
function semantic(node: RunnerTopologyNode) {
  return node.unresolved
    ? 'Unresolved target'
    : node.generation
      ? generationLabel(node.generation)
      : node.resource?.kind.replace(/_/g, ' ') ||
        (node.workspace ? 'Logical workspace' : 'Generation · unconfirmed resource')
}
function observed(node: RunnerTopologyNode) {
  return (
    node.resource?.state ||
    node.workspace?.observed_state ||
    node.generation?.observed_state ||
    'unknown'
  )
}
function lifecycle(node: RunnerTopologyNode) {
  return node.generation?.status || (node.column === 0 ? node.workspace?.status : undefined)
}
function mismatch(node: RunnerTopologyNode) {
  return lifecycle(node) && runnerStateLabel(lifecycle(node)) !== runnerStateLabel(observed(node))
}
function observationRecent(node: RunnerTopologyNode) {
  return (
    !!node.resource &&
    props.runtimes.some((runtime) => runtime.runtime_type === node.runtime && runtime.fresh)
  )
}
function vmSample(node: RunnerTopologyNode) {
  if (
    node.runtime !== 'qemu' ||
    node.column !== 0 ||
    !node.resource ||
    !node.workspace ||
    node.resource.state !== 'running'
  )
    return
  const sample = props.vmMetrics?.[node.workspace.id]
  if (
    !sample ||
    !Number.isFinite(sample.cpu_usage_percent) ||
    sample.cpu_usage_percent < 0 ||
    sample.cpu_usage_percent > 100 ||
    !Number.isFinite(sample.ram_used_bytes) ||
    !Number.isFinite(sample.ram_total_bytes) ||
    sample.ram_total_bytes <= 0 ||
    sample.ram_used_bytes < 0 ||
    sample.ram_used_bytes > sample.ram_total_bytes
  )
    return
  return sample
}
</script>

<template>
  <div class="dependency-map min-w-0 space-y-3">
    <div class="flex min-w-0 flex-wrap items-center gap-2">
      <div class="relative min-w-0 flex-1 basis-48">
        <Search class="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
        <Input
          v-model="query"
          aria-label="Search dependency map"
          placeholder="Find a workspace, disk or image…"
          class="pl-9"
        />
      </div>
      <Button
        v-if="otherCount"
        variant="outline"
        size="sm"
        :aria-expanded="showOther"
        @click="showOther = !showOther"
        >Other resources ({{ otherCount }})</Button
      >
    </div>
    <p v-if="query.trim()" role="status" class="text-xs text-muted-foreground">
      {{ matches.length }} matches · showing their dependency paths
    </p>
    <div ref="canvas" class="topology-canvas relative rounded-xl border bg-muted/40 p-3">
      <svg
        class="topology-lines pointer-events-none absolute inset-0 h-full w-full overflow-visible"
        aria-hidden="true"
      >
        <defs>
          <marker
            :id="markerId"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
          </marker>
        </defs>
        <path
          v-for="line in lines"
          :key="line.key"
          :d="line.d"
          fill="none"
          :stroke-width="line.highlighted ? 2 : 1.25"
          :stroke-dasharray="line.usage ? '5 4' : undefined"
          :marker-end="`url(#${markerId})`"
          :class="line.highlighted ? 'text-primary' : 'text-muted-foreground/50'"
          stroke="currentColor"
        />
      </svg>
      <section
        v-for="scope in runtimeScopes.filter((runtime) =>
          visible.some((n) => n.runtime === runtime),
        )"
        :key="scope"
        class="relative space-y-3"
        :class="runtimeScopes.length > 1 ? 'mb-5 last:mb-0' : ''"
        :aria-label="`${scope} dependency map`"
      >
        <h3 v-if="runtimeScopes.length > 1" class="border-b pb-2 text-xs font-semibold">
          {{ scope === 'qemu' ? 'QEMU' : scope === 'docker' ? 'Docker' : scope }}
        </h3>
        <div
          class="topology-columns relative grid gap-10"
          :class="columns.length === 3 ? 'grid-cols-3' : 'grid-cols-2'"
        >
          <section
            v-for="{ title, column, icon } in columns"
            :key="title"
            class="min-w-0 space-y-2"
            :aria-label="title"
          >
            <h4 class="mb-3 flex items-center gap-2 text-xs font-semibold text-muted-foreground">
              <component :is="icon" class="size-3.5" />{{ title
              }}<span class="ml-auto tabular-nums">{{
                visible.filter((n) => n.runtime === scope && displayColumn(n) === column).length
              }}</span>
            </h4>
            <Button
              v-for="node in visible.filter(
                (n) => n.runtime === scope && displayColumn(n) === column,
              )"
              :key="node.key"
              variant="outline"
              :data-node-key="node.key"
              :aria-label="`Inspect ${node.label}`"
              :aria-pressed="selected === node.key"
              class="topology-node h-auto w-full min-w-0 justify-start rounded-lg bg-background shadow-sm px-2.5 py-2 text-left whitespace-normal"
              :class="{
                'border-primary ring-2 ring-primary/20': selected === node.key,
                'border-primary/50 bg-primary/5': selectedPath.has(node.key),
                'opacity-50': selected && !selectedPath.has(node.key),
                'ring-1 ring-primary': matches.some((n) => n.key === node.key),
              }"
              @click="select(node)"
            >
              <span class="block w-full min-w-0 space-y-0.5 leading-tight">
                <span class="flex min-w-0 items-center gap-1.5 text-xs font-medium">
                  <component :is="icon" class="size-3.5 shrink-0 text-muted-foreground" />
                  <span class="truncate">{{ node.label }}</span>
                </span>
                <span class="flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span
                    class="truncate"
                    :title="
                      node.generation && !node.resource
                        ? 'No physical resource confirmed'
                        : semantic(node)
                    "
                    >{{ semantic(node) }}</span
                  >
                  <span
                    class="ml-auto flex shrink-0 items-center gap-1"
                    :title="`${observationRecent(node) ? 'Observed' : 'Last confirmed observed state'}: ${runnerStateLabel(observed(node))}`"
                  >
                    <span
                      class="size-1.5 rounded-full"
                      :class="
                        observationRecent(node) &&
                        ['running', 'ready', 'present', 'available'].includes(observed(node))
                          ? 'bg-success'
                          : runnerStateClass(observed(node)).includes('text-destructive')
                            ? 'bg-destructive'
                            : runnerStateClass(observed(node)).includes('text-warning')
                              ? 'bg-warning'
                              : 'bg-muted-foreground/50'
                      "
                    />
                    <span
                      ><span class="sr-only">Observed: </span
                      >{{ runnerStateLabel(observed(node)) }}</span
                    >
                  </span>
                </span>
                <span
                  v-if="mismatch(node)"
                  class="block truncate text-[11px] text-muted-foreground"
                  :title="`Lifecycle: ${lifecycle(node)} · Observed: ${observed(node)}`"
                  >Lifecycle {{ runnerStateLabel(lifecycle(node)) }} · Observed
                  {{ runnerStateLabel(observed(node)) }}</span
                >
                <span
                  class="flex flex-wrap justify-between gap-x-2 text-[11px] text-muted-foreground"
                >
                  <span>{{ node.runtime }}</span>
                  <span v-if="!node.storage && node.resource?.allocated_bytes != null"
                    >{{ storageBytes(node.resource.allocated_bytes) }} allocated</span
                  >
                  <span
                    v-if="
                      node.generation?.size_bytes != null &&
                      node.generation.size_bytes !== node.resource?.allocated_bytes
                    "
                    >{{ storageBytes(node.generation.size_bytes) }} image ({{
                      node.generation.size_source
                    }})</span
                  >
                </span>
                <span
                  v-if="vmSample(node)"
                  class="block text-[11px] text-muted-foreground"
                  :title="metricsTimestamp ? `VM sample: ${metricsTimestamp}` : 'Latest VM sample'"
                  >VM CPU {{ vmSample(node)!.cpu_usage_percent.toFixed(0) }}% · RAM
                  {{ storageBytes(vmSample(node)!.ram_used_bytes) }} /
                  {{ storageBytes(vmSample(node)!.ram_total_bytes) }}</span
                >
                <template v-if="node.storage">
                  <span class="block text-[11px] text-muted-foreground">
                    Used {{ storageBytes(node.storage.allocatedBytes)
                    }}<template v-if="node.storage.unknownCount">
                      · partial ({{ node.storage.unknownCount }} unknown)</template
                    >
                  </span>
                  <span class="block text-[11px] text-muted-foreground"
                    >Disk capacity {{ storageBytes(node.storage.virtualBytes) }}</span
                  >
                </template>
                <span class="mobile-relations space-y-1 pt-1 text-[11px] text-muted-foreground"
                  ><span
                    v-for="edge in visibleEdges.filter((e) => e.from === node.key)"
                    :key="edge.to"
                    class="flex items-start gap-1"
                    ><ArrowRight class="mt-0.5 size-3 shrink-0" /><span class="break-words"
                      >{{ edge.kind === 'usage' ? 'Uses image (domain)' : 'Depends on' }}:
                      {{ nodeLabel(edge.to) }}</span
                    ></span
                  ></span
                >
              </span>
            </Button>
            <p
              v-if="!visible.some((n) => n.runtime === scope && displayColumn(n) === column)"
              class="text-xs text-muted-foreground"
            >
              None in this view
            </p>
          </section>
        </div>
      </section>
      <p v-if="!graph.nodes.length" role="status" class="pt-3 text-sm text-muted-foreground">
        No dependency inventory available.
      </p>
    </div>
    <div
      class="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground"
    >
      <p class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>→ Consumer to dependency</span><span>━ Confirmed physical</span
        ><span>┄ Uses image · domain / transitive only</span>
      </p>
      <span>{{ visible.length }} / {{ graph.nodes.length }} nodes</span>
    </div>
    <Button
      v-if="showAll || visible.length < eligible.length"
      variant="ghost"
      size="sm"
      @click="showAll = !showAll"
      >{{ showAll ? 'Show compact preview' : `Show all ${eligible.length} nodes` }}</Button
    >
    <ul class="sr-only" aria-label="Dependency relationships">
      <li v-for="edge in visibleEdges" :key="JSON.stringify(edge)">
        {{ nodeLabel(edge.from) }} → {{ nodeLabel(edge.to) }} ·
        {{
          edge.kind === 'physical'
            ? 'Confirmed physical dependency'
            : 'Uses image (domain relation, not a confirmed physical edge)'
        }}
      </li>
    </ul>
  </div>
</template>

<style scoped>
.dependency-map {
  container-type: inline-size;
}
.mobile-relations {
  display: none;
}
@container (max-width: 640px) {
  .topology-columns {
    grid-template-columns: minmax(0, 1fr);
    gap: 1rem;
  }
  .topology-lines {
    display: none;
  }
  .mobile-relations {
    display: block;
  }
  .topology-node {
    overflow-wrap: anywhere;
  }
}
@media (max-width: 640px) {
  .topology-columns {
    grid-template-columns: minmax(0, 1fr);
    gap: 1rem;
  }
  .topology-lines {
    display: none;
  }
  .mobile-relations {
    display: block;
  }
  .topology-node {
    overflow-wrap: anywhere;
  }
}
</style>
