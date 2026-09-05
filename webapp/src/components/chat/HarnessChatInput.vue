<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { Send, Square } from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'
import type { FileNode } from '@/types'
import { getProviderConfig } from '@/services/harness.api'
import {
  applyMentionCandidate,
  detectMentionQuery,
  filterMentionCandidates,
  flattenFilePaths,
  type MentionCandidate,
} from '@/lib/harnessMentions'

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  stoppable?: boolean
  busyMessage?: string
  workspaceId?: string
  /** Known workspace files for `@file` suggestions (file-explorer tree). */
  files?: FileNode[]
  /**
   * Model picker value. Empty string means "org default" — the backend
   * falls back to the org's configured default model when empty.
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
const textareaRef = ref<{ $el?: unknown } | null>(null)
const localMode = ref<HarnessSessionMode>(props.mode ?? 'build')
const localModel = ref(props.model ?? '')
const modelOptions = ref<string[]>([])
const modelLoading = ref(false)

const ORG_DEFAULT = '__org_default__'
const CUSTOM_VALUE = '__custom__'

/** Select value: org default, a known model, or the custom free-text entry. */
const selectValue = computed(() =>
  localModel.value === ''
    ? ORG_DEFAULT
    : modelOptions.value.includes(localModel.value)
      ? localModel.value
      : CUSTOM_VALUE,
)

const modelSelectTitle = computed(() =>
  modelOptions.value.length > 0
    ? 'Model from the org provider config (empty = org default)'
    : 'No provider config found — enter a model manually or leave empty for the backend default',
)

async function loadModels(workspaceId: string | undefined): Promise<void> {
  if (!workspaceId) {
    modelOptions.value = []
    return
  }
  modelLoading.value = true
  try {
    const config = await getProviderConfig(workspaceId)
    const models = [config.default_model, config.small_model].filter(
      (m): m is string => typeof m === 'string' && m.trim().length > 0,
    )
    modelOptions.value = [...new Set(models)]
  } catch {
    modelOptions.value = []
  } finally {
    modelLoading.value = false
  }
}

onMounted(() => {
  void loadModels(props.workspaceId)
})

watch(
  () => props.workspaceId,
  (next) => {
    localModel.value = ''
    emit('update:model', '')
    void loadModels(next)
  },
)

watch(
  () => props.model,
  (next) => {
    if (typeof next === 'string' && next !== localModel.value) localModel.value = next
  },
)

const canSend = computed(
  () => prompt.value.trim().length > 0 && !props.disabled && !props.sending,
)
const canStop = computed(() => Boolean(props.stoppable) && !props.sending)

function handleSend(): void {
  if (!canSend.value) return
  emit('send', prompt.value.trim(), localMode.value, localModel.value.trim())
  prompt.value = ''
  closeMention()
}

function setMode(mode: HarnessSessionMode): void {
  localMode.value = mode
  emit('update:mode', mode)
}

function setModel(value: unknown): void {
  const next = value === ORG_DEFAULT ? '' : value === CUSTOM_VALUE ? localModel.value : String(value ?? '')
  localModel.value = next
  emit('update:model', next)
}

function onCustomModelInput(e: Event): void {
  const value = (e.target as HTMLInputElement).value
  localModel.value = value
  emit('update:model', value)
}

function clearInput(): void {
  prompt.value = ''
}

defineExpose({ clearInput })

// --- @file / @agent autocomplete (local filtering, no backend change) --------

const mentionOpen = ref(false)
const mentionIndex = ref(0)
const mentionQuery = ref('')

const mentionCandidates = computed<MentionCandidate[]>(() =>
  mentionOpen.value
    ? filterMentionCandidates(mentionQuery.value, flattenFilePaths(props.files ?? []))
    : [],
)

function textareaEl(): HTMLTextAreaElement | null {
  const root = textareaRef.value
  if (!root) return null
  const el = (root as { $el?: unknown }).$el
  return el instanceof HTMLTextAreaElement ? el : null
}

function closeMention(): void {
  mentionOpen.value = false
  mentionIndex.value = 0
  mentionQuery.value = ''
}

