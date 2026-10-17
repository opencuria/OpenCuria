<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { Skeleton } from '@/components/ui/skeleton'
import { MessageSquare } from '@lucide/vue'
import type { HarnessMessage } from '@/types/harness'
import { sortHarnessMessages } from '@/lib/harnessReducer'
import { pinHarnessMessageKey } from '@/lib/harnessMessagePin'
import HarnessMessageView from './HarnessMessageView.vue'

const props = defineProps<{
  messages: HarnessMessage[]
  loading?: boolean
  streamingMessageId?: string | null
  /** @deprecated Prefer `streamingMessageId`; kept for compat. */
  streamingSessionId?: string | null
  childSessionIds?: Record<string, string>
  /** State + next step of the finished run; shown under the last answer. */
  summary?: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
  edit: [messageId: string, text: string]
  fork: [messageId: string]
}>()

const scrollEl = ref<HTMLElement | null>(null)
const stickToBottom = ref(true)
const heights = new Map<string, number>()
const observer = ref<ResizeObserver | null>(null)
const observedNodes = new Set<HTMLElement>()
const pinnedIds = ref(new Set<string>())
const virtualRange = ref({ start: 0, end: 0 })
const scrollAnchor = ref<{ id: string; top: number } | null>(null)
const heightRevision = ref(0)
let resizeFrame = 0
const estimateHeight = 120
const rowGap = 24
const sortedMessages = computed(() => sortHarnessMessages(props.messages))
const virtualEnabled = computed(() => sortedMessages.value.length >= 40)

let indexedMessageIds: string[] = []
let messageIndexes = new Map<string, number>()
// prefix[i] is the height of rows [0, i), including their internal flex gaps.
let heightPrefix: number[] = [0]

function rebuildHeightPrefix(): void {
  const prefix: number[] = [0]
  for (let index = 0; index < indexedMessageIds.length; index += 1) {
    prefix[index + 1] =
      prefix[index]! +
      (heights.get(indexedMessageIds[index]!) ?? estimateHeight) +
      (index > 0 ? rowGap : 0)
  }
  heightPrefix = prefix
}

function syncMessageIndex(messages: HarnessMessage[]): void {
  const nextIds = messages.map((message) => message.id)
  if (
    nextIds.length === indexedMessageIds.length &&
    nextIds.every((id, index) => id === indexedMessageIds[index])
  )
    return

  const currentIds = new Set(nextIds)
  for (const id of heights.keys()) {
    if (!currentIds.has(id)) heights.delete(id)
  }
  indexedMessageIds = nextIds
  messageIndexes = new Map(nextIds.map((id, index) => [id, index]))
  const rangeStart = Math.min(
    Math.max(0, virtualRange.value.start),
    Math.max(0, nextIds.length - 20),
  )
  const rangeEnd = Math.min(
    nextIds.length,
    Math.max(rangeStart, Math.min(virtualRange.value.end, rangeStart + 40)),
  )
  if (virtualRange.value.start !== rangeStart || virtualRange.value.end !== rangeEnd)
    virtualRange.value = { start: rangeStart, end: rangeEnd }
  const livePins = new Set([...pinnedIds.value].filter((id) => messageIndexes.has(id)))
  if (livePins.size !== pinnedIds.value.size) pinnedIds.value = livePins
  rebuildHeightPrefix()
  heightRevision.value += 1
  if (scrollEl.value) scheduleRangeUpdate()
}

syncMessageIndex(sortedMessages.value)

function hiddenHeight(start: number, end: number): number {
  if (end <= start) return 0
  // The flex gap before the first hidden row belongs to a visible boundary,
  // not to the spacer. Internal gaps between hidden rows remain exact.
  return heightPrefix[end]! - heightPrefix[start]! - (start > 0 ? rowGap : 0)
}

