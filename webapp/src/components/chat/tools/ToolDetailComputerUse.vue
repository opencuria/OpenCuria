<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { computerUseSummary, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const summary = computed(() => computerUseSummary(props.part))
const preview = computed(() => truncatePreview(props.part.output || '', 400))
</script>

<template>
  <div data-testid="tool-detail-computer-use" class="min-w-0 space-y-1">
    <p class="truncate text-[11px] text-muted-foreground">{{ summary }}</p>
    <pre
      v-if="preview && part.state === 'error'"
      class="max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-destructive"
    >{{ preview }}</pre>
  </div>
</template>
