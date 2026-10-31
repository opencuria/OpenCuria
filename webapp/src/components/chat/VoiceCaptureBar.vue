<script setup lang="ts">
import { computed } from 'vue'
import { Mic, MousePointer2, Loader2, Square, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { pointingError } from '@/lib/voicePointing'
import { voiceCaptureControls as controls } from '@/lib/voiceCaptureControls'
const elapsed = computed(() => {
  const seconds = Math.floor((controls.value?.elapsedMs || 0) / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
})
</script>
<template>
  <div
    v-if="controls"
    class="flex items-center gap-2 rounded-lg bg-muted px-3 py-2 text-xs"
    role="status"
    aria-live="polite"
    data-testid="voice-capture-bar"
  >
    <Mic v-if="controls.state === 'recording'" :size="14" class="text-destructive" />
    <X v-else-if="controls.state === 'error'" :size="14" class="text-destructive" />
    <Loader2 v-else :size="14" class="animate-spin" />
    <span v-if="controls.state === 'recording'"
      >{{ controls.pointing ? 'Dictating + pointing' : 'Dictating'
      }}<span aria-hidden="true"> · {{ elapsed }}</span></span
    >
    <span v-else-if="controls.state === 'error'">Voice input needs attention. Review it in the composer.</span>
    <span v-else>{{
      controls.state === 'requesting' ? 'Waiting for microphone…' : 'Preparing your text…'
    }}</span>
    <MousePointer2 v-if="controls.pointing" :size="14" />
    <span v-if="controls.state === 'recording' && controls.pointing && pointingError" class="text-destructive"
      >Visual capture failed. Finish to keep your dictation.</span
    >
    <Button
      v-if="controls.state === 'recording'"
      size="sm"
      variant="secondary"
      class="h-7"
      aria-label="Finish dictation"
      @click="controls.stop"
      ><Square :size="12" /> Finish</Button
    >
    <Button
      size="icon"
      variant="ghost"
      class="h-7 w-7"
      aria-label="Discard dictation"
      @click="controls.cancel"
      ><X :size="14"
    /></Button>
  </div>
</template>
