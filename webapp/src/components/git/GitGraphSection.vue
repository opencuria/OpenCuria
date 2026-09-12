<script setup lang="ts">
/**
 * Git graph section — async commit expansion now lazy-loads details.
 *
 * Header offers a branch filter and a create-branch action (disabled for
 * unborn repos without a base commit). Local branch tags open a dropdown
 * with checkout / rename / delete / merge actions; remote refs open a
 * dropdown with checkout / copy actions. Rows and graph nodes are
 * window-virtualized (overscan ~20); an IntersectionObserver sentinel
 * paginates history when `hasMore` (the "Load more" button stays as
 * fallback for environments without IntersectionObserver).
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import type { GitCommit, GitRefTag } from '@/types/git'
import {
  GIT_GRAPH_COLORS,
  GIT_GRAPH_GRID_Y,
  GIT_GRAPH_NODE_R,
  branchPaths,
  computeGitGraphLayout,
  filterReachableCommits,
  vertexPixel,
} from '@/lib/gitGraph'
import { useGitStore } from '@/stores/git'
import { copyToClipboard } from '@/lib/clipboard'
import { formatRelativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Check,
  ChevronDown,
  Copy,
  GitBranch,
  GitBranchPlus,
  GitCommitHorizontal,
  GitMerge,
  Loader2,
  Pencil,
  Trash2,
} from '@lucide/vue'
import GitBranchDialog from './GitBranchDialog.vue'
import GitCommitDetailsView from './GitCommitDetailsView.vue'
import GitDeleteBranchDialog from './GitDeleteBranchDialog.vue'
import GitMergeDialog from './GitMergeDialog.vue'

const store = useGitStore()

/** Fixed table header height in px; the graph SVG starts below it. */
const HEADER_HEIGHT = 29
const DATE_COL_WIDTH = 72
const NUM_COLUMNS = 3

function branchColor(colour: number): string {
  return GIT_GRAPH_COLORS[colour % GIT_GRAPH_COLORS.length] ?? 'var(--muted-foreground)'
}

/** Branch filter: null shows all branches. */
const filterBranch = ref<string | null>(null)

const activeFilter = computed(() => {
  const branches = store.currentRepo?.branches ?? []
  return branches.some((b) => b.name === filterBranch.value)
    ? filterBranch.value
    : null
})

const repo = computed(() => store.currentRepo)

/** Commits in display order (honours the branch filter). */
const commits = computed(() => {
  const current = repo.value
  if (!current) return []
  if (activeFilter.value) {
    const branch = current.branches.find((b) => b.name === activeFilter.value)
    if (branch) {
      return filterReachableCommits(current.commits, branch.tipHash)
    }
  }
  return current.commits
})

const headHash = computed(() => repo.value?.headHash ?? null)
const expandY = computed(() => store.cdvHeight)
const expandedIndex = computed(() => {
  if (!store.expandedCommitHash) return -1
  return commits.value.findIndex((c) => c.hash === store.expandedCommitHash)
})

const layout = computed(() =>
  computeGitGraphLayout(commits.value, {
    headHash: headHash.value,
    expandedIndex: expandedIndex.value,
    expandY: expandY.value,
  }),
)

const renderedBranches = computed(() =>
  layout.value.branches.map((branch) => ({
    colour: branch.colour,
    paths: branchPaths(branch, expandedIndex.value, expandY.value),
  })),
)

const renderedNodes = computed(() =>
  layout.value.vertices.map((vertex) => {
    const pixel = vertexPixel(vertex, expandedIndex.value, expandY.value)
    return { ...vertex, cx: pixel.cx, cy: pixel.cy }
  }),
)

const vertexColours = computed(() => layout.value.vertexColours)

const graphColWidth = computed(() => layout.value.contentWidth)
const graphSvgWidth = computed(() => layout.value.contentWidth)
const graphSvgHeight = computed(() => layout.value.height)

