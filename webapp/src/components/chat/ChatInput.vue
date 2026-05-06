<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { UiSelect, UiTextarea, UiButton, UiBadge, UiDialog } from '@/components/ui'
import {
  Send,
  Square,
  BookText,
  X,
  Image,
  Video,
  Loader2,
  CheckCircle,
  AlertCircle,
  File as FileIcon,
  FileText,
} from 'lucide-vue-next'
import { useChatInputCache } from '@/composables/useChatInputCache'
import type { AgentOption, Skill } from '@/types'
import WorkspaceFilePicker from './WorkspaceFilePicker.vue'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { sendFilesUpload } from '@/services/socket'
import {
  buildWorkspaceReferenceMarkdown,
  classifyWorkspaceFile,
  extractWorkspacePathReferences,
  type WorkspaceFileKind,
} from '@/lib/workspaceFileRefs'

const props = defineProps<{
  disabled?: boolean
  sending?: boolean
  agentOptions?: AgentOption[]
  selectedOptions?: Record<string, string>
  skillOptions?: Skill[]
  workspaceId?: string
  chatId?: string | null
  busyMessage?: string
  stoppable?: boolean
}>()

const emit = defineEmits<{
  send: [prompt: string, options: Record<string, string>, skillIds: string[]]
  stop: []
  'update:selectedOptions': [value: Record<string, string>]
}>()

const prompt = ref('')
const selectedSkillIds = ref<string[]>([])
const skillDropdownOpen = ref(false)
const imagePickerOpen = ref(false)
const isDragging = ref(false)
const skillButtonRef = ref<HTMLElement | null>(null)
const imageButtonRef = ref<HTMLElement | null>(null)
const stopConfirmOpen = ref(false)

const imageStore = useWorkspaceImageStore()

const SKILL_DROPDOWN_WIDTH = 256
const IMAGE_PICKER_WIDTH = 288
const VIEWPORT_PADDING = 12

type FloatingStyle = {
  top: string
  left: string
  width: string
}

const skillDropdownStyle = ref<FloatingStyle>({
  top: '0px',
  left: '0px',
  width: `${SKILL_DROPDOWN_WIDTH}px`,
})

const imagePickerStyle = ref<FloatingStyle>({
  top: '0px',
  left: '0px',
  width: `${IMAGE_PICKER_WIDTH}px`,
})

let uploadCounter = 0
function nextUploadId(): string {
  return `chat-media-upload-${++uploadCounter}-${Date.now()}`
}

function buildFloatingStyle(
  element: HTMLElement | null,
  width: number,
): FloatingStyle {
  if (!element) {
    return {
      top: '0px',
      left: '0px',
      width: `${width}px`,
    }
  }

  const rect = element.getBoundingClientRect()
  const maxLeft = Math.max(VIEWPORT_PADDING, window.innerWidth - width - VIEWPORT_PADDING)
  const left = Math.min(Math.max(rect.left, VIEWPORT_PADDING), maxLeft)
  const top = Math.max(VIEWPORT_PADDING, rect.top - 8)

  return {
    top: `${top}px`,
    left: `${left}px`,
    width: `${Math.min(width, window.innerWidth - VIEWPORT_PADDING * 2)}px`,
  }
}

function updateFloatingPositions(): void {
  if (skillDropdownOpen.value) {
    skillDropdownStyle.value = buildFloatingStyle(skillButtonRef.value, SKILL_DROPDOWN_WIDTH)
  }
  if (imagePickerOpen.value) {
    imagePickerStyle.value = buildFloatingStyle(imageButtonRef.value, IMAGE_PICKER_WIDTH)
  }
}

function handleWindowUpdate(): void {
  updateFloatingPositions()
}

// Initialize cache composable
const { loadFromCache, saveToCache, clearCache } = useChatInputCache(
  () => props.workspaceId || '',
  () => props.chatId,
)

