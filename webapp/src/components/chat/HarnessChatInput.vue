<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import ComposerRichEditor from '@/components/chat/ComposerRichEditor.vue'
import HarnessMentionImages from '@/components/chat/HarnessMentionImages.vue'
import HarnessMentionSheet from '@/components/chat/HarnessMentionSheet.vue'
import { imageMentionTokens, removeComposerToken } from '@/lib/composerTokens'
import {
  BookText,
  Check,
  ChevronDown,
  Hammer,
  ListTodo,
  Loader2,
  Paperclip,
  Send,
  Square,
  X,
} from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'
import type { FileNode, Skill } from '@/types'
import { sendFilesUpload, subscribeToWorkspace } from '@/services/socket'
import { resolveCatalogModel, snapEffort, type ProviderModel } from '@/lib/harnessModels'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { loadRecentModels, recentCatalogModels, useRecentModels } from '@/lib/recentModels'
import { useChatInputCache } from '@/composables/useChatInputCache'
import ModelPicker from '@/components/common/ModelPicker.vue'
import WorkspaceFileIcon from '@/components/files/WorkspaceFileIcon.vue'
import {
  appendUploadMentions,
  CHAT_UPLOAD_DIR,
  collectUploadDirFilenames,
  fileToBase64,
  getDroppedFiles,
  hasTreePath,
  hasUploadMention,
  isFileDrag,
  nextChatUploadRequestId,
  resolveUniqueFilename,
  sanitizeUploadFilename,
  UPLOAD_MAX_BYTES,
  uploadTargetPath,
} from '@/lib/chatUpload'
import { OPEN_SETTINGS_EVENT } from '@/components/settings/settingsTabs'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceFileEvents } from '@/composables/useWorkspaceFileEvents'
import { useHarnessStore } from '@/stores/harness'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
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

/** External drag state mirrored from the surrounding drop zone (chat history
 * or home view). Renders a subtle highlight on the composer card only —
 * there is no full-area overlay. Drop handling lives in the parent, which
 * forwards files via the exposed `uploadChatFiles`. */
export interface ChatUploadDragState {
  active: boolean
  uploading: boolean
}

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
  uploadDrag?: ChatUploadDragState
  /** Scheduled-task editor uses the shared composer without chat-only state. */
  variant?: 'chat' | 'scheduled-task'
  prompt?: string
  skillIds?: string[]
  filesAvailable?: boolean
  promptError?: string
  resetKey?: string | number
}>()

const emit = defineEmits<{
  send: [
    prompt: string,
    mode: HarnessSessionMode,
    model: string,
    skillIds: string[],
    effort: string,
  ]
  stop: []
  'update:model': [value: string]
  'update:effort': [value: string]
  'update:mode': [value: HarnessSessionMode]
  'update:prompt': [value: string]
  'update:skillIds': [value: string[]]
  uploading: [value: boolean]
  /** Emitted on every textarea change so the parent can mirror `@`/`/` state into the sheet stack. */
  'mention-change': [open: boolean, query: string, candidates: MentionCandidate[], index: number]
  /** Emitted when the user picks a mention candidate from the sheet stack. */
  'mention-select': [candidate: MentionCandidate]
  'toggle-context': []
  'context-metrics': [metrics: { used: number; limit: number; percent: number }]
}>()

const isScheduledTask = computed(() => props.variant === 'scheduled-task')
const prompt = ref(props.prompt ?? '')
const textareaRef = ref<InstanceType<typeof ComposerRichEditor> | null>(null)
const imageTokens = computed(() => imageMentionTokens(prompt.value))
function removeImageToken(start: number, end: number): void {
  const next = removeComposerToken(prompt.value, start, end)
  prompt.value = next.text
  void nextTick(() => {
    textareaRef.value?.focus()
    textareaRef.value?.setCursor(next.cursor)
  })
}
const localMode = ref<HarnessSessionMode>(props.mode ?? 'build')
const localModel = ref(props.model ?? '')
const localEffort = ref(props.effort ?? '')
const catalog = ref<ProviderModel[]>([])
const { entries: recentEntries } = useRecentModels()
const recentModels = computed(() => recentCatalogModels(catalog.value))
const modelLoading = ref(true)
const providerMissing = ref(false)
const selectedSkillIds = ref<string[]>([...(props.skillIds ?? [])])
const fileInputRef = ref<HTMLInputElement | null>(null)
const isUploading = ref(false)
/**
 * Serializes concurrent `uploadChatFiles` calls (e.g. a drop handled by
 * both the composer card and the panel zone): later batches chain onto the
 * in-flight upload instead of sending the same files a second time.
 */
