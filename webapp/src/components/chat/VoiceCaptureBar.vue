<script setup lang="ts">
import { computed, ref } from 'vue'
import { useElementSize } from '@vueuse/core'
import { Loader2, MousePointer2, Send, Square, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { pointingEngaged, pointingError } from '@/lib/voicePointing'
import { voiceCaptureControls as controls } from '@/lib/voiceCaptureControls'

/** `embedded` replaces the composer toolbar; otherwise the bar floats in a viewer. */
defineProps<{ embedded?: boolean }>()
const recording = computed(() => controls.value?.state === 'recording')
const waveform = ref<HTMLElement | null>(null)
const { width } = useElementSize(waveform)
// Newest first: the row is reversed so fresh samples enter at the right edge.
const bars = computed(() => {
  const levels = controls.value?.levels ?? []
  // Cover the measured width with 3px bars and 3px gaps, padding history with quiet dots.
  const count = Math.ceil((width.value + 3) / 6)
  return Array.from({ length: count }, (_, index) =>
    Math.round(3 + Math.min(1, levels[levels.length - 1 - index] ?? 0) * 25),
  )
})
</script>

<template>
  <div
    v-if="controls"
    class="flex min-w-0 items-center gap-1.5"
    :class="embedded ? 'flex-1' : 'rounded-full bg-muted p-1'"
    role="group"
    aria-label="Voice input"
    data-testid="voice-capture-bar"
  >
    <Button
      type="button"
      variant="secondary"
      size="icon-sm"
      title="Discard recording"
      aria-label="Discard recording"
      data-testid="voice-cancel"
      @click="controls.cancel"
    >
      <X :size="16" />
    </Button>
    <div class="flex h-8 min-w-0 flex-1 items-center px-1" role="status" aria-live="polite">
      <template v-if="controls.state === 'recording'">
        <span class="sr-only">Recording</span>
        <div
          ref="waveform"
          class="flex h-full min-w-0 flex-1 flex-row-reverse items-center gap-[3px] overflow-hidden"
          aria-hidden="true"
          data-testid="voice-waveform"
        >
          <span
            v-for="(height, index) in bars"
            :key="index"
            class="w-[3px] shrink-0 rounded-full bg-foreground/60"
            :style="{ height: `${height}px` }"
          />
        </div>
      </template>
      <span v-else-if="controls.state === 'error'" class="truncate text-xs text-destructive">
        {{
          embedded
            ? 'Voice input needs attention.'
            : 'Voice input needs attention. Review it in the composer.'
        }}
      </span>
      <span v-else class="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
        <Loader2 :size="14" class="shrink-0 animate-spin" />
        <span class="truncate">{{
          controls.state === 'requesting' ? 'Waiting for microphone…' : 'Transcribing…'
        }}</span>
      </span>
    </div>
    <span v-if="recording && pointingError" class="shrink-0 text-xs text-destructive"
      >Pointing failed</span
    >
    <span
      v-else-if="recording && pointingEngaged"
      class="shrink-0 text-primary"
      role="img"
      title="Pointing is recorded"
      aria-label="Pointing is recorded"
      ><MousePointer2 :size="14"
    /></span>
    <template v-if="controls.state !== 'error'">
      <Button
        type="button"
        variant="secondary"
        size="icon-sm"
        :disabled="!recording"
        title="Stop and edit"
        aria-label="Stop and edit"
        data-testid="voice-stop"
        @click="controls.stop"
      >
        <Square class="size-3 fill-current" />
      </Button>
      <Button
        type="button"
        size="icon-sm"
        :disabled="!recording"
        title="Stop and send"
        aria-label="Stop and send"
        data-testid="voice-send"
        @click="controls.send"
      >
        <Send :size="16" />
      </Button>
    </template>
  </div>
</template>
