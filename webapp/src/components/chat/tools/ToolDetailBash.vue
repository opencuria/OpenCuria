<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg, truncatePreview } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

const command = computed(() => {
  const fromArgs = stringArg(parseToolArguments(props.part), 'command')
  if (fromArgs) return fromArgs
  const title = (props.part.title || '').trim()
  return title.startsWith('$ ') ? title.slice(2) : title
})

const exitCode = computed(() => {
  const raw = props.part.meta?.['exit_code']
  return typeof raw === 'number' ? raw : null
})

const failed = computed(
  () => props.part.state === 'error' || (exitCode.value != null && exitCode.value !== 0),
)

const preview = computed(() => truncatePreview(props.part.output || ''))
</script>

<template>
  <div data-testid="tool-detail-bash" class="min-w-0 space-y-1">
    <div class="flex min-w-0 items-center gap-1.5">
      <code class="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
        $ {{ command || 'bash' }}
      </code>
      <span
        v-if="exitCode != null"
        data-testid="tool-detail-bash-exit"
        class="shrink-0 rounded px-1 py-px font-mono text-[10px]"
        :class="failed ? 'bg-destructive/15 text-destructive' : 'bg-muted text-muted-foreground'"
      >
        exit {{ exitCode }}
      </span>
    </div>
    <pre
      v-if="preview"
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/80 px-2 py-1.5 font-mono text-[11px]"
      :class="failed ? 'text-destructive' : 'text-muted-foreground'"
    >{{ preview }}</pre>
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">Running…</p>
  </div>
</template>
