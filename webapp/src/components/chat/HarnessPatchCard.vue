<script setup lang="ts">
import { computed, ref } from 'vue'
import { Button } from '@/components/ui/button'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  part: HarnessPart
}>()

const view = ref<'diff' | 'before' | 'after'>('diff')

const path = computed(() => String(props.part.meta?.['path'] ?? props.part.title ?? 'file'))
const oldContent = computed(() => String(props.part.meta?.['old_content'] ?? ''))
const newContent = computed(() => String(props.part.meta?.['new_content'] ?? ''))
const unifiedDiff = computed(() => props.part.output || '')

const displayed = computed(() => {
  if (view.value === 'before') return oldContent.value || '(empty file)'
  if (view.value === 'after') return newContent.value || '(empty file)'
  return unifiedDiff.value || '(no diff available)'
})
</script>

<template>
  <div class="w-full overflow-hidden rounded-xl border border-border bg-card">
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
      <p class="truncate text-xs font-medium text-foreground">{{ path }}</p>
      <div class="flex items-center gap-1">
        <Button
          type="button"
          size="sm"
          :variant="view === 'diff' ? 'default' : 'outline'"
          class="h-7 px-2 text-xs"
          @click="view = 'diff'"
        >
          Diff
        </Button>
        <Button
          type="button"
          size="sm"
          :variant="view === 'before' ? 'default' : 'outline'"
          class="h-7 px-2 text-xs"
          @click="view = 'before'"
        >
          Before
        </Button>
        <Button
          type="button"
          size="sm"
          :variant="view === 'after' ? 'default' : 'outline'"
          class="h-7 px-2 text-xs"
          @click="view = 'after'"
        >
          After
        </Button>
      </div>
    </div>
    <pre
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-xs text-muted-foreground"
    >{{ displayed }}</pre>
  </div>
</template>
