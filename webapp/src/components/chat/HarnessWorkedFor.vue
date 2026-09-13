<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown } from '@lucide/vue'
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
      class="flex w-full min-w-0 items-center gap-1.5 py-0.5 text-left text-xs font-normal text-muted-foreground hover:text-foreground"
    >
      <ChevronDown
        :size="12"
        class="shrink-0 opacity-70 transition-transform"
        :class="open ? '' : '-rotate-90'"
      />
      <span data-testid="harness-worked-for-label">{{ label }}</span>
    </CollapsibleTrigger>
    <CollapsibleContent class="pt-1">
      <slot />
    </CollapsibleContent>
  </Collapsible>
</template>
