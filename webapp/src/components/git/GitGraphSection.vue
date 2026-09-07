<script setup lang="ts">
/**
 * GitGraphSection — commit graph (Git Graph style) for the Git tab.
 *
 * Renders the mock commit history as an SVG lane graph with HTML rows on
 * top. Right-clicking a commit row opens a context menu (checkout commit,
 * create branch, copy hash); clicking a branch tag opens a dropdown with
 * branch actions (checkout, rename, merge in both directions). The graph
 * always fits the panel width — lanes are compact and text truncates.
 */
import { computed, ref } from 'vue'
import type { CSSProperties } from 'vue'
import type { GitGraphEdge } from '@/lib/gitGraph'
import type { GitRefTag } from '@/stores/git'
import { computeGraphLayout, filterReachableCommits } from '@/lib/gitGraph'
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
import GitMergeDialog from './GitMergeDialog.vue'

const store = useGitStore()
const notifications = useNotificationStore()

// --- Graph geometry (logical lanes/rows → pixels) ---

const ROW_HEIGHT = 40
const LANE_WIDTH = 16
const NODE_RADIUS = 4
const GRAPH_PAD_LEFT = 6

/** Branch filter: null shows all branches. */
const filterBranch = ref<string | null>(null)

const activeFilter = computed(() => {
  const branches = store.currentRepo?.branches ?? []
  return branches.some((b) => b.name === filterBranch.value)
    ? filterBranch.value
    : null
})

const layout = computed(() => {
  const repo = store.currentRepo
  if (!repo) return { rows: [], edges: [], laneCount: 0 }
  if (activeFilter.value) {
    const branch = repo.branches.find((b) => b.name === activeFilter.value)
    if (branch) {
      return computeGraphLayout(
        filterReachableCommits(repo.commits, branch.tipHash),
      )
    }
  }
  return computeGraphLayout(repo.commits)
})

const graphWidth = computed(
  () => GRAPH_PAD_LEFT * 2 + Math.max(1, layout.value.laneCount) * LANE_WIDTH,
)
const graphHeight = computed(() => layout.value.rows.length * ROW_HEIGHT)

function nodeX(lane: number): number {
  return GRAPH_PAD_LEFT + lane * LANE_WIDTH + LANE_WIDTH / 2
}

function nodeY(row: number): number {
  return row * ROW_HEIGHT + ROW_HEIGHT / 2
}

function colorVar(color: number): string {
  return `var(--chart-${color})`
}

function edgePath(edge: GitGraphEdge): string {
  const x1 = nodeX(edge.fromLane)
  const y1 = nodeY(edge.fromRow)
  const x2 = nodeX(edge.toLane)
  const y2 = nodeY(edge.toRow)
  if (!edge.curved) return `M ${x1} ${y1} L ${x2} ${y2}`
  const bend = Math.min(ROW_HEIGHT, (y2 - y1) / 2)
  return `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`
}

// --- Row / tag display ---

const isDetached = computed(() => store.currentRepo?.currentBranch === null)

function isHead(hash: string): boolean {
  return store.currentRepo?.headHash === hash
}

/** Ref tags pointing at a commit, plus a HEAD marker when detached. */
function tagsFor(hash: string): GitRefTag[] {
  const tags = store.tagsByHash.get(hash) ?? []
  if (isDetached.value && isHead(hash)) {
    return [{ name: 'HEAD', remote: false, current: true }, ...tags]
  }
  return tags
}

/** Deterministic chart color per ref name. */
function tagColor(name: string): number {
  let hash = 0
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return (hash % 5) + 1
}

