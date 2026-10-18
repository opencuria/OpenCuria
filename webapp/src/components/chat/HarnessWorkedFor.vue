<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronRight } from '@lucide/vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'

const props = defineProps<{
  elapsedLabel: string
}>()

const open = ref(false)

const label = computed(() => (props.elapsedLabel ? `Worked for ${props.elapsedLabel}` : 'Worked'))
</script>

<template>
  <Collapsible v-model:open="open" data-testid="harness-worked-for" class="min-w-0">
    <CollapsibleTrigger
      class="group/worked -mx-1.5 flex w-[calc(100%+0.75rem)] min-w-0 items-center gap-1.5 rounded-md px-1.5 py-[3px] text-left text-xs font-medium [@media(pointer:coarse)]:py-1.5 text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:bg-muted/60 focus-visible:outline-none"
    >
      <span data-testid="harness-worked-for-label">{{ label }}</span>
      <ChevronRight
        :size="12"
        class="shrink-0 opacity-0 transition-[opacity,transform] group-hover/worked:opacity-70 group-focus-visible/worked:opacity-70 group-data-[state=open]/worked:rotate-90 group-data-[state=open]/worked:opacity-70 [@media(hover:none)]:opacity-50"
      />
    </CollapsibleTrigger>
    <CollapsibleContent class="-mx-1.5 overflow-hidden px-1.5 data-[state=closed]:hidden">
      <div class="pt-1.5">
        <slot />
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
