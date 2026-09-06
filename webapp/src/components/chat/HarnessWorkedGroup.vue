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
import { toolDisplayLabel } from '@/lib/toolDisplay'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import HarnessWorkRow from './HarnessWorkRow.vue'

const props = defineProps<{
  parts: HarnessPart[]
}>()

const userOverride = ref<boolean | null>(null)

const isRunning = computed(() => props.parts.some((part) => part.state === 'running'))

const open = computed({
  get: () => userOverride.value ?? false,
  set: (value: boolean) => {
    userOverride.value = value
  },
})

const runningParts = computed(() => props.parts.filter((part) => part.state === 'running'))

const runningCount = computed(() => runningParts.value.length)

const liveTitle = computed(() => {
  const latest = [...runningParts.value].reverse()[0]
  return latest ? toolDisplayLabel(latest) : ''
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
      class="flex w-full min-w-0 items-center gap-1.5 py-0.5 text-left text-xs font-normal text-muted-foreground hover:text-foreground"
    >
      <ChevronDown
        :size="12"
        class="shrink-0 opacity-70 transition-transform"
        :class="open ? '' : '-rotate-90'"
      />
      <span>Worked</span>
      <span data-testid="harness-worked-count" class="opacity-70">{{ workCount }}</span>
      <template v-if="isRunning">
        <LoadingSpinner :size="10" class="shrink-0" />
        <span
          v-if="liveTitle"
          data-testid="harness-worked-live"
          class="min-w-0 truncate opacity-80"
        >
          {{ liveTitle }}
        </span>
        <span
          v-if="runningCount > 1"
          data-testid="harness-worked-running"
          class="shrink-0 opacity-70"
        >
          {{ runningCount }} running
        </span>
      </template>
    </CollapsibleTrigger>
    <CollapsibleContent class="pl-4">
      <template v-for="part in parts" :key="part.id">
        <HarnessWorkRow
          v-if="isWorkItem(part)"
          :part="part"
          grouped
        />
      </template>
    </CollapsibleContent>
  </Collapsible>
</template>