let uploadQueue: Promise<void> = Promise.resolve()
/** Target paths with an in-flight or recently finished chat upload. */
const inFlightUploadPaths = new Set<string>()
function uploadReservationKey(workspaceId: string, path: string): string {
  return `${workspaceId}:${path}`
}
const harness = useHarnessStore()
const authStore = useAuthStore()
let applyingDefaults = false
let modelRequestGeneration = 0

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
  () =>
    !props.disabled &&
    !props.sending &&
    Boolean(props.workspaceId) &&
    (!isScheduledTask.value || props.filesAvailable !== false),
)
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
let releaseMentionSubscription: (() => void) | null = null

const composerId = useId()
const mentionListboxId = `composer-mentions-${composerId}`
const skillListboxId = `composer-skills-${composerId}`
const activeListboxId = computed(() =>
  composerKind.value === 'skill' ? skillListboxId : mentionListboxId,
)
const activeOptionId = computed(() => `${activeListboxId.value}-option-${mentionIndex.value}`)
const suggestionOpen = computed(
  () => isScheduledTask.value && mentionOpen.value && mentionCandidates.value.length > 0,
)
const suggestionAnchor = ref<HTMLElement | null>(null)
function suggestionContentElement(): HTMLElement | null {
  return (
    document
      .getElementById(activeListboxId.value)
      ?.closest<HTMLElement>('[data-testid="composer-suggestions-portal"]') ?? null
  )
}
const suggestionMaxHeight = ref(192)
let suggestionResizeObserver: ResizeObserver | null = null
let suggestionVerticalOffset = 0

function clampSuggestionIntoViewport(): void {
  const content = suggestionContentElement()
  if (!content) return
  const viewport = window.visualViewport
  const viewportTop = viewport?.offsetTop ?? 0
  const viewportBottom = viewportTop + (viewport?.height ?? window.innerHeight)
  const rect = content.getBoundingClientRect()
  // Keep the Popper placement on top; in cramped viewports, offset only the
  // portalled overlay so at least one option remains reachable without layout shift.
  const baseTop = rect.top - suggestionVerticalOffset
  const maxTop = Math.max(viewportTop + 8, viewportBottom - 8 - rect.height)
  const nextOffset = Math.min(Math.max(baseTop, viewportTop + 8), maxTop) - baseTop
  suggestionVerticalOffset = nextOffset
  content.style.translate = `0 ${nextOffset}px`
}

function updateSuggestionMaxHeight(): void {
  const anchor = suggestionAnchor.value
  if (!anchor) return
  const viewport = window.visualViewport
  const viewportTop = viewport?.offsetTop ?? 0
  const viewportBottom = viewportTop + (viewport?.height ?? window.innerHeight)
  const viewportHeight = Math.max(1, viewportBottom - viewportTop)
  const maxVisibleHeight = Math.max(1, viewportHeight - 16)
  const minVisibleHeight = Math.min(48, maxVisibleHeight)
  const anchorRect = anchor.getBoundingClientRect()
  if (anchorRect.height === 0) {
    // jsdom and hidden layout containers don't provide viewport coordinates.
    suggestionMaxHeight.value = 192
    return
  }
  const availableAbove = Math.floor(anchorRect.top - viewportTop - 8)
  suggestionMaxHeight.value = Math.min(
    maxVisibleHeight,
    Math.max(minVisibleHeight, Math.min(192, availableAbove)),
  )
  suggestionVerticalOffset = 0
  const content = suggestionContentElement()
  if (content) content.style.translate = ''
  void nextTick(clampSuggestionIntoViewport)
}

function stopObservingSuggestionViewport(): void {
  suggestionResizeObserver?.disconnect()
  suggestionResizeObserver = null
  window.removeEventListener('resize', updateSuggestionMaxHeight)
  window.removeEventListener('scroll', updateSuggestionMaxHeight, true)
  window.visualViewport?.removeEventListener('resize', updateSuggestionMaxHeight)
  window.visualViewport?.removeEventListener('scroll', updateSuggestionMaxHeight)
}

watch(suggestionOpen, async (open) => {
  stopObservingSuggestionViewport()
  if (!open) return
  await nextTick()
  suggestionVerticalOffset = 0
  const content = suggestionContentElement()
  if (content) content.style.translate = ''
  updateSuggestionMaxHeight()
  if (typeof ResizeObserver !== 'undefined' && suggestionAnchor.value) {
    suggestionResizeObserver = new ResizeObserver(() => {
      updateSuggestionMaxHeight()
      clampSuggestionIntoViewport()
    })
    suggestionResizeObserver.observe(suggestionAnchor.value)
    const content = suggestionContentElement()
    if (content) suggestionResizeObserver.observe(content)
  }
  window.addEventListener('resize', updateSuggestionMaxHeight)
  window.addEventListener('scroll', updateSuggestionMaxHeight, true)
  window.visualViewport?.addEventListener('resize', updateSuggestionMaxHeight)
  window.visualViewport?.addEventListener('scroll', updateSuggestionMaxHeight)
})