const renderEntries = computed(() => {
  const messages = sortedMessages.value
  if (!virtualEnabled.value)
    return messages.map((message) => ({ kind: 'message' as const, message }))

  // Measurement changes update spacer heights without rebuilding the message
  // index; scrolling only touches the bounded visible/pinned index set.
  void heightRevision.value
  const indexes = new Set<number>()
  for (let index = virtualRange.value.start; index < virtualRange.value.end; index += 1) {
    if (index >= 0 && index < messages.length) indexes.add(index)
  }
  for (const id of pinnedIds.value) {
    const index = messageIndexes.get(id)
    if (index !== undefined && index >= 0 && index < messages.length && indexes.size < 1000)
      indexes.add(index)
  }

  const entries: Array<
    { kind: 'message'; message: HarnessMessage } | { kind: 'spacer'; id: string; height: number }
  > = []
  const renderedIndexes = [...indexes].sort((a, b) => a - b)
  let nextHiddenIndex = 0
  for (const index of renderedIndexes) {
    if (nextHiddenIndex < index) {
      entries.push({
        kind: 'spacer',
        id: `gap-${nextHiddenIndex}-${index}`,
        height: hiddenHeight(nextHiddenIndex, index),
      })
    }
    entries.push({ kind: 'message', message: messages[index]! })
    nextHiddenIndex = index + 1
  }
  if (nextHiddenIndex < messages.length) {
    entries.push({
      kind: 'spacer',
      id: `gap-${nextHiddenIndex}-end`,
      height: hiddenHeight(nextHiddenIndex, messages.length),
    })
  }
  return entries
})

const lastMessageLength = computed(() => {
  const last = sortedMessages.value[sortedMessages.value.length - 1]
  if (!last) return 0
  return last.content.length + last.parts.reduce((total, part) => total + part.output.length, 0)
})

/** The summary belongs to the final answer of the session, nowhere else. */
const lastMessageId = computed(() => {
  const last = sortedMessages.value[sortedMessages.value.length - 1]
  return last?.role === 'assistant' ? last.id : null
})

const resolvedStreamingId = computed(() => {
  if (props.streamingMessageId) {
    const match = sortedMessages.value.find((message) => message.id === props.streamingMessageId)
    return match?.role === 'assistant' && match.completed_at == null ? match.id : null
  }
  if (!props.streamingSessionId) return null
  for (let i = sortedMessages.value.length - 1; i >= 0; i -= 1) {
    const message = sortedMessages.value[i]
    if (
      message?.role === 'assistant' &&
      message.session_id === props.streamingSessionId &&
      message.completed_at == null
    )
      return message.id
  }
  return null
})

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}
function firstVisibleMessage(el: HTMLElement): HTMLElement | undefined {
  const viewport = el.getBoundingClientRect()
  return [...el.querySelectorAll<HTMLElement>('[data-message-id]')].find((node) => {
    const rect = node.getBoundingClientRect()
    return rect.bottom > viewport.top && rect.top < viewport.bottom
  })
}
function onScroll(): void {
  const el = scrollEl.value
  if (!el) return
  stickToBottom.value = isNearBottom(el)
  const first = firstVisibleMessage(el)
  scrollAnchor.value = first?.dataset.messageId
    ? { id: first.dataset.messageId, top: first.getBoundingClientRect().top }
    : null
  scheduleRangeUpdate()
}
async function scrollToBottom(force = false): Promise<void> {
  await nextTick()
  const el = scrollEl.value
  if (el && (force || stickToBottom.value)) el.scrollTop = el.scrollHeight
}

function firstRowEndingAtOrAfter(offset: number, length: number): number {
  let low = 0
  let high = length
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2)
    if (heightPrefix[middle + 1]! >= offset) high = middle
    else low = middle + 1
  }
  return low
}

