<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { GitFork, Pencil, User } from '@lucide/vue'
import type { HarnessMessage } from '@/types/harness'
import {
  buildMessageBlocks,
  type MessageRenderBlock,
  type RenderBlock,
} from '@/lib/harnessBlocks'
import { hasRunningToolOrSubtask } from '@/lib/harnessSubtaskActivity'
import { formatMessageHoverLine } from '@/lib/harnessUsage'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { formatHarnessModelEffort, type ProviderModel } from '@/lib/harnessModels'
import { elapsedMs, formatElapsed } from '@/lib/formatElapsed'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessThinking from './HarnessThinking.vue'
import HarnessBlockList from './HarnessBlockList.vue'
import HarnessWorkedFor from './HarnessWorkedFor.vue'

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

const finished = computed(() => props.streaming !== true)

const blocks = computed(() =>
  buildMessageBlocks(props.message.parts, { finished: finished.value }),
)

const elapsedLabel = computed(() => {
  const ms = elapsedMs(props.message.created_at, props.message.completed_at)
  return ms == null ? '' : formatElapsed(ms)
})

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

function blockKey(index: number): string {
  const block = blocks.value[index]
  if (!block) return `block-${index}`
  if (block.kind === 'group') {
    return `group-${block.parts[0]?.id ?? index}`
  }
  if (block.kind === 'workedFor') {
    const first = block.blocks[0]
    if (first?.kind === 'group') return `workedFor-${first.parts[0]?.id ?? index}`
    if (first && first.kind !== 'group') return `workedFor-${first.part.id}`
    return `workedFor-${index}`
  }
  if (block.kind === 'compaction') {
    return `compaction-${block.part.id}`
  }
  return `${block.kind}-${block.part.id}`
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

function asRenderBlocks(block: MessageRenderBlock): RenderBlock[] {
  return block.kind === 'workedFor' ? [] : [block]
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
            v-if="block.kind === 'workedFor'"
            data-block-kind="workedFor"
          >
            <HarnessWorkedFor :elapsed-label="elapsedLabel">
              <HarnessBlockList
                :blocks="block.blocks"
                :child-session-ids="childSessionIds"
                :models="models ?? catalog"
                @open-subtask="emit('openSubtask', $event)"
              />
            </HarnessWorkedFor>
          </div>
          <HarnessBlockList
            v-else
            :blocks="asRenderBlocks(block)"
            :show-streaming-cursor="Boolean(streaming && lastBlockIsText && index === blocks.length - 1)"
            :child-session-ids="childSessionIds"
            :models="models ?? catalog"
            @open-subtask="emit('openSubtask', $event)"
          />
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
