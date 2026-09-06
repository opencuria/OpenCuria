<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Textarea } from '@/components/ui/textarea'
import {
  BookText,
  Check,
  ChevronDown,
  Hammer,
  ListTodo,
  Paperclip,
  Send,
  Square,
  X,
} from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'
import type { FileNode, Skill } from '@/types'
import { getProviderConfig } from '@/services/harness.api'
import { resolveCatalogModel, type ProviderModel } from '@/lib/harnessModels'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { useChatInputCache } from '@/composables/useChatInputCache'
import WorkspaceFilePicker from '@/components/chat/WorkspaceFilePicker.vue'
import HarnessModelPicker from '@/components/chat/HarnessModelPicker.vue'
import { buildWorkspaceReferenceMarkdown, classifyWorkspaceFile } from '@/lib/workspaceFileRefs'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import {
  applyMentionCandidate,
  consumeSlashQuery,
  detectMentionQuery,
  detectSlashQuery,
  filterMentionCandidates,
  filterSkillCandidates,
  flattenFilePaths,
  mentionFileSearchQuery,
  mergeMentionFilePaths,
  createPointerHoverGate,
  MENTION_FIND_LIMIT,
  type MentionCandidate,
} from '@/lib/harnessMentions'

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  stoppable?: boolean
  busyMessage?: string
  workspaceId?: string
  sessionId?: string | null
  files?: FileNode[]
  model?: string
  effort?: string
  mode?: HarnessSessionMode
  skillOptions?: Skill[]
  /**
   * External mention navigation state mirrored from the parent sheet stack.
   * When `mentionControlled` is true the input renders no popup itself and
   * emits `mention-select` instead of inserting candidates directly.
   */
  mentionControlled?: boolean
  mentionActiveIndex?: number
  /**
   * True when a composer sheet is rendered directly above the input. Removes
   * the wrapper top padding so the sheet sits flush on the input card.
   */
  attached?: boolean
  contextUsed?: number
  contextOpen?: boolean
}>()

const emit = defineEmits<{
  send: [prompt: string, mode: HarnessSessionMode, model: string, skillIds: string[], effort: string]
  stop: []
  'update:model': [value: string]
  'update:effort': [value: string]
  'update:mode': [value: HarnessSessionMode]
  /** Emitted on every textarea change so the parent can mirror `@`/`/` state into the sheet stack. */
  'mention-change': [open: boolean, query: string, candidates: MentionCandidate[], index: number]
  /** Emitted when the user picks a mention candidate from the sheet stack. */
  'mention-select': [candidate: MentionCandidate]
  'toggle-context': []
  'context-metrics': [metrics: { used: number; limit: number; percent: number }]
}>()

const prompt = ref('')
const textareaRef = ref<{ $el?: unknown } | null>(null)
const localMode = ref<HarnessSessionMode>(props.mode ?? 'build')
const localModel = ref(props.model ?? '')
const localEffort = ref(props.effort ?? '')
const catalog = ref<ProviderModel[]>([])
const orgDefaultModel = ref('')
const modelLoading = ref(false)
const providerMissing = ref(false)
const selectedSkillIds = ref<string[]>([])
const filePickerOpen = ref(false)

const { loadFromCache, saveToCache, clearCache } = useChatInputCache(
  () => props.workspaceId || '',
  () => props.sessionId,
)

const selectedSkills = computed(() =>
  (props.skillOptions ?? []).filter((skill) => selectedSkillIds.value.includes(skill.id)),
)

const canSend = computed(() => prompt.value.trim().length > 0 && !props.disabled && !props.sending)
const canStop = computed(() => Boolean(props.stoppable) && !props.sending)
const canManageFiles = computed(
  () => !props.disabled && !props.sending && Boolean(props.workspaceId),
)

const CONTEXT_RING_RADIUS = 6
const contextRingCircumference = 2 * Math.PI * CONTEXT_RING_RADIUS

