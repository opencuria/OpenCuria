<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Badge } from '@/components/ui/badge'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { ChevronDown, CircleAlert, CircleCheck, Wrench } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  part: HarnessPart
}>()

const open = ref(false)

const badgeVariant = computed(() => {
  switch (props.part.state) {
    case 'completed':
      return 'secondary'
    case 'error':
      return 'destructive'
    case 'running':
      return 'default'
    default:
      return 'outline'
  }
})

const toolName = computed(() => props.part.tool || props.part.title || 'tool')

const outputPreview = computed(() => {
  const output = props.part.output || ''
  if (output.length <= 2000) return output
  return `${output.slice(0, 2000)}\n…[truncated ${output.length} chars total]`
})
</script>

<template>
  <Collapsible v-model:open="open" class="w-full rounded-xl border border-border bg-card">
    <CollapsibleTrigger
      class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
      :class="part.state === 'pending' ? 'opacity-60' : ''"
    >
      <Wrench :size="14" class="shrink-0 text-muted-foreground" />
      <span class="min-w-0 flex-1 truncate font-medium text-foreground">
        {{ part.title || toolName }}
      </span>
      <Badge :variant="badgeVariant" class="shrink-0">
        <LoadingSpinner v-if="part.state === 'running'" :size="10" />
        {{ part.state }}
      </Badge>
      <ChevronDown
        :size="14"
        class="shrink-0 text-muted-foreground transition-transform"
        :class="open ? 'rotate-180' : ''"
      />
    </CollapsibleTrigger>
    <CollapsibleContent class="px-3 pb-3">
      <div class="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Tooltip>
          <TooltipTrigger as-child>
            <code class="truncate rounded bg-muted px-1.5 py-0.5 font-mono">{{ toolName }}</code>
          </TooltipTrigger>
          <TooltipContent>
            <span class="font-mono">{{ toolName }}</span>
            <span v-if="part.call_id" class="ml-1 opacity-70">{{ part.call_id }}</span>
          </TooltipContent>
        </Tooltip>
        <span v-if="part.meta && typeof part.meta['step'] !== 'undefined'" class="shrink-0">
          step {{ String(part.meta['step']) }}
        </span>
      </div>
      <pre
        v-if="outputPreview"
        class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-muted-foreground"
        :class="part.state === 'error' ? 'text-destructive' : ''"
      >{{ outputPreview }}</pre>
      <p
        v-else-if="part.state === 'running'"
        class="mt-2 flex items-center gap-2 text-xs text-muted-foreground"
      >
        <LoadingSpinner :size="12" />
        Running…
      </p>
      <p v-else class="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
        <CircleCheck v-if="part.state === 'completed'" :size="12" />
        <CircleAlert v-else-if="part.state === 'error'" :size="12" />
        {{ part.state === 'completed' ? 'Completed with no output.' : `State: ${part.state}` }}
      </p>
    </CollapsibleContent>
  </Collapsible>
</template>
