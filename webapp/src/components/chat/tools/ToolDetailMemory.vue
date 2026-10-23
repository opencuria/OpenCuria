<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

interface MemoryEntry {
  path: string
  lineStart?: string | number
  lineEnd?: string | number
  note?: string
}

function asEntries(value: unknown): MemoryEntry[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const record = item as Record<string, unknown>
    if (typeof record.path !== 'string' || !record.path.trim()) return []
    return [
      {
        path: record.path,
        ...(typeof record.lineStart === 'string' || typeof record.lineStart === 'number'
          ? { lineStart: record.lineStart }
          : {}),
        ...(typeof record.lineEnd === 'string' || typeof record.lineEnd === 'number'
          ? { lineEnd: record.lineEnd }
          : {}),
        ...(typeof record.note === 'string' ? { note: record.note } : {}),
      },
    ]
  })
}

const args = computed(() => parseToolArguments(props.part))
const operation = computed(() => stringArg(args.value, 'operation'))
const requestedEntries = computed(() => asEntries(args.value.entries))
const threadIds = computed(() =>
  Array.isArray(args.value.thread_ids)
    ? args.value.thread_ids.filter((item): item is string => typeof item === 'string')
    : [],
)
const outputData = computed<Record<string, unknown> | null>(() => {
  try {
    const parsed: unknown = JSON.parse(props.part.output || '')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
})
const citations = computed(() => {
  const data = outputData.value
  if (!data) return []
  return asEntries(data.citations ?? data.entries ?? data.sources)
})
const preview = computed(() => {
  if (outputData.value) {
    if (
      citations.value.length &&
      Object.keys(outputData.value).every((key) =>
        ['citations', 'entries', 'sources'].includes(key),
      )
    )
      return ''
    return truncatePreview(JSON.stringify(outputData.value, null, 2))
  }
  return truncatePreview(props.part.output || '')
})

function lineRange(entry: MemoryEntry): string {
  if (entry.lineStart == null && entry.lineEnd == null) return ''
  if (entry.lineStart != null && entry.lineEnd != null) {
    return entry.lineStart === entry.lineEnd
      ? `L${entry.lineStart}`
      : `L${entry.lineStart}–L${entry.lineEnd}`
  }
  return `L${entry.lineStart ?? entry.lineEnd}`
}
</script>

<template>
  <div data-testid="tool-detail-memory" class="min-w-0 space-y-2">
    <p v-if="operation" class="text-[11px] text-muted-foreground">{{ operation }}</p>
    <div v-if="requestedEntries.length" class="space-y-1">
      <p class="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        Requested entries
      </p>
      <ul class="space-y-1">
        <li
          v-for="(entry, index) in requestedEntries"
          :key="`${entry.path}-${index}`"
          class="min-w-0 text-[11px]"
        >
          <code class="break-all font-mono text-foreground/90">{{ entry.path }}</code>
          <span v-if="lineRange(entry)" class="ml-1 font-mono text-muted-foreground">
            {{ lineRange(entry) }}
          </span>
          <p v-if="entry.note" class="break-words text-muted-foreground">{{ entry.note }}</p>
        </li>
      </ul>
    </div>
    <div v-if="threadIds.length" class="text-[11px] text-muted-foreground">
      Threads: <code class="font-mono">{{ threadIds.join(', ') }}</code>
    </div>
    <div v-if="citations.length" class="space-y-1">
      <p class="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Citations</p>
      <ul class="space-y-1">
        <li
          v-for="(entry, index) in citations"
          :key="`${entry.path}-${index}`"
          class="min-w-0 text-[11px]"
        >
          <code class="break-all font-mono text-foreground/90">{{ entry.path }}</code>
          <span v-if="lineRange(entry)" class="ml-1 font-mono text-muted-foreground">
            {{ lineRange(entry) }}
          </span>
          <p v-if="entry.note" class="break-words text-muted-foreground">{{ entry.note }}</p>
        </li>
      </ul>
    </div>
    <pre
      v-if="preview"
      data-testid="tool-detail-memory-output"
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-muted/40 px-2.5 py-2 font-mono text-[11px]"
      :class="part.state === 'error' ? 'text-destructive' : 'text-muted-foreground'"
      >{{ preview }}</pre
    >
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">
      Reading memory…
    </p>
  </div>
</template>
