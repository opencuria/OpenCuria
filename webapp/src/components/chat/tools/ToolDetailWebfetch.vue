<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const url = computed(() => {
  const fromMeta = props.part.meta?.['url']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  return stringArg(parseToolArguments(props.part), 'url')
})

const preview = computed(() => truncatePreview(props.part.output || ''))
</script>

<template>
  <div data-testid="tool-detail-webfetch" class="min-w-0 space-y-1">
    <a
      v-if="url"
      :href="url"
      target="_blank"
      rel="noopener noreferrer"
      class="block truncate font-mono text-[11px] text-primary hover:underline"
    >{{ url }}</a>
    <pre
      v-if="preview"
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 px-2 py-1.5 font-mono text-[11px] text-muted-foreground"
    >{{ preview }}</pre>
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">Fetching…</p>
  </div>
</template>
