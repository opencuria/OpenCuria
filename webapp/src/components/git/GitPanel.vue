<script setup lang="ts">
/**
 * GitPanel — Git tab content for the workspace side panel.
 *
 * Loads the productive git snapshot for the current workspace (with
 * polling), then shows a repo/branch header on top with the Changes and
 * Graph sections stacked vertically (default 50/50) and a drag handle
 * between them. Each section scrolls vertically on its own; nothing
 * scrolls horizontally.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useGitStore } from '@/stores/git'
import { useSidePanelStore } from '@/stores/sidePanel'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { ArrowDown, ArrowUp, GitBranch, RefreshCw } from '@lucide/vue'
import GitChangesSection from './GitChangesSection.vue'
import GitGraphSection from './GitGraphSection.vue'

const props = defineProps<{
  workspaceId: string
}>()

const store = useGitStore()
const sidePanel = useSidePanelStore()
const fetchBusy = ref(false)
const gitVisible = computed(
  () => sidePanel.activeTab === 'git' && sidePanel.isOpen,
)

/** Throttle for tab-visibility auto-fetches (ms). */
const AUTO_FETCH_THROTTLE_MS = 10_000
let lastAutoFetchAt: number | null = null
let lastAutoFetchKey: string | null = null
let activation: { workspaceId: string; generation: number; promise: Promise<void> } | null = null
let lifecycleGeneration = 0
let mounted = false

function shouldAutoFetch(): boolean {
  const repo = store.currentRepo
  if (!gitVisible.value || !repo) return false
  if (store.busyOperation !== null || fetchBusy.value) return false
  const key = `${props.workspaceId}:${repo.path}`
  return lastAutoFetchKey !== key || lastAutoFetchAt === null ||
    Date.now() - lastAutoFetchAt >= AUTO_FETCH_THROTTLE_MS
}

function autoFetchSilent(): void {
  if (!shouldAutoFetch()) return
  const repo = store.currentRepo
  if (!repo) return
  lastAutoFetchAt = Date.now()
  lastAutoFetchKey = `${props.workspaceId}:${repo.path}`
  void store.fetchRemote({ silent: true })
}

/** Initialize or refresh once on activation; timers only run while visible. */
function activateGitTab(): Promise<void> {
  if (!mounted || !gitVisible.value) return Promise.resolve()
  const workspaceId = props.workspaceId
  const generation = lifecycleGeneration
  if (activation?.workspaceId === workspaceId && activation.generation === generation) {
    return activation.promise
  }

  const promise = (async () => {
    // Load summaries first, so a hide during that request cannot trigger a
    // chained details request. Details are requested only after rechecking
    // the activation token and visibility.
    if (store.workspaceId !== workspaceId) {
      await store.initialize(workspaceId, { withDetails: false })
    } else {
      await store.refresh({ silent: true, withDetails: false })
    }
    if (
      !mounted || generation !== lifecycleGeneration ||
      !gitVisible.value || props.workspaceId !== workspaceId
    ) return

    const repo = store.currentRepo
    if (repo && !store.repoDetails[repo.path]) {
      await store.ensureDetails(repo.path, { silent: true })
    }
    if (
      !mounted || generation !== lifecycleGeneration ||
      !gitVisible.value || props.workspaceId !== workspaceId
    ) return
    autoFetchSilent()
    store.startPolling()
  })()
  activation = { workspaceId, generation, promise }
  void promise.finally(() => {
    if (activation?.promise === promise) activation = null
  })
  return promise
}

watch(
  () => [props.workspaceId, gitVisible.value] as const,
  ([workspaceId, visible], [previousWorkspaceId, wasVisible]) => {
    if (!visible) {
      lifecycleGeneration += 1
      activation = null
      store.stopPolling()
      return
    }
    if (!wasVisible || workspaceId !== previousWorkspaceId) {
      lifecycleGeneration += 1
      activation = null
      if (workspaceId !== previousWorkspaceId) store.stopPolling()
      void activateGitTab()
    }
  },
)

onMounted(() => {
  mounted = true
  if (gitVisible.value) void activateGitTab()
})

onUnmounted(() => {
  mounted = false
  lifecycleGeneration += 1
  activation = null
  store.stopPolling()
})

async function handleRetry(): Promise<void> {
  if (!gitVisible.value) return
  await store.refresh()
}

async function handleRefreshAll(): Promise<void> {
  if (!gitVisible.value || fetchBusy.value || store.loading || store.busyOperation !== null) return
  fetchBusy.value = true
  try {
    // Fetch first (explicit: errors toast), then reload summaries+details.
    if (store.currentRepo) {
      lastAutoFetchAt = Date.now()
      lastAutoFetchKey = `${props.workspaceId}:${store.currentRepo.path}`
      await store.fetchRemote({ silent: false })
    }
    if (gitVisible.value) await store.refresh()
  } finally {
    fetchBusy.value = false
  }
}

const branchLabel = computed(
  () => store.currentRepo?.currentBranch ?? 'DETACHED HEAD',
)
const ahead = computed(() => store.currentBranch?.ahead ?? 0)
const behind = computed(() => store.currentBranch?.behind ?? 0)
/** True while the selected repo's full details are still loading. */
const detailsPending = computed(() => {
  const path = store.currentRepo?.path
  if (!path) return false
  return store.detailsLoading[path] === true && !(path in store.repoDetails)
})

function repoLabel(repo: { name: string; currentBranch: string | null }): string {
  return repo.currentBranch ? `${repo.name} (${repo.currentBranch})` : repo.name
}

// --- Vertical split between Changes and Graph ---

const MIN_SECTION_PERCENT = 20
const MAX_SECTION_PERCENT = 80

const containerRef = ref<HTMLElement | null>(null)
const splitPercent = ref(50)
const isResizing = ref(false)

