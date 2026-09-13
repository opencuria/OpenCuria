<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'

const props = defineProps<{
  elapsedLabel: string
}>()

const open = ref(false)

const label = computed(() =>
  props.elapsedLabel ? `Worked for ${props.elapsedLabel}` : 'Worked',
)
</script>

<template>
  <Collapsible
    v-model:open="open"
    data-testid="harness-worked-for"
    class="min-w-0"
  >
    <CollapsibleTrigger
      class="flex w-full min-w-0 items-center py-0.5 text-left text-xs font-semibold text-muted-foreground hover:text-foreground"
    >
      <span data-testid="harness-worked-for-label">{{ label }}</span>
    </CollapsibleTrigger>
    <CollapsibleContent class="overflow-hidden data-[state=closed]:hidden">
      <div class="pt-1">
        <slot />
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
