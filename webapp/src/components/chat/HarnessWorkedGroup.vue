<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { HarnessPart } from '@/types/harness'
import { countWorkItems, isWorkItem } from '@/lib/harnessBlocks'
import { toolDisplayLabel } from '@/lib/toolDisplay'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import HarnessWorkRow from './HarnessWorkRow.vue'
import HarnessPageControls from './HarnessPageControls.vue'
import { HARNESS_PAGE_SIZE, lastHarnessPage, shouldResetHarnessPage } from '@/lib/harnessPagination'

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
const page = ref(0)
const workParts = computed(() => props.parts.filter(isWorkItem))
const visibleParts = computed(() =>
  workParts.value.slice(page.value * HARNESS_PAGE_SIZE, (page.value + 1) * HARNESS_PAGE_SIZE),
)

const workIdentities = computed(() => workParts.value.map((part) => `${part.type}:${part.id}`))
watch(workIdentities, (next, previous) => {
  if (shouldResetHarnessPage(previous, next)) page.value = 0
  else page.value = Math.min(page.value, lastHarnessPage(next.length))
})
watch(
  () => props.parts.map((part) => `${part.id}:${part.state}`).join('|'),
  () => {
    if (isRunning.value) page.value = lastHarnessPage(workCount.value)
  },
)
watch(
  () => props.parts.map((part) => `${part.id}:${part.state}`).join('|'),
  () => {
    if (isRunning.value) page.value = lastHarnessPage(workCount.value)
  },
)
</script>

<template>
  <Collapsible v-model:open="open" data-testid="harness-worked-group" class="min-w-0">
    <CollapsibleTrigger
      class="flex w-full min-w-0 items-center gap-1.5 py-0.5 text-left text-xs font-normal text-muted-foreground hover:text-foreground"
    >
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
      <HarnessPageControls
        :page="page"
        :total-items="workCount"
        label="Worked items pages"
        @previous="page = Math.max(0, page - 1)"
        @next="page = Math.min(Math.ceil(workCount / HARNESS_PAGE_SIZE) - 1, page + 1)"
      />
      <HarnessWorkRow v-for="part in visibleParts" :key="part.id" :part="part" grouped />
    </CollapsibleContent>
  </Collapsible>
</template>