const isDetached = computed(() => repo.value?.currentBranch === null)
/** No commits yet (unborn repo): there is no base commit to branch from. */
const isUnborn = computed(
  () => (repo.value?.commits.length ?? 0) === 0 || repo.value?.headHash === null,
)
const isBusy = computed(() => store.busyOperation !== null)
const hoveredHash = ref<string | null>(null)

function isHead(hash: string): boolean {
  return repo.value?.headHash === hash
}

function isExpanded(hash: string): boolean {
  return store.expandedCommitHash === hash
}

function rowStyle(index: number): CSSProperties {
  return {
    '--git-graph-color': branchColor(vertexColours.value[index] ?? 0),
  } as CSSProperties
}

function onRowClick(hash: string, event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  if (target?.closest('button, a, input')) return
  void store.toggleCommitDetails(hash)
}

function onRowKeydown(hash: string, event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  void store.toggleCommitDetails(hash)
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && store.expandedCommitHash) {
    store.closeCommitDetails()
  }
}

/** Ref tags pointing at a commit, plus a HEAD marker when detached. */
function tagsFor(hash: string): GitRefTag[] {
  const tags = store.tagsByHash.get(hash) ?? []
  if (isDetached.value && isHead(hash)) {
    return [{ name: 'HEAD', remote: false, current: true }, ...tags]
  }
  return tags
}

function tagStyle(tag: GitRefTag, colour: number): CSSProperties {
  if (tag.remote) {
    return { borderColor: 'var(--border)', color: 'var(--muted-foreground)' }
  }
  const color = branchColor(colour)
  if (tag.current) {
    return {
      backgroundColor: color,
      borderColor: color,
      color: 'var(--primary-foreground)',
    }
  }
  return {
    borderColor: color,
    color,
    backgroundColor: `color-mix(in oklch, ${color} 12%, transparent)`,
  }
}

// --- Actions & dialogs ---

const branchDialogOpen = ref(false)
const branchDialogMode = ref<'create' | 'rename'>('create')
const branchDialogBranch = ref('')
const branchDialogFromHash = ref('')

function openCreateBranch(fromHash: string): void {
  branchDialogMode.value = 'create'
  branchDialogFromHash.value = fromHash
  branchDialogBranch.value = ''
  branchDialogOpen.value = true
}

function openRenameBranch(name: string): void {
  branchDialogMode.value = 'rename'
  branchDialogBranch.value = name
  branchDialogOpen.value = true
}

const mergeDialogOpen = ref(false)
const mergeDirection = ref<'into-current' | 'current-into'>('into-current')
const mergeBranch = ref('')

function openMerge(direction: 'into-current' | 'current-into', branch: string): void {
  mergeDirection.value = direction
  mergeBranch.value = branch
  mergeDialogOpen.value = true
}

const deleteDialogOpen = ref(false)
const deleteBranchName = ref('')

function openDeleteBranch(name: string): void {
  // The current branch can never be deleted (backend would reject).
  if (name === store.currentRepo?.currentBranch) return
  deleteBranchName.value = name
  deleteDialogOpen.value = true
}

function copyHash(hash: string): void {
  void copyToClipboard(hash, 'commit hash')
}

async function loadMore(): Promise<void> {
  if (store.historyLoading || store.busyOperation !== null) return
  await store.loadMoreHistory(activeFilter.value ?? undefined)
}

// --- Window virtualization ----------------------------------------------------
// The layout is always computed for ALL commits via computeGitGraphLayout
// (pure TS, cheap) so branch lines stay continuous across the whole graph.
// Only the visible window (+ overscan) is rendered into the DOM: table rows,
// graph nodes and branch path segments. Spacer <tr>s preserve the table
// height; the SVG overlay keeps full height and only renders visible items.

const OVERSCAN_ROWS = 20
/** Assumed viewport height when no layout info exists (e.g. jsdom tests). */
const DEFAULT_VIEWPORT_HEIGHT = 480

/** Scroll metrics of the ScrollArea viewport (fallback: window scroll). */
const scrollTop = ref(0)
const viewportHeight = ref(0)
const scrollHost = ref<HTMLElement | null>(null)
let scrollCleanup: (() => void) | null = null

