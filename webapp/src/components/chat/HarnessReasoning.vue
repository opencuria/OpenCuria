<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Badge } from '@/components/ui/badge'
import { Brain, ChevronDown } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'

const props = defineProps<{
  part: HarnessPart
}>()

const open = ref(false)

const preview = computed(() => {
  const firstLine = (props.part.output || '').split('\n')[0] ?? ''
  return firstLine.length > 120 ? `${firstLine.slice(0, 120)}…` : firstLine
})
</script>

<template>
  <Collapsible v-model:open="open" class="w-full max-w-3xl rounded-xl border border-border bg-muted/40">
    <CollapsibleTrigger class="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-muted-foreground">
      <Brain :size="14" class="shrink-0" />
      <span class="font-medium">Reasoning</span>
      <span v-if="!open && preview" class="min-w-0 flex-1 truncate">{{ preview }}</span>
      <Badge variant="outline" class="ml-auto shrink-0">{{ part.state }}</Badge>
      <ChevronDown
        :size="14"
        class="shrink-0 transition-transform"
        :class="open ? 'rotate-180' : ''"
      />
    </CollapsibleTrigger>
    <CollapsibleContent class="px-3 pb-3">
      <HarnessMarkdown :text="part.output" compact />
    </CollapsibleContent>
  </Collapsible>
</template>