const CONTEXT_RING_RADIUS = 6
const contextRingCircumference = 2 * Math.PI * CONTEXT_RING_RADIUS

const contextLimit = computed(() => {
  const catalogModel = resolveCatalogModel(catalog.value, localModel.value)
  return catalogModel?.context_length ?? 0
})

const contextUsed = computed(() => Math.max(0, props.contextUsed ?? 0))

const contextPercent = computed(() => {
  if (contextLimit.value <= 0) return 0
  return Math.round((contextUsed.value / contextLimit.value) * 100)
})

const contextRingOffset = computed(
  () => contextRingCircumference * (1 - Math.min(100, Math.max(0, contextPercent.value)) / 100),
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

/**
 * Öffnet das Provider-Tab direkt per Window-Event — bewusst OHNE Router-Navigation:
 * Der Host hängt global in AppLayout, der `/?settings=`-Deep-Link bleibt für
 * Redirects/e2e erhalten. So kann der Banner-Klick keine Navigation abbrechen
 * oder eine Replace-Schleife auslösen (gemeldeter "Page Unresponsive"-Hänger).
 */
function openProviderSettings(): void {
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'provider' } }))
}

function applyModeDefaults(mode: HarnessSessionMode, opts: { resetDirty?: boolean } = {}): void {
  const defaults = harness.agentDefault(mode)
  if (isScheduledTask.value || (!defaults.model && !defaults.effort)) return
  applyingDefaults = true
  try {
    localModel.value = defaults.model
    const catalogModel = resolveCatalogModel(catalog.value, defaults.model)
    localEffort.value = catalogModel ? snapEffort(catalogModel, defaults.effort) : defaults.effort
    emit('update:model', localModel.value)
    emit('update:effort', localEffort.value)
  } finally {
    applyingDefaults = false
  }
  if (opts.resetDirty) harness.composerDirty = false
}

async function loadProviderModels(): Promise<void> {
  modelLoading.value = true
  providerMissing.value = false
  const requestGeneration = ++modelRequestGeneration
  const organizationId = authStore.activeOrganizationId
  try {
    const [models] = await Promise.all([
      loadProviderModelsCached(),
      ...(isScheduledTask.value ? [] : [harness.loadAgentConfigs()]),
      loadRecentModels(),
    ])
    if (
      requestGeneration !== modelRequestGeneration ||
      organizationId !== authStore.activeOrganizationId
    )
      return
    catalog.value = models
    if (catalog.value.length === 0) providerMissing.value = true
  } catch {
    if (
      requestGeneration !== modelRequestGeneration ||
      organizationId !== authStore.activeOrganizationId
    )
      return
    providerMissing.value = true
    catalog.value = []
  } finally {
    if (
      requestGeneration === modelRequestGeneration &&
      organizationId === authStore.activeOrganizationId
    )
      modelLoading.value = false
  }
  if (!isScheduledTask.value && !harness.composerDirty && !props.model && !props.effort) {
    applyModeDefaults(localMode.value, { resetDirty: true })
  }
}

onMounted(() => {
  if (!isScheduledTask.value) {
    const cached = loadFromCache()
    if (cached) prompt.value = cached
  }
  void loadProviderModels()
  void nextTick(() => {
    resizeTextarea()
    observeTextareaResize()
  })
})

onBeforeUnmount(() => {
  uploadDisposed = true
  invalidateUploads()
  if (!isScheduledTask.value) saveToCache(prompt.value)
  if (mentionFindTimer) {
    clearTimeout(mentionFindTimer)
    mentionFindTimer = null
  }
  textareaResizeObserver?.disconnect()
  textareaResizeObserver = null
  stopObservingSuggestionViewport()
  releaseMentionSubscription?.()
  releaseMentionSubscription = null
})

watch(
  () => props.workspaceId,
  () => {
    if (isScheduledTask.value) invalidateUploads()
    if (!isScheduledTask.value) {
      harness.composerDirty = false
      prompt.value = loadFromCache()
    } else {
      closeComposerQuery()
      mentionSearchPaths.value = []
      releaseMentionSubscription?.()
      releaseMentionSubscription = null
    }
    void loadProviderModels()
  },
)

watch(
  () => authStore.activeOrganizationId,
  () => void loadProviderModels(),
)

