<script setup lang="ts">
/**
 * GitGraphSection — compact commit graph for the Git tab.
 *
 * Three columns (Graph | Description | Date) with an SVG lane graph
 * overlaid on the graph column. Clicking a commit expands inline details
 * below it; the graph stretches for the details height. The details panel
 * starts at the Description column so lane lines never cover the text.
 *
 * Right-clicking a commit opens a context menu (checkout, create branch,
 * copy hash); clicking a branch tag opens branch actions (checkout,
 * rename, merge).
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

function onRowKeydown(hash: string, event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
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

function copyHash(hash: string): void {
  navigator.clipboard.writeText(hash)
  notifications.info('Copied commit hash', hash)
}
</script>

<template>
  <div class="flex h-full flex-col" data-testid="git-graph-section">
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
                :stroke="path.isCommitted ? branchColor(path.colour) : 'var(--muted-foreground)'"
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
            <template v-for="(commit, index) in commits" :key="commit.hash">
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
                          <DropdownMenu v-if="!tag.remote && tag.name !== 'HEAD'">
                            <DropdownMenuTrigger as-child>
                              <button
                                type="button"
                                class="inline-flex h-[18px] shrink-0 cursor-pointer items-center gap-0.5 overflow-hidden rounded-md border px-1 text-[11px] leading-[16px]"
                                :class="{ 'font-medium': tag.current }"
                                :style="tagStyle(tag, vertexColours[index] ?? 0)"
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
                            class="inline-flex h-[18px] shrink-0 items-center gap-0.5 overflow-hidden rounded-md border px-1 text-[11px] leading-[16px]"
                            :style="tagStyle(tag, vertexColours[index] ?? 0)"
                            :data-testid="`git-ref-tag-${tag.name}`"
                          >
                            <GitBranch :size="10" class="shrink-0" />
                            <span class="truncate">{{ tag.name }}</span>
                          </span>
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
                <td
                  :colspan="NUM_COLUMNS"
                  class="border-y border-border bg-muted/30 p-0"
                  :style="{ paddingLeft: `${graphColWidth}px` }"
                >
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
