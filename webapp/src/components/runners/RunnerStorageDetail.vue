<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useNow } from '@vueuse/core'
import {
  ArrowLeft,
  Camera,
  ChevronDown,
  Clock,
  HardDrive,
  Layers,
  Monitor,
  RefreshCw,
  Search,
  Server,
  TriangleAlert,
} from '@lucide/vue'
import type { Runner, RunnerSystemMetrics } from '@/types'
import { RunnerStatus } from '@/types'
import type { DeletionTarget, StorageGeneration } from '@/types/runnerStorage'
import { useAuthStore } from '@/stores/auth'
import { useRunnerStorage, storageBytes } from '@/composables/useRunnerStorage'
import { cancelDeletion } from '@/services/runnerStorage.api'
import { updateRunnerImageBuild } from '@/services/workspaces.api'
import {
  buildRunnerTopology,
  projectRunnerTopology,
  type RunnerTopologyNode,
} from '@/lib/runnerTopology'
import {
  currentRunnerDefault,
  generationLabel,
  runnerStateClass,
  runnerStateLabel,
} from '@/lib/runnerPresentation'
import { formatRelativeTime } from '@/lib/utils'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import ImageDeletionDialog from '@/components/images/ImageDeletionDialog.vue'
import EditRunnerResourcesDialog from './EditRunnerResourcesDialog.vue'
import RunnerResourceOverview from './RunnerResourceOverview.vue'
import RunnerDependencyMap from './RunnerDependencyMap.vue'
import RunnerStorageDonut from './RunnerStorageDonut.vue'
import RunnerNodeInspector from './RunnerNodeInspector.vue'
import RunnerActivityList from './RunnerActivityList.vue'

const props = defineProps<{ runner: Runner }>()
const emit = defineEmits<{ back: [] }>()
const auth = useAuthStore()
const runnerId = computed(() => props.runner.id)
const { data, deletions, error, loading, refreshPending, load, refresh } =
  useRunnerStorage(runnerId)