// Load cached input on mount and when workspace/chat changes
onMounted(() => {
  const cached = loadFromCache()
  if (cached) {
    prompt.value = cached
  }

  window.addEventListener('resize', handleWindowUpdate)
  window.addEventListener('scroll', handleWindowUpdate, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleWindowUpdate)
  window.removeEventListener('scroll', handleWindowUpdate, true)
})

// Watch for chatId changes to load different cache
watch(
  () => props.chatId,
  () => {
    const cached = loadFromCache()
    prompt.value = cached || ''
  },
)

// Auto-save prompt to cache when it changes
watch(
  () => prompt.value,
  (newPrompt) => {
    saveToCache(newPrompt)
  },
)

const canSend = computed(
  () => prompt.value.trim().length > 0 && !props.disabled && !props.sending,
)
const canStop = computed(() => Boolean(props.stoppable) && !props.sending)
const canManageFiles = computed(() => !props.disabled && !props.sending && Boolean(props.workspaceId))

const selectedSkills = computed(
  () => (props.skillOptions ?? []).filter((s) => selectedSkillIds.value.includes(s.id)),
)

// --- Image preview in input ---

const localTextPreviews = ref<Record<string, string>>({})
const localVideoPreviews = ref<Record<string, string>>({})

type PromptFileReference = {
  path: string
  label: string
  kind: WorkspaceFileKind
}

/** Extract all /workspace/... file references referenced in the current prompt. */
const promptFileRefs = computed<PromptFileReference[]>(() => {
  const refs = extractWorkspacePathReferences(prompt.value)
  const deduped = new Map<string, PromptFileReference>()

  for (const ref of refs) {
    if (deduped.has(ref.path)) continue
    deduped.set(ref.path, {
      path: ref.path,
      label: ref.label,
      kind: classifyWorkspaceFile(ref.path),
    })
  }

  return [...deduped.values()]
})

/** Fetch media whenever the referenced paths change. */
watch(
  promptFileRefs,
  (refs) => {
    if (!props.workspaceId) return
    for (const ref of refs) {
      if (ref.kind === 'image') {
        imageStore.fetchImage(props.workspaceId, ref.path)
      } else if (ref.kind === 'video') {
        imageStore.fetchVideo(props.workspaceId, ref.path)
      }
    }
  },
  { immediate: true },
)

const promptPreviewEntries = computed<Array<{
  path: string
  label: string
  kind: WorkspaceFileKind
  url: string | null
  textPreview: string | null
}>>(() => {
  return promptFileRefs.value.map((ref) => ({
    path: ref.path,
    label: ref.label || ref.path.split('/').pop() || 'file',
    kind: ref.kind,
    url:
      ref.kind === 'image'
        ? imageStore.getImageUrl(ref.path)
        : ref.kind === 'video'
          ? imageStore.getVideoUrl(ref.path) || localVideoPreviews.value[ref.path] || null
          : null,
    textPreview: ref.kind === 'text' ? localTextPreviews.value[ref.path] || null : null,
  }))
})

// --- Upload logic ---

const MEDIA_UPLOAD_DIR = '/workspace/.user-uploaded-media'

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('Failed to read file'))
    reader.onload = () => resolve((reader.result as string) || '')
    reader.readAsDataURL(file)
  })
}

function makeTextPreview(raw: string): string {
  return raw.replace(/\r\n/g, '\n').trim().slice(0, 320)
}

async function uploadWorkspaceFile(file: File): Promise<void> {
  if (!props.workspaceId) return
  if (props.disabled || props.sending) return

  const MAX_SIZE = 10 * 1024 * 1024 // 10 MB
  if (file.size > MAX_SIZE) return

  const kind = classifyWorkspaceFile(file.name, file.type || undefined)

  const dataUrl = await readFileAsDataUrl(file)
  const base64 = dataUrl.split(',')[1] ?? ''
  const safeFilename = file.name.replace(/[^a-zA-Z0-9._-]/g, '_')
  const path = `${MEDIA_UPLOAD_DIR}/${safeFilename}`

  if (kind === 'image') {
    imageStore.storeLocalImage(path, dataUrl)
  } else if (kind === 'video') {
    localVideoPreviews.value[path] = dataUrl
  } else if (kind === 'text') {
    const text = await file.text()
    localTextPreviews.value[path] = makeTextPreview(text)
  }

  // Insert markdown reference into prompt
  const ref = buildWorkspaceReferenceMarkdown(file.name, path, kind)
  prompt.value = prompt.value ? `${prompt.value}\n${ref}` : ref

  // Upload via WebSocket and track state for UI feedback
  const requestId = nextUploadId()
  imageStore.trackUpload(requestId, path)
  sendFilesUpload(props.workspaceId!, requestId, MEDIA_UPLOAD_DIR, safeFilename, base64)
}