function updateRange(): void {
  const el = scrollEl.value
  const messages = sortedMessages.value
  if (!el || !virtualEnabled.value) {
    if (virtualRange.value.start !== 0 || virtualRange.value.end !== messages.length)
      virtualRange.value = { start: 0, end: messages.length }
    return
  }

  const top = el.scrollTop
  const bottom = top + el.clientHeight
  const firstVisible = firstRowEndingAtOrAfter(top, messages.length)
  let start = Math.max(0, (firstVisible >= messages.length ? messages.length : firstVisible) - 5)
  const visibleEnd = firstRowEndingAtOrAfter(bottom, messages.length)
  let end =
    visibleEnd >= messages.length ? messages.length : Math.min(messages.length, visibleEnd + 6)
  const maxVisibleRows = 40
  if (end - start > maxVisibleRows) {
    start = Math.max(0, Math.min(start, firstVisible - 5))
    end = Math.min(messages.length, start + maxVisibleRows)
  }
  if (virtualRange.value.start !== start || virtualRange.value.end !== end)
    virtualRange.value = { start, end }
}
function scheduleRangeUpdate(): void {
  if (!virtualEnabled.value || resizeFrame) return
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = 0
    updateRange()
  })
}

function syncObservedMessages(): void {
  const resizeObserver = observer.value
  const el = scrollEl.value
  if (!resizeObserver || !el) return

  const currentNodes = new Set(el.querySelectorAll<HTMLElement>('[data-message-id]'))
  for (const node of observedNodes) {
    if (!currentNodes.has(node)) {
      resizeObserver.unobserve(node)
      observedNodes.delete(node)
    }
  }
  for (const node of currentNodes) {
    if (!observedNodes.has(node)) {
      resizeObserver.observe(node)
      observedNodes.add(node)
    }
  }
}

function onResize(entries: ResizeObserverEntry[]): void {
  const el = scrollEl.value
  if (!el) return

  const changedEntries: Array<{ node: HTMLElement; id: string; height: number; prior: number }> = []
  for (const entry of entries) {
    const node = entry.target as HTMLElement
    const id = node.dataset.messageId
    if (!id || !messageIndexes.has(id) || !el.contains(node)) continue
    const height = entry.contentRect.height
    const prior = heights.get(id) ?? estimateHeight
    if (height === prior) continue
    changedEntries.push({ node, id, height, prior })
  }
  // ResizeObserver may report already-settled boxes after observing or a
  // rerender. Do not schedule more work for those no-op notifications.
  if (changedEntries.length === 0) return

  const nodeFor = (id: string): HTMLElement | undefined =>
    [...el.querySelectorAll<HTMLElement>('[data-message-id]')].find(
      (node) => node.dataset.messageId === id,
    )
  const savedAnchor = scrollAnchor.value
  const anchorNode = savedAnchor
    ? nodeFor(savedAnchor.id)
    : [...el.querySelectorAll<HTMLElement>('[data-message-id]')].find(
        (node) => node.getBoundingClientRect().bottom > el.getBoundingClientRect().top,
      )
  const anchorTop = savedAnchor?.top ?? anchorNode?.getBoundingClientRect().top ?? 0
  let heightDeltaAboveAnchor = 0
  for (const { node, id, height, prior } of changedEntries) {
    if (node !== anchorNode && node.getBoundingClientRect().top < anchorTop)
      heightDeltaAboveAnchor += height - prior
    heights.set(id, height)
  }
  rebuildHeightPrefix()
  heightRevision.value += 1

  if (stickToBottom.value) {
    void nextTick(() => {
      if (stickToBottom.value) el.scrollTop = el.scrollHeight
      scheduleRangeUpdate()
    })
    return
  }
  if (anchorNode) {
    const anchorId = anchorNode.dataset.messageId
    const priorTop =
      savedAnchor && savedAnchor.id === anchorId
        ? savedAnchor.top
        : anchorNode.getBoundingClientRect().top
    void nextTick(() => {
      const current = nodeFor(anchorId ?? '')
      if (!current) return
      const delta = current.getBoundingClientRect().top - priorTop
      if (Math.abs(delta) > 0.5) el.scrollTop += delta
      scrollAnchor.value = { id: anchorId ?? '', top: current.getBoundingClientRect().top }
    })
  } else if (heightDeltaAboveAnchor !== 0) {
    void nextTick(() => {
      el.scrollTop += heightDeltaAboveAnchor
    })
  }
  scheduleRangeUpdate()
}

provide(pinHarnessMessageKey, (messageId, pinned) => {
  const next = new Set(pinnedIds.value)
  if (pinned) next.add(messageId)
  else next.delete(messageId)
  pinnedIds.value = next
})