const contextLimit = computed(() => {
  const catalogModel = resolveCatalogModel(
    catalog.value,
    localModel.value,
    orgDefaultModel.value,
  )
  return catalogModel?.context_length ?? 0
})

const contextUsed = computed(() => Math.max(0, props.contextUsed ?? 0))

const contextPercent = computed(() => {
  if (contextLimit.value <= 0) return 0
  return Math.round((contextUsed.value / contextLimit.value) * 100)
})

const contextRingOffset = computed(() =>
  contextRingCircumference * (1 - Math.min(100, Math.max(0, contextPercent.value)) / 100),
)

const contextAriaLabel = computed(() => {
  if (contextLimit.value <= 0) return 'Context usage unknown'
  return `Context usage ${contextPercent.value}%`
})

watch(
  [contextUsed, contextLimit, contextPercent],
  () => {
    emit('context-metrics', {
      used: contextUsed.value,
      limit: contextLimit.value,
      percent: contextPercent.value,
    })
  },
  { immediate: true },
)

const modeIcon = computed(() => (localMode.value === 'plan' ? ListTodo : Hammer))
const modeLabel = computed(() => (localMode.value === 'plan' ? 'Plan' : 'Build'))

async function loadProviderModels(): Promise<void> {
  modelLoading.value = true
  providerMissing.value = false
  try {
    const config = await getProviderConfig()
    orgDefaultModel.value = config.default_model || ''
    if (!config.has_api_key) {
      providerMissing.value = true
      catalog.value = []
      return
    }
    catalog.value = await loadProviderModelsCached()
  } catch {
    providerMissing.value = true
    catalog.value = []
  } finally {
    modelLoading.value = false
  }
}

onMounted(() => {
  const cached = loadFromCache()
  if (cached) prompt.value = cached
  void loadProviderModels()
  void nextTick(() => {
    resizeTextarea()
    observeTextareaResize()
  })
})

onBeforeUnmount(() => {
  saveToCache(prompt.value)
  if (mentionFindTimer) {
    clearTimeout(mentionFindTimer)
    mentionFindTimer = null
  }
  textareaResizeObserver?.disconnect()
  textareaResizeObserver = null
})

watch(
  () => props.workspaceId,
  () => {
    localModel.value = ''
    emit('update:model', '')
    prompt.value = loadFromCache()
    void loadProviderModels()
  },
)

watch(
  () => props.sessionId,
  () => {
    prompt.value = loadFromCache()
  },
)

watch(
  () => props.model,
  (next) => {
    if (typeof next === 'string' && next !== localModel.value) localModel.value = next
  },
)

watch(
  () => props.effort,
  (next) => {
    if (typeof next === 'string' && next !== localEffort.value) localEffort.value = next
  },
)

watch(
  () => props.mode,
  (next) => {
    if (next) localMode.value = next
  },
)

watch(prompt, (value) => {
  saveToCache(value)
  void nextTick(resizeTextarea)
})

function handleSend(): void {
  if (!canSend.value) return
  emit('send', prompt.value.trim(), localMode.value, localModel.value.trim(), [
    ...selectedSkillIds.value,
  ], localEffort.value.trim())
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
  closeComposerQuery()
}

function setMode(mode: HarnessSessionMode): void {
  localMode.value = mode
  emit('update:mode', mode)
}

function toggleMode(): void {
  setMode(localMode.value === 'build' ? 'plan' : 'build')
}

function setModel(value: string): void {
  localModel.value = value
  emit('update:model', value)
}

function setEffort(value: string): void {
  localEffort.value = value
  emit('update:effort', value)
}

function addSkill(id: string): void {
  if (!selectedSkillIds.value.includes(id)) {
    selectedSkillIds.value = [...selectedSkillIds.value, id]
  }
}

function removeSkill(id: string): void {
  selectedSkillIds.value = selectedSkillIds.value.filter((skillId) => skillId !== id)
}