function readScrollMetrics(): void {
  const host = scrollHost.value
  if (host) {
    scrollTop.value = host.scrollTop
    viewportHeight.value = host.clientHeight
  } else {
    scrollTop.value = window.scrollY
    viewportHeight.value = window.innerHeight
  }
}

function onScroll(): void {
  readScrollMetrics()
}

function findScrollViewport(root: HTMLElement | null): HTMLElement | null {
  if (!root) return null
  if (root.dataset.slot === 'scroll-area-viewport') return root
  return root.querySelector('[data-slot="scroll-area-viewport"]')
}

function attachScrollTracking(): void {
  detachScrollTracking()
  readScrollMetrics()
  const root = sectionRoot.value
  const viewport = findScrollViewport(root)
  if (viewport) {
    scrollHost.value = viewport
    viewport.addEventListener('scroll', onScroll, { passive: true })
    const canObserve = typeof ResizeObserver !== 'undefined'
    const observer = canObserve ? new ResizeObserver(() => readScrollMetrics()) : null
    observer?.observe(viewport)
    readScrollMetrics()
    scrollCleanup = () => {
      viewport.removeEventListener('scroll', onScroll)
      observer?.disconnect()
      scrollHost.value = null
    }
    return
  }
  window.addEventListener('scroll', onScroll, { passive: true })
  scrollCleanup = () => window.removeEventListener('scroll', onScroll)
}

function detachScrollTracking(): void {
  scrollCleanup?.()
  scrollCleanup = null
}

function rowHeightAt(index: number): number {
  return index === expandedIndex.value && expandedIndex.value > -1
    ? GIT_GRAPH_GRID_Y + expandY.value
    : GIT_GRAPH_GRID_Y
}

/** Cumulative offset (px) of the top of row `index`. */
function rowOffsetTop(index: number): number {
  const expanded = expandedIndex.value
  const extra = expanded > -1 && index > expanded ? expandY.value : 0
  return index * GIT_GRAPH_GRID_Y + extra
}

/** Inclusive visible window [start, end] over commit indices. */
const visibleRange = computed(() => {
  const total = commits.value.length
  if (total === 0) return { start: 0, end: -1 }
  // jsdom reports clientHeight 0 (no layout) — assume a typical viewport so
  // long histories still virtualize instead of rendering everything.
  const height = viewportHeight.value > 0 ? viewportHeight.value : DEFAULT_VIEWPORT_HEIGHT
  const top = Math.max(0, scrollTop.value - HEADER_HEIGHT)
  const bottom = top + height
  // Binary search: first index whose row bottom exceeds `top`.
  let lo = 0
  let hi = total - 1
  let first = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (rowOffsetTop(mid) + rowHeightAt(mid) > top) {
      first = mid
      hi = mid - 1
    } else {
      lo = mid + 1
    }
  }
  let last = first
  while (last + 1 < total && rowOffsetTop(last + 1) < bottom) last++
  return {
    start: Math.max(0, first - OVERSCAN_ROWS),
    end: Math.min(total - 1, last + OVERSCAN_ROWS),
  }
})

const topSpacerHeight = computed(() => rowOffsetTop(visibleRange.value.start))
const bottomSpacerHeight = computed(() => {
  const total = commits.value.length
  const { end } = visibleRange.value
  if (total === 0 || end < 0) return 0
  const totalHeight = rowOffsetTop(total - 1) + rowHeightAt(total - 1)
  return Math.max(0, totalHeight - (rowOffsetTop(end) + rowHeightAt(end)))
})

interface VisibleRow {
  commit: GitCommit
  index: number
  colour: number
}

const visibleRows = computed<VisibleRow[]>(() => {
  const { start, end } = visibleRange.value
  if (end < start) return []
  const rows: VisibleRow[] = []
  for (let i = start; i <= end; i++) {
    const commit = commits.value[i]
    if (!commit) continue
    rows.push({ commit, index: i, colour: vertexColours.value[i] ?? 0 })
  }
  return rows
})