onMounted(() => {
  scrollEl.value?.addEventListener('scroll', onScroll, { passive: true })
  if (typeof ResizeObserver !== 'undefined') observer.value = new ResizeObserver(onResize)
  virtualRange.value = {
    start: Math.max(0, sortedMessages.value.length - 20),
    end: sortedMessages.value.length,
  }
  void nextTick(async () => {
    syncObservedMessages()
    await scrollToBottom(true)
    updateRange()
  })
})
onUnmounted(() => {
  scrollEl.value?.removeEventListener('scroll', onScroll)
  observer.value?.disconnect()
  observedNodes.clear()
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
})
watch(
  [renderEntries, virtualEnabled],
  () => {
    void nextTick(syncObservedMessages)
  },
  { flush: 'post' },
)
watch(
  sortedMessages,
  (messages) => {
    syncMessageIndex(messages)
  },
  { flush: 'sync' },
)
watch(
  () => sortedMessages.value.length,
  () => {
    if (stickToBottom.value) {
      virtualRange.value = {
        start: Math.max(0, sortedMessages.value.length - 20),
        end: sortedMessages.value.length,
      }
    }
    void nextTick(() => {
      if (stickToBottom.value) void scrollToBottom()
      scheduleRangeUpdate()
    })
  },
)
watch(lastMessageLength, () => {
  void scrollToBottom()
})
watch(
  () => props.summary,
  (summary) => {
    // The sentence arrives after the answer; keep it in view when following the tail.
    if (summary && stickToBottom.value) void scrollToBottom()
  },
)
watch(
  () => props.messages[0]?.session_id ?? null,
  () => {
    stickToBottom.value = true
    heights.clear()
    rebuildHeightPrefix()
    heightRevision.value += 1
    pinnedIds.value = new Set()
    scrollAnchor.value = null
    virtualRange.value = {
      start: Math.max(0, sortedMessages.value.length - 20),
      end: sortedMessages.value.length,
    }
    void nextTick(() => {
      updateRange()
      void scrollToBottom(true)
    })
  },
)
watch(
  () => sortedMessages.value[sortedMessages.value.length - 1]?.id ?? null,
  () => {
    if (stickToBottom.value) void scrollToBottom()
  },
)
</script>

<template>
  <div
    ref="scrollEl"
    class="min-h-0 h-full flex-1 overflow-x-hidden overflow-y-auto px-3 sm:px-6 py-4"
  >
    <div
      v-if="loading && sortedMessages.length === 0"
      class="mx-auto flex w-full max-w-3xl flex-col gap-4"
    >
      <Skeleton class="h-16 w-full" /><Skeleton class="h-24 w-full" /><Skeleton
        class="h-12 w-2/3"
      />
    </div>
    <div v-else-if="sortedMessages.length" class="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <template
        v-for="entry in renderEntries"
        :key="entry.kind === 'message' ? entry.message.id : entry.id"
      >
        <div
          v-if="entry.kind === 'spacer'"
          :data-spacer-id="entry.id"
          :style="{ height: `${entry.height}px` }"
          aria-hidden="true"
        />
        <HarnessMessageView
          v-else
          :data-message-id="entry.message.id"
          :message="entry.message"
          :streaming="entry.message.id === resolvedStreamingId"
          :child-session-ids="childSessionIds"
          :summary="entry.message.id === lastMessageId ? summary : ''"
          :disabled="disabled"
          @open-subtask="emit('openSubtask', $event)"
          @edit="(messageId, text) => emit('edit', messageId, text)"
          @fork="(messageId) => emit('fork', messageId)"
        />
      </template>
    </div>
    <div v-else class="flex h-full flex-col items-center justify-center px-6 py-12 text-center">
      <div class="mb-4 text-muted-foreground"><MessageSquare :size="40" /></div>
      <h3 class="mb-1 text-lg font-medium text-foreground">Start a chat</h3>
      <p class="max-w-sm text-sm text-muted-foreground">
        Send a message below to begin a new harness session.
      </p>
    </div>
  </div>
</template>