function handleFileSelected(path: string, filename: string): void {
  if (!canManageFiles.value) return
  const kind = classifyWorkspaceFile(path)
  const ref = buildWorkspaceReferenceMarkdown(filename, path, kind)
  prompt.value = prompt.value ? `${prompt.value}\n${ref}` : ref
  filePickerOpen.value = false
}

function clearInput(): void {
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
}

const fileExplorer = useFileExplorerStore()
const mentionOpen = ref(false)
const mentionIndex = ref(0)
const mentionQuery = ref('')
const composerKind = ref<'mention' | 'skill' | null>(null)
const mentionSearchPaths = ref<string[]>([])
const mentionPopupListRef = ref<HTMLElement | null>(null)
const mentionPointerHover = createPointerHoverGate()
let mentionFindTimer: ReturnType<typeof setTimeout> | null = null
let mentionFindGeneration = 0
let mentionSearchStarted = false

const MENTION_FIND_DEBOUNCE_MS = 150

/**
 * Locally filtered mention/skill candidates. In controlled (sheet-stack) mode the
 * parent mirrors this list into the topmost stack sheet.
 */
const mentionCandidates = computed<MentionCandidate[]>(() => {
  if (!mentionOpen.value) return []
  if (composerKind.value === 'skill') {
    return filterSkillCandidates(mentionQuery.value, props.skillOptions ?? [])
  }
  return filterMentionCandidates(
    mentionQuery.value,
    mergeMentionFilePaths(mentionSearchPaths.value, flattenFilePaths(props.files ?? [])),
  )
})

const mentionPopupCandidates = computed<MentionCandidate[]>(() =>
  props.mentionControlled ? [] : mentionCandidates.value,
)

watch(
  [mentionOpen, mentionQuery, mentionCandidates, mentionIndex],
  ([open, query, candidates, index]) => {
    if (props.mentionControlled) emit('mention-change', open, query, candidates, index)
  },
)

watch(
  () => props.mentionActiveIndex,
  (index) => {
    if (props.mentionControlled && typeof index === 'number') {
      mentionIndex.value = index
    }
  },
)

watch(
  () => props.mentionControlled,
  (controlled) => {
    if (controlled) {
      emit(
        'mention-change',
        mentionOpen.value,
        mentionQuery.value,
        mentionCandidates.value,
        mentionIndex.value,
      )
    }
  },
)

watch(mentionCandidates, (candidates) => {
  if (mentionIndex.value >= candidates.length) {
    mentionIndex.value = Math.max(0, candidates.length - 1)
  }
})

watch(
  () => [mentionIndex.value, mentionPopupCandidates.value.length] as const,
  () => {
    if (props.mentionControlled || mentionPopupCandidates.value.length === 0) return
    void nextTick(() => {
      const active = mentionPopupListRef.value?.querySelector<HTMLElement>(
        `[data-mention-index="${mentionIndex.value}"]`,
      )
      if (typeof active?.scrollIntoView === 'function') {
        active.scrollIntoView({ block: 'nearest' })
      }
    })
  },
)

function requestMentionSelect(candidate: MentionCandidate): void {
  if (props.mentionControlled) {
    emit('mention-select', candidate)
    return
  }
  chooseMention(candidate)
}

function onMentionPopupMouseMove(event: MouseEvent, index: number): void {
  if (!mentionPointerHover.moved(event)) return
  mentionIndex.value = index
}

function textareaEl(): HTMLTextAreaElement | null {
  const root = textareaRef.value
  if (!root) return null
  const el = (root as { $el?: unknown }).$el
  return el instanceof HTMLTextAreaElement ? el : null
}

const MAX_COMPOSER_HEIGHT = 200
let textareaResizeObserver: ResizeObserver | null = null

/**
 * Grow the composer to fit its content, capped at MAX_COMPOSER_HEIGHT.
 * Empty input resets to CSS min-height so it stays one line.
 */
