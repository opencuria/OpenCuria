<script setup lang="ts">
/**
 * GitGraphSection — commit graph table (Git Graph style) for the Git tab.
 *
 * Renders the mock commit history as a table with Graph | Description |
 * Date | Author | Commit columns and an SVG lane graph (ported geometry
 * from vscode-git-graph) overlaid on the graph column. Clicking a commit
 * row expands an inline commit details view below it; the graph stretches
 * vertically for the details height. Columns (except the last visible one)
 * are resizable via drag handles in the header, persisted through the
 * git store.
 *
 * Right-clicking a commit row opens a context menu (checkout commit,
 * create branch, copy hash); clicking a branch tag opens a dropdown with
 * branch actions (checkout, rename, merge in both directions).
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { CSSProperties } from 'vue'
import type { GitRefTag } from '@/stores/git'
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
import { useNotificationStore } from '@/stores/notifications'
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
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Check,
  ChevronDown,
  Copy,
  GitBranch,
  GitBranchPlus,
  GitCommitHorizontal,
  GitMerge,
  Pencil,
} from '@lucide/vue'
import GitBranchDialog from './GitBranchDialog.vue'
import GitCommitDetailsView from './GitCommitDetailsView.vue'
import GitMergeDialog from './GitMergeDialog.vue'

const store = useGitStore()
const notifications = useNotificationStore()

// --- Geometry (mirrors vscode-git-graph grid defaults) ---

/** Fixed table header height in px; the graph SVG starts below it. */
const HEADER_HEIGHT = 29
const MIN_GRAPH_WIDTH = 40
const MIN_COL_WIDTH = 60
const MAX_COL_WIDTH = 420

function branchColor(colour: number): string {
  return GIT_GRAPH_COLORS[colour % GIT_GRAPH_COLORS.length] ?? '#888888'
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

const graphSvgWidth = computed(() => layout.value.contentWidth)
const graphSvgHeight = computed(() => layout.value.height)

// --- Columns: visibility + resizable widths ---

const showDate = ref(true)
const showAuthor = ref(true)
const showCommit = ref(true)

const numColumns = computed(
  () =>
    2 +
    (showDate.value ? 1 : 0) +
    (showAuthor.value ? 1 : 0) +
    (showCommit.value ? 1 : 0),
)

/**
 * Persisted widths as [graph, date, author, commit] (description is flex,
 * mirroring Git Graph's saveColumnWidths). Null means auto layout.
 */
function effectiveWidths(): [number, number, number, number] | null {
  const stored = store.columnWidths
  if (
    stored &&
    stored.length >= 4 &&
    stored.slice(0, 4).every((v) => typeof v === 'number' && Number.isFinite(v))
  ) {
    return [
      Math.min(MAX_COL_WIDTH, Math.max(MIN_GRAPH_WIDTH, stored[0] as number)),
      Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, stored[1] as number)),
      Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, stored[2] as number)),
      Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, stored[3] as number)),
    ]
  }
  return null
}

/** Base widths used when the user starts dragging from auto layout. */
function defaultWidths(): [number, number, number, number] {
  return [
    Math.min(
      MAX_COL_WIDTH,
      Math.max(MIN_GRAPH_WIDTH, layout.value.contentWidth),
    ),
    90,
    110,
    70,
  ]
}

const graphColWidth = computed(
  () => effectiveWidths()?.[0] ?? layout.value.contentWidth,
)
const dateColWidth = computed(() => effectiveWidths()?.[1])
const authorColWidth = computed(() => effectiveWidths()?.[2])
const commitColWidth = computed(() => effectiveWidths()?.[3])
const hasFixedLayout = computed(() => effectiveWidths() !== null)

/** Visible column numbers (0 graph, 1 description, 2 date, 3 author, 4 commit). */
const visibleCols = computed<number[]>(() => {
  const cols = [0, 1]
  if (showDate.value) cols.push(2)
  if (showAuthor.value) cols.push(3)
  if (showCommit.value) cols.push(4)
  return cols
})