function tagStyle(tag: GitRefTag): CSSProperties {
  if (tag.remote) {
    return { borderColor: 'var(--border)', color: 'var(--muted-foreground)' }
  }
  const color = `var(--chart-${tagColor(tag.name)})`
  if (tag.current) {
    return {
      backgroundColor: color,
      borderColor: color,
      color: 'var(--primary-foreground)',
    }
  }
  return { borderColor: color, color }
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
    <!-- Header: branch filter + create branch -->
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

    <!-- Graph body -->
    <ScrollArea class="min-h-0 flex-1">
      <div
        v-if="layout.rows.length > 0"
        class="relative"
        :style="{ height: `${graphHeight}px` }"
      >
        <!-- Lane edges + commit nodes -->
        <svg
          :width="graphWidth"
          :height="graphHeight"
          class="absolute left-0 top-0"
          aria-hidden="true"
          data-testid="git-graph-svg"
        >
          <path
            v-for="(edge, index) in layout.edges"
            :key="`edge-${index}`"
            :d="edgePath(edge)"
            :stroke="colorVar(edge.color)"
            stroke-width="1.5"
            fill="none"
          />
          <circle
            v-for="(row, index) in layout.rows"
            :key="`node-${row.commit.hash}`"
            :cx="nodeX(row.lane)"
            :cy="nodeY(index)"
            :r="NODE_RADIUS"
            :fill="isHead(row.commit.hash) ? colorVar(row.color) : 'var(--card)'"
            :stroke="colorVar(row.color)"
            stroke-width="1.5"
          />
        </svg>

        <!-- Commit rows -->
        <ContextMenu
          v-for="(row, index) in layout.rows"
          :key="row.commit.hash"
        >
          <ContextMenuTrigger as-child>
            <div
              class="flex min-w-0 cursor-default flex-col justify-center gap-0 pr-2 hover:bg-muted/60"
              :style="{
                height: `${ROW_HEIGHT}px`,
                paddingLeft: `${graphWidth}px`,
              }"
              data-testid="git-graph-row"
            >
              <div class="flex min-w-0 items-center gap-1">
                <template v-for="tag in tagsFor(row.commit.hash)" :key="tag.name">
                  <DropdownMenu v-if="!tag.remote && tag.name !== 'HEAD'">
                    <DropdownMenuTrigger as-child>
                      <button
                        type="button"
                        class="shrink-0 cursor-pointer rounded-full border px-1.5 text-[10px] font-medium leading-4"
                        :style="tagStyle(tag)"
                        :data-testid="`git-branch-tag-${tag.name}`"
                        @click.stop
                      >
                        {{ tag.name }}
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
                    class="shrink-0 rounded-full border px-1.5 text-[10px] font-medium leading-4"
                    :style="tagStyle(tag)"
                    :data-testid="`git-ref-tag-${tag.name}`"
                  >
                    {{ tag.name }}
                  </span>
                </template>
                <span class="min-w-0 truncate text-xs text-foreground">
                  {{ row.commit.message }}
                </span>
              </div>
              <div class="flex min-w-0 items-center gap-1 truncate text-[10px] text-muted-foreground">
                <span class="truncate">{{ row.commit.author }}</span>
                <span>·</span>
                <span class="shrink-0">
                  {{ formatRelativeTime(row.commit.timestamp) }}
                </span>
                <span>·</span>
                <span class="shrink-0 font-mono">
                  {{ row.commit.hash.slice(0, 7) }}
                </span>
              </div>
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent>
            <ContextMenuItem
              :data-testid="`git-commit-checkout-${row.commit.hash}`"
              @click="store.checkoutCommit(row.commit.hash)"
            >
              <GitCommitHorizontal :size="13" />
              Checkout Commit
            </ContextMenuItem>
            <ContextMenuItem
              :data-testid="`git-commit-create-branch-${row.commit.hash}`"
              @click="openCreateBranch(row.commit.hash)"
            >
              <GitBranchPlus :size="13" />
              Create Branch Here
            </ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem
              :data-testid="`git-commit-copy-${row.commit.hash}`"
              @click="copyHash(row.commit.hash)"
            >
              <Copy :size="13" />
              Copy Commit Hash
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>
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
