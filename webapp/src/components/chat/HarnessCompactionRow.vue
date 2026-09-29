<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Separator } from '@/components/ui/separator'
import type { HarnessPart } from '@/types/harness'
import { useHarnessPartDetail } from '@/lib/harnessPartDetail'
import HarnessMarkdown from './HarnessMarkdown.vue'

const props = defineProps<{ part: HarnessPart }>()
const open = ref(false)
const partRef = computed(() => props.part)
const { loading, error, load, setExpanded } = useHarnessPartDetail(partRef)
watch(open, (expanded) => {
  setExpanded(expanded)
  if (expanded) void load()
})
</script>

<template>
  <div data-testid="harness-compaction-details">
    <Collapsible v-model:open="open" class="min-w-0">
      <div class="flex items-center gap-2">
        <Separator class="flex-1" />
        <CollapsibleTrigger
          class="flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <span data-testid="harness-compaction-divider">Session compacted</span>
        </CollapsibleTrigger>
        <Separator class="flex-1" />
      </div>
      <CollapsibleContent class="pt-2">
        <p
          v-if="loading"
          data-testid="harness-part-detail-loading"
          class="text-xs text-muted-foreground"
        >
          Loading summary…
        </p>
        <div v-else-if="error" class="text-xs text-destructive">
          <p>{{ error }}</p>
          <button type="button" class="underline" @click="load">Retry</button>
        </div>
        <div
          v-else
          class="max-h-48 overflow-auto rounded-md border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
        >
          <HarnessMarkdown :text="part.output || String(part.display?.summary ?? '')" compact />
        </div>
      </CollapsibleContent>
    </Collapsible>
  </div>
</template>
