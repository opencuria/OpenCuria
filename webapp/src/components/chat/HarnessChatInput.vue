<script setup lang="ts">
import { computed, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Send, Square } from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  stoppable?: boolean
  busyMessage?: string
  /**
   * Model picker value. There is no ProviderConfig REST endpoint in M6, so
   * this composer uses a plain text field with a default instead — the
   * backend falls back to the org's configured default model when empty.
   */
  model?: string
  mode?: HarnessSessionMode
}>()

const emit = defineEmits<{
  send: [prompt: string, mode: HarnessSessionMode, model: string]
  stop: []
  'update:model': [value: string]
  'update:mode': [value: HarnessSessionMode]
}>()

const prompt = ref('')
const localMode = ref<HarnessSessionMode>(props.mode ?? 'build')
const localModel = ref(props.model ?? '')

const canSend = computed(
  () => prompt.value.trim().length > 0 && !props.disabled && !props.sending,
)
const canStop = computed(() => Boolean(props.stoppable) && !props.sending)

function handleSend(): void {
  if (!canSend.value) return
  emit('send', prompt.value.trim(), localMode.value, localModel.value.trim())
  prompt.value = ''
}

function handleKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function setMode(mode: HarnessSessionMode): void {
  localMode.value = mode
  emit('update:mode', mode)
}

function onModelInput(e: Event): void {
  const value = (e.target as HTMLInputElement).value
  localModel.value = value
  emit('update:model', value)
}

function clearInput(): void {
  prompt.value = ''
}

defineExpose({ clearInput })
</script>

<template>
  <div class="pt-3 px-3 sm:pt-4 sm:px-4 pb-2 bg-transparent min-w-0 w-full">
    <div class="flex flex-col rounded-xl border bg-card shadow-sm border-border focus-within:border-primary transition-all duration-200">
      <!-- Plan/Build toggle + model field -->
      <div class="flex flex-wrap items-center gap-2 px-4 pt-3">
        <div class="flex rounded-lg bg-muted p-0.5" role="tablist" aria-label="Agent mode">
          <button
            type="button"
            role="tab"
            :aria-selected="localMode === 'plan'"
            class="rounded-md px-3 py-1 text-xs font-medium transition-colors"
            :class="localMode === 'plan' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
            @click="setMode('plan')"
          >
            Plan
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="localMode === 'build'"
            class="rounded-md px-3 py-1 text-xs font-medium transition-colors"
            :class="localMode === 'build' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
            @click="setMode('build')"
          >
            Build
          </button>
        </div>
        <Input
          :value="localModel"
          placeholder="Model (empty = org default)"
          class="h-8 max-w-64 text-xs"
          title="No ProviderConfig endpoint exists in M6 — leave empty to use the org default model."
          @input="onModelInput"
        />
      </div>
      <Textarea
        v-model="prompt"
        :disabled="disabled"
        :rows="1"
        placeholder="Send a prompt to the agent…"
        class="min-h-[50px] max-h-[200px] w-full resize-none !border-0 !shadow-none focus:!shadow-none focus:!border-transparent !ring-0 !outline-none focus-visible:ring-0 focus-visible:outline-none !rounded-none !bg-transparent px-4 py-3 text-base"
        @keydown="handleKeydown"
      />
      <div class="flex items-center justify-end gap-2 p-2 pl-3 pb-3">
        <Button
          v-if="stoppable"
          :disabled="!canStop"
          size="icon"
          class="h-9 w-9 rounded-full transition-all shrink-0"
          :class="canStop ? 'bg-error text-white hover:bg-error/90' : 'bg-muted text-muted-foreground'"
          title="Stop current run"
          @click="emit('stop')"
        >
          <Square :size="16" />
        </Button>
        <Button
          v-else
          :disabled="!canSend"
          size="icon"
          class="h-9 w-9 rounded-full transition-all shrink-0"
          :class="canSend ? 'bg-primary text-primary-foreground hover:bg-primary-hover' : 'bg-muted text-muted-foreground'"
          @click="handleSend"
        >
          <Send :size="18" />
        </Button>
      </div>
    </div>
    <p v-if="busyMessage" class="text-xs text-center text-amber-600 dark:text-amber-400 mt-2">
      {{ busyMessage }}
    </p>
    <p v-else class="hidden sm:block text-xs text-center text-muted-foreground mt-2">
      Press <kbd class="font-mono font-medium text-foreground">Enter</kbd> to send,
      <kbd class="font-mono font-medium text-foreground">Shift+Enter</kbd> for newline
    </p>
  </div>
</template>
