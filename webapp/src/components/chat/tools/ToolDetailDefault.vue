<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { resolveToolName, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const toolName = computed(() => resolveToolName(props.part) || props.part.title || 'tool')
const preview = computed(() => truncatePreview(props.part.output || ''))
</script>

<template>
  <div data-testid="tool-detail-default" class="min-w-0 space-y-1">
    <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
      {{ toolName }}
    </code>
    <pre
      v-if="preview"
      class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]"
      :class="part.state === 'error' ? 'text-destructive' : 'text-muted-foreground'"
    >{{ preview }}</pre>
    <p v-else-if="part.state === 'running'" class="mt-1 text-[11px] text-muted-foreground">
      Running…
    </p>
  </div>
</template>
