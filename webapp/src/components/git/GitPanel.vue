<script setup lang="ts">
/**
 * GitPanel — Git tab content for the workspace side panel.
 *
 * Frontend-only for now: all data comes from the mock git store. Layout is
 * a repo/branch header on top, then the Changes section and the commit
 * Graph section stacked vertically (default 50/50) with a drag handle
 * between them. Each section scrolls vertically on its own; nothing
 * scrolls horizontally.
 */
import { computed, ref } from 'vue'
import { useGitStore } from '@/stores/git'
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

// Reserved for the future backend git API; mock data is workspace-agnostic.
defineProps<{
  workspaceId: string
}>()

const store = useGitStore()

const branchLabel = computed(
  () => store.currentRepo?.currentBranch ?? 'DETACHED HEAD',
)
const ahead = computed(() => store.currentBranch?.ahead ?? 0)
const behind = computed(() => store.currentBranch?.behind ?? 0)

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
              {{ repo.name }}
            </SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-7 w-7 shrink-0"
          title="Fetch"
          data-testid="git-fetch"
          @click="store.fetchRemote()"
        >
          <RefreshCw :size="13" />
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
        <GitChangesSection />
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
        <GitGraphSection />
      </div>
    </div>
  </div>
</template>