// --- SVG overlay: DOM only renders the visible window -------------------------
// Branch path segments are filtered by the row span they cover (parsed from
// the path's y-extent, incl. expandY shift); nodes are filtered by hash set
// (layout order may differ from display order after child-before-parent
// reorder, so index-based filtering would be wrong).

const visibleNodeHashes = computed(() => {
  const { start, end } = visibleRange.value
  const set = new Set<string>()
  for (let i = start; i <= end; i++) {
    const hash = commits.value[i]?.hash
    if (hash) set.add(hash)
  }
  return set
})

interface VisibleBranchPath {
  key: string
  d: string
  isCommitted: boolean
  colour: number
}

/** Parse an SVG path's y-extent (min/max) to decide window visibility. */
function pathYRange(d: string): { min: number; max: number } | null {
  const matches = d.match(/-?\d+(?:\.\d+)?/g)
  if (!matches || matches.length < 4) return null
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  // Path commands alternate x,y pairs.
  for (let i = 1; i < matches.length; i += 2) {
    const y = Number(matches[i])
    if (Number.isNaN(y)) continue
    if (y < min) min = y
    if (y > max) max = y
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null
  return { min, max }
}

const visibleBranches = computed<VisibleBranchPath[]>(() => {
  const { start, end } = visibleRange.value
  if (end < start) return []
  const top = rowOffsetTop(start) - OVERSCAN_ROWS * GIT_GRAPH_GRID_Y
  const bottom = rowOffsetTop(end) + rowHeightAt(end) + OVERSCAN_ROWS * GIT_GRAPH_GRID_Y
  const out: VisibleBranchPath[] = []
  renderedBranches.value.forEach((branch, branchIndex) => {
    branch.paths.forEach((path, pathIndex) => {
      const range = pathYRange(path.d)
      // No parseable extent → keep (safe fallback, never drop lines).
      if (!range || (range.max >= top && range.min <= bottom)) {
        out.push({
          key: `branch-${branchIndex}-path-${pathIndex}`,
          d: path.d,
          isCommitted: path.isCommitted,
          colour: path.colour,
        })
      }
    })
  })
  return out
})

const visibleNodes = computed(() =>
  renderedNodes.value.filter((node) => visibleNodeHashes.value.has(node.hash)),
)

// --- Infinite scroll -----------------------------------------------------------

const sectionRoot = ref<HTMLElement | null>(null)
const sentinel = ref<HTMLElement | null>(null)
let sentinelObserver: IntersectionObserver | null = null
/** In-flight guard: store.historyLoading flips async, so guard locally too. */
let loadMoreInFlight = false

async function maybeLoadMore(): Promise<void> {
  const repo = store.currentRepo
  if (!repo?.hasMore || store.historyLoading || store.busyOperation !== null) return
  if (loadMoreInFlight) return
  loadMoreInFlight = true
  try {
    await store.loadMoreHistory(activeFilter.value ?? undefined)
  } finally {
    loadMoreInFlight = false
  }
}

function setupSentinelObserver(): void {
  teardownSentinelObserver()
  const target = sentinel.value
  if (!target || typeof IntersectionObserver === 'undefined') return
  const root = scrollHost.value ?? findScrollViewport(sectionRoot.value)
  sentinelObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) void maybeLoadMore()
      }
    },
    { root: root ?? null, rootMargin: '200px' },
  )
  sentinelObserver.observe(target)
}

function teardownSentinelObserver(): void {
  sentinelObserver?.disconnect()
  sentinelObserver = null
}

onMounted(() => {
  window.addEventListener('keydown', onEscape)
  attachScrollTracking()
  setupSentinelObserver()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onEscape)
  detachScrollTracking()
  teardownSentinelObserver()
})

// Re-attach tracking + observer once the ScrollArea viewport exists, and
// re-observe the sentinel whenever it (re-)renders (e.g. toggled by hasMore).
watch(
  [sectionRoot, sentinel, () => store.currentRepo?.hasMore],
  () => {
    attachScrollTracking()
    setupSentinelObserver()
  },
  { flush: 'post' },
)