function refreshMention(): void {
  const el = textareaEl()
  if (!el) return
  const query = detectMentionQuery(el.value, el.selectionStart ?? el.value.length)
  if (query === null) {
    closeMention()
    return
  }
  mentionQuery.value = query
  mentionIndex.value = 0
  mentionOpen.value = true
}

function chooseMention(candidate: MentionCandidate): void {
  const el = textareaEl()
  if (!el) return
  const cursor = el.selectionStart ?? el.value.length
  const next = applyMentionCandidate(el.value, cursor, candidate)
  prompt.value = next.text
  closeMention()
  void nextTick(() => {
    el.focus()
    el.setSelectionRange(next.cursor, next.cursor)
  })
}

function onPromptInput(): void {
  refreshMention()
}

function onPromptKeydown(e: KeyboardEvent): void {
  if (mentionOpen.value && mentionCandidates.value.length > 0) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const delta = e.key === 'ArrowDown' ? 1 : -1
      const count = mentionCandidates.value.length
      mentionIndex.value = (mentionIndex.value + delta + count) % count
      return
    }
    if (e.key === 'Tab' || e.key === 'Enter') {
      const candidate = mentionCandidates.value[mentionIndex.value]
      if (candidate) {
        e.preventDefault()
        chooseMention(candidate)
        return
      }
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closeMention()
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function handleKeydown(e: KeyboardEvent): void {
  onPromptKeydown(e)
}
</script>

<template>
  <div class="pt-3 px-3 sm:pt-4 sm:px-4 pb-2 bg-transparent min-w-0 w-full">
    <div class="flex flex-col rounded-xl border bg-card shadow-sm border-border focus-within:border-primary transition-all duration-200">
      <!-- Plan/Build toggle + model picker -->
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
        <Select :model-value="selectValue" @update:model-value="setModel">
          <SelectTrigger class="h-8 max-w-64 text-xs" :title="modelSelectTitle">
            <SelectValue placeholder="Model (org default)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem :value="ORG_DEFAULT">Org default</SelectItem>
            <SelectItem v-for="name in modelOptions" :key="name" :value="name">
              {{ name }}
            </SelectItem>
            <SelectItem :value="CUSTOM_VALUE">Custom…</SelectItem>
          </SelectContent>
        </Select>
        <Input
          v-if="selectValue === CUSTOM_VALUE || modelOptions.length === 0"
          :value="localModel"
          placeholder="Model (empty = org default)"
          class="h-8 max-w-64 text-xs"
          :title="modelSelectTitle"
          @input="onCustomModelInput"
        />
        <span v-if="modelLoading" class="text-xs text-muted-foreground">Loading models…</span>
      </div>
      <div class="relative">
        <Textarea
          ref="textareaRef"
          v-model="prompt"
          :disabled="disabled"
          :rows="1"
          placeholder="Send a prompt to the agent… (@file, @agent)"
          class="min-h-[50px] max-h-[200px] w-full resize-none !border-0 !shadow-none focus:!shadow-none focus:!border-transparent !ring-0 !outline-none focus-visible:ring-0 focus-visible:outline-none !rounded-none !bg-transparent px-4 py-3 text-base"
          @keydown="handleKeydown"
          @input="onPromptInput"
        />
        <div
          v-if="mentionOpen && mentionCandidates.length > 0"
          class="absolute left-4 right-4 bottom-full z-10 mb-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover shadow-md py-1"
          role="listbox"
          aria-label="Mention suggestions"
        >
          <button
            v-for="(candidate, idx) in mentionCandidates"
            :key="`${candidate.kind}:${candidate.insert}`"
            type="button"
            role="option"
            :aria-selected="idx === mentionIndex"
            class="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors"
            :class="idx === mentionIndex ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'"
            @mousedown.prevent="chooseMention(candidate)"
            @mousemove="mentionIndex = idx"
          >
            <span
              class="rounded px-1 py-0.5 text-[10px] font-medium"
              :class="candidate.kind === 'agent' ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'"
            >
              {{ candidate.kind }}
            </span>
            <span class="truncate flex-1">{{ candidate.label }}</span>
          </button>
        </div>
      </div>
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
