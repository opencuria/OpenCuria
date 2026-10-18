<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { HarnessPart } from '@/types/harness'
import { countWorkItems, isWorkItem } from '@/lib/harnessBlocks'
import { ChevronRight, Layers } from '@lucide/vue'
import { toolDisplayLabel } from '@/lib/toolDisplay'
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
</script>

<template>
  <Collapsible v-model:open="open" data-testid="harness-worked-group" class="min-w-0">
    <CollapsibleTrigger
      class="group/row -mx-1.5 flex w-[calc(100%+0.75rem)] min-w-0 items-center gap-2 rounded-md px-1.5 py-[3px] text-left text-xs font-normal [@media(pointer:coarse)]:py-1.5 text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:bg-muted/60 focus-visible:outline-none"
      :aria-busy="isRunning || undefined"
    >
      <Layers
        :size="13"
        class="shrink-0 opacity-60 transition-opacity group-hover/row:opacity-100"
      />
      <span>Worked</span>
      <span
        data-testid="harness-worked-count"
        class="rounded-full bg-muted px-1.5 text-[10px] leading-4 tabular-nums text-muted-foreground group-hover/row:bg-background"
        >{{ workCount }}</span
      >
      <template v-if="isRunning">
        <span
          v-if="liveTitle"
          data-testid="harness-worked-live"
          class="harness-shimmer min-w-0 truncate"
        >
          {{ liveTitle }}
        </span>
        <span
          v-if="runningCount > 1"
          data-testid="harness-worked-running"
          class="shrink-0 text-muted-foreground/70"
        >
          {{ runningCount }} running
        </span>
      </template>
      <ChevronRight
        :size="12"
        class="shrink-0 opacity-0 transition-[opacity,transform] group-hover/row:opacity-70 group-focus-visible/row:opacity-70 group-data-[state=open]/row:rotate-90 group-data-[state=open]/row:opacity-70 [@media(hover:none)]:opacity-50"
      />
    </CollapsibleTrigger>
    <CollapsibleContent>
      <div class="ml-1.5 border-l border-border/70 pb-1 pl-3 pt-0.5">
        <HarnessPageControls
          :page="page"
          :total-items="workCount"
          label="Worked items pages"
          @previous="page = Math.max(0, page - 1)"
          @next="page = Math.min(Math.ceil(workCount / HARNESS_PAGE_SIZE) - 1, page + 1)"
        />
        <HarnessWorkRow v-for="part in visibleParts" :key="part.id" :part="part" grouped />
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
