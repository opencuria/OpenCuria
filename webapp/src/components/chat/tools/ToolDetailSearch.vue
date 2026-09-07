<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const lines = computed(() =>
  (props.part.output || '')
    .split('\n')
    .map((line) => line.trimEnd())
    .filter((line) => line.length > 0),
)

const count = computed(() => {
  const fromMeta = props.part.meta?.['count']
  if (typeof fromMeta === 'number') return fromMeta
  return lines.value.length
})

const query = computed(() => {
  const args = parseToolArguments(props.part)
  return stringArg(args, 'pattern') || stringArg(args, 'path')
})
</script>

<template>
  <div data-testid="tool-detail-search" class="min-w-0 space-y-1">
    <div class="flex items-center gap-1.5 text-[11px] text-muted-foreground">
      <span v-if="query" class="min-w-0 truncate font-mono">{{ query }}</span>
      <span
        data-testid="tool-detail-search-count"
        class="shrink-0 rounded bg-muted px-1 py-px font-mono text-[10px]"
      >
        {{ count }}
      </span>
    </div>
    <ul
      v-if="lines.length"
      class="max-h-64 overflow-auto rounded-md bg-muted/50 px-2 py-1.5 font-mono text-[11px] text-muted-foreground"
    >
      <li v-for="(line, index) in lines" :key="index" class="truncate">{{ line }}</li>
    </ul>
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">Searching…</p>
    <p v-else class="text-[11px] text-muted-foreground">No matches</p>
  </div>
</template>