// Window size changes don't fire viewport scroll events — poll metrics.
watch(
  () => commits.value.length,
  () => readScrollMetrics(),
)

// --- Remote-branch checkout -----------------------------------------------------

function copyRefName(name: string): void {
  void copyToClipboard(name, 'branch name')
}
</script>

<template>
  <div ref="sectionRoot" class="flex h-full flex-col" data-testid="git-graph-section">
    <div class="flex shrink-0 items-center gap-1 border-b border-border px-2 py-1">
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            variant="ghost"
            size="sm"
            class="h-6 min-w-0 gap-1.5 px-1.5 text-xs"
            data-testid="git-graph-filter"
          >
            <GitBranch :size="12" class="shrink-0" />
            <span class="truncate">{{ activeFilter ?? 'All branches' }}</span>
            <ChevronDown :size="11" class="shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem
            data-testid="git-graph-filter-all"
            @click="filterBranch = null"
          >
            <Check
              :size="13"
              :class="{ invisible: activeFilter !== null }"
            />
            All branches
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            v-for="branch in store.currentRepo?.branches ?? []"
            :key="branch.name"
            :data-testid="`git-graph-filter-branch-${branch.name}`"
            @click="filterBranch = branch.name"
          >
            <Check
              :size="13"
              :class="{ invisible: activeFilter !== branch.name }"
            />
            {{ branch.name }}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <span class="flex-1" />
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger as-child>
            <span class="inline-flex">
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-6 w-6"
                :disabled="isUnborn"
                title="Create branch"
                data-testid="git-create-branch"
                @click="openCreateBranch(store.currentRepo?.headHash ?? '')"
              >
                <GitBranchPlus :size="13" />
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent v-if="isUnborn" side="bottom">
            No commits yet — no base commit to branch from
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </div>

    <ScrollArea class="min-h-0 flex-1">
      <div v-if="commits.length > 0" class="relative">
        <svg
          :width="graphSvgWidth"
          :height="graphSvgHeight"
          class="pointer-events-none absolute left-0 top-0 z-[2]"
          :style="{ top: `${HEADER_HEIGHT}px` }"
          aria-hidden="true"
          data-testid="git-graph-svg"
        >
          <template v-for="path in visibleBranches" :key="path.key">
            <path
              :d="path.d"
              stroke="var(--card)"
              stroke-opacity="0.75"
              stroke-width="4"
              fill="none"
            />
            <path
              :d="path.d"
              :stroke="path.isCommitted ? branchColor(path.colour) : 'var(--muted-foreground)'"
              stroke-width="2"
              fill="none"
            />
          </template>
          <circle
            v-for="node in visibleNodes"
            :key="`node-${node.hash}`"
            :cx="node.cx"
            :cy="node.cy"
            :r="hoveredHash === node.hash ? GIT_GRAPH_NODE_R + 1 : GIT_GRAPH_NODE_R"
            :fill="node.isCurrent ? 'var(--card)' : branchColor(node.colour)"
            :stroke="node.isCurrent ? branchColor(node.colour) : 'var(--card)'"
            :stroke-opacity="node.isCurrent ? undefined : 0.75"
            :stroke-width="node.isCurrent ? 2 : 1"
            class="pointer-events-auto cursor-pointer"
            :data-testid="`git-graph-node-${node.hash}`"
            @mouseenter="hoveredHash = node.hash"
            @mouseleave="hoveredHash = null"
            @click.stop="void store.toggleCommitDetails(node.hash)"
          />
        </svg>

        <table class="w-full table-fixed border-collapse">
          <colgroup>
            <col :style="{ width: `${graphColWidth}px` }" />
            <col />
            <col :style="{ width: `${DATE_COL_WIDTH}px` }" />
          </colgroup>
          <thead class="sticky top-0 z-[3] bg-card">
            <tr :style="{ height: `${HEADER_HEIGHT - 1}px` }">
              <th
                class="border-b border-border px-3 text-left text-xs font-medium text-muted-foreground"
                data-testid="git-graph-header-graph"
              >
                Graph
              </th>
              <th
                class="border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-testid="git-graph-header-description"
              >
                Description
              </th>
              <th
                class="border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-testid="git-graph-header-date"
              >
                Date
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-if="topSpacerHeight > 0"
              data-testid="git-graph-spacer-top"
              aria-hidden="true"
            >
              <td :colspan="NUM_COLUMNS" :style="{ height: `${topSpacerHeight}px`, padding: 0 }" />
            </tr>
            <template v-for="{ commit, index, colour } in visibleRows" :key="commit.hash">
              <ContextMenu>
                <ContextMenuTrigger as-child>
                  <tr
                    class="cursor-pointer hover:bg-muted"
                    :class="{ 'bg-muted': isExpanded(commit.hash) }"
                    :style="[{ height: `${GIT_GRAPH_GRID_Y}px` }, rowStyle(index)]"
                    :data-color="vertexColours[index]"
                    :data-testid="'git-graph-row'"
                    :data-hash="commit.hash"
                    tabindex="0"
                    @click="onRowClick(commit.hash, $event)"
                    @keydown="onRowKeydown(commit.hash, $event)"
                  >
                    <td />
                    <td class="max-w-0 px-1">
                      <span class="flex min-w-0 items-center gap-1 leading-6">
                        <template v-for="tag in tagsFor(commit.hash)" :key="tag.name">
                          <span
                            v-if="tag.name === 'HEAD'"
                            class="inline-flex h-[18px] shrink-0 items-center gap-0.5 overflow-hidden rounded-md border px-1 text-[11px] leading-[16px]"
                            :style="tagStyle(tag, colour)"
                            :data-testid="`git-ref-tag-${tag.name}`"
                          >
                            <GitBranch :size="10" class="shrink-0" />
                            <span class="truncate">{{ tag.name }}</span>
                          </span>
                          <DropdownMenu v-else-if="tag.remote">
                            <DropdownMenuTrigger as-child>
                              <button
                                type="button"
                                class="inline-flex h-[18px] shrink-0 cursor-pointer items-center gap-0.5 overflow-hidden rounded-md border px-1 text-[11px] leading-[16px]"
                                :style="tagStyle(tag, colour)"
                                :data-testid="`git-ref-tag-${tag.name}`"
                                @click.stop
                              >
                                <GitBranch :size="10" class="shrink-0" />
                                <span class="truncate">{{ tag.name }}</span>
                              </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="start">
                              <DropdownMenuItem
                                :disabled="isBusy"
                                :data-testid="`git-remote-checkout-${tag.name}`"
                                @click="void store.checkoutRemoteBranch(tag.name)"
                              >
                                <Check :size="13" />
                                Checkout
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                :data-testid="`git-remote-copy-${tag.name}`"
                                @click="copyRefName(tag.name)"
                              >
                                <Copy :size="13" />
                                Copy name
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                          <DropdownMenu v-else>
                            <DropdownMenuTrigger as-child>
                              <button
                                type="button"
                                class="inline-flex h-[18px] shrink-0 cursor-pointer items-center gap-0.5 overflow-hidden rounded-md border px-1 text-[11px] leading-[16px]"
                                :class="{ 'font-medium': tag.current }"
                                :style="tagStyle(tag, colour)"
                                :data-testid="`git-branch-tag-${tag.name}`"
                                @click.stop
                              >
                                <GitBranch :size="10" class="shrink-0" />
                                <span class="truncate">{{ tag.name }}</span>
                              </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="start">
                              <DropdownMenuItem
                                :disabled="tag.current"
                                :data-testid="`git-tag-checkout-${tag.name}`"
                                @click="void store.checkoutBranch(tag.name)"
                              >
                                <Check :size="13" />
                                Checkout
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                :data-testid="`git-tag-rename-${tag.name}`"
                                @click="openRenameBranch(tag.name)"
                              >
                                <Pencil :size="13" />
                                Rename Branch
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                :disabled="tag.current || isBusy"
                                :data-testid="`git-tag-delete-${tag.name}`"
                                @click="openDeleteBranch(tag.name)"
                              >
                                <Trash2 :size="13" />
                                Delete Branch
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                :disabled="tag.current || isDetached"
                                :data-testid="`git-tag-merge-into-current-${tag.name}`"
                                @click="openMerge('into-current', tag.name)"
                              >
                                <GitMerge :size="13" />
                                Merge into Current Branch
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                :disabled="tag.current || isDetached"
                                :data-testid="`git-tag-merge-current-into-${tag.name}`"
                                @click="openMerge('current-into', tag.name)"
                              >
                                <GitMerge :size="13" />
                                Merge Current into '{{ tag.name }}'
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </template>
                        <span
                          class="block min-w-0 flex-1 truncate text-xs text-foreground"
                          :class="{ 'font-medium': isHead(commit.hash) }"
                          :title="commit.message"
                        >
                          {{ commit.message }}
                        </span>
                      </span>
                    </td>
                    <td
                      class="max-w-0 whitespace-nowrap px-1 text-xs text-muted-foreground"
                      :title="commit.timestamp"
                    >
                      <span class="block truncate leading-6">
                        {{ formatRelativeTime(commit.timestamp) }}
                      </span>
                    </td>
                  </tr>
                </ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuItem
                    :data-testid="`git-commit-checkout-${commit.hash}`"
                    @click="void store.checkoutCommit(commit.hash)"
                  >
                    <GitCommitHorizontal :size="13" />
                    Checkout Commit
                  </ContextMenuItem>
                  <ContextMenuItem
                    :disabled="isUnborn"
                    :data-testid="`git-commit-create-branch-${commit.hash}`"
                    @click="openCreateBranch(commit.hash)"
                  >
                    <GitBranchPlus :size="13" />
                    Create Branch Here
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem
                    :data-testid="`git-commit-copy-${commit.hash}`"
                    @click="void copyHash(commit.hash)"
                  >
                    <Copy :size="13" />
                    Copy Commit Hash
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
              <tr
                v-if="isExpanded(commit.hash)"
                :key="`${commit.hash}-details`"
                data-testid="git-commit-details-row"
              >
                <td
                  :colspan="NUM_COLUMNS"
                  class="border-y border-border bg-muted/30 p-0"
                  :style="{ paddingLeft: `${graphColWidth}px` }"
                >
                  <GitCommitDetailsView :hash="commit.hash" />
                </td>
              </tr>
            </template>
            <tr
              v-if="bottomSpacerHeight > 0"
              data-testid="git-graph-spacer-bottom"
              aria-hidden="true"
            >
              <td :colspan="NUM_COLUMNS" :style="{ height: `${bottomSpacerHeight}px`, padding: 0 }" />
            </tr>
          </tbody>
        </table>
        <div
          v-if="store.currentRepo?.hasMore"
          class="flex justify-center px-2 py-2"
        >
          <div ref="sentinel" data-testid="git-history-sentinel" class="h-px w-full" aria-hidden="true" />
          <Button
            variant="outline"
            size="sm"
            class="h-7 text-xs"
            :disabled="store.historyLoading || isBusy"
            data-testid="git-load-more"
            @click="void loadMore()"
          >
            <Loader2 v-if="store.historyLoading" :size="12" class="animate-spin" />
            {{ store.historyLoading ? 'Loading…' : 'Load more' }}
          </Button>
        </div>
      </div>
      <p
        v-else
        class="px-3 py-4 text-center text-xs text-muted-foreground"
        data-testid="git-graph-empty"
      >
        {{ isUnborn ? 'No commits yet — commit something to start the history' : 'No commits to show' }}
      </p>
    </ScrollArea>

    <GitBranchDialog
      v-model:open="branchDialogOpen"
      :mode="branchDialogMode"
      :branch-name="branchDialogBranch"
      :from-hash="branchDialogFromHash"
    />
    <GitDeleteBranchDialog
      v-model:open="deleteDialogOpen"
      :branch="deleteBranchName"
    />
    <GitMergeDialog
      v-model:open="mergeDialogOpen"
      :direction="mergeDirection"
      :branch="mergeBranch"
    />
  </div>
</template>