// --- Drag & drop ---

function handleDragOver(e: DragEvent): void {
  if (!canManageFiles.value) return
  e.preventDefault()
  isDragging.value = true
}

function handleDragLeave(e: DragEvent): void {
  // Only reset if leaving the container itself, not a child element
  if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) {
    isDragging.value = false
  }
}

function handleDrop(e: DragEvent): void {
  if (!canManageFiles.value) return
  e.preventDefault()
  isDragging.value = false
  const files = Array.from(e.dataTransfer?.files ?? [])
  for (const file of files) {
    void uploadWorkspaceFile(file)
  }
}

// --- Skill picker ---

function toggleSkill(id: string): void {
  const idx = selectedSkillIds.value.indexOf(id)
  if (idx === -1) {
    selectedSkillIds.value = [...selectedSkillIds.value, id]
  } else {
    selectedSkillIds.value = selectedSkillIds.value.filter((sid) => sid !== id)
  }
}

function removeSkill(id: string): void {
  selectedSkillIds.value = selectedSkillIds.value.filter((sid) => sid !== id)
}

function updateOption(key: string, value: string): void {
  emit('update:selectedOptions', { ...props.selectedOptions, [key]: value })
}

function handleSend(): void {
  if (!canSend.value) return
  emit('send', prompt.value.trim(), props.selectedOptions ?? {}, [...selectedSkillIds.value])
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
}

function requestStop(): void {
  if (!canStop.value) return
  stopConfirmOpen.value = true
}

function confirmStop(): void {
  emit('stop')
  stopConfirmOpen.value = false
}

function restorePrompt(text: string): void {
  prompt.value = text
  saveToCache(text)
}

function clearInput(): void {
  prompt.value = ''
  selectedSkillIds.value = []
  clearCache()
}

defineExpose({ restorePrompt, clearInput })

function handleKeydown(e: KeyboardEvent): void {
  // Enter to send, Shift+Enter for newline
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function handleFileSelected(path: string, filename: string): void {
  if (!canManageFiles.value) return
  const kind = classifyWorkspaceFile(path)
  const ref = buildWorkspaceReferenceMarkdown(filename, path, kind)
  prompt.value = prompt.value ? `${prompt.value}\n${ref}` : ref
  imagePickerOpen.value = false
}

function handlePickerUpload(file: File): void {
  if (!canManageFiles.value) return
  void uploadWorkspaceFile(file)
  imagePickerOpen.value = false
}

function removeFileRef(path: string): void {
  // Remove markdown workspace references for this path from the prompt
  const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`\\n?!?\\[[^\\]]*\\]\\(${escaped}\\)`, 'g')
  prompt.value = prompt.value.replace(re, '').trimStart()
  delete localTextPreviews.value[path]
  delete localVideoPreviews.value[path]
}

watch(skillDropdownOpen, async (open) => {
  if (!open) return
  imagePickerOpen.value = false
  await nextTick()
  updateFloatingPositions()
})

watch(imagePickerOpen, async (open) => {
  if (!open) return
  if (!canManageFiles.value) {
    imagePickerOpen.value = false
    return
  }
  skillDropdownOpen.value = false
  await nextTick()
  updateFloatingPositions()
})

watch(canManageFiles, (allowed) => {
  if (!allowed) {
    isDragging.value = false
    imagePickerOpen.value = false
  }
})
</script>

