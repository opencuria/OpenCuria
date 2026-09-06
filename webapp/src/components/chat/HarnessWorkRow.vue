<script setup lang="ts">
import { computed, ref, watch, type Component } from 'vue'
import { ChevronDown } from '@lucide/vue'

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import type { HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'
import ToolDetailBash from './tools/ToolDetailBash.vue'
import ToolDetailComputerUse from './tools/ToolDetailComputerUse.vue'
import ToolDetailDefault from './tools/ToolDetailDefault.vue'
import ToolDetailQuestion from './tools/ToolDetailQuestion.vue'
import ToolDetailRead from './tools/ToolDetailRead.vue'
import ToolDetailSearch from './tools/ToolDetailSearch.vue'
import ToolDetailTodos from './tools/ToolDetailTodos.vue'
import ToolDetailWebfetch from './tools/ToolDetailWebfetch.vue'
import {
  resolveToolName,
  toolDisplayIcon,
  toolDisplayLabel,
} from '@/lib/toolDisplay'

const COMPUTER_USE_DETAIL_TOOLS = new Set([
  'view_screen',
  'view_region',
  'move_mouse',
  'left_click',
  'right_click',
  'middle_click',
  'double_click',
  'drag',
  'scroll',
  'type_text',
  'press_key',
  'open_url',
  'wait',
])

const DETAIL_BY_TOOL: Record<string, Component> = {
  bash: ToolDetailBash,
  read: ToolDetailRead,
  glob: ToolDetailSearch,
  grep: ToolDetailSearch,
  list: ToolDetailSearch,
  webfetch: ToolDetailWebfetch,
  todowrite: ToolDetailTodos,
  question: ToolDetailQuestion,
  ask_user: ToolDetailQuestion,
}

const props = withDefaults(
  defineProps<{
    part: HarnessPart
    grouped?: boolean
  }>(),
  { grouped: false },
)

const open = ref(props.part.state === 'error')

watch(
  () => props.part.state,
  (state) => {
    if (state === 'error') open.value = true
  },
)

const label = computed(() => toolDisplayLabel(props.part))
const icon = computed<Component>(() => toolDisplayIcon(props.part))

const reasoningPreview = computed(() => {
  if (props.part.type !== 'reasoning') return ''
  return (props.part.output || '').replace(/\s+/g, ' ').trim()
})

const detailComponent = computed<Component>(() => {
  const tool = resolveToolName(props.part).toLowerCase()
  if (DETAIL_BY_TOOL[tool]) return DETAIL_BY_TOOL[tool]!
  if (COMPUTER_USE_DETAIL_TOOLS.has(tool)) return ToolDetailComputerUse
  return ToolDetailDefault
})

const rowClass = computed(() => {
  const classes = [
    'flex w-full min-w-0 items-center gap-1.5 py-0.5 text-left text-xs font-normal',
  ]
  if (props.part.state === 'error') {
    classes.push('text-destructive')
  } else {
    classes.push('text-muted-foreground')
  }
  classes.push('hover:text-foreground')
  return classes.join(' ')
})
</script>

<template>
  <div
    data-testid="harness-work-row"
    :data-part-id="part.id"
    :data-part-type="part.type"
    :data-grouped="grouped ? '1' : '0'"
    data-expandable="1"
    class="min-w-0"
  >
    <Collapsible v-model:open="open">
      <CollapsibleTrigger :class="rowClass">
        <component :is="icon" :size="12" class="shrink-0 opacity-80" />
        <span data-testid="harness-work-row-label" class="min-w-0 shrink-0 truncate">
          {{ label }}
        </span>
        <span
          v-if="!open && reasoningPreview"
          data-testid="harness-work-row-preview"
          class="min-w-0 flex-1 truncate opacity-70"
        >
          {{ reasoningPreview }}
        </span>
        <span v-else class="min-w-0 flex-1" />
        <LoadingSpinner v-if="part.state === 'running'" :size="10" class="shrink-0" />
        <ChevronDown
          :size="12"
          class="shrink-0 opacity-70 transition-transform"
          :class="open ? 'rotate-180' : ''"
        />
      </CollapsibleTrigger>
      <CollapsibleContent class="pb-1 pl-[18px]">
        <HarnessMarkdown
          v-if="part.type === 'reasoning'"
          :text="part.output"
          compact
        />
        <component :is="detailComponent" v-else :part="part" />
      </CollapsibleContent>
    </Collapsible>
  </div>
</template>