watch(
  () => props.sessionId,
  () => {
    if (!isScheduledTask.value) prompt.value = loadFromCache()
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
  if (isScheduledTask.value) emit('update:prompt', value)
  else saveToCache(value)
  void nextTick(resizeTextarea)
})
watch(
  () => props.prompt,
  (value) => {
    if (isScheduledTask.value && typeof value === 'string' && value !== prompt.value) {
      closeComposerQuery()
      prompt.value = value
    }
  },
)
watch(
  () => props.resetKey,
  () => {
    if (!isScheduledTask.value) return
    invalidateUploads()
    closeComposerQuery()
    releaseMentionSubscription?.()
    releaseMentionSubscription = null
    localMode.value = props.mode ?? 'build'
    localModel.value = props.model ?? ''
    localEffort.value = props.effort ?? ''
    selectedSkillIds.value = [...(props.skillIds ?? [])]
  },
)
watch(
  () => props.skillIds,
  (value) => {
    if (
      isScheduledTask.value &&
      JSON.stringify(value ?? []) !== JSON.stringify(selectedSkillIds.value)
    )
      selectedSkillIds.value = [...(value ?? [])]
  },
  { deep: true },
)
watch(selectedSkillIds, (ids) => {
  if (isScheduledTask.value) emit('update:skillIds', [...ids])
})

function handleSend(): void {
  if (isScheduledTask.value || !canSend.value) return
  emit(
    'send',
    prompt.value.trim(),
    localMode.value,
    localModel.value.trim(),
    [...selectedSkillIds.value],
    localEffort.value.trim(),
  )
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
  closeComposerQuery()
}

function setMode(mode: HarnessSessionMode): void {
  if (mode === localMode.value) {
    emit('update:mode', mode)
    return
  }
  localMode.value = mode
  emit('update:mode', mode)
  if (!isScheduledTask.value && !harness.composerDirty)
    applyModeDefaults(mode, { resetDirty: true })
}

function toggleMode(): void {
  setMode(localMode.value === 'build' ? 'plan' : 'build')
}

function setModel(value: string): void {
  localModel.value = value
  emit('update:model', value)
  if (!applyingDefaults && !isScheduledTask.value) harness.markComposerDirty()
}

function setEffort(value: string): void {
  localEffort.value = value
  emit('update:effort', value)
  if (!applyingDefaults && !isScheduledTask.value) harness.markComposerDirty()
}

function addSkill(id: string): void {
  if (!selectedSkillIds.value.includes(id)) {
    selectedSkillIds.value = [...selectedSkillIds.value, id]
  }
}

function removeSkill(id: string): void {
  selectedSkillIds.value = selectedSkillIds.value.filter((skillId) => skillId !== id)
}
watch(
  () => props.filesAvailable,
  (available) => {
    if (!isScheduledTask.value) return
    if (!available) {
      cancelMentionFileSearch()
    } else if (mentionOpen.value && composerKind.value === 'mention') {
      scheduleMentionFileSearch(mentionQuery.value)
    }
  },
)

/**
 * Upload browser files into the chat upload directory and insert an
 * `@file:` mention token per uploaded file (exactly like a mention-picker
 * selection: space-separated, one trailing space).
 *
 * Uploads never overwrite: file names are sanitized (`[^a-zA-Z0-9._-] →
 * _`) and deduplicated against the explorer tree (`_1`, `_2`, … suffix).
 * The runner `mkdir -p`s the upload directory on upload; the explorer tree
 * is refreshed afterwards so the files show up under Files and `@` search.
 *
 * Concurrent calls are serialized: a second drop that arrives while an
 * upload is in flight waits for the first batch instead of uploading the
 * same files twice.
 */
let uploadGeneration = 0
let uploadDisposed = false
let uploadDirectoryAbortController: AbortController | null = null
const ownedPendingUploadIds = new Set<string>()

function invalidateUploads(): void {
  uploadGeneration += 1
  uploadDirectoryAbortController?.abort()
  uploadDirectoryAbortController = null
  for (const requestId of ownedPendingUploadIds) fileExplorer.cancelUpload(requestId)
  ownedPendingUploadIds.clear()
  isUploading.value = false
  if (isScheduledTask.value && !uploadDisposed) emit('uploading', false)
}

async function uploadChatFiles(fileList: File[] | FileList): Promise<void> {
  const files = Array.from(fileList)
  if (files.length === 0 || !canManageFiles.value || !props.workspaceId) return
  const workspaceId = props.workspaceId
  const generation = uploadGeneration
  // Chain onto an in-flight batch so the same files are never uploaded
  // twice (double `@file:` references from reentrant drop handlers).
  const run = uploadQueue.then(() => runChatUpload(files, workspaceId, generation))
  uploadQueue = run.catch(() => undefined)
  await run
}

