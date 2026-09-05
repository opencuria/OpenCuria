<script setup lang="ts">
import { computed, ref, type Component } from 'vue'
import {
  ChevronDown,
  FilePenLine,
  FileText,
  FolderOpen,
  Globe,
  Lightbulb,
  ListTodo,
  Monitor,
  MousePointer2,
  Network,
  Search,
  Terminal,
  Wrench,
} from '@lucide/vue'

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import type { HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'

const props = withDefaults(
  defineProps<{
    part: HarnessPart
    grouped?: boolean
  }>(),
  { grouped: false },
)

const open = ref(false)

const canExpand = computed(() => {
  if (!props.grouped) return true
  return props.part.type === 'reasoning' || props.part.state === 'error'
})

const toolName = computed(() => props.part.tool || props.part.title || 'tool')

const label = computed(() => {
  if (props.part.type === 'reasoning') return 'Thought'
  return props.part.title || toolName.value
})

const COMPUTER_USE_VIEW_TOOLS = new Set(['view_screen', 'view_region'])
const COMPUTER_USE_INPUT_TOOLS = new Set([
  'move_mouse',
  'left_click',
  'right_click',
  'middle_click',
  'double_click',
  'drag',
  'scroll',
  'type_text',
  'press_key',
  'wait',
  'ask_user',
])

const icon = computed<Component>(() => {
  if (props.part.type === 'reasoning') return Lightbulb
  const tool = (props.part.tool || '').trim().toLowerCase()
  switch (tool) {
    case 'bash':
      return Terminal
    case 'read':
      return FileText
    case 'write':
    case 'edit':
      return FilePenLine
    case 'glob':
    case 'grep':
      return Search
    case 'list':
      return FolderOpen
    case 'task':
      return Network
    case 'webfetch':
    case 'open_url':
      return Globe
    case 'todowrite':
      return ListTodo
    default:
      if (COMPUTER_USE_VIEW_TOOLS.has(tool)) return Monitor
      if (COMPUTER_USE_INPUT_TOOLS.has(tool)) return MousePointer2
      return Wrench
  }
})

const outputPreview = computed(() => {
  const output = props.part.output || ''
  if (output.length <= 2000) return output
  return `${output.slice(0, 2000)}\n…[truncated ${output.length} chars total]`
})

const tooltipPreview = computed(() => {
  const output = (props.part.output || '').replace(/\s+/g, ' ').trim()
  if (!output) return ''
  return output.length > 200 ? `${output.slice(0, 200)}…` : output
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
  if (canExpand.value) {
    classes.push('hover:text-foreground')
  }
  return classes.join(' ')
})
</script>

<template>
  <div
    data-testid="harness-work-row"
    :data-part-id="part.id"
    :data-part-type="part.type"
    :data-grouped="grouped ? '1' : '0'"
    :data-expandable="canExpand ? '1' : '0'"
    class="min-w-0"
  >
    <Collapsible v-if="canExpand" v-model:open="open">
      <CollapsibleTrigger :class="rowClass">
        <component :is="icon" :size="12" class="shrink-0 opacity-80" />
        <span data-testid="harness-work-row-label" class="min-w-0 flex-1 truncate">
          {{ label }}
        </span>
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
        <template v-else>
          <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {{ toolName }}
          </code>
          <pre
            v-if="outputPreview"
            class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-muted-foreground"
            :class="part.state === 'error' ? 'text-destructive' : ''"
          >{{ outputPreview }}</pre>
          <p
            v-else-if="part.state === 'running'"
            class="mt-1 flex items-center gap-1.5 text-[11px] text-muted-foreground"
          >
            <LoadingSpinner :size="10" />
            Running…
          </p>
        </template>
      </CollapsibleContent>
    </Collapsible>
    <TooltipProvider v-else>
      <Tooltip>
        <TooltipTrigger as-child>
          <div :class="rowClass">
            <component :is="icon" :size="12" class="shrink-0 opacity-80" />
            <span data-testid="harness-work-row-label" class="min-w-0 flex-1 truncate">
              {{ label }}
            </span>
            <LoadingSpinner v-if="part.state === 'running'" :size="10" class="shrink-0" />
          </div>
        </TooltipTrigger>
        <TooltipContent class="max-w-sm">
          <p class="font-normal">{{ label }}</p>
          <p v-if="tooltipPreview" class="mt-0.5 opacity-80">{{ tooltipPreview }}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  </div>
</template>
