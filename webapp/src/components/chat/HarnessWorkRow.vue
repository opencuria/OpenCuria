<script setup lang="ts">
import { computed, ref, watch, type Component } from 'vue'
import { useHarnessPartDetail } from '@/lib/harnessPartDetail'
import { ChevronRight } from '@lucide/vue'

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import type { HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'
import ToolDetailBash from './tools/ToolDetailBash.vue'
import ToolDetailComputerUse from './tools/ToolDetailComputerUse.vue'
import ToolDetailDefault from './tools/ToolDetailDefault.vue'
import ToolDetailImageGeneration from './tools/ToolDetailImageGeneration.vue'
import ToolDetailMemory from './tools/ToolDetailMemory.vue'
import ToolDetailRead from './tools/ToolDetailRead.vue'
import ToolDetailSearch from './tools/ToolDetailSearch.vue'
import ToolDetailTodos from './tools/ToolDetailTodos.vue'
import ToolDetailWebfetch from './tools/ToolDetailWebfetch.vue'
import { isComputerUseTool, resolveToolName, toolDisplayIcon, toolDisplayLabel } from '@/lib/toolDisplay'
import { toolRowLabel } from '@/lib/toolRowLabel'
import { cn } from '@/lib/utils'

const DETAIL_BY_TOOL: Record<string, Component> = {
  bash: ToolDetailBash,
  read: ToolDetailRead,
  glob: ToolDetailSearch,
  grep: ToolDetailSearch,
  list: ToolDetailSearch,
  webfetch: ToolDetailWebfetch,
  websearch: ToolDetailDefault,
  memory: ToolDetailMemory,
  image_generation: ToolDetailImageGeneration,
  tool_call: ToolDetailDefault,
  todowrite: ToolDetailTodos,
}

const props = withDefaults(
  defineProps<{
    part: HarnessPart
    grouped?: boolean
  }>(),
  { grouped: false },
)

const open = ref(false)

const partRef = computed(() => props.part)
const {
  loading: detailLoading,
  error: detailError,
  load: loadDetail,
  setExpanded,
} = useHarnessPartDetail(partRef)

watch(
  open,
  (expanded) => {
    setExpanded(expanded)
    if (expanded) void loadDetail()
  },
  { immediate: true },
)
watch(
  () => props.part.state,
  (state) => {
    if (open.value && (state === 'completed' || state === 'error')) void loadDetail()
  },
)

const label = computed(() => toolDisplayLabel(props.part))
const icon = computed<Component>(() => toolDisplayIcon(props.part))
const parts = computed(() => toolRowLabel(props.part))

const running = computed(() => props.part.state === 'running' || props.part.state === 'pending')
const failed = computed(() => props.part.state === 'error')

/** Colour classes drop out while shimmering so the label inherits the gradient. */
function tone(cls: string): string {
  if (running.value) return ''
  return failed.value ? 'text-destructive' : cls
}

const reasoningPreview = computed(() => {
  if (props.part.type !== 'reasoning') return ''
  if (props.part.display?.summary) return String(props.part.display.summary)
  const lines = (props.part.output || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  const lastLine = lines[lines.length - 1] ?? ''
  return lastLine.replace(/\s+/g, ' ')
})

const detailComponent = computed<Component>(() => {
  const tool = resolveToolName(props.part).toLowerCase()
  if (DETAIL_BY_TOOL[tool]) return DETAIL_BY_TOOL[tool]!
  if (isComputerUseTool(props.part)) return ToolDetailComputerUse
  return ToolDetailDefault
})

const rowClass = computed(() =>
  cn(
    'group/row -mx-1.5 flex w-[calc(100%+0.75rem)] min-w-0 items-center gap-2 rounded-md px-1.5 py-[3px] text-left text-xs font-normal transition-colors [@media(pointer:coarse)]:py-1.5',
    'hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none',
    failed.value ? 'text-destructive' : 'text-muted-foreground hover:text-foreground',
  ),
)
</script>

<template>
  <div
    data-testid="harness-work-row"
    :data-part-id="part.id"
    :data-part-type="part.type"
    :data-grouped="grouped ? '1' : '0'"
    :data-state="part.state"
    data-expandable="1"
    class="min-w-0"
  >
    <Collapsible v-model:open="open">
      <CollapsibleTrigger :class="rowClass" :title="label" :aria-busy="running || undefined">
        <component
          :is="icon"
          :size="13"
          class="shrink-0 transition-opacity"
          :class="failed ? 'opacity-90' : 'opacity-60 group-hover/row:opacity-100'"
        />
        <span
          data-testid="harness-work-row-label"
          class="min-w-0 truncate"
          :class="running ? 'harness-shimmer' : ''"
        >
          <span :class="tone('text-muted-foreground')">{{ parts.verb }}</span
          ><template v-if="parts.object">
            {{ ' ' }}<span :class="tone('text-foreground/90')">{{ parts.object }}</span></template
          >
        </span>
        <span
          v-if="parts.hint"
          data-testid="harness-work-row-hint"
          class="min-w-0 shrink-[2] truncate text-muted-foreground/60"
        >
          {{ parts.hint }}
        </span>
        <span
          v-if="!open && reasoningPreview"
          data-testid="harness-work-row-preview"
          class="min-w-0 flex-1 truncate text-muted-foreground/70"
        >
          {{ reasoningPreview }}
        </span>
        <ChevronRight
          :size="12"
          class="shrink-0 opacity-0 transition-[opacity,transform] group-hover/row:opacity-70 group-focus-visible/row:opacity-70 group-data-[state=open]/row:rotate-90 group-data-[state=open]/row:opacity-70 [@media(hover:none)]:opacity-50"
        />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div class="ml-1.5 border-l border-border/70 pb-1.5 pl-3.5 pt-0.5">
          <div
            v-if="detailLoading"
            data-testid="harness-part-detail-loading"
            class="py-1 text-xs text-muted-foreground"
          >
            <LoadingSpinner :size="12" /> Loading details…
          </div>
          <div v-else-if="detailError" class="py-1 text-xs text-destructive">
            <p>{{ detailError }}</p>
            <button type="button" class="mt-1 underline" @click="loadDetail">Retry</button>
          </div>
          <template v-else>
            <HarnessMarkdown v-if="part.type === 'reasoning'" :text="part.output" compact />
            <component :is="detailComponent" v-else :part="part" />
          </template>
        </div>
      </CollapsibleContent>
    </Collapsible>
  </div>
</template>