async function runChatUpload(
  files: File[],
  workspaceId: string,
  generation: number,
): Promise<void> {
  if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId) return
  isUploading.value = true
  if (isScheduledTask.value) emit('uploading', true)
  const notify = useNotificationStore()
  const reserved: string[] = []
  try {
    let taken: Set<string>
    if (isScheduledTask.value) {
      if (!releaseMentionSubscription)
        releaseMentionSubscription = subscribeToWorkspace(workspaceId)
      const abortController = new AbortController()
      uploadDirectoryAbortController = abortController
      const options = { signal: abortController.signal }
      const root = await fileExplorer.fetchDirectoryEntries(workspaceId, '/workspace', options)
      if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId)
        return
      if (!root) {
        notify.error('Upload failed', 'Could not check workspace upload filenames. Please retry.')
        return
      }
      const metadata = root.some(
        (entry) => entry.name === '.opencuria' && entry.type === 'directory',
      )
        ? await fileExplorer.fetchDirectoryEntries(workspaceId, '/workspace/.opencuria', options)
        : []
      if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId)
        return
      if (metadata === null) {
        notify.error('Upload failed', 'Could not check workspace upload filenames. Please retry.')
        return
      }
      const uploadDirectoryExists = metadata.some(
        (entry) => entry.name === 'user-uploaded' && entry.type === 'directory',
      )
      const directory = uploadDirectoryExists
        ? await fileExplorer.fetchDirectoryEntries(workspaceId, CHAT_UPLOAD_DIR, options)
        : []
      if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId)
        return
      if (directory === null) {
        notify.error('Upload failed', 'Could not check workspace upload filenames. Please retry.')
        return
      }
      taken = new Set(directory.map((entry) => entry.name))
    } else {
      if (!hasTreePath(fileExplorer.tree, CHAT_UPLOAD_DIR)) {
        await fileExplorer.fetchDirectory(workspaceId, CHAT_UPLOAD_DIR).catch(() => undefined)
      }
      taken = collectUploadDirFilenames(fileExplorer.tree)
    }
    if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId)
      return
    // Per-file transfer outcome; resolves to the workspace path on success,
    // null on failure (failures are toasted via the store already).
    // Every reserved path is released below, on success and on failure.
    const transfers: Array<Promise<string | null>> = []

    for (const file of files) {
      if (file.size > UPLOAD_MAX_BYTES) {
        const message = `File "${file.name}" exceeds the 10 MB upload limit and was skipped.`
        notify.error('Upload failed', message)
        continue
      }
      // Skip files whose upload is already referenced or in flight: a
      // drop handled by more than one drop zone (composer card + parent
      // panel) runs the second batch after the first, and must not upload
      // or reference the same file again.
      const filename = resolveUniqueFilename(taken, sanitizeUploadFilename(file.name))
      const targetPath = uploadTargetPath(filename)
      const reservation = uploadReservationKey(workspaceId, targetPath)
      if (inFlightUploadPaths.has(reservation) || hasUploadMention(prompt.value, targetPath)) {
        continue
      }
      inFlightUploadPaths.add(reservation)
      reserved.push(reservation)
      let content: string
      try {
        content = await fileToBase64(file)
      } catch {
        notify.error('Upload failed', `File "${file.name}" could not be read.`)
        continue
      }
      if (uploadDisposed || generation !== uploadGeneration || workspaceId !== props.workspaceId) {
        inFlightUploadPaths.delete(reservation)
        continue
      }
      const requestId = nextChatUploadRequestId(filename)
      // Register first so a synchronous sendFilesUpload throw (oversize
      // cap) still resolves through the tracked promise instead of
      // hanging the upload: fail the pending upload, then surface the
      // error visibly.
      const tracked = fileExplorer.trackAndUpload(
        workspaceId,
        requestId,
        CHAT_UPLOAD_DIR,
        filename,
        content,
        !isScheduledTask.value,
      )
      if (isScheduledTask.value) ownedPendingUploadIds.add(requestId)
      transfers.push(
        tracked.then(
          () => {
            ownedPendingUploadIds.delete(requestId)
            return targetPath
          },
          () => {
            ownedPendingUploadIds.delete(requestId)
            return null
          },
        ),
      )
      try {
        sendFilesUpload(workspaceId, requestId, CHAT_UPLOAD_DIR, filename, content, false)
      } catch (err) {
        const message = err instanceof Error ? err.message : 'The file could not be uploaded.'
        fileExplorer.failUpload(requestId, message)
      }
    }

    // Wait for all uploads to complete (success or error); failures are
    // already consumed above so this never throws.
    const uploaded = (await Promise.all(transfers)).filter((path): path is string => path !== null)
    if (
      uploaded.length > 0 &&
      !uploadDisposed &&
      generation === uploadGeneration &&
      workspaceId === props.workspaceId
    ) {
      prompt.value = appendUploadMentions(prompt.value, uploaded)
      if (isScheduledTask.value) emit('update:prompt', prompt.value)
      else saveToCache(prompt.value)
      void nextTick(resizeTextarea)
      focusTextarea()
      if (!isScheduledTask.value) fileExplorer.fetchDirectory(workspaceId, '/workspace')
    }
  } finally {
    if (uploadDirectoryAbortController?.signal.aborted || generation !== uploadGeneration) {
      uploadDirectoryAbortController = null
    } else if (isScheduledTask.value) {
      uploadDirectoryAbortController = null
    }
    for (const path of reserved) inFlightUploadPaths.delete(path)
    if (generation === uploadGeneration) {
      isUploading.value = false
      if (isScheduledTask.value && !uploadDisposed) emit('uploading', false)
      if (fileInputRef.value) fileInputRef.value.value = ''
    }
  }
}

