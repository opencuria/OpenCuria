<script setup lang="ts">
/**
 * HarnessQueueSheet — outgoing messages: steers queued for the running turn,
 * then messages scheduled for later.
 *
 * Ambient sheet flush on top of the composer: one compact line per message
 * (full text in the tooltip). A steer can be withdrawn back into the
 * composer; a scheduled message can also be rescheduled from its time or
 * sent now. Never takes focus; typing stays uninterrupted.
 */
import { ArrowUp, CalendarClock, CircleAlert, Clock, Loader2, X } from '@lucide/vue'
import { useNow } from '@vueuse/core'
import { Button } from '@/components/ui/button'
import SendLaterMenu from '@/components/chat/SendLaterMenu.vue'
import { formatSendTime } from '@opencuria/chat-core/sendLater'
import type { HarnessScheduledMessage, HarnessSteer } from '@opencuria/chat-core/harness'

withDefaults(
  defineProps<{
    steers: HarnessSteer[]
    /** Messages scheduled for later, earliest first. */
    scheduled?: HarnessScheduledMessage[]
    /** Non-interactive when rendered as a peek edge below a higher-priority sheet. */
    peek?: boolean
    /** The chat cannot take a message right now (e.g. the workspace is stopped). */
    sendDisabled?: boolean
  }>(),
  { scheduled: () => [] },
)

const emit = defineEmits<{
  withdraw: [steerId: string]
  'send-scheduled': [messageId: string]
  'withdraw-scheduled': [messageId: string]
  'reschedule-scheduled': [messageId: string, at: Date]
}>()

/** Keeps "Today" and "Tomorrow" right while the sheet stays open. */
const now = useNow({ interval: 60_000 })

/** Single-line preview: line breaks and runs of whitespace collapse. */
function preview(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function sendTime(message: HarnessScheduledMessage): string {
  return formatSendTime(message.send_at, now.value)
}
</script>

<template>
  <TransitionGroup
    tag="ul"
    name="queue-row"
    class="queue-sheet relative flex max-h-36 flex-col overflow-y-auto px-2 py-1.5"
    aria-label="Queued messages"
    data-testid="composer-queue-sheet"
  >
    <li
      v-for="steer in steers"
      :key="`steer:${steer.id}`"
      class="flex min-h-8 items-center gap-2 rounded-lg px-2"
      data-testid="composer-queue-row"
    >
      <Clock :size="14" class="shrink-0 text-muted-foreground" aria-hidden="true" />
      <span
        class="min-w-0 flex-1 truncate text-sm text-muted-foreground"
        :title="steer.content"
        data-testid="composer-queue-text"
      >
        {{ preview(steer.content) }}
      </span>
      <Button
        v-if="!peek"
        type="button"
        variant="ghost"
        size="icon-xs"
        class="shrink-0 text-muted-foreground hover:text-foreground"
        title="Withdraw"
        aria-label="Withdraw queued message"
        data-testid="composer-queue-withdraw"
        @click="emit('withdraw', steer.id)"
      >
        <X :size="14" />
      </Button>
    </li>
    <li
      v-for="message in scheduled"
      :key="`scheduled:${message.id}`"
      class="rounded-lg px-2"
      :data-status="message.status"
      data-testid="composer-scheduled-row"
    >
      <div class="flex min-h-8 items-center gap-2">
        <Loader2
          v-if="message.status === 'sending'"
          :size="14"
          class="shrink-0 animate-spin text-muted-foreground"
          aria-hidden="true"
        />
        <CircleAlert
          v-else-if="message.status === 'failed'"
          :size="14"
          class="shrink-0 text-destructive"
          aria-hidden="true"
        />
        <CalendarClock
          v-else
          :size="14"
          class="shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <SendLaterMenu
          v-if="!peek && message.status !== 'sending'"
          align="start"
          :initial="message.send_at"
          action-label="Reschedule"
          @pick="(at) => emit('reschedule-scheduled', message.id, at)"
        >
          <Button
            type="button"
            variant="ghost"
            size="xs"
            class="shrink-0 px-1.5 font-normal tabular-nums text-muted-foreground hover:text-foreground"
            title="Reschedule"
            :aria-label="`Reschedule, now ${sendTime(message)}`"
            data-testid="composer-scheduled-time"
          >
            {{ sendTime(message) }}
          </Button>
        </SendLaterMenu>
        <span
          v-else
          class="shrink-0 px-1.5 text-xs tabular-nums text-muted-foreground"
          data-testid="composer-scheduled-time"
        >
          {{ message.status === 'sending' ? 'Sending…' : sendTime(message) }}
        </span>
        <span
          class="min-w-0 flex-1 truncate text-sm text-muted-foreground"
          :title="message.content"
          data-testid="composer-scheduled-text"
        >
          {{ preview(message.content) }}
        </span>
        <template v-if="!peek && message.status !== 'sending'">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            class="shrink-0 text-muted-foreground hover:text-foreground"
            :disabled="sendDisabled"
            :title="message.status === 'failed' ? 'Try again now' : 'Send now'"
            :aria-label="message.status === 'failed' ? 'Try again now' : 'Send now'"
            data-testid="composer-scheduled-send-now"
            @click="emit('send-scheduled', message.id)"
          >
            <ArrowUp :size="14" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            class="shrink-0 text-muted-foreground hover:text-foreground"
            title="Withdraw"
            aria-label="Withdraw scheduled message"
            data-testid="composer-scheduled-withdraw"
            @click="emit('withdraw-scheduled', message.id)"
          >
            <X :size="14" />
          </Button>
        </template>
      </div>
      <p
        v-if="message.status === 'failed'"
        class="truncate pb-1.5 pl-7 text-xs text-destructive/80"
        :title="message.error"
        data-testid="composer-scheduled-error"
      >
        Not sent{{ message.error ? ` · ${message.error}` : '' }}
      </p>
    </li>
  </TransitionGroup>
</template>

<style scoped>
@media (prefers-reduced-motion: no-preference) {
  .queue-sheet {
    animation: queue-sheet-in 200ms ease-out both;
  }

  .queue-row-enter-active,
  .queue-row-leave-active {
    transition:
      opacity 160ms ease-out,
      transform 160ms ease-out;
  }

  .queue-row-enter-from {
    opacity: 0;
    transform: translateY(4px);
  }

  .queue-row-leave-to {
    opacity: 0;
    transform: translateX(8px);
  }

  .queue-row-leave-active {
    position: absolute;
    inset-inline: 0.5rem;
  }

  .queue-row-move {
    transition: transform 160ms ease-out;
  }
}

@keyframes queue-sheet-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
