<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useHarnessPartDetail } from '@/lib/harnessPartDetail'
import { ChevronDown } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import { diffFileName, parseFileDiff, type DiffLineType } from '@/lib/fileDiff'

const props = defineProps<{
  part: HarnessPart
}>()

const open = ref(false)
const partRef = computed(() => props.part)
const {
  loading: detailLoading,
  error: detailError,
  load: loadDetail,
  setExpanded,
} = useHarnessPartDetail(partRef)
watch(open, (expanded) => {
  setExpanded(expanded)
  if (expanded) void loadDetail()
})

const path = computed(() =>
  String(props.part.display?.path ?? props.part.meta?.['path'] ?? props.part.title ?? 'file'),
)
const fileName = computed(() => diffFileName(path.value))
const previewLines = computed(() =>
  (props.part.display?.preview ?? []).map((line) => ({
    type: line.type,
    oldNo: line.oldNo,
    newNo: line.newNo,
    content: line.content,
  })),
)
const parsed = computed(() => parseFileDiff(props.part.output || ''))
const lines = computed(() => {
  if (props.part.detail_loaded === false) return previewLines.value
  return open.value ? parsed.value.expanded : parsed.value.collapsed
})
const additions = computed(() => props.part.display?.additions ?? parsed.value.additions)
const deletions = computed(() => props.part.display?.deletions ?? parsed.value.deletions)
const canExpand = computed(
  () =>
    props.part.detail_loaded === false ||
    parsed.value.expanded.length > parsed.value.collapsed.length,
)

const lineNumberWidth = computed(() => {
  let max = 0
  for (const line of lines.value) {
    max = Math.max(max, line.oldNo ?? 0, line.newNo ?? 0)
  }
  return Math.max(2, String(max).length)
})

const ROW_CLASSES: Record<DiffLineType, string> = {
  context: 'text-foreground',
  add: 'bg-success/10 text-foreground',
  del: 'bg-error/10 text-foreground',
}

const GUTTER_CLASSES: Record<DiffLineType, string> = {
  context: 'bg-transparent',
  add: 'bg-success',
  del: 'bg-error',
}

const SIGN_CLASSES: Record<DiffLineType, string> = {
  context: 'text-muted-foreground/50',
  add: 'text-success',
  del: 'text-error',
}

function lineSign(type: DiffLineType): string {
  if (type === 'add') return '+'
  if (type === 'del') return '−'
  return ' '
}

function lineNumber(oldNo: number | null, newNo: number | null): string {
  return String(newNo ?? oldNo ?? '')
}
</script>

<template>
  <div
    data-testid="harness-patch-card"
    class="w-full overflow-hidden rounded-xl border border-border bg-card"
  >
    <button
      type="button"
      data-testid="harness-patch-header"
      class="flex w-full min-w-0 items-center gap-1.5 px-3 py-1.5 text-left text-xs hover:bg-muted/40"
      @click="open = !open"
    >
      <ChevronDown
        :size="12"
        class="shrink-0 text-muted-foreground opacity-70 transition-transform"
        :class="open ? '' : '-rotate-90'"
      />
      <span data-testid="harness-patch-name" class="min-w-0 truncate font-medium text-foreground">
        {{ fileName }}
      </span>
      <span class="ml-auto flex shrink-0 items-center gap-1.5 font-medium tabular-nums">
        <span v-if="additions > 0" data-testid="harness-patch-additions" class="text-success"
          >+{{ additions }}</span
        >
        <span v-if="deletions > 0" data-testid="harness-patch-deletions" class="text-error"
          >-{{ deletions }}</span
        >
      </span>
    </button>
    <div
      v-if="detailLoading"
      data-testid="harness-part-detail-loading"
      class="px-3 py-2 text-xs text-muted-foreground"
    >
      Loading full diff…
    </div>
    <div v-if="detailError" class="px-3 py-2 text-xs text-destructive">
      <p>{{ detailError }}</p>
      <button type="button" class="mt-1 underline" @click="loadDetail">Retry</button>
    </div>
    <div
      v-if="lines.length"
      data-testid="harness-patch-diff"
      class="overflow-x-auto border-t border-border py-0.5 font-mono text-xs leading-5"
    >
      <div
        v-for="(line, index) in lines"
        :key="`${line.type}-${line.oldNo}-${line.newNo}-${index}`"
        class="flex min-w-0"
        :class="ROW_CLASSES[line.type]"
        :data-diff-type="line.type"
      >
        <span class="w-0.5 shrink-0" :class="GUTTER_CLASSES[line.type]" />
        <span
          class="shrink-0 select-none px-2 text-right text-muted-foreground/50"
          :style="{ width: `${lineNumberWidth + 2}ch` }"
          >{{ lineNumber(line.oldNo, line.newNo) }}</span
        >
        <span class="shrink-0 select-none pr-1" :class="SIGN_CLASSES[line.type]">{{
          lineSign(line.type)
        }}</span>
        <span class="min-w-0 whitespace-pre pr-3">{{ line.content || ' ' }}</span>
      </div>
    </div>
    <p
      v-else-if="!detailLoading && !detailError"
      data-testid="harness-patch-empty"
      class="border-t border-border px-3 py-2 font-mono text-xs text-muted-foreground"
    >
      (no diff available)
    </p>
    <span v-if="canExpand" class="sr-only">{{ open ? 'expanded' : 'collapsed' }}</span>
  </div>
</template>