function resizeTextarea(): void {
  const el = textareaEl()
  if (!el) return
  if (!el.value) {
    el.style.height = ''
    el.style.overflowY = ''
    return
  }
  el.style.height = 'auto'
  const content = el.scrollHeight
  if (content <= 0) return
  el.style.height = `${Math.min(content, MAX_COMPOSER_HEIGHT)}px`
  el.style.overflowY = content > MAX_COMPOSER_HEIGHT ? 'auto' : 'hidden'
}

function observeTextareaResize(): void {
  if (typeof ResizeObserver === 'undefined') return
  const el = textareaEl()
  if (!el) return
  textareaResizeObserver?.disconnect()
  let lastWidth = el.offsetWidth
  textareaResizeObserver = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width ?? 0
    if (width === lastWidth) return
    lastWidth = width
    resizeTextarea()
  })
  textareaResizeObserver.observe(el)
}

function closeComposerQuery(): void {
  mentionOpen.value = false
  mentionIndex.value = 0
  mentionQuery.value = ''
  composerKind.value = null
  mentionSearchPaths.value = []
  mentionSearchStarted = false
  mentionPointerHover.reset()
  mentionFindGeneration += 1
  if (mentionFindTimer) {
    clearTimeout(mentionFindTimer)
    mentionFindTimer = null
  }
}

async function runMentionFileSearch(workspaceId: string, query: string): Promise<void> {
  const generation = ++mentionFindGeneration
  const paths = await fileExplorer.findFiles(workspaceId, query, MENTION_FIND_LIMIT)
  if (generation !== mentionFindGeneration) return
  mentionSearchPaths.value = paths
}

function scheduleMentionFileSearch(rawQuery: string): void {
  const fileQuery = mentionFileSearchQuery(rawQuery)
  if (fileQuery === null || !props.workspaceId) {
    mentionSearchPaths.value = []
    return
  }
  if (mentionFindTimer) {
    clearTimeout(mentionFindTimer)
    mentionFindTimer = null
  }
  const workspaceId = props.workspaceId
  const run = () => {
    void runMentionFileSearch(workspaceId, fileQuery)
  }
  if (!mentionSearchStarted) {
    mentionSearchStarted = true
    run()
    return
  }
  mentionFindTimer = setTimeout(run, MENTION_FIND_DEBOUNCE_MS)
}

function refreshComposerQuery(): void {
  const el = textareaEl()
  if (!el) return
  const cursor = el.selectionStart ?? el.value.length
  const mention = detectMentionQuery(el.value, cursor)
  if (mention !== null) {
    composerKind.value = 'mention'
    mentionQuery.value = mention
    mentionIndex.value = 0
    mentionOpen.value = true
    scheduleMentionFileSearch(mention)
    return
  }
  const slash = detectSlashQuery(el.value, cursor)
  if (slash !== null) {
    composerKind.value = 'skill'
    mentionQuery.value = slash
    mentionIndex.value = 0
    mentionOpen.value = true
    return
  }
  closeComposerQuery()
}

function chooseMention(candidate: MentionCandidate): void {
  const el = textareaEl()
  if (!el) return
  const cursor = el.selectionStart ?? el.value.length
  if (candidate.kind === 'skill') {
    addSkill(candidate.insert)
    const next = consumeSlashQuery(el.value, cursor)
    prompt.value = next.text
    closeComposerQuery()
    void nextTick(() => {
      el.focus()
      el.setSelectionRange(next.cursor, next.cursor)
    })
    return
  }
  const next = applyMentionCandidate(el.value, cursor, candidate)
  prompt.value = next.text
  closeComposerQuery()
  void nextTick(() => {
    el.focus()
    el.setSelectionRange(next.cursor, next.cursor)
  })
}

defineExpose({ clearInput, chooseMention })

