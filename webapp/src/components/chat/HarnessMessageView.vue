<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { pinHarnessMessageKey } from '@/lib/harnessMessagePin'
import { BookText, CornerDownRight } from '@lucide/vue'
import type { HarnessMessage } from '@/types/harness'
import { buildMessageBlocks, type MessageRenderBlock, type RenderBlock } from '@/lib/harnessBlocks'
import { getMessageCopyText } from '@/lib/harnessMessageCopy'
import { hasRunningToolOrSubtask } from '@/lib/harnessSubtaskActivity'
import { buildAgentStepViews, hasRunningAgentSequence } from '@/lib/harnessAgentSteps'
import { formatMessageHoverLine } from '@/lib/harnessUsage'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { formatHarnessModelEffort, type ProviderModel } from '@/lib/harnessModels'
import { elapsedMs, formatElapsed } from '@/lib/formatElapsed'
import { useSkillStore } from '@/stores/skills'
import HarnessUserBubble from './HarnessUserBubble.vue'
import HarnessMessageEditCard from './HarnessMessageEditCard.vue'
import HarnessAgentStep from './HarnessAgentStep.vue'
import HarnessThinking from './HarnessThinking.vue'
import HarnessBlockList from './HarnessBlockList.vue'
import HarnessWorkedFor from './HarnessWorkedFor.vue'
import HarnessPageControls from './HarnessPageControls.vue'
import HarnessMessageFooter from './HarnessMessageFooter.vue'
import { HARNESS_PAGE_SIZE, lastHarnessPage, shouldResetHarnessPage } from '@/lib/harnessPagination'

const props = defineProps<{
  message: HarnessMessage
  streaming?: boolean
  childSessionIds?: Record<string, string>
  models?: ProviderModel[]
  /** Brief user-goal recap, shown quietly under the final answer. */
  summary?: string
  /** Hide edit/fork actions (read-only workspace or subagent session). */
  disabled?: boolean
  /** The session is running: fork is hidden, edit reruns after a stop confirmation. */
  busy?: boolean
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
  edit: [messageId: string, text: string, selection: { model: string; effort: string }]
  fork: [messageId: string]
}>()

const pinMessage = inject(pinHarnessMessageKey, null)
const messageRoot = ref<HTMLElement | null>(null)
function onMessageFocusIn(): void {
  pinMessage?.(props.message.id, true)
}
function onMessageFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget as Node | null
  if (!messageRoot.value?.contains(next)) pinMessage?.(props.message.id, false)
}
onUnmounted(() => pinMessage?.(props.message.id, false))

const finished = computed(() => props.streaming !== true)
const copyText = computed(() => getMessageCopyText(props.message, props.streaming === true))

const blocks = computed(() => buildMessageBlocks(props.message.parts, { finished: finished.value }))
const blockPage = ref(props.streaming ? lastHarnessPage(blocks.value.length) : 0)
const followingTail = ref(props.streaming === true)
const visibleEntries = computed(() => {
  const start = blockPage.value * HARNESS_PAGE_SIZE
  const end = (blockPage.value + 1) * HARNESS_PAGE_SIZE
  const entries = blocks.value
    .slice(start, end)
    .map((block, index) => ({ block, index: start + index }))
  const cursorIndex = streamingCursor.value?.topIndex
  if (cursorIndex !== undefined && (cursorIndex < start || cursorIndex >= end)) {
    const cursorBlock = blocks.value[cursorIndex]
    if (cursorBlock) {
      // Keep the live answer attached to its cursor without exceeding one page.
      if (entries.length === HARNESS_PAGE_SIZE) entries.pop()
      entries.push({ block: cursorBlock, index: cursorIndex })
      entries.sort((left, right) => left.index - right.index)
    }
  }
  return entries
})
const visibleBlockIndexes = computed(() => new Set(visibleEntries.value.map(({ index }) => index)))

function previousBlockPage(): void {
  blockPage.value = Math.max(0, blockPage.value - 1)
  followingTail.value = false
}

