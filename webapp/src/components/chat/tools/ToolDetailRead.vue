<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const path = computed(() => {
  const fromMeta = props.part.meta?.['path']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  return stringArg(parseToolArguments(props.part), 'path')
})

const preview = computed(() => truncatePreview(props.part.output || ''))
</script>

<template>
  <div data-testid="tool-detail-read" class="min-w-0 space-y-1">
    <code
      v-if="path"
      class="block truncate font-mono text-[11px] text-muted-foreground"
    >{{ path }}</code>
    <pre
      v-if="preview"
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 px-2 py-1.5 font-mono text-[11px] text-muted-foreground"
    >{{ preview }}</pre>
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">Reading…</p>
  </div>
</template>