function showResizeHandle(col: number): boolean {
  const cols = visibleCols.value
  return cols.includes(col) && cols[cols.length - 1] !== col
}

/** Stored width index for a fixed column (graph/date/author/commit). */
function widthIndexForCol(col: number): number | null {
  if (col === 0) return 0
  if (col === 2) return 1
  if (col === 3) return 2
  if (col === 4) return 3
  return null
}

/** First visible fixed column (as stored index) right of the given column. */
function nextFixedColIndex(col: number): number | null {
  const fixed: Array<{ col: number; index: number; visible: boolean }> = [
    { col: 2, index: 1, visible: showDate.value },
    { col: 3, index: 2, visible: showAuthor.value },
    { col: 4, index: 3, visible: showCommit.value },
  ]
  for (const entry of fixed) {
    if (entry.col > col && entry.visible) return entry.index
  }
  return null
}

interface ResizeState {
  leftCol: number
  startX: number
  base: [number, number, number, number]
}

let resizeState: ResizeState | null = null

function onResizeMove(event: PointerEvent): void {
  const state = resizeState
  if (!state) return
  const dx = event.clientX - state.startX
  const next: [number, number, number, number] = [...state.base] as [
    number,
    number,
    number,
    number,
  ]
  const clamp = (value: number, min: number): number =>
    Math.min(MAX_COL_WIDTH, Math.max(min, Math.round(value)))
  if (state.leftCol === 0) {
    next[0] = clamp((state.base[0] as number) + dx, MIN_GRAPH_WIDTH)
  } else {
    const ownIndex = widthIndexForCol(state.leftCol)
    const nextIndex = nextFixedColIndex(state.leftCol)
    if (ownIndex !== null && state.leftCol !== 1) {
      // Fixed column grows while its right neighbour shrinks (Git Graph).
      const ownBase = state.base[ownIndex] as number
      let delta = dx
      if (ownBase + delta < MIN_COL_WIDTH) {
        delta = MIN_COL_WIDTH - ownBase
      }
      if (nextIndex !== null) {
        const nextBase = state.base[nextIndex] as number
        if (nextBase - delta < MIN_COL_WIDTH) {
          delta = nextBase - MIN_COL_WIDTH
        }
        next[nextIndex] = clamp(nextBase - delta, MIN_COL_WIDTH)
      }
      next[ownIndex] = clamp(ownBase + delta, MIN_COL_WIDTH)
    } else if (nextIndex !== null) {
      // Flexible description column: only the neighbour shrinks/grows.
      next[nextIndex] = clamp((state.base[nextIndex] as number) - dx, MIN_COL_WIDTH)
    } else {
      return
    }
  }
  store.setColumnWidths([...next])
}

function onResizeEnd(): void {
  resizeState = null
  window.removeEventListener('pointermove', onResizeMove)
  window.removeEventListener('pointerup', onResizeEnd)
  window.removeEventListener('pointercancel', onResizeEnd)
}

function onResizeStart(event: PointerEvent, leftCol: number): void {
  event.preventDefault()
  event.stopPropagation()
  resizeState = {
    leftCol,
    startX: event.clientX,
    base: effectiveWidths() ?? defaultWidths(),
  }
  window.addEventListener('pointermove', onResizeMove)
  window.addEventListener('pointerup', onResizeEnd)
  window.addEventListener('pointercancel', onResizeEnd)
}

// --- Row / tag display ---

const isDetached = computed(() => repo.value?.currentBranch === null)
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
  store.toggleCommitDetails(hash)
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && store.expandedCommitHash) {
    store.closeCommitDetails()
  }
}

onMounted(() => {
  window.addEventListener('keydown', onEscape)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onEscape)
})

/** Ref tags pointing at a commit, plus a HEAD marker when detached. */
function tagsFor(hash: string): GitRefTag[] {
  const tags = store.tagsByHash.get(hash) ?? []
  if (isDetached.value && isHead(hash)) {
    return [{ name: 'HEAD', remote: false, current: true }, ...tags]
  }
  return tags
}