function nextBlockPage(): void {
  blockPage.value = Math.min(lastHarnessPage(blocks.value.length), blockPage.value + 1)
  followingTail.value = blockPage.value === lastHarnessPage(blocks.value.length)
}
function blockIdentity(block: MessageRenderBlock): string {
  if (block.kind === 'workedFor') {
    const parts = block.blocks.flatMap((inner) =>
      inner.kind === 'group' ? inner.parts : [inner.part],
    )
    return `workedFor:${parts[0]?.id ?? ''}:${parts[parts.length - 1]?.id ?? ''}`
  }
  if (block.kind === 'group') return `work:${block.parts[0]?.id ?? ''}`
  if (block.kind === 'single' && block.part.type === 'tool') return `work:${block.part.id}`
  return `${block.kind}:${block.part.id}`
}

const blockIdentities = computed(() => blocks.value.map(blockIdentity))
watch(blockIdentities, (next, previous) => {
  if (shouldResetHarnessPage(previous, next)) {
    if (!props.streaming) blockPage.value = 0
    else if (followingTail.value) blockPage.value = lastHarnessPage(next.length)
    else blockPage.value = Math.min(blockPage.value, lastHarnessPage(next.length))
  } else {
    blockPage.value = Math.min(blockPage.value, lastHarnessPage(next.length))
  }
})
watch(
  () => props.streaming,
  (streaming) => {
    followingTail.value = streaming === true
    if (followingTail.value) blockPage.value = lastHarnessPage(blocks.value.length)
  },
)
watch(
  () => blocks.value.length,
  (length, previous) => {
    if (props.streaming && followingTail.value && length > previous) {
      blockPage.value = lastHarnessPage(length)
    }
  },
)
watch(
  () => props.message.parts.length,
  (length, previous) => {
    if (props.streaming && followingTail.value && length > previous) {
      blockPage.value = lastHarnessPage(blocks.value.length)
    }
  },
)

const elapsedLabel = computed(() => {
  const ms = elapsedMs(props.message.created_at, props.message.completed_at)
  return ms == null ? '' : formatElapsed(ms)
})

const agentViews = computed(() =>
  buildAgentStepViews(props.message.parts, {
    streaming: props.streaming === true,
    // Only the final (non-streaming) message outcome may turn the last
    // step into `Failed`; while streaming no stale error may leak in.
    ...(props.streaming === true
      ? {}
      : { finish: props.message.finish, messageError: props.message.error }),
  }),
)

function agentViewFor(partId: string): (typeof agentViews.value)[number] | undefined {
  return agentViews.value.find((view) => view.part.id === partId)
}

function isAgentBlockConnected(index: number): boolean {
  const next = blocks.value[index + 1]
  return visibleBlockIndexes.value.has(index + 1) && next?.kind === 'agent'
}

const lastBlockIsText = computed(() => {
  // Streaming cursor belongs on the trailing answer text. After idle the
  // final text lives outside the card; while streaming the flat timeline
  // still ends in that same text block.
  const last = blocks.value[blocks.value.length - 1]
  if (last?.kind === 'text') return true
  if (last?.kind === 'workedFor') {
    return last.blocks[last.blocks.length - 1]?.kind === 'text'
  }
  return false
})

/** Cursor offset inside the flat list or inside the finished card. */
const streamingCursor = computed(() => {
  if (props.streaming !== true || !lastBlockIsText.value) return null
  const lastIndex = blocks.value.length - 1
  const last = blocks.value[lastIndex]
  if (last?.kind === 'workedFor') {
    const inner = last.blocks.length - 1
    // While streaming the flat timeline ends in text; after idle wraps it
    // the cursor stays on that same trailing text inside the card. When
    // agent plans split the timeline the cursor belongs on the trailing
    // top-level text instead (inner stays null).
    const innerLast = last.blocks[inner]
    if (innerLast?.kind === 'text') return { topIndex: lastIndex, innerIndex: inner }
    return null
  }
  return { topIndex: lastIndex, innerIndex: null as number | null }
})

