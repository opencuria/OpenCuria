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
const pinnedIds = ref(new Set<string>())
const virtualRange = ref({ start: 0, end: 0 })
const measured = ref(0)
const scrollAnchor = ref<{ id: string; top: number } | null>(null)
let resizeFrame = 0
const estimateHeight = 120
const rowGap = 24
const sortedMessages = computed(() => sortHarnessMessages(props.messages))
const virtualEnabled = computed(() => sortedMessages.value.length >= 40)

const renderEntries = computed(() => {
  const messages = sortedMessages.value
  if (!virtualEnabled.value)
    return messages.map((message) => ({ kind: 'message' as const, message }))
  const indexes = new Set<number>()
  for (let index = virtualRange.value.start; index < virtualRange.value.end; index += 1)
    indexes.add(index)
  for (const id of pinnedIds.value) {
    const index = messages.findIndex((message) => message.id === id)
    if (index >= 0 && indexes.size < 1000) indexes.add(index)
  }
  // Guard against pathological viewport sizes / pinning while retaining the
  // visible window first and keeping transcript DOM strictly bounded.
  if (indexes.size > 1000) {
    const visible = [...indexes].sort((a, b) => a - b).slice(0, 1000)
    indexes.clear()
    visible.forEach((index) => indexes.add(index))
  }
  const entries: Array<
    { kind: 'message'; message: HarnessMessage } | { kind: 'spacer'; id: string; height: number }
  > = []
  let hiddenStart = -1
  let hiddenHeight = 0
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]!
    if (!indexes.has(index)) {
      if (hiddenStart < 0) hiddenStart = index
      else hiddenHeight += rowGap
      hiddenHeight += heights.get(message.id) ?? estimateHeight
      continue
    }
    if (hiddenStart >= 0) {
      entries.push({ kind: 'spacer', id: `gap-${hiddenStart}-${index}`, height: hiddenHeight })
      hiddenStart = -1
      hiddenHeight = 0
    }
    entries.push({ kind: 'message', message })
  }
  if (hiddenStart >= 0)
    entries.push({ kind: 'spacer', id: `gap-${hiddenStart}-end`, height: hiddenHeight })
  return entries
})

const lastMessageLength = computed(() => {
  const last = sortedMessages.value[sortedMessages.value.length - 1]
  if (!last) return 0
  return last.content.length + last.parts.reduce((total, part) => total + part.output.length, 0)
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
function updateRange(): void {
  const el = scrollEl.value
  const messages = sortedMessages.value
  if (!el || !virtualEnabled.value) {
    virtualRange.value = { start: 0, end: messages.length }
    return
  }
  const top = el.scrollTop
  const bottom = top + el.clientHeight
  const cumulative: number[] = []
  let total = 0
  for (let i = 0; i < messages.length; i += 1) {
    if (i > 0) total += rowGap
    total += heights.get(messages[i]!.id) ?? estimateHeight
    cumulative.push(total)
  }
  const firstVisible = cumulative.findIndex((end) => end >= top)
  let start = Math.max(0, (firstVisible < 0 ? messages.length : firstVisible) - 5)
  const visibleEnd = cumulative.findIndex((end) => end >= bottom)
  let end = visibleEnd < 0 ? messages.length : Math.min(messages.length, visibleEnd + 6)
  const maxVisibleRows = 40
  if (end - start > maxVisibleRows) {
    start = Math.max(0, Math.min(start, firstVisible - 5))
    end = Math.min(messages.length, start + maxVisibleRows)
  }
  virtualRange.value = { start, end }
  measured.value += 1
}
function scheduleRangeUpdate(): void {
  if (!virtualEnabled.value || resizeFrame) return
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = 0
    updateRange()
  })
}
function observeMessages(): void {
  observer.value?.disconnect()
  scrollEl.value
    ?.querySelectorAll<HTMLElement>('[data-message-id]')
    .forEach((node) => observer.value?.observe(node))
}
function onResize(entries: ResizeObserverEntry[]): void {
  const el = scrollEl.value
  if (!el) return
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
  for (const entry of entries) {
    const node = entry.target as HTMLElement
    const id = node.dataset.messageId
    if (!id) continue
    const priorHeight = heights.get(id) ?? estimateHeight
    if (node !== anchorNode && node.getBoundingClientRect().top < anchorTop) {
      heightDeltaAboveAnchor += entry.contentRect.height - priorHeight
    }
    heights.set(id, entry.contentRect.height)
  }
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
    observeMessages()
    await scrollToBottom(true)
    updateRange()
  })
})
onUnmounted(() => {
  scrollEl.value?.removeEventListener('scroll', onScroll)
  observer.value?.disconnect()
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
})
watch(
  [renderEntries, virtualEnabled],
  () => {
    void nextTick(observeMessages)
  },
  { flush: 'post' },
)
watch(
  () => measured.value,
  () => {
    void nextTick(observeMessages)
  },
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
  () => props.messages[0]?.session_id ?? null,
  () => {
    stickToBottom.value = true
    heights.clear()
    pinnedIds.value = new Set()
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