function onPromptInput(): void {
  refreshComposerQuery()
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
    if (e.key === 'Tab' && !e.shiftKey) {
      const candidate = mentionCandidates.value[mentionIndex.value]
      if (candidate) {
        e.preventDefault()
        requestMentionSelect(candidate)
        return
      }
    }
    if (e.key === 'Enter') {
      const candidate = mentionCandidates.value[mentionIndex.value]
      if (candidate) {
        e.preventDefault()
        requestMentionSelect(candidate)
        return
      }
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closeComposerQuery()
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

function onComposerKeydown(e: KeyboardEvent): void {
  if (e.key === 'Tab' && e.shiftKey) {
    e.preventDefault()
    toggleMode()
  }
}
</script>

<template>
  <div
    class="relative min-w-0 w-full bg-transparent px-3 pb-2 sm:px-4"
    :class="attached ? 'pt-0' : 'pt-3 sm:pt-4'"
    @keydown="onComposerKeydown"
  >
    <div
      class="flex flex-col rounded-xl border border-border bg-card shadow-sm transition-all duration-200 focus-within:border-primary"
      data-testid="composer-card"
    >
      <RouterLink
        v-if="providerMissing"
        to="/org-settings?tab=provider"
        class="mx-4 mt-3 w-fit rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
      >
        Configure OpenRouter in Org Settings
      </RouterLink>

      <div v-if="selectedSkills.length" class="flex flex-wrap gap-1.5 px-4 pt-3">
        <span
          v-for="skill in selectedSkills"
          :key="skill.id"
          class="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
        >
          <BookText :size="10" />
          {{ skill.name }}
          <button
            type="button"
            class="transition-opacity hover:opacity-70"
            @click="removeSkill(skill.id)"
          >
            <X :size="11" />
          </button>
        </span>
      </div>

      <div class="relative">
        <Textarea
          ref="textareaRef"
          v-model="prompt"
          :disabled="disabled"
          :rows="1"
          placeholder="Plan, Build, / for skills, @ for context"
          class="min-h-10 max-h-[200px] w-full resize-none !rounded-none !border-0 !bg-transparent px-4 py-2 text-base !shadow-none !outline-none !ring-0 transition-[height] duration-100 ease-out focus:!border-transparent focus:!shadow-none focus-visible:!outline-none focus-visible:ring-0 md:min-h-9"
          data-testid="composer-textarea"
          @keydown="handleKeydown"
          @input="onPromptInput"
        />
        <div
          v-if="mentionPopupCandidates.length > 0"
          ref="mentionPopupListRef"
          class="absolute bottom-full left-4 right-4 z-10 mb-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover py-1 shadow-md"
          role="listbox"
          :aria-label="composerKind === 'skill' ? 'Skill suggestions' : 'Mention suggestions'"
        >
          <button
            v-for="(candidate, idx) in mentionPopupCandidates"
            :key="`${candidate.kind}:${candidate.insert}`"
            type="button"
            role="option"
            :aria-selected="idx === mentionIndex"
            :data-mention-index="idx"
            class="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors"
            :class="
              idx === mentionIndex
                ? 'bg-muted text-foreground'
                : 'text-muted-foreground hover:text-foreground'
            "
            @mousedown.prevent="requestMentionSelect(candidate)"
            @mousemove="onMentionPopupMouseMove($event, idx)"
          >
            <span
              class="rounded px-1 py-0.5 text-[10px] font-medium"
              :class="
                candidate.kind === 'agent' || candidate.kind === 'skill'
                  ? 'bg-primary/10 text-primary'
                  : 'bg-muted text-muted-foreground'
              "
            >
              {{ candidate.kind }}
            </span>
            <span class="truncate flex-1">{{ candidate.label }}</span>
          </button>
        </div>
      </div>

      <div class="flex items-center gap-1 p-2 pl-2 pb-2">
        <DropdownMenu>
          <DropdownMenuTrigger as-child>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              class="h-8 gap-1.5 rounded-full px-2.5 text-xs font-medium"
              data-testid="composer-mode-trigger"
              :disabled="disabled"
            >
              <component :is="modeIcon" :size="14" />
              {{ modeLabel }}
              <ChevronDown :size="12" class="opacity-70" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" class="min-w-36">
            <DropdownMenuItem class="text-xs" data-testid="composer-mode-build" @click="setMode('build')">
              <Hammer :size="14" />
              Build
              <Check v-if="localMode === 'build'" class="ml-auto size-3.5" />
            </DropdownMenuItem>
            <DropdownMenuItem class="text-xs" data-testid="composer-mode-plan" @click="setMode('plan')">
              <ListTodo :size="14" />
              Plan
              <Check v-if="localMode === 'plan'" class="ml-auto size-3.5" />
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <HarnessModelPicker
          :model="localModel"
          :effort="localEffort"
          :models="catalog"
          :loading="modelLoading"
          :default-model="orgDefaultModel"
          :disabled="disabled"
          @update:model="setModel"
          @update:effort="setEffort"
        />

        <div class="ml-auto flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            class="h-8 w-8 text-muted-foreground hover:text-foreground"
            :class="contextOpen ? 'text-foreground' : ''"
            :title="contextAriaLabel"
            :aria-label="contextAriaLabel"
            data-testid="composer-context-usage"
            @click="emit('toggle-context')"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              aria-hidden="true"
              class="shrink-0"
            >
              <circle
                cx="8"
                cy="8"
                :r="CONTEXT_RING_RADIUS"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                class="text-muted-foreground/30"
              />
              <circle
                cx="8"
                cy="8"
                :r="CONTEXT_RING_RADIUS"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                class="text-primary"
                :stroke-dasharray="contextRingCircumference"
                :stroke-dashoffset="contextRingOffset"
                transform="rotate(-90 8 8)"
              />
            </svg>
          </Button>
          <div v-if="workspaceId" class="relative">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              class="h-8 w-8 text-muted-foreground hover:text-foreground"
              :disabled="!canManageFiles"
              title="Attach a workspace file"
              data-testid="composer-attach"
              @click="filePickerOpen = !filePickerOpen"
            >
              <Paperclip :size="16" />
            </Button>
            <div v-if="filePickerOpen" class="absolute bottom-full right-0 z-20 mb-1">
              <WorkspaceFilePicker
                :workspace-id="workspaceId"
                :disabled="!canManageFiles"
                @select="handleFileSelected"
                @close="filePickerOpen = false"
              />
            </div>
          </div>
          <Button
            v-if="stoppable"
            :disabled="!canStop"
            size="icon"
            class="h-8 w-8 shrink-0 rounded-full transition-all"
            :class="
              canStop
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'bg-muted text-muted-foreground'
            "
            title="Stop current run"
            data-testid="composer-stop"
            @click="emit('stop')"
          >
            <Square :size="14" />
          </Button>
          <Button
            v-else
            :disabled="!canSend"
            size="icon"
            class="h-8 w-8 shrink-0 rounded-full transition-all"
            :class="
              canSend
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'bg-muted text-muted-foreground'
            "
            title="Send"
            data-testid="composer-send"
            @click="handleSend"
          >
            <Send :size="16" />
          </Button>
        </div>
      </div>
    </div>
    <p v-if="busyMessage" class="mt-2 text-center text-xs text-amber-600 dark:text-amber-400">
      {{ busyMessage }}
    </p>
    <p v-else class="mt-2 hidden text-center text-xs text-muted-foreground sm:block">
      Press <kbd class="font-mono font-medium text-foreground">Enter</kbd> to send,
      <kbd class="font-mono font-medium text-foreground">Shift+Enter</kbd> for newline,
      <kbd class="font-mono font-medium text-foreground">Shift+Tab</kbd> to switch mode
    </p>
  </div>
</template>
