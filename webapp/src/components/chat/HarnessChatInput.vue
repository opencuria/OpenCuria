<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
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
import { BookText, FolderTree, Send, Square, X } from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'
import type { FileNode, Skill } from '@/types'
import { getProviderConfig } from '@/services/harness.api'
import { useChatInputCache } from '@/composables/useChatInputCache'
import WorkspaceFilePicker from '@/components/chat/WorkspaceFilePicker.vue'
import {
  applyMentionCandidate,
  detectMentionQuery,
  filterMentionCandidates,
  flattenFilePaths,
  type MentionCandidate,
} from '@/lib/harnessMentions'
import { buildWorkspaceReferenceMarkdown, classifyWorkspaceFile } from '@/lib/workspaceFileRefs'

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  stoppable?: boolean
  busyMessage?: string
  workspaceId?: string
  sessionId?: string | null
  files?: FileNode[]
  model?: string
  mode?: HarnessSessionMode
  skillOptions?: Skill[]
}>()

const emit = defineEmits<{
  send: [prompt: string, mode: HarnessSessionMode, model: string, skillIds: string[]]
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
const providerMissing = ref(false)
const selectedSkillIds = ref<string[]>([])
const skillDropdownOpen = ref(false)
const filePickerOpen = ref(false)
const skillButtonRef = ref<HTMLElement | null>(null)
const fileButtonRef = ref<HTMLElement | null>(null)

const ORG_DEFAULT = '__org_default__'
const CUSTOM_VALUE = '__custom__'

const { loadFromCache, saveToCache, clearCache } = useChatInputCache(
  () => props.workspaceId || '',
  () => props.sessionId,
)

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

const selectedSkills = computed(() =>
  (props.skillOptions ?? []).filter((skill) => selectedSkillIds.value.includes(skill.id)),
)

const canSend = computed(
  () => prompt.value.trim().length > 0 && !props.disabled && !props.sending,
)
const canStop = computed(() => Boolean(props.stoppable) && !props.sending)
const canManageFiles = computed(
  () => !props.disabled && !props.sending && Boolean(props.workspaceId),
)

async function loadProviderConfig(): Promise<void> {
  modelLoading.value = true
  providerMissing.value = false
  try {
    const config = await getProviderConfig()
    if (!config.has_api_key) {
      providerMissing.value = true
      modelOptions.value = []
      return
    }
    const models = [config.default_model, config.small_model].filter(
      (m): m is string => typeof m === 'string' && m.trim().length > 0,
    )
    modelOptions.value = [...new Set(models)]
  } catch {
    providerMissing.value = true
    modelOptions.value = []
  } finally {
    modelLoading.value = false
  }
}

onMounted(() => {
  const cached = loadFromCache()
  if (cached) prompt.value = cached
  void loadProviderConfig()
})

onBeforeUnmount(() => {
  saveToCache(prompt.value)
})

watch(
  () => props.workspaceId,
  () => {
    localModel.value = ''
    emit('update:model', '')
    prompt.value = loadFromCache()
    void loadProviderConfig()
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
  () => props.mode,
  (next) => {
    if (next) localMode.value = next
  },
)

watch(prompt, (value) => {
  saveToCache(value)
})

function handleSend(): void {
  if (!canSend.value) return
  emit('send', prompt.value.trim(), localMode.value, localModel.value.trim(), [
    ...selectedSkillIds.value,
  ])
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
  closeMention()
}

function setMode(mode: HarnessSessionMode): void {
  localMode.value = mode
  emit('update:mode', mode)
}

function setModel(value: unknown): void {
  const next =
    value === ORG_DEFAULT ? '' : value === CUSTOM_VALUE ? localModel.value : String(value ?? '')
  localModel.value = next
  emit('update:model', next)
}

function onCustomModelInput(e: Event): void {
  const value = (e.target as HTMLInputElement).value
  localModel.value = value
  emit('update:model', value)
}

function toggleSkill(id: string): void {
  if (selectedSkillIds.value.includes(id)) {
    selectedSkillIds.value = selectedSkillIds.value.filter((skillId) => skillId !== id)
  } else {
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

defineExpose({ clearInput })

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

watch(skillDropdownOpen, (open) => {
  if (open) filePickerOpen.value = false
})

watch(filePickerOpen, (open) => {
  if (open) skillDropdownOpen.value = false
})
</script>

<template>
  <div class="min-w-0 w-full bg-transparent px-3 pb-2 pt-3 sm:px-4 sm:pt-4">
    <div class="flex flex-col rounded-xl border border-border bg-card shadow-sm transition-all duration-200 focus-within:border-primary">
      <div class="flex flex-wrap items-center gap-2 px-4 pt-3">
        <RouterLink
          v-if="providerMissing"
          to="/org-settings?tab=provider"
          class="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
        >
          Configure OpenRouter in Org Settings
        </RouterLink>
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

        <div class="relative">
          <Button
            ref="skillButtonRef"
            type="button"
            variant="outline"
            size="sm"
            class="h-8 gap-1 text-xs"
            :disabled="!skillOptions?.length"
            @click="skillDropdownOpen = !skillDropdownOpen"
          >
            <BookText :size="14" />
            Skills
            <span v-if="selectedSkillIds.length" class="text-primary">({{ selectedSkillIds.length }})</span>
          </Button>
          <div
            v-if="skillDropdownOpen && skillOptions?.length"
            class="absolute bottom-full left-0 z-20 mb-1 w-64 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover p-1 shadow-md"
          >
            <button
              v-for="skill in skillOptions"
              :key="skill.id"
              type="button"
              class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-muted"
              :class="selectedSkillIds.includes(skill.id) ? 'bg-primary/10 text-primary' : 'text-foreground'"
              @mousedown.prevent="toggleSkill(skill.id)"
            >
              <BookText :size="12" />
              <span class="truncate">{{ skill.name }}</span>
            </button>
          </div>
        </div>

        <div v-if="workspaceId" class="relative">
          <Button
            ref="fileButtonRef"
            type="button"
            variant="outline"
            size="sm"
            class="h-8 gap-1 text-xs"
            :disabled="!canManageFiles"
            @click="filePickerOpen = !filePickerOpen"
          >
            <FolderTree :size="14" />
            Files
          </Button>
          <div v-if="filePickerOpen" class="absolute bottom-full left-0 z-20 mb-1">
            <WorkspaceFilePicker
              :workspace-id="workspaceId"
              :disabled="!canManageFiles"
              @select="handleFileSelected"
              @close="filePickerOpen = false"
            />
          </div>
        </div>
      </div>

      <div v-if="selectedSkills.length" class="flex flex-wrap gap-1.5 px-4 pb-0 pt-2">
        <span
          v-for="skill in selectedSkills"
          :key="skill.id"
          class="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
        >
          <BookText :size="10" />
          {{ skill.name }}
          <button type="button" class="transition-opacity hover:opacity-70" @click="removeSkill(skill.id)">
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
          placeholder="Send a prompt to the agent… (@file, @agent)"
          class="min-h-[50px] max-h-[200px] w-full resize-none !rounded-none !border-0 !bg-transparent px-4 py-3 text-base !shadow-none !outline-none !ring-0 focus:!border-transparent focus:!shadow-none focus-visible:!outline-none focus-visible:ring-0"
          @keydown="handleKeydown"
          @input="onPromptInput"
        />
        <div
          v-if="mentionOpen && mentionCandidates.length > 0"
          class="absolute bottom-full left-4 right-4 z-10 mb-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover py-1 shadow-md"
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
          class="h-9 w-9 shrink-0 rounded-full transition-all"
          :class="canStop ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : 'bg-muted text-muted-foreground'"
          title="Stop current run"
          @click="emit('stop')"
        >
          <Square :size="16" />
        </Button>
        <Button
          v-else
          :disabled="!canSend"
          size="icon"
          class="h-9 w-9 shrink-0 rounded-full transition-all"
          :class="canSend ? 'bg-primary text-primary-foreground hover:bg-primary/90' : 'bg-muted text-muted-foreground'"
          @click="handleSend"
        >
          <Send :size="18" />
        </Button>
      </div>
    </div>
    <p v-if="busyMessage" class="mt-2 text-center text-xs text-amber-600 dark:text-amber-400">
      {{ busyMessage }}
    </p>
    <p v-else class="mt-2 hidden text-center text-xs text-muted-foreground sm:block">
      Press <kbd class="font-mono font-medium text-foreground">Enter</kbd> to send,
      <kbd class="font-mono font-medium text-foreground">Shift+Enter</kbd> for newline
    </p>
  </div>
</template>