function focusTextarea(): void {
  textareaRef.value?.focus(true)
}

/** Paperclip: open the native system file dialog for upload only. */
function triggerFileUpload(): void {
  if (!canManageFiles.value) return
  fileInputRef.value?.click()
}

function handleFileInput(event: Event): void {
  if (!canManageFiles.value) return
  const files = (event.target as HTMLInputElement).files
  if (!files || files.length === 0) return
  void uploadChatFiles(files)
}

function handleComposerDrop(event: DragEvent): void {
  if (!canManageFiles.value || !isFileDrag(event)) return
  // The composer card lives inside the panel/home drop zone: stop
  // propagation so the same drop is not uploaded a second time by the
  // parent forwarder (which also calls `uploadChatFiles`).
  event.preventDefault()
  event.stopPropagation()
  const files = getDroppedFiles(event)
  if (files.length === 0) return
  void uploadChatFiles(files)
}

function handleComposerDragOver(event: DragEvent): void {
  if (!canManageFiles.value || !isFileDrag(event)) return
  event.preventDefault()
  event.stopPropagation()
}

function clearInput(): void {
  prompt.value = ''
  selectedSkillIds.value = []
  if (!isScheduledTask.value) clearCache()
}

/**
 * Prefill the composer without sending (OpenCode fork parity: navigate
 * with prompt). Persists to the per-session cache so it survives remounts.
 */
function setPrompt(text: string): void {
  prompt.value = text
  if (!isScheduledTask.value) saveToCache(text)
  void nextTick(resizeTextarea)
}

