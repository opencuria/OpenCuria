<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

interface TodoRow {
  content: string
  status: string
}

function asTodoRows(value: unknown): TodoRow[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const content = (item as { content?: unknown }).content
    const status = (item as { status?: unknown }).status
    if (typeof content !== 'string' || !content) return []
    return [{ content, status: typeof status === 'string' ? status : 'pending' }]
  })
}

const rows = computed<TodoRow[]>(() => {
  const fromArgs = asTodoRows(parseToolArguments(props.part).todos)
  if (fromArgs.length) return fromArgs
  return (props.part.output || '')
    .split('\n')
    .flatMap((line) => {
      const match = line.match(/^\[([^\]]+)\]\s*(.+)$/)
      if (!match) return []
      return [{ status: match[1] ?? 'pending', content: match[2] ?? '' }]
    })
})
</script>

<template>
  <ul data-testid="tool-detail-todos" class="min-w-0 space-y-0.5">
    <li
      v-for="(row, index) in rows"
      :key="index"
      class="flex items-baseline gap-1.5 text-[11px]"
    >
      <span class="shrink-0 font-mono text-muted-foreground">{{ row.status }}</span>
      <span class="min-w-0 truncate text-foreground">{{ row.content }}</span>
    </li>
    <li v-if="!rows.length" class="text-[11px] text-muted-foreground">
      {{ part.output || 'Todo list cleared.' }}
    </li>
  </ul>
</template>