function clampSplit(percent: number): number {
  return Math.min(MAX_SECTION_PERCENT, Math.max(MIN_SECTION_PERCENT, percent))
}

function updateSplit(clientY: number): void {
  const container = containerRef.value
  if (!container) return
  const rect = container.getBoundingClientRect()
  if (rect.height <= 0) return
  splitPercent.value = clampSplit(((clientY - rect.top) / rect.height) * 100)
}

function onSplitPointerDown(event: PointerEvent): void {
  event.preventDefault()
  isResizing.value = true
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture?.(event.pointerId)
  updateSplit(event.clientY)
}

function onSplitPointerMove(event: PointerEvent): void {
  if (!isResizing.value) return
  updateSplit(event.clientY)
}

function onSplitPointerUp(event: PointerEvent): void {
  if (!isResizing.value) return
  isResizing.value = false
  const handle = event.currentTarget as HTMLElement
  if (handle.hasPointerCapture?.(event.pointerId)) {
    handle.releasePointerCapture(event.pointerId)
  }
}
</script>

<template>
  <div class="flex h-full flex-col bg-card" data-testid="side-panel-git">
    <!-- Loading / error / empty states -->
    <div v-if="store.loading && store.repos.length === 0" class="flex flex-1 items-center justify-center" data-testid="git-loading">
      <RefreshCw :size="16" class="animate-spin text-muted-foreground" />
    </div>
    <div v-else-if="store.error && store.repos.length === 0" class="flex flex-1 flex-col items-center justify-center gap-2 px-4 text-center" data-testid="git-error">
      <p class="text-xs text-error">{{ store.error }}</p>
      <Button variant="outline" size="sm" class="h-7 text-xs" data-testid="git-retry" @click="void handleRetry()">
        Retry
      </Button>
    </div>
    <div v-else-if="store.repos.length === 0" class="flex flex-1 flex-col items-center justify-center gap-2 px-4 text-center" data-testid="git-empty">
      <p class="text-xs text-muted-foreground">No git repositories found in this workspace.</p>
      <Button variant="outline" size="sm" class="h-7 text-xs" data-testid="git-retry" @click="void handleRetry()">
        Refresh
      </Button>
    </div>
    <template v-else>
    <!-- Repo + branch header -->
    <div class="shrink-0 space-y-1.5 border-b border-border px-3 py-2">
      <div class="flex items-center gap-1.5">
        <Select
          :model-value="store.selectedRepoId"
          @update:model-value="(v) => store.selectRepo(String(v))"
        >
          <SelectTrigger
            class="h-7 min-w-0 flex-1 text-xs"
            data-testid="git-repo-select"
          >
            <SelectValue placeholder="Select repository" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem
              v-for="repo in store.repos"
              :key="repo.id"
              :value="repo.id"
              :data-testid="`git-repo-option-${repo.id}`"
            >
              {{ repoLabel(repo) }}
            </SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-7 w-7 shrink-0"
          title="Fetch + Refresh"
          data-testid="git-refresh"
          :disabled="fetchBusy || store.loading || store.busyOperation !== null"
          @click="void handleRefreshAll()"
        >
          <RefreshCw :size="13" :class="{ 'animate-spin': fetchBusy || store.loading || store.busyOperation === 'fetch' }" />
        </Button>
      </div>
      <div class="flex items-center gap-1.5 text-xs">
        <GitBranch :size="12" class="shrink-0 text-muted-foreground" />
        <span
          class="truncate font-medium text-foreground"
          data-testid="git-current-branch"
        >
          {{ branchLabel }}
        </span>
        <span
          v-if="ahead > 0"
          class="flex shrink-0 items-center text-muted-foreground"
          title="Commits ahead of origin"
          data-testid="git-ahead"
        >
          <ArrowUp :size="11" />{{ ahead }}
        </span>
        <span
          v-if="behind > 0"
          class="flex shrink-0 items-center text-muted-foreground"
          title="Commits behind origin"
          data-testid="git-behind"
        >
          <ArrowDown :size="11" />{{ behind }}
        </span>
        <span class="ml-auto truncate pl-2 text-muted-foreground">
          {{ store.currentRepo?.path }}
        </span>
      </div>
    </div>

    <!-- Stacked Changes / Graph sections with a vertical drag handle -->
    <div
      ref="containerRef"
      class="flex min-h-0 flex-1 flex-col"
      :class="{ 'select-none': isResizing }"
    >
      <div
        class="min-h-0 shrink-0 overflow-hidden"
        :style="{ height: `${splitPercent}%` }"
        data-testid="git-changes-container"
      >
        <div
          v-if="detailsPending"
          class="flex h-full items-center justify-center"
          data-testid="git-details-loading"
        >
          <RefreshCw :size="16" class="animate-spin text-muted-foreground" />
        </div>
        <GitChangesSection v-else />
      </div>
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize changes and graph sections"
        title="Drag to resize"
        class="group relative h-2 shrink-0 cursor-row-resize touch-none"
        data-testid="git-split-handle"
        @pointerdown="onSplitPointerDown"
        @pointermove="onSplitPointerMove"
        @pointerup="onSplitPointerUp"
        @pointercancel="onSplitPointerUp"
      >
        <div
          class="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border transition-colors group-hover:bg-primary"
          :class="{ 'bg-primary': isResizing }"
        />
      </div>
      <div class="min-h-0 flex-1 overflow-hidden" data-testid="git-graph-container">
        <div
          v-if="detailsPending"
          class="flex h-full items-center justify-center"
          data-testid="git-details-loading-graph"
        >
          <RefreshCw :size="16" class="animate-spin text-muted-foreground" />
        </div>
        <GitGraphSection v-else />
      </div>
    </div>
    </template>
  </div>
</template>
