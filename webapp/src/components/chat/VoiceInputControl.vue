<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Mic, RotateCcw, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import VoiceCaptureBar from '@/components/chat/VoiceCaptureBar.vue'
import { getVoiceConfig } from '@/services/voice.api'
import { useVoiceInput, voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import { OPEN_SETTINGS_EVENT } from '@/components/settings/settingsTabs'

const props = defineProps<{ workspaceId: string; contextKey?: string; disabled?: boolean }>()
const emit = defineEmits<{
  /** `send` asks the composer to submit right away instead of offering an edit. */
  result: [text: string, path: string | undefined, send: boolean]
  busy: [value: boolean]
}>()
const loading = ref(false)
const configError = ref('')
const setupMissing = ref(false)
// Survives processing errors so Retry and "Use dictation only" keep the chosen action.
let sendOnResult = false
const voice = useVoiceInput((text, path) => {
  const send = sendOnResult
  sendOnResult = false
  emit('result', text, path, send)
})
const { state, error, warning, pointingFailed, audioLevels } = voice
const busy = computed(() => voice.busy.value || loading.value)
const notice = computed(() => error.value || configError.value || warning.value)
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
  sendOnResult = false
  voice.cancel()
}
function finish(send: boolean): void {
  if (state.value !== 'recording') return
  sendOnResult = send
  void voice.stop()
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
  sendOnResult = false
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
watch(() => [props.workspaceId, props.contextKey], dismiss)
watch(
  () => props.disabled,
  (value) => {
    if (value) dismiss()
  },
)
watch([busy, state, audioLevels], () => {
  emit('busy', busy.value)
  if (busy.value)
    voiceCaptureControls.value = {
      workspaceId: props.workspaceId,
      // Loading voice settings precedes the microphone prompt.
      state: voice.busy.value
        ? (state.value as 'requesting' | 'recording' | 'processing' | 'error')
        : 'requesting',
      levels: audioLevels.value,
      stop: () => finish(false),
      send: () => finish(true),
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
  <div class="flex min-w-0 items-center" :class="busy ? 'flex-1' : ''">
    <VoiceCaptureBar v-if="busy" embedded />
    <Button
      v-else
      type="button"
      variant="ghost"
      size="icon"
      class="h-8 w-8 text-muted-foreground hover:text-foreground"
      :disabled="disabled"
      title="Dictate a message"
      aria-label="Dictate a message"
      data-testid="composer-voice"
      @click="start"
    >
      <Mic :size="16" />
    </Button>
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
