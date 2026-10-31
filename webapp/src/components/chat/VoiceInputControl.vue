<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Mic, MousePointer2, Loader2, RotateCcw, Square, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { getVoiceConfig } from '@/services/voice.api'
import { useVoiceInput, voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import { OPEN_SETTINGS_EVENT } from '@/components/settings/settingsTabs'

const props = defineProps<{ workspaceId: string; contextKey?: string; disabled?: boolean }>()
const emit = defineEmits<{ result: [text: string, path?: string]; busy: [value: boolean] }>()
const pointing = ref(false)
const loading = ref(false)
const configError = ref('')
const setupMissing = ref(false)
const voice = useVoiceInput((text, path) => emit('result', text, path))
const { state, error, warning, elapsedMs, pointingFailed, audioLevel } = voice
const busy = computed(() => voice.busy.value || loading.value)
const notice = computed(() => error.value || configError.value || warning.value)
const microphoneLabel = computed(() =>
  state.value === 'recording' ? 'Finish dictation' : 'Dictate a message',
)
let configGeneration = 0
let configAbort: AbortController | null = null

function invalidateConfig(): void {
  configGeneration++
  configAbort?.abort()
  configAbort = null
  loading.value = false
}
function dismiss(): void {
  invalidateConfig()
  configError.value = ''
  setupMissing.value = false
  voice.cancel()
}
function openSettings(): void {
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'provider' } }))
}
async function start(): Promise<void> {
  if (props.disabled || busy.value) return
  if (!voiceSupported()) {
    configError.value = voiceUnavailableReason()
    return
  }
  const generation = ++configGeneration
  configAbort?.abort()
  const request = new AbortController()
  configAbort = request
  const timeout = setTimeout(() => request.abort(), 20_000)
  loading.value = true
  configError.value = ''
  setupMissing.value = false
  try {
    const config = await getVoiceConfig(props.workspaceId, request.signal)
    if (generation !== configGeneration || props.disabled) return
    if (!config.available || !config.default_model) {
      setupMissing.value = true
      configError.value = 'Set up voice input in Settings.'
      return
    }
    await voice.start({
      workspaceId: props.workspaceId,
      model: config.default_model,
      pointing: pointing.value,
      maxDurationMs: config.max_duration_ms,
      maxAudioBytes: config.max_audio_bytes,
    })
  } catch (cause) {
    if (generation !== configGeneration) return
    configError.value = request.signal.aborted
      ? 'Voice settings could not be loaded. Please try again.'
      : cause instanceof Error
        ? cause.message
        : 'Voice input could not be started.'
  } finally {
    clearTimeout(timeout)
    if (generation === configGeneration) {
      loading.value = false
      configAbort = null
    }
  }
}
function toggleRecording(): void {
  if (state.value === 'recording') void voice.stop()
  else void start()
}
watch(() => [props.workspaceId, props.contextKey], dismiss)
watch(
  () => props.disabled,
  (value) => {
    if (value) dismiss()
  },
)
watch([busy, state, elapsedMs, pointing, audioLevel], () => {
  emit('busy', busy.value)
  if (voice.busy.value)
    voiceCaptureControls.value = {
      workspaceId: props.workspaceId,
      state: state.value as 'requesting' | 'recording' | 'processing' | 'error',
      elapsedMs: elapsedMs.value,
      audioLevel: audioLevel.value,
      pointing: pointing.value,
      stop: () => {
        void voice.stop()
      },
      cancel: dismiss,
    }
  else if (voiceCaptureControls.value?.cancel === dismiss) voiceCaptureControls.value = null
})
onBeforeUnmount(() => {
  invalidateConfig()
  emit('busy', false)
  if (voiceCaptureControls.value?.cancel === dismiss) voiceCaptureControls.value = null
})
</script>

<template>
  <div class="flex items-center gap-0.5">
    <Button
      type="button"
      variant="ghost"
      size="icon"
      class="h-8 w-8"
      :class="state === 'recording' ? 'text-destructive' : 'text-muted-foreground'"
      :disabled="disabled || (busy && state !== 'recording')"
      :title="microphoneLabel"
      :aria-label="microphoneLabel"
      :aria-busy="loading || state === 'processing'"
      data-testid="composer-voice"
      @click="toggleRecording"
    >
      <Loader2
        v-if="loading || state === 'requesting' || state === 'processing'"
        :size="16"
        class="animate-spin"
      />
      <Square v-else-if="state === 'recording'" :size="14" />
      <Mic v-else :size="16" />
    </Button>
    <Button
      type="button"
      variant="ghost"
      size="icon"
      class="h-8 w-8"
      :class="pointing ? 'bg-primary/10 text-primary hover:bg-primary/15' : 'text-muted-foreground'"
      :disabled="disabled || busy"
      :aria-pressed="pointing"
      title="Record pointing too"
      aria-label="Record pointing too"
      data-testid="composer-voice-pointing"
      @click="pointing = !pointing"
      ><MousePointer2 :size="15"
    /></Button>
    <div
      v-if="notice"
      class="absolute bottom-full left-4 right-4 mb-2 rounded-lg border bg-popover px-3 py-2 text-xs shadow-md"
      :role="error || configError ? 'alert' : 'status'"
    >
      <p>{{ notice }}</p>
      <div class="mt-2 flex gap-2">
        <Button v-if="setupMissing" variant="secondary" size="sm" class="h-7" @click="openSettings"
          >Open Settings</Button
        >
        <Button
          v-else-if="state === 'error' && !pointingFailed"
          variant="secondary"
          size="sm"
          class="h-7"
          @click="voice.retry"
          ><RotateCcw :size="12" /> Retry</Button
        >
        <Button
          v-if="pointingFailed"
          variant="secondary"
          size="sm"
          class="h-7"
          @click="voice.useDictationOnly"
          >Use dictation only</Button
        >
        <Button variant="ghost" size="sm" class="h-7" @click="dismiss"
          ><X :size="12" /> {{ state === 'error' ? 'Discard' : 'Dismiss' }}</Button
        >
      </div>
    </div>
  </div>
</template>
