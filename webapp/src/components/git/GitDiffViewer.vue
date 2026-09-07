<script setup lang="ts">
/**
 * GitDiffViewer — mock diff view for a changed file, shown in the main
 * content area (replacing the chat) when a change is clicked in the Git
 * tab. Mirrors the FileViewer chrome; diff content comes from the mock
 * git store until a backend git API exists.
 */
import { computed } from 'vue'
import type { GitFileStatus } from '@/types/git'
import { useGitStore } from '@/stores/git'
import { Button } from '@/components/ui/button'
import { FileCode2, FileText, X } from '@lucide/vue'

// Reserved for the future backend git API; mock data is workspace-agnostic.
defineProps<{
  workspaceId: string
}>()

const store = useGitStore()

const change = computed(() => store.viewingDiffChange)

const fileName = computed(
  () => change.value?.path.split('/').pop() ?? '',
)
const directoryPath = computed(() => {
  if (!change.value) return ''
  const parts = change.value.path.split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') : '/'
})

const CODE_EXTENSIONS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'go', 'rs', 'java', 'c', 'h', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'sh', 'sql', 'html', 'css',
  'scss', 'json', 'yaml', 'yml', 'toml', 'xml', 'md',
])

const fileIcon = computed(() => {
  const ext = fileName.value.split('.').pop()?.toLowerCase() ?? ''
  return CODE_EXTENSIONS.has(ext) ? FileCode2 : FileText
})

const STATUS_LABELS: Record<GitFileStatus, string> = {
  M: 'Modified',
  A: 'Added',
  D: 'Deleted',
}

const STATUS_COLORS: Record<GitFileStatus, string> = {
  M: 'border-warning/40 text-warning',
  A: 'border-success/40 text-success',
  D: 'border-error/40 text-error',
}

interface DiffRow {
  key: string
  type: 'hunk' | 'context' | 'add' | 'del'
  oldNo: number | null
  newNo: number | null
  content: string
}

/** Flatten hunks into renderable rows with old/new line numbers. */
const rows = computed<DiffRow[]>(() => {
  const result: DiffRow[] = []
  for (const [hunkIndex, hunk] of (change.value?.diff ?? []).entries()) {
    result.push({
      key: `h-${hunkIndex}`,
      type: 'hunk',
      oldNo: null,
      newNo: null,
      content: hunk.header,
    })
    let oldLine = hunk.oldStart
    let newLine = hunk.newStart
    for (const [lineIndex, line] of hunk.lines.entries()) {
      result.push({
        key: `${hunkIndex}-${lineIndex}`,
        type: line.type,
        oldNo: line.type === 'add' ? null : oldLine,
        newNo: line.type === 'del' ? null : newLine,
        content: line.content,
      })
      if (line.type !== 'add') oldLine += 1
      if (line.type !== 'del') newLine += 1
    }
  }
  return result
})

const lineNumberWidth = computed(() => {
  let max = 0
  for (const row of rows.value) {
    max = Math.max(max, row.oldNo ?? 0, row.newNo ?? 0)
  }
  return Math.max(2, String(max).length)
})

const ROW_CLASSES: Record<DiffRow['type'], string> = {
  hunk: 'bg-muted/60 text-muted-foreground',
  context: 'text-foreground',
  add: 'bg-success/10 text-foreground',
  del: 'bg-error/10 text-foreground',
}

const SIGN_CLASSES: Record<DiffRow['type'], string> = {
  hunk: '',
  context: 'text-muted-foreground/50',
  add: 'text-success',
  del: 'text-error',
}

function rowSign(row: DiffRow): string {
  if (row.type === 'add') return '+'
  if (row.type === 'del') return '−'
  return ' '
}

function handleClose(): void {
  store.closeDiff()
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col" data-testid="git-diff-viewer">
    <!-- Header (mirrors FileViewer chrome) -->
    <div class="flex shrink-0 items-center gap-3 border-b border-border bg-card px-4 py-2">
      <div
        class="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-xs)] border border-border bg-muted/50 text-muted-foreground"
      >
        <component :is="fileIcon" :size="15" />
      </div>
      <div class="min-w-0 flex-1">
        <div class="truncate text-sm font-medium text-foreground" data-testid="git-diff-name">
          {{ fileName }}
        </div>
        <div class="truncate text-xs text-muted-foreground" data-testid="git-diff-path">
          {{ directoryPath }}
        </div>
      </div>
      <span
        v-if="change"
        class="hidden shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium sm:inline-block"
        :class="STATUS_COLORS[change.status]"
        data-testid="git-diff-status"
      >
        {{ STATUS_LABELS[change.status] }}
        <template v-if="change.staged"> · Staged</template>
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        title="Close"
        data-testid="git-diff-close"
        @click="handleClose"
      >
        <X :size="14" />
      </Button>
    </div>

    <!-- Diff content -->
    <div class="min-h-0 flex-1 overflow-auto bg-background">
      <div class="p-3">
        <div
          v-if="rows.length > 0"
          class="overflow-hidden rounded-[var(--radius-xs)] border border-border bg-muted/30"
          data-testid="git-diff-code"
        >
          <div class="overflow-x-auto py-2">
            <div
              v-for="row in rows"
              :key="row.key"
              class="flex font-mono text-xs leading-5"
              :class="ROW_CLASSES[row.type]"
              :data-diff-type="row.type"
            >
              <template v-if="row.type === 'hunk'">
                <span class="w-full select-none px-3">{{ row.content }}</span>
              </template>
              <template v-else>
                <span
                  class="shrink-0 select-none pr-2 text-right text-muted-foreground/50"
                  :style="{ width: `${lineNumberWidth + 1}ch` }"
                >{{ row.oldNo ?? '' }}</span>
                <span
                  class="shrink-0 select-none border-r border-border/60 pr-2 text-right text-muted-foreground/50"
                  :style="{ width: `${lineNumberWidth + 1}ch`, marginRight: '0.5rem' }"
                >{{ row.newNo ?? '' }}</span>
                <span
                  class="shrink-0 select-none pr-1"
                  :class="SIGN_CLASSES[row.type]"
                >{{ rowSign(row) }}</span>
                <span class="whitespace-pre">{{ row.content || ' ' }}</span>
              </template>
            </div>
          </div>
        </div>
        <p v-else class="px-1 text-xs text-muted-foreground">
          No textual diff available for this change.
        </p>
      </div>
    </div>
  </div>
</template>
