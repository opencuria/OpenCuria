<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { User, ChevronDown } from '@lucide/vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { buildRenderBlocks } from '@/lib/harnessBlocks'
import { hasRunningToolOrSubtask } from '@/lib/harnessSubtaskActivity'
import { formatMessageHoverLine } from '@/lib/harnessUsage'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import type { ProviderModel } from '@/lib/harnessModels'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Separator } from '@/components/ui/separator'
import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessWorkRow from './HarnessWorkRow.vue'
import HarnessWorkedGroup from './HarnessWorkedGroup.vue'
import HarnessSubtaskCard from './HarnessSubtaskCard.vue'
import HarnessPatchCard from './HarnessPatchCard.vue'
import HarnessThinking from './HarnessThinking.vue'

const props = defineProps<{
  message: HarnessMessage
  streaming?: boolean
  childSessionIds?: Record<string, string>
  models?: ProviderModel[]
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
}>()

const blocks = computed(() => buildRenderBlocks(props.message.parts))

const lastBlockIsText = computed(() => {
  const last = blocks.value[blocks.value.length - 1]
  return last?.kind === 'text'
})

/** Thinking only in idle gaps: busy turn, no live tool/subtask. */
const showThinking = computed(
  () => props.streaming === true && !hasRunningToolOrSubtask(props.message.parts),
)

const catalog = ref<ProviderModel[]>(props.models ?? [])

const usageLine = computed(() => {
  if (props.streaming || props.message.role !== 'assistant') return null
  return formatMessageHoverLine(props.message, props.models ?? catalog.value)
})

onMounted(async () => {
  if (props.models) return
  try {
    catalog.value = await loadProviderModelsCached()
  } catch {
    catalog.value = []
  }
})

function childIdFor(part: HarnessPart): string | null {
  const fromMeta = part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  const fromMap = props.childSessionIds?.[String(part.meta?.['subtask_id'] ?? '')]
  return fromMap ?? null
}

function blockKey(index: number): string {
  const block = blocks.value[index]
  if (!block) return `block-${index}`
  if (block.kind === 'group') {
    return `group-${block.parts[0]?.id ?? index}`
  }
  if (block.kind === 'compaction') {
    return `compaction-${block.part.id}`
  }
  return `${block.kind}-${block.part.id}`
}

const compactionOpen = ref<Record<string, boolean>>({})

function isCompactionOpen(partId: string): boolean {
  return compactionOpen.value[partId] === true
}

function setCompactionOpen(partId: string, open: boolean): void {
  compactionOpen.value = { ...compactionOpen.value, [partId]: open }
}
</script>

<template>
  <!-- User message -->
  <div v-if="message.role === 'user'" class="flex items-start gap-3 justify-end">
    <div class="min-w-0 max-w-3xl">
      <div
        class="overflow-x-auto rounded-[var(--radius-md)] rounded-br-sm bg-primary text-primary-foreground px-4 py-3 text-sm break-words"
      >
        <HarnessMarkdown :text="message.content" compact />
      </div>
    </div>
    <div class="flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary shrink-0">
      <User :size="14" />
    </div>
  </div>

  <!-- Assistant message: chronological blocks in left prose shell -->
  <div v-else class="group flex items-start gap-3">
    <div class="min-w-0 flex-1 max-w-3xl py-2 text-sm text-foreground">
      <div v-if="blocks.length" class="flex flex-col gap-2">
        <template v-for="(block, index) in blocks" :key="blockKey(index)">
          <div
            v-if="block.kind === 'text'"
            :data-block-kind="'text'"
            :data-part-id="block.part.id"
          >
            <HarnessMarkdown :text="block.part.output" />
            <span
              v-if="streaming && lastBlockIsText && index === blocks.length - 1"
              class="ml-0.5 inline-block h-4 w-2 animate-pulse bg-primary/60 align-middle"
            />
          </div>
          <div
            v-else-if="block.kind === 'single'"
            :data-block-kind="'single'"
            :data-part-id="block.part.id"
          >
            <HarnessWorkRow :part="block.part" />
          </div>
          <div
            v-else-if="block.kind === 'group'"
            :data-block-kind="'group'"
          >
            <HarnessWorkedGroup :parts="block.parts" />
          </div>
          <div
            v-else-if="block.kind === 'card'"
            :data-block-kind="'card'"
            :data-part-id="block.part.id"
          >
            <HarnessSubtaskCard
              v-if="block.part.type === 'subtask'"
              :part="block.part"
              :child-session-id="childIdFor(block.part)"
              @open-subtask="emit('openSubtask', $event)"
            />
            <HarnessPatchCard v-else-if="block.part.type === 'patch'" :part="block.part" />
            <div
              v-else
              class="w-full overflow-x-auto rounded-xl border border-border bg-card px-3 py-2"
            >
              <p class="text-xs font-medium text-muted-foreground">
                {{ block.part.title || block.part.type }}
              </p>
              <pre class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-muted-foreground">{{ block.part.output }}</pre>
            </div>
          </div>
          <div
            v-else-if="block.kind === 'compaction'"
            :data-block-kind="'compaction'"
            :data-part-id="block.part.id"
            data-testid="harness-compaction-divider"
            class="py-1"
          >
            <Collapsible
              :open="isCompactionOpen(block.part.id)"
              class="min-w-0"
              @update:open="setCompactionOpen(block.part.id, $event)"
            >
              <div class="flex items-center gap-2">
                <Separator class="flex-1" />
                <CollapsibleTrigger
                  class="flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                >
                  <ChevronDown
                    :size="12"
                    class="shrink-0 opacity-70 transition-transform"
                    :class="isCompactionOpen(block.part.id) ? '' : '-rotate-90'"
                  />
                  <span>Session compacted</span>
                </CollapsibleTrigger>
                <Separator class="flex-1" />
              </div>
              <CollapsibleContent class="pt-2">
                <div
                  class="max-h-48 overflow-auto rounded-md border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
                >
                  <HarnessMarkdown :text="block.part.output" compact />
                </div>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </template>
      </div>
      <HarnessThinking v-if="showThinking" :class="blocks.length ? 'mt-2' : ''" />
      <p
        v-if="usageLine"
        data-testid="harness-message-usage"
        class="mt-1 h-4 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
      >
        {{ usageLine }}
      </p>
      <p v-if="message.error" class="mt-2 text-xs text-destructive">{{ message.error }}</p>
    </div>
  </div>
</template>