const tab = ref('overview')
const freshMetrics = ref<RunnerSystemMetrics | null>(null)
const runtimeFilter = ref('all')
const inventoryQuery = ref('')
const showDeleted = ref(false)
const selectedKey = ref<string | null>(null)
const target = ref<DeletionTarget | null>(null)
const targetName = ref('')
const busy = ref(false)
const cockpit = ref<HTMLElement | null>(null)
const scanBusy = ref(false)
const actionError = ref('')
const online = computed(() => props.runner.status === RunnerStatus.ONLINE)
const now = useNow({ interval: 15_000 })
const confirmedRuntimes = computed(() =>
  (data.value?.runtimes ?? []).map((runtime) => {
    const times = [runtime.collected_at, runtime.received_at].map((value) =>
      value ? Date.parse(value) : NaN,
    )
    const recent = times.every(
      (time) =>
        Number.isFinite(time) &&
        time <= now.value.getTime() &&
        now.value.getTime() - time < 300_000,
    )
    return {
      ...runtime,
      fresh: runtime.fresh && online.value && !!data.value?.runner_online && !error.value && recent,
    }
  }),
)
const stored = computed(() => data.value?.generations.filter((i) => i.status !== 'deleted') ?? [])
const deleted = computed(() => data.value?.generations.filter((i) => i.status === 'deleted') ?? [])
const runtimeTypes = computed(() =>
  [
    ...new Set([
      ...(props.runner.available_runtimes ?? []),
      ...(data.value?.runtimes.map((r) => r.runtime_type) ?? []),
      ...stored.value.map((i) => i.runtime_type),
    ]),
  ].filter(Boolean),
)
const visibleRuntimes = computed(
  () =>
    confirmedRuntimes.value.filter(
      (r) => runtimeFilter.value === 'all' || r.runtime_type === runtimeFilter.value,
    ) ?? [],
)
const visibleGenerations = computed(() =>
  stored.value.filter(
    (i) => runtimeFilter.value === 'all' || i.runtime_type === runtimeFilter.value,
  ),
)
const topology = computed(() =>
  buildRunnerTopology(data.value?.runtimes ?? [], data.value?.generations ?? []),
)
const presentation = computed(() => projectRunnerTopology(topology.value))
const visibleTopology = computed(() => {
  const nodes = presentation.value.nodes.filter(
    (node) =>
      node.generation?.status !== 'deleted' &&
      (runtimeFilter.value === 'all' || node.runtime === runtimeFilter.value),
  )
  const keys = new Set(nodes.map((node) => node.key))
  return {
    ...presentation.value,
    nodes,
    edges: presentation.value.edges.filter((edge) => keys.has(edge.from) && keys.has(edge.to)),
  }
})
const selected = computed(
  () =>
    presentation.value.nodes.find((n) => n.key === selectedKey.value) ??
    topology.value.nodes.find((n) => n.key === selectedKey.value) ??
    null,
)
const presentationSelectedKey = computed(() =>
  selectedKey.value
    ? (presentation.value.physicalToPresentation?.get(selectedKey.value) ?? selectedKey.value)
    : null,
)
const knownWorkspaces = computed(
  () => new Set(topology.value.nodes.flatMap((n) => (n.workspace ? [n.workspace.id] : []))).size,
)
const resourceCount = computed(
  () => data.value?.runtimes.reduce((sum, r) => sum + r.resources.length, 0) ?? 0,
)
const inventoryState = computed(() => {
  if (!data.value) return 'Inventory unavailable'
  if (!data.value.runner_online || !online.value) return 'Runner offline · Last confirmed inventory'
  if (!data.value.latest_complete || !data.value.runtimes.length)
    return 'Partial / unavailable inventory'
  if (confirmedRuntimes.value.some((r) => !r.fresh)) return 'Stale inventory'
  return 'Recent inventory · Cached, not live'
})
const inventoryUncertain = computed(
  () =>
    !online.value ||
    !data.value?.runner_online ||
    !data.value?.latest_complete ||
    !data.value.runtimes.length ||
    confirmedRuntimes.value.some((r) => !r.fresh),
)
const lastInventory = computed(
  () =>
    data.value?.runtimes
      .map((r) => r.collected_at)
      .filter((t): t is string => !!t && Number.isFinite(Date.parse(t)))
      .sort()
      .slice(-1)[0],
)
const inventoryProblems = computed(
  () => data.value?.runtimes.reduce((sum, r) => sum + (r.diagnostics?.errors.length ?? 0), 0) ?? 0,
)
const activityProblems = computed(
  () =>
    (data.value?.operations.filter(
      (o) =>
        o.task__error ||
        ['failed', 'intervention', 'intervention_required'].includes(o.task__status) ||
        ['intervention', 'intervention_required'].includes(o.phase),
    ).length ?? 0) +
    deletions.value.filter((d) =>
      ['reconfirmation_required', 'failed', 'intervention_required'].includes(d.phase),
    ).length +
    (data.value?.capture_requests.filter(
      (c) =>
        (!['completed', 'cancelled'].includes(c.phase) && c.diagnostic) ||
        c.resume_suppressed ||
        ['failed', 'intervention_required'].includes(c.phase),
    ).length ?? 0),
)
const activeActivities = computed(
  () =>
    (data.value?.operations.length ?? 0) +
    deletions.value.filter((d) => !['completed', 'cancelled'].includes(d.phase)).length +
    (data.value?.capture_requests.filter(
      (c) => !['completed', 'cancelled', 'failed'].includes(c.phase),
    ).length ?? 0),
)
const inventoryGroups = computed(() => {
  const term = inventoryQuery.value.trim().toLowerCase()
  const matches = (image: StorageGeneration) =>
    [image.name, image.definition_name, image.id, image.runner_ref].some((t) =>
      t?.toLowerCase().includes(term),
    ) &&
    (runtimeFilter.value === 'all' || image.runtime_type === runtimeFilter.value)
  return [
    {
      name: 'Base / build images',
      icon: Layers,
      images: stored.value.filter((i) => i.origin_type !== 'workspace_capture' && matches(i)),
    },
    {
      name: 'Captured images',
      icon: Camera,
      images: stored.value.filter((i) => i.origin_type === 'workspace_capture' && matches(i)),
    },
    ...(showDeleted.value
      ? [{ name: 'Deleted history', icon: Clock, images: deleted.value.filter(matches) }]
      : []),
  ]
})
const inventoryResources = computed(() =>
  topology.value.nodes.filter(
    (n) =>
      n.resource &&
      (runtimeFilter.value === 'all' || n.runtime === runtimeFilter.value) &&
      [n.label, n.resource.physical_id, ...n.resource.aliases].some((t) =>
        t.toLowerCase().includes(inventoryQuery.value.trim().toLowerCase()),
      ),
  ),
)
watch(runtimeFilter, () => {
  selectedKey.value = null
})
watch(selectedKey, async (key) => {
  if (!key) return
  await nextTick()
  const heading = cockpit.value?.querySelector<HTMLElement>('[data-testid=runner-inspector] h4')
  heading?.scrollIntoView?.({
    block: 'nearest',
    behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
  })
})
function selectNode(node: RunnerTopologyNode) {
  selectedKey.value = node.key
}
function selectImage(image: StorageGeneration) {
  selectedKey.value =
    presentation.value.nodes.find((n) => n.generation?.id === image.id)?.key ?? null
}
function requestDelete(value: DeletionTarget, name: string) {
  target.value = value
  targetName.value = name
}
async function scan() {
  if (scanBusy.value || refreshPending.value) return
  scanBusy.value = true
  try {
    await refresh()
  } finally {
    scanBusy.value = false
  }
}
async function cancel(id: string) {
  if (busy.value || !deletions.value.find((d) => d.id === id)?.can_cancel) return
  busy.value = true
  actionError.value = ''
  try {
    await cancelDeletion(id)
    await load()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Cancellation refused'
  } finally {
    busy.value = false
  }
}
async function rebuild(definition: string) {
  const image = stored.value.find((i) => i.definition_id === definition && currentRunnerDefault(i))
  if (
    busy.value ||
    !image ||
    ['pending_deletion', 'deleting'].includes(image.status) ||
    stored.value.some(
      (i) =>
        i.build_job_id === image.build_job_id &&
        i.is_pending &&
        ['pending', 'building', 'creating'].includes(i.status),
    )
  )
    return
  busy.value = true
  actionError.value = ''
  try {
    await updateRunnerImageBuild(definition, props.runner.id, { action: 'rebuild' })
    await load()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Rebuild refused'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="min-w-0 space-y-5 text-sm" data-testid="runner-cockpit" ref="cockpit">
    <header class="space-y-3">
      <Button
        variant="ghost"
        size="sm"
        class="-ml-2 text-xs text-muted-foreground"
        @click="emit('back')"
        ><ArrowLeft />Back to runners</Button
      >
      <div class="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div class="flex min-w-0 flex-1 basis-64 items-center gap-3">
          <div
            class="flex size-11 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary"
          >
            <Server :size="21" />
          </div>
          <div class="min-w-0 space-y-1">
            <h2 class="truncate text-xl font-semibold tracking-tight">
              {{ runner.name || runner.id.slice(0, 8) }}
            </h2>
            <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span class="inline-flex items-center gap-1.5"
                ><span
                  class="size-1.5 rounded-full"
                  :class="online ? 'bg-success' : 'bg-muted-foreground'"
                />{{ online ? 'Online' : 'Offline' }}</span
              >
              <span v-for="type in runtimeTypes" :key="type">{{ type.toUpperCase() }}</span>
              <span
                >{{ online ? 'Connected' : 'Last seen' }}
                {{
                  formatRelativeTime(online ? runner.connected_at : runner.disconnected_at)
                }}</span
              >
            </div>
          </div>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <EditRunnerResourcesDialog
            v-if="auth.isAdmin && runnerSupportsRuntime(runner, 'qemu')"
            :runner="runner"
          />
          <Button variant="outline" size="sm" :disabled="scanBusy || refreshPending" @click="scan"
            ><RefreshCw :class="{ 'animate-spin': scanBusy || refreshPending }" />{{
              refreshPending ? 'Scan queued' : 'Refresh inventory'
            }}</Button
          >
        </div>
      </div>
    </header>
    <div
      v-if="error"
      role="alert"
      class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive"
    >
      <span>{{ error }}</span
      ><Button variant="outline" size="sm" :disabled="loading" @click="load"
        >Retry inspection</Button
      >
    </div>
    <div
      v-if="actionError"
      role="alert"
      class="rounded-lg border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive"
    >
      {{ actionError }}
    </div>
    <p v-if="refreshPending" role="status" class="text-xs text-muted-foreground">
      Scan queued · awaiting a new complete inventory{{
        online ? '' : ' when the runner reconnects'
      }}.
    </p>
    <Tabs v-model="tab" class="min-w-0 gap-4">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-1">
        <TabsList variant="line" aria-label="Runner detail views">
          <TabsTrigger value="overview">Overview</TabsTrigger
          ><TabsTrigger value="inventory">Inventory</TabsTrigger
          ><TabsTrigger value="activities"
            >Activities<span
              v-if="activeActivities"
              class="rounded-full bg-muted px-1.5 text-[10px] tabular-nums"
              >{{ activeActivities }}</span
            ></TabsTrigger
          >
        </TabsList>
        <Select v-if="runtimeTypes.length > 1 && tab !== 'activities'" v-model="runtimeFilter"
          ><SelectTrigger size="sm" aria-label="Filter runtime" class="w-32"
            ><SelectValue /></SelectTrigger
          ><SelectContent
            ><SelectItem value="all">All runtimes</SelectItem
            ><SelectItem v-for="type in runtimeTypes" :key="type" :value="type">{{
              type.toUpperCase()
            }}</SelectItem></SelectContent
          ></Select
        >
      </div>
      <TabsContent value="overview" class="min-w-0 space-y-5">
        <RunnerResourceOverview
          :runner="runner"
          :runtimes="confirmedRuntimes"
          @sample="freshMetrics = $event"
        />
        <div
          v-if="inventoryProblems || activityProblems"
          class="flex flex-wrap gap-2"
          aria-label="Runner attention"
        >
          <Button
            v-if="inventoryProblems"
            variant="outline"
            size="sm"
            class="border-warning/30 text-warning"
            @click="tab = 'inventory'"
            ><TriangleAlert />Inventory diagnostics · {{ inventoryProblems }}</Button
          >
          <Button
            v-if="activityProblems"
            variant="outline"
            size="sm"
            class="border-warning/30 text-warning"
            @click="tab = 'activities'"
            ><TriangleAlert />Activity needs review · {{ activityProblems }}</Button
          >
        </div>
        <RunnerStorageDonut
          v-if="runtimeFilter !== 'docker' && runtimeTypes.includes('qemu') && data"
          :runtimes="visibleRuntimes"
          :topology="visibleTopology"
          :selected-key="presentationSelectedKey"
          @select="selectNode"
        />
        <section class="min-w-0 space-y-3" aria-label="Runner dependencies">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h3 class="text-sm font-semibold">Dependencies</h3>
              <p class="mt-1 text-xs text-muted-foreground">
                {{ knownWorkspaces }} known workspaces · {{ stored.length }} images ·
                {{ resourceCount }} physical resources
              </p>
            </div>
            <div class="text-right text-[11px] text-muted-foreground">
              <p role="status" :class="inventoryUncertain ? 'text-warning' : ''">
                {{ inventoryState }}
              </p>
              <p v-if="lastInventory">Collected {{ formatRelativeTime(lastInventory) }}</p>
            </div>
          </div>
          <div
            v-if="loading && !data"
            class="grid grid-cols-2 gap-4"
            role="status"
            aria-label="Loading last confirmed inventory"
          >
            <Skeleton v-for="i in 2" :key="i" class="h-32 rounded-lg" />
          </div>
          <RunnerDependencyMap
            v-else
            :runtimes="visibleRuntimes"
            :generations="visibleGenerations"
            :topology="visibleTopology"
            :selected-key="presentationSelectedKey"
            :vm-metrics="freshMetrics?.vm_metrics"
            :metrics-timestamp="freshMetrics?.timestamp"
            @select="selectNode"
          />
          <RunnerNodeInspector
            v-if="selected"
            :node="selected"
            :generations="data?.generations ?? []"
            :busy="busy"
            @close="selectedKey = null"
            @rebuild="rebuild"
            @delete="requestDelete"
          />
        </section>
      </TabsContent>
      <TabsContent value="inventory" class="min-w-0 space-y-5">
        <div class="flex min-w-0 flex-wrap items-center gap-2">
          <div class="relative min-w-0 flex-1 basis-48">
            <Search
              class="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground"
            /><Input
              v-model="inventoryQuery"
              class="pl-9"
              aria-label="Search inventory"
              placeholder="Find an image or resource…"
            />
          </div>
          <Button
            v-if="deleted.length"
            variant="outline"
            size="sm"
            :aria-pressed="showDeleted"
            @click="showDeleted = !showDeleted"
            >{{ showDeleted ? 'Hide' : 'Show' }} deleted history ({{ deleted.length }})</Button
          >
        </div>
        <Collapsible class="rounded-lg border border-border px-3">
          <CollapsibleTrigger as-child
            ><Button
              variant="ghost"
              class="h-auto w-full justify-between rounded-none px-0 py-3 text-xs"
              ><span :class="inventoryUncertain ? 'text-warning' : 'text-muted-foreground'">{{
                inventoryState
              }}</span
              ><ChevronDown /></Button
          ></CollapsibleTrigger>
          <CollapsibleContent class="space-y-3 pb-3 text-xs text-muted-foreground">
            <p>
              Snapshot {{ data?.latest_snapshot_id ?? 'Unknown' }} · Lifecycle intent and observed
              state are independent. Partial or stale inventory cannot prove absence.
            </p>
            <div v-for="runtime in visibleRuntimes" :key="runtime.runtime_type" class="space-y-1">
              <p class="font-medium">
                {{ runtime.runtime_type.toUpperCase() }} ·
                {{ runtime.fresh ? 'Recent confirmed inventory' : 'Stale / unknown inventory' }}
              </p>
              <p class="break-words">
                Collected {{ runtime.collected_at || 'Unknown' }} · Received
                {{ runtime.received_at || 'Unknown' }}
              </p>
              <p>
                Latest scan
                {{ runtime.diagnostics?.complete ? 'complete' : 'partial / unavailable' }} · Foreign
                resources: {{ runtime.diagnostics?.foreign_resource_count ?? 'Unknown' }}
              </p>
              <p>
                Managed logical sizes:
                {{ runtime.resources.filter((r) => r.managed && r.logical_bytes != null).length }}
                measured ·
                {{ runtime.resources.filter((r) => r.managed && r.logical_bytes == null).length }}
                unknown
              </p>
              <p
                v-for="message in runtime.diagnostics?.errors"
                :key="message"
                role="alert"
                class="text-destructive"
              >
                {{ message }}
              </p>
            </div>
            <p>
              Image sizes are not filesystem usage. Docker layers overlap; logical / shared sizes
              are not summed.
            </p>
          </CollapsibleContent>
        </Collapsible>
        <section v-for="group in inventoryGroups" :key="group.name" class="space-y-2">
          <h3 class="flex items-center gap-2 text-xs font-semibold text-muted-foreground">
            <component :is="group.icon" :size="14" />{{ group.name }} · {{ group.images.length }}
          </h3>
          <div
            v-if="group.images.length"
            class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
          >
            <Button
              v-for="image in group.images"
              :key="image.id"
              variant="ghost"
              class="h-auto w-full min-w-0 justify-start gap-3 rounded-none px-4 py-3 text-left whitespace-normal"
              :aria-label="`Inspect image ${image.name}`"
              :aria-pressed="selected?.generation?.id === image.id"
              @click="selectImage(image)"
            >
              <component
                :is="image.origin_type === 'workspace_capture' ? Camera : Layers"
                class="size-4 shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 flex-1"
                ><span class="block truncate text-sm font-medium">{{ image.name }}</span
                ><span class="mt-1 block text-xs text-muted-foreground"
                  >{{ image.runtime_type?.toUpperCase() }} ·
                  {{
                    image.origin_type === 'workspace_capture'
                      ? image.owner_label || 'Capture'
                      : image.definition_name || 'Legacy definition'
                  }}<template v-if="image.origin_type !== 'workspace_capture'">
                    · {{ image.generation == null ? 'Legacy' : `g${image.generation}` }}</template
                  ></span
                ></span
              >
              <span class="hidden shrink-0 text-right text-xs text-muted-foreground sm:block"
                ><span class="block tabular-nums">{{ storageBytes(image.size_bytes) }}</span
                ><span>{{ image.dependencies.length }} dependents</span></span
              >
              <span class="flex shrink-0 flex-col items-end gap-1"
                ><Badge variant="secondary" class="text-[10px]">{{ generationLabel(image) }}</Badge
                ><Badge
                  variant="outline"
                  class="text-[10px]"
                  :class="runnerStateClass(image.status)"
                  >{{ runnerStateLabel(image.status) }}</Badge
                ></span
              >
            </Button>
          </div>
          <p v-else class="py-2 text-xs text-muted-foreground">No matching generations.</p>
        </section>
        <Collapsible :default-open="!!inventoryQuery">
          <CollapsibleTrigger as-child
            ><Button variant="outline" size="sm"
              ><HardDrive />Physical resources · {{ inventoryResources.length
              }}<ChevronDown /></Button
          ></CollapsibleTrigger>
          <CollapsibleContent class="pt-3">
            <div
              class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
            >
              <Button
                v-for="node in inventoryResources"
                :key="node.key"
                variant="ghost"
                class="h-auto w-full min-w-0 justify-start gap-3 rounded-none px-3 py-3 text-left whitespace-normal"
                :aria-label="`Inspect resource ${node.label}`"
                @click="selectNode(node)"
              >
                <component
                  :is="node.column === 0 ? Monitor : HardDrive"
                  class="size-4 shrink-0 text-muted-foreground"
                /><span class="min-w-0 flex-1"
                  ><span class="block truncate text-xs font-medium">{{ node.label }}</span
                  ><span class="block text-[11px] text-muted-foreground"
                    >{{ node.runtime }} · {{ node.resource?.kind }} ·
                    {{ node.resource?.managed ? 'Managed' : 'Unmanaged / unknown' }}</span
                  ></span
                ><span class="shrink-0 text-xs tabular-nums text-muted-foreground">{{
                  storageBytes(node.resource?.allocated_bytes)
                }}</span
                ><Badge variant="outline" class="text-[10px]">{{ node.resource?.state }}</Badge>
              </Button>
            </div>
          </CollapsibleContent>
        </Collapsible>
        <RunnerNodeInspector
          v-if="selected"
          :node="selected"
          :generations="data?.generations ?? []"
          :busy="busy"
          @close="selectedKey = null"
          @rebuild="rebuild"
          @delete="requestDelete"
        />
      </TabsContent>
      <TabsContent value="activities" class="min-w-0 space-y-4"
        ><RunnerActivityList
          v-if="data"
          :data="data"
          :deletions="deletions"
          :busy="busy"
          @changed="load"
          @cancel="cancel"
          @review="requestDelete"
        />
        <p v-else class="text-xs text-muted-foreground">
          Activity inventory unavailable.
        </p></TabsContent
      >
    </Tabs>
    <ImageDeletionDialog
      :target="target"
      :name="targetName"
      @close="target = null"
      @requested="load"
    />
  </div>
</template>