/** Thinking only in idle gaps: busy turn, no live tool/subtask or agent step. */
const showThinking = computed(
  () =>
    props.streaming === true &&
    !hasRunningToolOrSubtask(props.message.parts) &&
    !hasRunningAgentSequence(props.message.parts),
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

function blockKey(block: MessageRenderBlock, index: number): string {
  if (!block) return `block-${index}`
  if (block.kind === 'group') {
    return `group-${block.parts[0]?.id ?? index}`
  }
  if (block.kind === 'workedFor') {
    // Finished card identity follows the message turn plus its span: one
    // stable node across streaming→idle, no collisions between slices, and
    // trailing text/cursor never steal the collapsed card's key.
    const first = block.blocks[0]
    const last = block.blocks[block.blocks.length - 1]
    const firstId =
      !first || first.kind === 'group'
        ? ((first as { parts?: { id: string }[] } | undefined)?.parts?.[0]?.id ?? '')
        : first.part.id
    const lastId = !last || last.kind === 'group' ? '' : (last as { part: { id: string } }).part.id
    return `workedFor-${props.message.id}-${firstId}-${lastId || index}`
  }
  if (block.kind === 'compaction') {
    return `compaction-${block.part.id}`
  }
  if (block.kind === 'agent') {
    return `agent-${block.part.id}`
  }
  return `${block.kind}-${block.part.id}`
}

/** Inline edit state for user messages (OpenCode session.revert parity). */
const editing = ref(false)

const isForkableMessage = computed(
  () => props.message.role === 'assistant' && !props.disabled && !props.busy && finished.value,
)

/** Local optimistic ids (`local-user-*`) have no backend row to edit. */
const isEditableMessage = computed(
  () =>
    props.message.role === 'user' && !props.disabled && !props.message.id.startsWith('local-user-'),
)

function startEdit(): void {
  editing.value = true
}

function cancelEdit(): void {
  editing.value = false
}

/** Rerun even with an unchanged prompt; the card already confirmed a busy stop. */
function submitEdit(text: string, selection: { model: string; effort: string }): void {
  editing.value = false
  emit('edit', props.message.id, text, selection)
}

function forkFromHere(): void {
  emit('fork', props.message.id)
}

/** Skills active for this user turn (right-aligned pills, composer style). */
const skillStore = useSkillStore()
const messageSkills = computed(() => {
  if (props.message.role !== 'user') return []
  const ids = props.message.skill_ids ?? []
  if (ids.length === 0) return []
  return ids.map((id) => {
    const skill = skillStore.skills.find((entry) => entry.id === id)
    return { id, name: skill?.name ?? String(id).slice(0, 8) }
  })
})

function asRenderBlocks(block: MessageRenderBlock): RenderBlock[] {
  return block.kind === 'workedFor' ? [] : [block]
}
</script>

<template>
  <!-- User message -->
  <div
    v-if="message.role === 'user'"
    ref="messageRoot"
    class="group/message flex items-start gap-3 justify-end"
    @focusin="onMessageFocusIn"
    @focusout="onMessageFocusOut"
  >
    <div class="min-w-0" :class="editing ? 'w-full max-w-3xl' : 'max-w-[85%]'">
      <HarnessUserBubble v-if="!editing" :text="message.content" />
      <HarnessMessageEditCard
        v-else
        :text="message.content"
        :session-id="message.session_id"
        :busy="busy"
        @cancel="cancelEdit"
        @submit="submitEdit"
      />
      <div
        v-if="messageSkills.length > 0"
        class="mt-1.5 flex flex-wrap justify-end gap-1.5"
        data-testid="message-skills"
      >
        <span
          v-for="skill in messageSkills"
          :key="skill.id"
          class="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
          data-testid="message-skill-pill"
        >
          <BookText :size="10" />
          {{ skill.name }}
        </span>
      </div>
      <!-- Zero-height slot: actions float in the turn gap instead of widening it. -->
      <div v-if="!editing" class="relative z-10 h-0">
        <HarnessMessageFooter
          align="end"
          :copy-text="message.content"
          copy-label="message"
          :editable="isEditableMessage"
          @edit="startEdit"
        />
      </div>
    </div>
  </div>

  <!-- Assistant message: chronological blocks in left prose shell -->
  <div
    v-else
    ref="messageRoot"
    class="group/message flex items-start gap-3"
    @focusin="onMessageFocusIn"
    @focusout="onMessageFocusOut"
  >
    <div class="min-w-0 flex-1 max-w-3xl pb-2 pt-3 text-sm text-foreground">
      <div v-if="blocks.length" class="flex flex-col gap-2 [&>[data-block-kind=workedFor]]:-mb-0.5">
        <HarnessPageControls
          :page="blockPage"
          :total-items="blocks.length"
          label="Message blocks pages"
          @previous="previousBlockPage"
          @next="nextBlockPage"
        />
        <template v-for="entry in visibleEntries" :key="blockKey(entry.block, entry.index)">
          <div v-if="entry.block.kind === 'workedFor'" data-block-kind="workedFor">
            <HarnessWorkedFor :elapsed-label="elapsedLabel">
              <HarnessBlockList
                :blocks="entry.block.blocks"
                :show-streaming-cursor="
                  Boolean(
                    streamingCursor &&
                    streamingCursor.topIndex === entry.index &&
                    streamingCursor.innerIndex !== null,
                  )
                "
                :child-session-ids="childSessionIds"
                :models="models ?? catalog"
                :streaming="streaming"
                @open-subtask="emit('openSubtask', $event)"
              />
            </HarnessWorkedFor>
          </div>
          <div
            v-else-if="entry.block.kind === 'agent'"
            data-block-kind="agent"
            :data-part-id="entry.block.part.id"
            :class="isAgentBlockConnected(entry.index) ? 'mb-0.5' : ''"
          >
            <HarnessAgentStep
              :step="agentViewFor(entry.block.part.id)?.step ?? null"
              :part="entry.block.part"
              :status="agentViewFor(entry.block.part.id)?.status ?? 'completed'"
              :live="agentViewFor(entry.block.part.id)?.live ?? false"
              :legacy="agentViewFor(entry.block.part.id)?.legacy ?? true"
              :connected="isAgentBlockConnected(entry.index)"
            />
          </div>
          <HarnessBlockList
            v-else
            :blocks="asRenderBlocks(entry.block)"
            :show-streaming-cursor="
              Boolean(
                streamingCursor &&
                streamingCursor.topIndex === entry.index &&
                streamingCursor.innerIndex === null,
              )
            "
            :child-session-ids="childSessionIds"
            :models="models ?? catalog"
            :streaming="streaming"
            @open-subtask="emit('openSubtask', $event)"
          />
        </template>
      </div>
      <HarnessThinking v-if="showThinking" :class="blocks.length ? 'mt-2' : ''" />
      <Transition enter-active-class="transition-opacity duration-700" enter-from-class="opacity-0">
        <p
          v-if="summary && finished"
          data-testid="message-summary"
          class="mt-2.5 flex items-start gap-1.5 text-[13px] leading-snug text-muted-foreground"
        >
          <CornerDownRight class="mt-px size-3.5 shrink-0 opacity-60" aria-hidden="true" />
          <span><span class="sr-only">Conversation summary: </span>{{ summary }}</span>
        </p>
      </Transition>
      <HarnessMessageFooter
        v-if="copyText || isForkableMessage || usageLine || streamingModelLine"
        :copy-text="copyText"
        copy-label="answer"
        :forkable="isForkableMessage"
        :metadata="streamingModelLine ?? usageLine"
        :streaming="streaming"
        @fork="forkFromHere"
      />
    </div>
  </div>
</template>
