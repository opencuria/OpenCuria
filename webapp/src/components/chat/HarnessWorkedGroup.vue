<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { ChevronDown } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import { countWorkItems, isWorkItem } from '@/lib/harnessBlocks'
import HarnessWorkRow from './HarnessWorkRow.vue'
import HarnessStepFinish from './HarnessStepFinish.vue'

const props = defineProps<{
  parts: HarnessPart[]
}>()

const userOverride = ref<boolean | null>(null)

const isRunning = computed(() => props.parts.some((part) => part.state === 'running'))

const open = computed({
  get: () => userOverride.value ?? isRunning.value,
  set: (value: boolean) => {
    userOverride.value = value
  },
})

const workCount = computed(() => countWorkItems(props.parts))
</script>

<template>
  <Collapsible
    v-model:open="open"
    data-testid="harness-worked-group"
    class="min-w-0"
  >
    <CollapsibleTrigger
      class="flex w-full items-center gap-1.5 py-0.5 text-left text-xs font-normal text-muted-foreground hover:text-foreground"
    >
      <ChevronDown
        :size="12"
        class="shrink-0 opacity-70 transition-transform"
        :class="open ? '' : '-rotate-90'"
      />
      <span>Worked</span>
      <span data-testid="harness-worked-count" class="opacity-70">{{ workCount }}</span>
    </CollapsibleTrigger>
    <CollapsibleContent class="pl-4">
      <template v-for="part in parts" :key="part.id">
        <HarnessWorkRow
          v-if="isWorkItem(part)"
          :part="part"
          grouped
        />
        <div v-else-if="part.type === 'step-finish'" class="py-0.5">
          <HarnessStepFinish :part="part" />
        </div>
      </template>
    </CollapsibleContent>
  </Collapsible>
</template>