/** Deterministic graph palette colour per ref name. */
function tagColor(name: string): number {
  let hash = 0
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return hash % GIT_GRAPH_COLORS.length
}

function tagStyle(tag: GitRefTag): CSSProperties {
  if (tag.remote) {
    return { borderColor: 'var(--border)', color: 'var(--muted-foreground)' }
  }
  const color = branchColor(tagColor(tag.name))
  if (tag.current) {
    return {
      backgroundColor: color,
      borderColor: color,
      color: 'var(--primary-foreground)',
    }
  }
  return { borderColor: color, color }
}

function tagIconColor(tag: GitRefTag): string {
  if (tag.remote) return 'var(--muted-foreground)'
  return branchColor(tagColor(tag.name))
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

function copyHash(hash: string): void {
  navigator.clipboard.writeText(hash)
  notifications.info('Copied commit hash', hash)
}
</script>

<template>
  <div class="flex h-full flex-col" data-testid="git-graph-section">
    <!-- Header: branch filter + column toggles + create branch -->
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
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            variant="ghost"
            size="sm"
            class="h-6 min-w-0 gap-1.5 px-1.5 text-xs"
            title="Toggle columns"
            data-testid="git-columns-toggle"
          >
            <span class="truncate">Columns</span>
            <ChevronDown :size="11" class="shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            data-testid="git-column-toggle-date"
            @click="showDate = !showDate"
          >
            <Check :size="13" :class="{ invisible: !showDate }" />
            Date
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="git-column-toggle-author"
            @click="showAuthor = !showAuthor"
          >
            <Check :size="13" :class="{ invisible: !showAuthor }" />
            Author
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="git-column-toggle-commit"
            @click="showCommit = !showCommit"
          >
            <Check :size="13" :class="{ invisible: !showCommit }" />
            Commit
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="ghost"
        size="icon-sm"
        class="h-6 w-6"
        title="Create branch"
        data-testid="git-create-branch"
        @click="openCreateBranch(store.currentRepo?.headHash ?? '')"
      >
        <GitBranchPlus :size="13" />
      </Button>
    </div>

    <!-- Graph table -->
    <ScrollArea class="min-h-0 flex-1">
      <div v-if="commits.length > 0" class="relative">
        <!-- Lane edges + commit nodes (ported Git Graph geometry) -->
        <svg
          :width="graphSvgWidth"
          :height="graphSvgHeight"
          class="pointer-events-none absolute left-0 top-0 z-[2]"
          :style="{ top: `${HEADER_HEIGHT}px` }"
          aria-hidden="true"
          data-testid="git-graph-svg"
        >
          <template
            v-for="(branch, branchIndex) in renderedBranches"
            :key="`branch-${branchIndex}`"
          >
            <template
              v-for="(path, pathIndex) in branch.paths"
              :key="`branch-${branchIndex}-path-${pathIndex}`"
            >
              <path
                :d="path.d"
                stroke="var(--card)"
                stroke-opacity="0.75"
                stroke-width="4"
                fill="none"
              />
              <path
                :d="path.d"
                :stroke="path.isCommitted ? branchColor(path.colour) : '#808080'"
                stroke-width="2"
                fill="none"
              />
            </template>
          </template>
          <circle
            v-for="node in renderedNodes"
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
            @click.stop="store.toggleCommitDetails(node.hash)"
          />
        </svg>

        <table
          class="w-full border-collapse"
          :class="{ 'table-fixed': hasFixedLayout }"
        >
          <colgroup>
            <col :style="{ width: `${graphColWidth}px` }" />
            <col />
            <col v-if="showDate" :style="dateColWidth ? { width: `${dateColWidth}px` } : undefined" />
            <col v-if="showAuthor" :style="authorColWidth ? { width: `${authorColWidth}px` } : undefined" />
            <col v-if="showCommit" :style="commitColWidth ? { width: `${commitColWidth}px` } : undefined" />
          </colgroup>
          <thead class="sticky top-0 z-[1] bg-card">
            <tr :style="{ height: `${HEADER_HEIGHT - 1}px` }">
              <th
                class="relative border-b border-border px-3 text-left text-xs font-medium text-muted-foreground"
                data-col="0"
                data-testid="git-graph-header-graph"
              >
                Graph
                <span
                  v-if="showResizeHandle(0)"
                  class="absolute right-0 top-0 block h-full w-1.5 cursor-col-resize touch-none"
                  data-col="0"
                  data-testid="git-column-resize-0"
                  @pointerdown="onResizeStart($event, 0)"
                />
              </th>
              <th
                class="relative border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-col="1"
                data-testid="git-graph-header-description"
              >
                Description
                <span
                  v-if="showResizeHandle(1)"
                  class="absolute right-0 top-0 block h-full w-1.5 cursor-col-resize touch-none"
                  data-col="1"
                  data-testid="git-column-resize-1"
                  @pointerdown="onResizeStart($event, 1)"
                />
              </th>
              <th
                v-if="showDate"
                class="relative border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-col="2"
                data-testid="git-graph-header-date"
              >
                Date
                <span
                  v-if="showResizeHandle(2)"
                  class="absolute right-0 top-0 block h-full w-1.5 cursor-col-resize touch-none"
                  data-col="2"
                  data-testid="git-column-resize-2"
                  @pointerdown="onResizeStart($event, 2)"
                />
              </th>
              <th
                v-if="showAuthor"
                class="relative border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-col="3"
                data-testid="git-graph-header-author"
              >
                Author
                <span
                  v-if="showResizeHandle(3)"
                  class="absolute right-0 top-0 block h-full w-1.5 cursor-col-resize touch-none"
                  data-col="3"
                  data-testid="git-column-resize-3"
                  @pointerdown="onResizeStart($event, 3)"
                />
              </th>
              <th
                v-if="showCommit"
                class="relative border-b border-border px-1 text-left text-xs font-medium text-muted-foreground"
                data-col="4"
                data-testid="git-graph-header-commit"
              >
                Commit
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(commit, index) in commits" :key="commit.hash">
              <ContextMenu>
                <ContextMenuTrigger as-child>
                  <tr
                    class="cursor-pointer hover:bg-muted/60"
                    :class="{ 'bg-muted': isExpanded(commit.hash) }"
                    :style="[{ height: `${GIT_GRAPH_GRID_Y}px` }, rowStyle(index)]"
                    :data-color="vertexColours[index]"
                    :data-testid="'git-graph-row'"
                    :data-hash="commit.hash"
                    tabindex="0"
                    @click="onRowClick(commit.hash, $event)"
                  >
                    <td />
                    <td class="max-w-0 px-1">
                      <span class="flex min-w-0 items-center leading-6">
                        <span
                          v-if="isHead(commit.hash)"
                          class="inline-block h-[6px] w-[6px] shrink-0 rounded-full border-2"
                          :style="{
                            borderColor: branchColor(vertexColours[index] ?? 0),
                            marginRight: '5px',
                          }"
                          :title="
                            store.currentRepo?.currentBranch
                              ? `The branch &quot;${store.currentRepo.currentBranch}&quot; is currently checked out at this commit.`
                              : 'This commit is currently checked out.'
                          "
                        />
                        <template v-for="tag in tagsFor(commit.hash)" :key="tag.name">
                          <DropdownMenu v-if="!tag.remote && tag.name !== 'HEAD'">
                            <DropdownMenuTrigger as-child>
                              <button
                                type="button"
                                class="mr-[5px] inline-flex h-[18px] shrink-0 cursor-pointer items-center overflow-hidden rounded border text-[11px] leading-[18px]"
                                :class="{ 'font-bold': tag.current }"
                                :style="tagStyle(tag)"
                                :data-testid="`git-branch-tag-${tag.name}`"
                                @click.stop
                              >
                                <span
                                  class="flex h-full w-[18px] items-center justify-center"
                                  :style="{ backgroundColor: tagIconColor(tag) }"
                                >
                                  <GitBranch :size="11" :style="{ color: 'var(--card)' }" />
                                </span>
                                <span class="px-[5px]">{{ tag.name }}</span>
                              </button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="start">
                              <DropdownMenuItem
                                :disabled="tag.current"
                                :data-testid="`git-tag-checkout-${tag.name}`"
                                @click="store.checkoutBranch(tag.name)"
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
                          <span
                            v-else
                            class="mr-[5px] inline-flex h-[18px] shrink-0 items-center overflow-hidden rounded border text-[11px] leading-[18px]"
                            :style="tagStyle(tag)"
                            :data-testid="`git-ref-tag-${tag.name}`"
                          >
                            <span
                              class="flex h-full w-[18px] items-center justify-center"
                              :style="{ backgroundColor: tagIconColor(tag) }"
                            >
                              <GitBranch :size="11" :style="{ color: 'var(--card)' }" />
                            </span>
                            <span class="px-[5px]">{{ tag.name }}</span>
                          </span>
                        </template>
                        <span
                          class="block min-w-0 flex-1 truncate text-xs text-foreground"
                          :class="{
                            'font-bold': isHead(commit.hash),
                            'opacity-50': commit.parents.length > 1,
                          }"
                          :title="commit.message"
                        >
                          {{ commit.message }}
                        </span>
                      </span>
                    </td>
                    <td
                      v-if="showDate"
                      class="max-w-0 whitespace-nowrap px-1 text-xs text-muted-foreground"
                      :title="commit.timestamp"
                    >
                      <span class="block truncate leading-6">
                        {{ formatRelativeTime(commit.timestamp) }}
                      </span>
                    </td>
                    <td
                      v-if="showAuthor"
                      class="max-w-0 whitespace-nowrap px-1 text-xs text-muted-foreground"
                      :title="commit.author"
                    >
                      <span class="block truncate leading-6">{{ commit.author }}</span>
                    </td>
                    <td
                      v-if="showCommit"
                      class="max-w-0 whitespace-nowrap px-1 font-mono text-xs text-muted-foreground"
                      :title="commit.hash"
                    >
                      <span class="block truncate leading-6">
                        {{ commit.hash.slice(0, 7) }}
                      </span>
                    </td>
                  </tr>
                </ContextMenuTrigger>
                <ContextMenuContent>
                  <ContextMenuItem
                    :data-testid="`git-commit-checkout-${commit.hash}`"
                    @click="store.checkoutCommit(commit.hash)"
                  >
                    <GitCommitHorizontal :size="13" />
                    Checkout Commit
                  </ContextMenuItem>
                  <ContextMenuItem
                    :data-testid="`git-commit-create-branch-${commit.hash}`"
                    @click="openCreateBranch(commit.hash)"
                  >
                    <GitBranchPlus :size="13" />
                    Create Branch Here
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem
                    :data-testid="`git-commit-copy-${commit.hash}`"
                    @click="copyHash(commit.hash)"
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
                <td :colspan="numColumns" class="border-y border-border bg-muted/30 p-0">
                  <GitCommitDetailsView :hash="commit.hash" />
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p
        v-else
        class="px-3 py-4 text-center text-xs text-muted-foreground"
      >
        No commits yet
      </p>
    </ScrollArea>

    <GitBranchDialog
      v-model:open="branchDialogOpen"
      :mode="branchDialogMode"
      :branch-name="branchDialogBranch"
      :from-hash="branchDialogFromHash"
    />
    <GitMergeDialog
      v-model:open="mergeDialogOpen"
      :direction="mergeDirection"
      :branch="mergeBranch"
    />
  </div>
</template>
