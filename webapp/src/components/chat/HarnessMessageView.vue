<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { GitFork, Pencil, User, ChevronDown } from '@lucide/vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { buildRenderBlocks } from '@/lib/harnessBlocks'
import { hasRunningToolOrSubtask } from '@/lib/harnessSubtaskActivity'
import { formatMessageHoverLine } from '@/lib/harnessUsage'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { formatHarnessModelEffort, type ProviderModel } from '@/lib/harnessModels'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Separator } from '@/components/ui/separator'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
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
  /** Hide edit/fork actions (busy run or subagent session). */
  disabled?: boolean
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
  edit: [messageId: string, text: string]
  fork: [messageId: string]
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

const streamingModelLine = computed(() => {
  if (!props.streaming || props.message.role !== 'assistant') return null
  const modelId = (props.message.model ?? '').trim()
  const effort = (props.message.reasoning_effort ?? '').trim()
  if (!modelId && !effort) return null
  return formatHarnessModelEffort(modelId, effort, props.models ?? catalog.value)
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
  const fromMap =
    props.childSessionIds?.[String(part.meta?.['subtask_id'] ?? '')] ??
    props.childSessionIds?.[part.id]
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

/** Inline edit state for user messages (OpenCode session.revert parity). */
const editing = ref(false)
const editDraft = ref('')

/** Local optimistic ids (`local-user-*`) have no backend row to edit/fork. */
const isEditableMessage = computed(
  () =>
    props.message.role === 'user' &&
    !props.disabled &&
    !props.message.id.startsWith('local-user-'),
)

watch(
  () => props.message.content,
  () => {
    if (!editing.value) editDraft.value = props.message.content
  },
)

function startEdit(): void {
  editDraft.value = props.message.content
  editing.value = true
}

function cancelEdit(): void {
  editing.value = false
  editDraft.value = props.message.content
}

function saveEdit(): void {
  const text = editDraft.value.trim()
  if (!text || text === props.message.content) {
    editing.value = false
    return
  }
  editing.value = false
  emit('edit', props.message.id, text)
}

function forkFromHere(): void {
  emit('fork', props.message.id)
}
</script>

<template>
  <!-- User message -->
  <div v-if="message.role === 'user'" class="group flex items-start gap-3 justify-end">
    <div class="min-w-0 max-w-3xl">
      <div
        v-if="!editing"
        class="overflow-x-auto rounded-[var(--radius-md)] rounded-br-sm bg-primary text-primary-foreground px-4 py-3 text-sm break-words"
      >
        <HarnessMarkdown :text="message.content" compact />
      </div>
      <div v-else class="flex flex-col gap-2">
        <Textarea
          v-model="editDraft"
          class="min-h-20 text-sm"
          data-testid="message-edit-input"
          @keydown.escape="cancelEdit"
          @keydown.enter.exact.prevent="saveEdit"
        />
        <div class="flex items-center justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="message-edit-cancel"
            @click="cancelEdit"
          >
            Cancel
          </Button>
          <Button
            type="button"
            size="sm"
            data-testid="message-edit-save"
            :disabled="!editDraft.trim() || editDraft.trim() === message.content"
            @click="saveEdit"
          >
            Save
          </Button>
        </div>
      </div>
      <div
        v-if="isEditableMessage && !editing"
        class="mt-1 flex items-center justify-end gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
      >
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger as-child>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                class="h-7 w-7 text-muted-foreground hover:text-foreground"
                data-testid="message-edit"
                aria-label="Edit message and rerun"
                @click="startEdit"
              >
                <Pencil :size="13" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top"> Edit &amp; rerun </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger as-child>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                class="h-7 w-7 text-muted-foreground hover:text-foreground"
                data-testid="message-fork"
                aria-label="Fork session from here"
                @click="forkFromHere"
              >
                <GitFork :size="13" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top"> Fork from here </TooltipContent>
          </Tooltip>
        </TooltipProvider>
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
              :models="models ?? catalog"
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
        v-if="streamingModelLine"
        data-testid="harness-message-running-model"
        class="mt-1 text-xs text-muted-foreground"
      >
        {{ streamingModelLine }}
      </p>
      <p
        v-if="usageLine"
        data-testid="harness-message-usage"
        class="mt-1 h-4 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
      >
        {{ usageLine }}
      </p>
    </div>
  </div>
</template>
