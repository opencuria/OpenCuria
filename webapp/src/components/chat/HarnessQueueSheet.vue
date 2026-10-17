<script setup lang="ts">
/**
 * HarnessQueueSheet — messages queued for the running turn.
 *
 * Ambient sheet flush on top of the composer: one compact line per queued
 * steer (full text in the tooltip) and a ghost button to withdraw it back
 * into the composer. Never takes focus; typing stays uninterrupted.
 */
import { Clock, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import type { HarnessSteer } from '@/types/harness'

defineProps<{
  steers: HarnessSteer[]
  /** Non-interactive when rendered as a peek edge below a higher-priority sheet. */
  peek?: boolean
}>()

const emit = defineEmits<{
  withdraw: [steerId: string]
}>()

/** Single-line preview: line breaks and runs of whitespace collapse. */
function preview(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
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
      :key="steer.id"
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