<template>
  <div class="pt-3 px-3 sm:pt-4 sm:px-4 pb-2 bg-transparent min-w-0 w-full">
    <div
      class="group relative flex flex-col rounded-xl border bg-surface shadow-sm focus-within:border-primary transition-all duration-200"
      :class="isDragging ? 'border-primary ring-1 ring-primary' : 'border-border'"
      @dragover="handleDragOver"
      @dragleave="handleDragLeave"
      @drop="handleDrop"
    >
      <!-- Drag overlay -->
      <div
        v-if="isDragging"
        class="absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-primary/10 pointer-events-none"
      >
        <span class="text-primary text-sm font-medium">Drop files to upload</span>
      </div>

      <!-- Selected skill pills (shown above textarea) -->
      <div v-if="selectedSkills.length" class="flex flex-wrap gap-1.5 px-4 pt-3 pb-0">
        <span
          v-for="skill in selectedSkills"
          :key="skill.id"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-primary/10 text-primary text-xs font-medium"
        >
          <BookText :size="10" />
          {{ skill.name }}
          <button
            type="button"
            class="hover:opacity-70 transition-opacity"
            @click="removeSkill(skill.id)"
          >
            <X :size="11" />
          </button>
        </span>
      </div>

      <!-- File previews for referenced workspace files -->
      <div
        v-if="promptPreviewEntries.length"
        class="flex flex-wrap gap-2 px-4 pt-3 pb-0"
      >
        <div
          v-for="entry in promptPreviewEntries"
          :key="entry.path"
          class="relative group/img inline-block"
        >
          <img
            v-if="entry.kind === 'image' && entry.url"
            :src="entry.url"
            :alt="entry.label"
            class="h-16 w-auto max-w-[120px] rounded-md object-cover border transition-colors"
            :class="imageStore.getUploadStatus(entry.path) === 'error' ? 'border-error' : 'border-border'"
          />
          <video
            v-else-if="entry.kind === 'video' && entry.url"
            :src="entry.url"
            class="h-16 w-auto max-w-[160px] rounded-md object-cover border border-border bg-black"
            muted
            playsinline
            preload="metadata"
          />
          <div
            v-else-if="entry.kind === 'text'"
            class="h-16 w-[180px] rounded-md border border-border bg-surface px-2 py-1.5 flex flex-col gap-1"
          >
            <div class="flex items-center gap-1.5 min-w-0">
              <FileText :size="13" class="text-primary shrink-0" />
              <span class="truncate text-[10px] font-medium text-fg">{{ entry.label }}</span>
            </div>
            <pre class="text-[10px] leading-4 text-muted-fg whitespace-pre-wrap break-words overflow-hidden">{{ entry.textPreview || 'Text file in workspace' }}</pre>
          </div>
          <div
            v-else-if="entry.kind === 'binary'"
            class="h-16 w-[180px] rounded-md border border-border bg-surface px-2 py-1.5 flex items-center gap-2"
          >
            <FileIcon :size="14" class="text-muted-fg shrink-0" />
            <div class="min-w-0">
              <div class="truncate text-[10px] font-medium text-fg">{{ entry.label }}</div>
              <div class="text-[10px] text-muted-fg">Binary file</div>
            </div>
          </div>
          <!-- Loading placeholder (no media URL yet) -->
          <div
            v-else
            class="h-16 w-20 rounded-md border border-border bg-muted flex items-center justify-center"
          >
            <Video v-if="entry.kind === 'video'" :size="18" class="text-muted-fg" />
            <Image v-else :size="18" class="text-muted-fg" />
          </div>

          <!-- Upload status overlay -->
          <div
            v-if="imageStore.getUploadStatus(entry.path)"
            class="absolute inset-0 rounded-md flex items-center justify-center"
            :class="{
              'bg-black/40': imageStore.getUploadStatus(entry.path) === 'uploading',
              'bg-success/20': imageStore.getUploadStatus(entry.path) === 'done',
              'bg-error/30': imageStore.getUploadStatus(entry.path) === 'error',
            }"
          >
            <Loader2
              v-if="imageStore.getUploadStatus(entry.path) === 'uploading'"
              :size="18"
              class="text-white animate-spin"
            />
            <CheckCircle
              v-else-if="imageStore.getUploadStatus(entry.path) === 'done'"
              :size="18"
              class="text-success"
            />
            <AlertCircle
              v-else-if="imageStore.getUploadStatus(entry.path) === 'error'"
              :size="18"
              class="text-error"
            />
          </div>

          <!-- Upload error tooltip -->
          <div
            v-if="imageStore.getUploadStatus(entry.path) === 'error'"
            class="absolute -bottom-5 left-0 right-0 text-center text-[9px] text-error whitespace-nowrap"
          >
            Upload failed
          </div>

          <!-- Remove button -->
          <button
            type="button"
            class="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-surface border border-border flex items-center justify-center opacity-0 group-hover/img:opacity-100 transition-opacity hover:bg-error hover:border-error hover:text-white"
            :title="`Remove reference to ${entry.path.split('/').pop()}`"
            @click="removeFileRef(entry.path)"
          >
            <X :size="9" />
          </button>
        </div>
      </div>

      <UiTextarea
        v-model="prompt"
        :disabled="disabled"
        :rows="1"
        placeholder="Send a prompt to the agent…"
        class="min-h-[50px] max-h-[200px] w-full resize-none !border-0 !shadow-none focus:!shadow-none focus:!border-transparent !ring-0 !outline-none focus-visible:ring-0 focus-visible:outline-none !rounded-none !bg-transparent px-4 py-3 text-base"
        style="backdrop-filter: none; -webkit-backdrop-filter: none;"
        autosize
        @keydown="handleKeydown"
      />

      <div class="flex items-center gap-2 p-2 pl-3 pb-3">
        <!-- Scrollable options row (takes all remaining space) -->
        <div
          class="flex-1 min-w-0 overflow-x-auto px-1 py-1 -mx-1 -my-1"
          style="scrollbar-width: none; -webkit-overflow-scrolling: touch;"
        >
          <div class="flex items-center gap-1.5 min-w-max">
            <!-- Generic option selects -->
            <div
              v-for="option in agentOptions"
              :key="option.key"
              class="w-auto min-w-[90px] sm:min-w-[130px]"
            >
              <UiSelect
                :model-value="selectedOptions?.[option.key] ?? option.default"
                :options="option.choices.map((c) => ({ value: c, label: c }))"
                class="h-8 text-xs border-transparent bg-muted/50 hover:bg-muted focus:ring-0 px-2 py-1 rounded-lg w-full"
                @update:model-value="updateOption(option.key, $event)"
              />
            </div>

            <!-- Skill picker -->
            <div v-if="(skillOptions?.length ?? 0) > 0" class="shrink-0">
              <button
                ref="skillButtonRef"
                type="button"
                class="flex items-center gap-1.5 h-8 px-2 py-1 rounded-lg text-xs transition-colors cursor-pointer"
                :class="selectedSkillIds.length ? 'text-primary' : 'text-muted-fg'"
                style="background: var(--glass-bg-subtle); border: 1px solid var(--glass-border); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);"
                @click="skillDropdownOpen = !skillDropdownOpen"
              >
                <BookText :size="13" />
                <span>Skills{{ selectedSkillIds.length ? ` (${selectedSkillIds.length})` : '' }}</span>
              </button>
            </div>

             <!-- Workspace file picker -->
             <div v-if="workspaceId" class="shrink-0">
              <button
                ref="imageButtonRef"
                type="button"
                class="flex items-center gap-1.5 h-8 px-2 py-1 rounded-lg text-xs transition-colors cursor-pointer"
                :class="[
                  imagePickerOpen ? 'text-primary' : 'text-muted-fg',
                  !canManageFiles ? 'opacity-50 cursor-not-allowed' : '',
                ]"
                style="background: var(--glass-bg-subtle); border: 1px solid var(--glass-border); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);"
                :title="canManageFiles ? 'Insert workspace file or upload' : 'Workspace must be running to insert or upload files'"
                :disabled="!canManageFiles"
                @click="canManageFiles && (imagePickerOpen = !imagePickerOpen)"
              >
                <FileIcon :size="13" />
                <span>Files</span>
               </button>
             </div>
          </div>
        </div>

        <!-- Send button always visible on the right -->
        <UiButton
          v-if="stoppable"
          :disabled="!canStop"
          size="icon"
          class="h-9 w-9 rounded-full transition-all shrink-0"
          :class="canStop ? 'bg-error text-white hover:bg-error/90' : 'bg-muted text-muted-fg'"
          title="Stop current completion"
          @click="requestStop"
        >
          <Square :size="16" />
        </UiButton>
        <UiButton
          v-else
          :disabled="!canSend"
          size="icon"
          class="h-9 w-9 rounded-full transition-all shrink-0"
          :class="canSend ? 'bg-primary text-primary-fg hover:bg-primary-hover' : 'bg-muted text-muted-fg'"
          @click="handleSend"
        >
          <Send :size="18" />
        </UiButton>

        <Teleport to="body">
          <template v-if="skillDropdownOpen">
            <div
              class="fixed inset-0 z-[120]"
              @click="skillDropdownOpen = false"
            />
            <div
              class="fixed z-[121] -translate-y-full rounded-[var(--radius-md)] glass-strong py-1 max-h-56 overflow-y-auto"
              :style="skillDropdownStyle"
            >
              <button
                v-for="skill in skillOptions"
                :key="skill.id"
                type="button"
                class="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-fg hover:bg-surface-hover transition-colors"
                @click="toggleSkill(skill.id)"
              >
                <span
                  class="w-4 h-4 rounded border flex items-center justify-center shrink-0 transition-colors"
                  :class="
                    selectedSkillIds.includes(skill.id)
                      ? 'bg-primary border-primary text-primary-fg'
                      : 'border-border'
                  "
                >
                  <svg
                    v-if="selectedSkillIds.includes(skill.id)"
                    viewBox="0 0 12 10"
                    fill="none"
                    class="w-2.5 h-2.5"
                  >
                    <path
                      d="M1 5l3 3 7-7"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                </span>
                <span class="truncate flex-1 text-left">{{ skill.name }}</span>
                <UiBadge class="shrink-0 text-[10px] py-0" variant="muted">
                  {{ skill.scope === 'organization' ? 'Org' : 'Mine' }}
                </UiBadge>
              </button>
            </div>
          </template>
        </Teleport>

        <Teleport to="body">
          <template v-if="imagePickerOpen && workspaceId">
            <div
              class="fixed inset-0 z-[120]"
              @click="imagePickerOpen = false"
            />
            <WorkspaceFilePicker
              :workspace-id="workspaceId"
              :disabled="!canManageFiles"
              class="fixed z-[121] -translate-y-full"
              :style="imagePickerStyle"
              @select="handleFileSelected"
              @close="imagePickerOpen = false"
              @upload="handlePickerUpload"
            />
          </template>
        </Teleport>
      </div>
    </div>
    <UiDialog
      :open="stopConfirmOpen"
      title="Stop current completion?"
      description="This will terminate the running agent process for this message."
      @update:open="(value) => (stopConfirmOpen = value)"
    >
      <div class="flex flex-col gap-4">
        <p class="text-sm text-muted-fg">
          The current response will be aborted immediately. The workspace remains running.
        </p>
        <div class="flex justify-end gap-2">
          <UiButton variant="outline" @click="stopConfirmOpen = false">Cancel</UiButton>
          <UiButton variant="destructive" @click="confirmStop">Stop completion</UiButton>
        </div>
      </div>
    </UiDialog>
    <p v-if="busyMessage" class="text-xs text-center text-warning mt-2">
      {{ busyMessage }}
    </p>
    <p v-else class="hidden sm:block text-xs text-center text-muted-fg mt-2">
      Press <kbd class="font-mono font-medium text-fg">Enter</kbd> to send,
      <kbd class="font-mono font-medium text-fg">Shift+Enter</kbd> for newline
    </p>
  </div>
</template>
