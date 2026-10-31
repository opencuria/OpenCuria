<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Mic, ChevronDown, Loader2, RotateCcw, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Checkbox } from '@/components/ui/checkbox'
import ModelPicker from '@/components/common/ModelPicker.vue'
import { getVoiceConfig, type VoiceConfig } from '@/services/voice.api'
import { useVoiceInput, voiceSupported, voiceUnavailableReason } from '@/composables/useVoiceInput'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import type { ProviderModel } from '@/lib/harnessModels'

const props = defineProps<{ workspaceId: string; contextKey?: string; disabled?: boolean }>()
const emit = defineEmits<{ result: [text: string, path?: string]; busy: [value: boolean] }>()
const open = ref(false)
const config = ref<VoiceConfig | null>(null)
const configError = ref('')
const loading = ref(false)
const model = ref('')
const pointing = ref(false)
const supported = voiceSupported()
const unavailableReason = voiceUnavailableReason()
const voice = useVoiceInput((text, path) => emit('result', text, path))
const { state, error, warning, elapsedMs, busy, pointingFailed } = voice
const models = computed<ProviderModel[]>(() =>
  (config.value?.models ?? []).map((item) => ({
    ...item,
    provider: item.id.split('/')[0] || 'openrouter',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: false,
    context_length: 0,
    max_output_tokens: 0,
  })),
)
let configGeneration = 0
let configAbort: AbortController | null = null

async function loadConfig(): Promise<void> {
  const generation = ++configGeneration
  configAbort?.abort()
  configAbort = new AbortController()
  loading.value = true
  configError.value = ''
  try {
    const result = await getVoiceConfig(props.workspaceId, configAbort.signal)
    if (generation !== configGeneration) return
    config.value = result
    if (!model.value) model.value = result.default_model
  } catch (cause) {
    if (generation !== configGeneration) return
    configError.value =
      cause instanceof Error ? cause.message : 'Voice settings could not be loaded.'
  } finally {
    if (generation === configGeneration) loading.value = false
  }
}
watch(open, (value) => {
  if (value && state.value === 'idle') void loadConfig()
})
watch(
  () => [props.workspaceId, props.contextKey],
  () => {
    voice.cancel()
    configGeneration++
    configAbort?.abort()
    config.value = null
    model.value = ''
    open.value = false
  },
)
watch(
  () => props.disabled,
  (value) => {
    if (value) voice.cancel()
  },
)
watch([state, elapsedMs, pointing], () => {
  emit('busy', busy.value)
  if (busy.value)
    voiceCaptureControls.value = {
      workspaceId: props.workspaceId,
      state: state.value as 'requesting' | 'recording' | 'processing' | 'error',
      elapsedMs: elapsedMs.value,
      pointing: pointing.value,
      stop: () => {
        void voice.stop()
      },
      cancel: voice.cancel,
    }
  else if (voiceCaptureControls.value?.cancel === voice.cancel) voiceCaptureControls.value = null
})
onBeforeUnmount(() => {
  emit('busy', false)
  configGeneration++
  configAbort?.abort()
  if (voiceCaptureControls.value?.cancel === voice.cancel) voiceCaptureControls.value = null
})
async function start(): Promise<void> {
  if (!supported || props.disabled || !config.value?.available || !model.value.trim()) return
  open.value = false
  await voice.start({
    workspaceId: props.workspaceId,
    model: model.value.trim(),
    pointing: pointing.value,
    maxDurationMs: config.value.max_duration_ms,
    maxAudioBytes: config.value.max_audio_bytes,
  })
}
</script>

<template>
  <div class="flex items-center">
    <Button
      type="button"
      variant="ghost"
      size="icon"
      class="h-8 w-8 text-muted-foreground"
      :disabled="disabled || busy"
      title="Dictate a message"
      aria-label="Dictate a message"
      data-testid="composer-voice"
      @click="config?.available ? start() : (open = true)"
    >
      <Mic :size="16" />
    </Button>
    <Popover v-model:open="open">
      <PopoverTrigger as-child>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          class="h-8 w-4 text-muted-foreground"
          :disabled="disabled || busy"
          title="Voice input settings"
          aria-label="Voice input settings"
        >
          <ChevronDown :size="12" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" side="top" class="w-80 space-y-3" @open-auto-focus.prevent>
        <div class="text-sm font-medium">Voice input</div>
        <p v-if="!supported" class="text-xs text-muted-foreground">
          {{ unavailableReason }}
        </p>
        <div v-if="loading" class="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 :size="14" class="animate-spin" /> Loading voice settings…
        </div>
        <template v-else>
          <p v-if="configError || !config?.available" class="text-xs text-muted-foreground">
            {{ configError || config?.reason || 'Configure a speech provider in Settings.' }}
          </p>
          <template v-if="config?.available">
            <ModelPicker
              v-model:model="model"
              effort=""
              :models="models"
              :show-effort="false"
              manual-fallback
              variant="field"
              default-model-label="Speech model"
            />
            <p
              v-if="config.cleanup_model"
              class="break-all text-xs text-muted-foreground"
              data-testid="voice-cleanup-model"
            >
              Text cleanup: {{ config.cleanup_model }} · Settings → Provider &amp; Models → Small Model (Build
              default if unset).
            </p>
            <label class="flex cursor-pointer items-center gap-2 text-sm">
              <Checkbox :model-value="pointing" @update:model-value="pointing = $event === true" />
              Record pointing too
            </label>
            <p class="text-xs text-muted-foreground">
              {{
                pointing
                  ? 'Show images or the workspace desktop while speaking. Pointer positions and desktop snapshots are saved in this workspace. Desktop clicks only point.'
                  : 'Filler words are removed and punctuation is added. Review the text before sending.'
              }}
            </p>
            <Button class="w-full" size="sm" :disabled="!supported || !model.trim()" @click="start"
              ><Mic :size="14" /> Start dictating</Button
            >
            <p class="text-xs text-muted-foreground">
              Microphone audio goes to your configured model provider. Up to 5 minutes.
            </p>
          </template>
          <Button v-else size="sm" variant="secondary" @click="loadConfig">Reload settings</Button>
        </template>
      </PopoverContent>
    </Popover>
    <div
      v-if="error || warning"
      class="absolute bottom-full left-4 right-4 mb-2 rounded-lg border bg-popover px-3 py-2 text-xs shadow-md"
      :role="error ? 'alert' : 'status'"
    >
      <p>{{ error || warning }}</p>
      <div class="mt-2 flex gap-2">
        <Button
          v-if="state === 'error' && !pointingFailed"
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
        <Button variant="ghost" size="sm" class="h-7" @click="voice.cancel"
          ><X :size="12" /> {{ state === 'error' ? 'Discard' : 'Dismiss' }}</Button
        >
      </div>
    </div>
  </div>
</template>