const fileExplorer = useFileExplorerStore()
if (props.variant === 'scheduled-task') useWorkspaceFileEvents(() => props.workspaceId ?? '')

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
    if (props.mentionControlled && typeof index === 'number') mentionIndex.value = index
  },
)
watch(
  () => props.mentionControlled,
  (controlled) => {
    if (controlled)
      emit(
        'mention-change',
        mentionOpen.value,
        mentionQuery.value,
        mentionCandidates.value,
        mentionIndex.value,
      )
  },
)
watch(mentionCandidates, (candidates) => {
  if (mentionIndex.value >= candidates.length)
    mentionIndex.value = Math.max(0, candidates.length - 1)
})
watch(
  () => [mentionIndex.value, mentionPopupCandidates.value.length] as const,
  () => {
    if (props.mentionControlled || mentionPopupCandidates.value.length === 0) return
    void nextTick(() => {
      const active = mentionPopupListRef.value?.querySelector<HTMLElement>(
        `[data-mention-index="${mentionIndex.value}"]`,
      )
      active?.scrollIntoView?.({ block: 'nearest' })
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

let textareaResizeObserver: ResizeObserver | null = null
function resizeTextarea(): void {
  textareaRef.value?.resize()
}
function observeTextareaResize(): void {
  if (typeof ResizeObserver === 'undefined') return
  const el = textareaRef.value?.el
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

function cancelMentionFileSearch(): void {
  mentionFindGeneration += 1
  mentionSearchPaths.value = []
  mentionSearchStarted = false
  if (mentionFindTimer) {
    clearTimeout(mentionFindTimer)
    mentionFindTimer = null
  }
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
  if (isScheduledTask.value && !releaseMentionSubscription)
    releaseMentionSubscription = subscribeToWorkspace(workspaceId)
  try {
    const paths = await fileExplorer.findFiles(workspaceId, query, MENTION_FIND_LIMIT)
    if (
      generation !== mentionFindGeneration ||
      workspaceId !== props.workspaceId ||
      (isScheduledTask.value && props.filesAvailable === false) ||
      composerKind.value !== 'mention'
    )
      return
    mentionSearchPaths.value = paths
  } catch {
    if (generation === mentionFindGeneration) mentionSearchPaths.value = []
  }
}

function scheduleMentionFileSearch(rawQuery: string): void {
  mentionFindGeneration += 1
  const fileQuery = mentionFileSearchQuery(rawQuery)
  if (
    fileQuery === null ||
    !props.workspaceId ||
    (isScheduledTask.value && props.filesAvailable === false)
  ) {
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
  const el = textareaRef.value
  if (!el) return
  const cursor = el.cursor()
  const text = el.value()
  const mention = detectMentionQuery(text, cursor)
  if (mention !== null) {
    if (composerKind.value !== 'mention') cancelMentionFileSearch()
    composerKind.value = 'mention'
    mentionQuery.value = mention
    mentionIndex.value = 0
    mentionOpen.value = true
    scheduleMentionFileSearch(mention)
    return
  }
  const slash = detectSlashQuery(text, cursor)
  if (slash !== null) {
    if (composerKind.value === 'mention') cancelMentionFileSearch()
    composerKind.value = 'skill'
    mentionQuery.value = slash
    mentionIndex.value = 0
    mentionOpen.value = true
    return
  }
  closeComposerQuery()
}

function chooseMention(candidate: MentionCandidate): void {
  const el = textareaRef.value
  if (!el) return
  const cursor = el.cursor()
  const text = el.value()
  if (candidate.kind === 'skill') {
    addSkill(candidate.insert)
    const next = consumeSlashQuery(text, cursor)
    prompt.value = next.text
    closeComposerQuery()
    void nextTick(() => {
      el.focus()
      el.setCursor(next.cursor)
    })
    return
  }
  if (isScheduledTask.value && candidate.kind === 'file' && props.filesAvailable === false) {
    closeComposerQuery()
    return
  }
  const next = applyMentionCandidate(text, cursor, candidate)
  prompt.value = next.text
  closeComposerQuery()
  void nextTick(() => {
    el.focus()
    el.setCursor(next.cursor)
  })
}

defineExpose({ clearInput, chooseMention, setPrompt, uploadChatFiles })

function onPromptInput(): void {
  refreshComposerQuery()
}

function onPromptKeydown(e: KeyboardEvent): void {
  if (e.isComposing) return
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
      e.stopPropagation()
      closeComposerQuery()
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey && !isScheduledTask.value) {
    e.preventDefault()
    handleSend()
  }
}

function handleKeydown(e: KeyboardEvent): void {
  onPromptKeydown(e)
}

function onSuggestionOpenChange(open: boolean): void {
  if (!open && mentionOpen.value) closeComposerQuery()
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
    class="relative min-w-0 w-full bg-transparent"
    :class="isScheduledTask ? 'p-0' : ['px-3 pb-2 sm:px-4', attached ? 'pt-0' : 'pt-3 sm:pt-4']"
    @keydown="onComposerKeydown"
  >
    <Popover :open="suggestionOpen" @update:open="onSuggestionOpenChange">
      <PopoverAnchor as-child>
        <div
          ref="suggestionAnchor"
          class="flex flex-col rounded-3xl border bg-card shadow-sm transition-all duration-200 focus-within:border-primary focus-within:shadow-md"
          :class="
            props.uploadDrag?.active ? 'border-primary ring-2 ring-primary/40' : 'border-border'
          "
          data-testid="composer-card"
          @dragover="handleComposerDragOver"
          @drop="handleComposerDrop"
        >
          <div v-if="selectedSkills.length" class="flex flex-wrap gap-1.5 px-4 pt-3">
            <span
              v-for="skill in selectedSkills"
              :key="skill.id"
              :data-testid="`composer-skill-chip-${skill.id}`"
              class="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
            >
              <BookText :size="10" />
              {{ skill.name }}
              <button
                type="button"
                class="transition-opacity hover:opacity-70"
                :aria-label="`Remove ${skill.name} skill`"
                :data-testid="`composer-skill-remove-${skill.id}`"
                @click="removeSkill(skill.id)"
              >
                <X :size="11" />
              </button>
            </span>
          </div>
          <button
            v-if="providerMissing"
            type="button"
            data-testid="composer-provider-cta"
            class="mx-4 mt-3 w-fit cursor-pointer rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
            @click="openProviderSettings"
          >
            Connect a provider in Settings
          </button>

          <HarnessMentionImages
            v-if="imageTokens.length"
            :tokens="imageTokens"
            :workspace-id="workspaceId ?? ''"
            removable
            strip-class="px-4 pt-3"
            @remove="removeImageToken"
          />
          <div class="relative">
            <ComposerRichEditor
              ref="textareaRef"
              v-model="prompt"
              :disabled="disabled"
              :placeholder="
                isScheduledTask
                  ? 'Describe the recurring work… Use / for skills and @ for context'
                  : 'Plan, Build, / for skills, @ for context'
              "
              :aria-label="isScheduledTask ? 'Scheduled task prompt' : 'Chat prompt'"
              :aria-invalid="isScheduledTask ? Boolean(promptError) : undefined"
              :aria-describedby="isScheduledTask && promptError ? 'task-prompt-error' : undefined"
              :aria-controls="suggestionOpen ? activeListboxId : undefined"
              :aria-activedescendant="suggestionOpen ? activeOptionId : undefined"
              :test-id="isScheduledTask ? 'task-prompt' : 'composer-textarea'"
              :editor-class="isScheduledTask ? 'min-h-24 md:min-h-24' : undefined"
              @keydown="handleKeydown"
              @input="onPromptInput"
            />
            <div
              v-if="!isScheduledTask && mentionPopupCandidates.length > 0"
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
                <WorkspaceFileIcon
                  v-if="candidate.kind === 'file'"
                  :path="
                    candidate.insert.startsWith('file:')
                      ? candidate.insert.slice('file:'.length)
                      : candidate.insert
                  "
                  :size="15"
                />
                <span
                  v-else
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
            <p
              v-if="isScheduledTask && promptError"
              id="task-prompt-error"
              class="border-t border-destructive/20 bg-destructive/5 px-4 py-2 text-xs text-destructive"
              data-testid="task-prompt-error"
              role="alert"
            >
              {{ promptError }}
            </p>
          </div>

          <div
            class="flex flex-wrap items-center gap-1 p-2 pl-2 pb-2"
            data-testid="composer-toolbar"
          >
            <div
              class="flex min-w-0 flex-[1_1_10rem] flex-wrap items-center gap-1"
              data-testid="composer-toolbar-leading"
            >
              <DropdownMenu>
                <DropdownMenuTrigger as-child>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    class="h-8 min-w-0 max-w-full shrink gap-1.5 rounded-full px-2.5 text-xs font-medium"
                    data-testid="composer-mode-trigger"
                    :disabled="disabled"
                  >
                    <component :is="modeIcon" :size="14" class="shrink-0" />
                    <span class="min-w-0 truncate">{{ modeLabel }}</span>
                    <ChevronDown :size="12" class="shrink-0 opacity-70" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent side="top" align="start" class="min-w-36">
                  <DropdownMenuItem
                    class="text-xs"
                    data-testid="composer-mode-build"
                    @click="setMode('build')"
                  >
                    <Hammer :size="14" />
                    Build
                    <Check v-if="localMode === 'build'" class="ml-auto size-3.5" />
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    class="text-xs"
                    data-testid="composer-mode-plan"
                    @click="setMode('plan')"
                  >
                    <ListTodo :size="14" />
                    Plan
                    <Check v-if="localMode === 'plan'" class="ml-auto size-3.5" />
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>

              <ModelPicker
                :model="localModel"
                :effort="localEffort"
                :models="catalog"
                :recent-models="recentModels"
                :recent-efforts="recentEntries"
                :loading="modelLoading"
                :disabled="disabled"
                :default-model-label="isScheduledTask ? 'Agent default' : 'Select model…'"
                :allow-default="isScheduledTask"
                @update:model="setModel"
                @update:effort="setEffort"
              />
            </div>

            <div class="ml-auto flex shrink-0 items-center gap-1">
              <Button
                v-if="!isScheduledTask"
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
                <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" class="shrink-0">
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
                  :disabled="!canManageFiles || isUploading"
                  title="Upload a file to the workspace"
                  aria-label="Upload a file to the workspace"
                  data-testid="composer-attach"
                  @click="triggerFileUpload"
                >
                  <Loader2 v-if="isUploading" :size="16" class="animate-spin" />
                  <Paperclip v-else :size="16" />
                </Button>
                <input
                  ref="fileInputRef"
                  type="file"
                  multiple
                  class="hidden"
                  data-testid="composer-file-input"
                  @change="handleFileInput"
                />
              </div>
              <Button
                v-if="stoppable && !isScheduledTask"
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
                v-else-if="!isScheduledTask"
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
      </PopoverAnchor>
      <PopoverContent
        v-if="suggestionOpen"
        side="top"
        align="start"
        :side-offset="6"
        :avoid-collisions="false"
        :disable-autofocus="true"
        :disable-outside-pointer-events="false"
        :style="{ maxHeight: `${suggestionMaxHeight}px` }"
        class="z-[100] min-h-0 w-[var(--reka-popover-trigger-width)] max-w-[calc(100vw-2rem)] overflow-y-auto rounded-xl border border-border bg-card p-0 shadow-lg"
        @placed="clampSuggestionIntoViewport"
        data-testid="composer-suggestions-portal"
        @open-auto-focus.prevent
        @close-auto-focus.prevent
        @escape-key-down.prevent="closeComposerQuery"
      >
        <HarnessMentionSheet
          :label="composerKind === 'skill' ? 'Skill suggestions' : 'Mention suggestions'"
          :listbox-id="activeListboxId"
          :candidates="mentionCandidates"
          :active-index="mentionIndex"
          @select="requestMentionSelect"
          @hover="mentionIndex = $event"
        />
      </PopoverContent>
    </Popover>
    <p v-if="busyMessage" class="mt-2 text-center text-xs text-amber-600 dark:text-amber-400">
      {{ busyMessage }}
    </p>
  </div>
</template>
