<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  CalendarClock, Check, ChevronDown, CircleAlert, Clock3, FilePlus2, FolderSearch,
  Loader2, Pause, Play, Plus, RefreshCw, Rocket, Trash2,
} from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import ComposerRichEditor from '@/components/chat/ComposerRichEditor.vue'
import HarnessModelPicker from '@/components/chat/HarnessModelPicker.vue'
import { listScheduledTasks, createScheduledTask, updateScheduledTask, deleteScheduledTask, runScheduledTaskNow, listScheduledTaskRuns } from '@/services/scheduledTasks.api'
import type { ScheduledTask, ScheduledTaskInput, ScheduledTaskRun } from '@/services/scheduledTasks.api'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useSkillStore } from '@/stores/skills'
import { WorkspaceStatus } from '@/types'
import type { FileEntryRaw, FilesFindResultEvent, FilesListResultEvent, FilesUploadResultEvent, Workspace } from '@/types'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { useRecentModels, recentCatalogModels } from '@/lib/recentModels'
import type { ProviderModel } from '@/lib/harnessModels'
import { fileToBase64, sanitizeUploadFilename, resolveUniqueFilename, CHAT_UPLOAD_DIR, uploadTargetPath, appendUploadMentions, hasUploadMention, UPLOAD_MAX_BYTES, nextChatUploadRequestId } from '@/lib/chatUpload'
import { onEvent, sendFilesFind, sendFilesList, sendFilesUpload, subscribeToWorkspace } from '@/services/socket'
import { isValidTimeZone, nextLocalOccurrence, validateSchedule, WEEKDAYS } from '@/lib/scheduledTasks'
import { useNotificationStore } from '@/stores/notifications'

const router = useRouter()
const workspaceStore = useWorkspaceStore()
const skillStore = useSkillStore()
const notifications = useNotificationStore()
const { entries: recentEntries } = useRecentModels()
const recentEffortEntries = computed(() => recentEntries.value)

const tasks = ref<ScheduledTask[]>([])
const runs = ref<ScheduledTaskRun[]>([])
const lastRunsByTask = ref<Record<string, ScheduledTaskRun | null>>({})
const selectedId = ref<string | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const actionBusy = ref(false)
const deleting = ref(false)
const deleteOpen = ref(false)
const dirty = ref(false)
const models = ref<ProviderModel[]>([])
const modelLoading = ref(true)
const fileQuery = ref('')
const fileMatches = ref<string[]>([])
const fileSearching = ref(false)
const uploadInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

interface FormState {
  name: string
  workspace_id: string
  prompt: string
  mode: 'plan' | 'build'
  model: string
  reasoning_effort: string
  skill_ids: string[]
  recurrence: 'daily' | 'weekly'
  weekdays: number[]
  local_time: string
  timezone_name: string
  enabled: boolean
}
function blankForm(): FormState {
  return {
    name: '', workspace_id: '', prompt: '', mode: 'build', model: '', reasoning_effort: '',
    skill_ids: [], recurrence: 'daily', weekdays: [0, 1, 2, 3, 4], local_time: '09:00',
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', enabled: true,
  }
}
const form = ref<FormState>(blankForm())
const errors = ref<Record<string, string>>({})
const selectedTask = computed(() => tasks.value.find((task) => task.id === selectedId.value) ?? null)
const isNew = computed(() => selectedId.value === null)
const visibleWorkspaces = computed(() => workspaceStore.workspaces.filter((workspace) => ![
  WorkspaceStatus.REMOVED, WorkspaceStatus.DELETED, WorkspaceStatus.DELETING,
  WorkspaceStatus.PENDING_DELETION,
].includes(workspace.status)))
const chosenWorkspace = computed(() => visibleWorkspaces.value.find((workspace) => workspace.id === form.value.workspace_id) ?? null)
const workspaceCanManageFiles = computed(() => chosenWorkspace.value?.status === WorkspaceStatus.RUNNING && chosenWorkspace.value.runner_online && !chosenWorkspace.value.active_operation)
const scheduleSummary = computed(() => {
  if (form.value.recurrence === 'daily') return `Every day at ${form.value.local_time}`
  const days = WEEKDAYS.filter((day) => form.value.weekdays.includes(day.value)).map((day) => day.label)
  return `${days.join(', ') || 'Choose weekdays'} at ${form.value.local_time}`
})
const nextPreview = computed(() => {
  if (!dirty.value && selectedTask.value) {
    const authoritative = new Date(selectedTask.value.next_run_at)
    if (!Number.isNaN(authoritative.getTime())) return authoritative
  }
  return nextLocalOccurrence(form.value.recurrence, form.value.weekdays, form.value.local_time, form.value.timezone_name)
})
const previewText = computed(() => nextPreview.value
  ? new Intl.DateTimeFormat(undefined, { timeZone: form.value.timezone_name, weekday: 'long', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(nextPreview.value)
  : 'Choose a valid time')
const isValidTimezone = computed(() => isValidTimeZone(form.value.timezone_name))
const fileSearchRequest = ref(0)
let formGeneration = 0
const directoryRequests = new Map<string, { workspaceId: string; path: string; resolve: (entries: FileEntryRaw[] | null) => void }>()
const uploadRequests = new Map<string, { workspaceId: string; resolve: (error: string | null) => void }>()
const workspaceSubscriptions = new Map<string, () => void>()
let removeUploadListener: (() => void) | null = null
const findRequests = new Map<string, { workspaceId: string; resolve: (paths: string[] | null) => void }>()
let removeFindListener: (() => void) | null = null
let directoryRequestCounter = 0
let removeDirectoryListener: (() => void) | null = null
const runsRequestGeneration = ref(0)
const taskStats = computed(() => ({ active: tasks.value.filter((task) => task.enabled).length, paused: tasks.value.filter((task) => !task.enabled).length }))

function taskWorkspaceName(workspaceId: string): string {
  const workspace = workspaceStore.workspaces.find((item) => item.id === workspaceId)
  return workspace?.name ?? `Workspace ${workspaceId.slice(0, 8)}`
}
function workspaceStatus(workspace: Workspace): string {
  if (workspace.active_operation) return workspaceStore.getWorkspaceTransitionLabel(workspace.id) ?? 'In progress'
  if (workspace.status === WorkspaceStatus.RUNNING && !workspace.runner_online) return 'Runner offline'
  return String(workspace.status).replace(/_/g, ' ')
}
function taskSchedule(task: ScheduledTask): string {
  if (task.recurrence === 'daily') return `Daily · ${task.local_time}`
  const days = WEEKDAYS.filter((day) => task.weekdays.includes(day.value)).map((day) => day.label).join(', ')
  return `${days || 'Weekly'} · ${task.local_time}`
}
function formatDate(value: string | null, timezone?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(undefined, {
    ...(timezone && isValidTimeZone(timezone) ? { timeZone: timezone } : {}),
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)
}
function formatRunStatus(status: string): string {
  return ({ claimed: 'Queued', running: 'Running', succeeded: 'Succeeded', error: 'Failed', skipped: 'Skipped', interrupted: 'Interrupted' } as Record<string, string>)[status] ?? status
}
function runStatusClass(status: string): string {
  if (status === 'succeeded') return 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
  if (status === 'error' || status === 'interrupted') return 'bg-destructive/10 text-destructive'
  if (status === 'running' || status === 'claimed') return 'bg-primary/10 text-primary'
  return 'bg-muted text-muted-foreground'
}
function chatPath(sessionId: string, workspaceId = selectedTask.value?.workspace_id ?? form.value.workspace_id): string {
  return `/workspaces/${workspaceId}?session=${encodeURIComponent(sessionId)}`
}
function applyTask(task: ScheduledTask): void {
  formGeneration += 1
  form.value = {
    name: task.name, workspace_id: task.workspace_id, prompt: task.prompt, mode: task.mode,
    model: task.model, reasoning_effort: task.reasoning_effort, skill_ids: [...task.skill_ids],
    recurrence: task.recurrence, weekdays: [...task.weekdays], local_time: task.local_time,
    timezone_name: task.timezone_name, enabled: task.enabled,
  }
  dirty.value = false
  errors.value = {}
  fileQuery.value = ''
  fileMatches.value = []
}
async function refresh(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const result = await listScheduledTasks()
    tasks.value = result
    if (selectedId.value && result.some((task) => task.id === selectedId.value)) {
      const refreshed = result.find((task) => task.id === selectedId.value)!
      if (!dirty.value) applyTask(refreshed)
    } else if (result.length && !dirty.value) {
      selectedId.value = result[0]!.id
      applyTask(result[0]!)
    } else if (selectedId.value && !dirty.value) {
      selectedId.value = null
      form.value = blankForm()
    }
    if (selectedId.value) await loadRuns(selectedId.value)
    else runs.value = []
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load scheduled tasks.'
  } finally {
    loading.value = false
  }
}
async function loadRuns(id: string): Promise<void> {
  const request = ++runsRequestGeneration.value
  try {
    const result = await listScheduledTaskRuns(id)
    lastRunsByTask.value[id] = result[0] ?? null
    if (request === runsRequestGeneration.value && selectedId.value === id) runs.value = result
  } catch {
    if (request === runsRequestGeneration.value && selectedId.value === id) runs.value = []
  }
}
function hasUnsavedChanges(): boolean {
  return dirty.value && (isNew.value || Boolean(selectedTask.value))
}
function selectTask(task: ScheduledTask): void {
  if (task.id === selectedId.value) return
  if (hasUnsavedChanges() && !window.confirm('Discard your unsaved schedule changes?')) return
  selectedId.value = task.id
  runsRequestGeneration.value += 1
  runs.value = []
  applyTask(task)
  void loadRuns(task.id)
}
function createNew(): void {
  if (hasUnsavedChanges() && !window.confirm('Discard your unsaved schedule changes?')) return
  selectedId.value = null
  runsRequestGeneration.value += 1
  formGeneration += 1
  runs.value = []
  form.value = blankForm()
  if (visibleWorkspaces.value.length === 1) form.value.workspace_id = visibleWorkspaces.value[0]!.id
  runs.value = []
  errors.value = {}
  dirty.value = false
  fileQuery.value = ''
  fileMatches.value = []
}
function toggleWeekday(day: number): void {
  const days = new Set(form.value.weekdays)
  if (days.has(day)) days.delete(day)
  else days.add(day)
  form.value.weekdays = [...days].sort((a, b) => a - b)
  dirty.value = true
}
function setWeekdays(days: number[]): void { form.value.weekdays = days; dirty.value = true }
function updateForm<K extends keyof FormState>(key: K, value: FormState[K]): void {
  form.value[key] = value
  dirty.value = true
}
function setMode(mode: 'plan' | 'build'): void {
  form.value.mode = mode
  // Keep model/effort empty when following the live agent defaults. User-selected values stay explicit.
  if (!form.value.model && !form.value.reasoning_effort) {
    form.value.model = ''
    form.value.reasoning_effort = ''
  }
  dirty.value = true
}
function validate(): boolean {
  const result = validateSchedule(form.value)
  errors.value = { ...result.errors }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(form.value.local_time)) errors.value.local_time = 'Enter a time in 24-hour HH:MM format.'
  return result.valid && !errors.value.local_time
}
async function save(): Promise<void> {
  if (saving.value || !validate()) return
  saving.value = true
  try {
    const payload: ScheduledTaskInput = { ...form.value, name: form.value.name.trim(), prompt: form.value.prompt.trim(), weekdays: form.value.recurrence === 'daily' ? [] : form.value.weekdays }
    const saved = selectedId.value
      ? await updateScheduledTask(selectedId.value, payload)
      : await createScheduledTask(payload)
    tasks.value = [...tasks.value.filter((task) => task.id !== saved.id), saved].sort((a, b) => a.next_run_at.localeCompare(b.next_run_at))
    selectedId.value = saved.id
    applyTask(saved)
    await loadRuns(saved.id)
    notifications.success('Schedule saved', `${saved.name} is ready to run.`)
  } catch (error) {
    notifications.error('Could not save schedule', error instanceof Error ? error.message : 'Please try again.')
  } finally { saving.value = false }
}
async function toggleEnabled(task: ScheduledTask): Promise<void> {
  actionBusy.value = true
  try {
    const updated = await updateScheduledTask(task.id, { enabled: !task.enabled })
    tasks.value = tasks.value.map((item) => item.id === updated.id ? updated : item)
    if (selectedId.value === updated.id) {
      if (dirty.value) form.value.enabled = updated.enabled
      else applyTask(updated)
    }
  } catch (error) { notifications.error('Schedule update failed', error instanceof Error ? error.message : 'Please try again.') }
  finally { actionBusy.value = false }
}
async function runNow(task: ScheduledTask): Promise<void> {
  actionBusy.value = true
  try {
    const run = await runScheduledTaskNow(task.id)
    if (selectedId.value === task.id) {
      runs.value = [run, ...runs.value.filter((item) => item.id !== run.id)]
      await loadRuns(task.id)
    }
    const messages: Record<string, string> = {
      running: 'A new chat was created for this run.', claimed: 'Your task is queued.', skipped: run.error || 'This run was skipped.', error: run.error || 'The run could not be started.',
    }
    notifications.info(run.status === 'error' || run.status === 'skipped' ? 'Task could not run' : 'Run started', messages[run.status] ?? 'Run status updated.')
  } catch (error) { notifications.error('Could not run task', error instanceof Error ? error.message : 'Please try again.') }
  finally { actionBusy.value = false }
}
async function confirmDelete(): Promise<void> {
  const task = selectedTask.value
  if (!task || deleting.value) return
  deleting.value = true
  try {
    await deleteScheduledTask(task.id)
    tasks.value = tasks.value.filter((item) => item.id !== task.id)
    selectedId.value = null
    runs.value = []
    if (tasks.value.length) await selectTask(tasks.value[0]!)
    else createNew()
    deleteOpen.value = false
    notifications.success('Schedule deleted', `${task.name} was removed.`)
  } catch (error) { notifications.error('Could not delete schedule', error instanceof Error ? error.message : 'Please try again.') }
  finally { deleting.value = false }
}
function fetchWorkspaceListing(workspaceId: string, path: string): Promise<FileEntryRaw[] | null> {
  if (!removeDirectoryListener) {
    removeDirectoryListener = onEvent('files:list_result', (data: FilesListResultEvent) => {
      const request = directoryRequests.get(data.request_id)
      if (!request || request.workspaceId !== data.workspace_id || request.path !== data.path) return
      directoryRequests.delete(data.request_id)
      request.resolve(data.error ? null : data.entries)
    })
  }
  if (!workspaceSubscriptions.has(workspaceId)) workspaceSubscriptions.set(workspaceId, subscribeToWorkspace(workspaceId))
  const requestId = `scheduled-task-files-${++directoryRequestCounter}-${Date.now()}`
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      directoryRequests.delete(requestId)
      resolve(null)
    }, 15_000)
    directoryRequests.set(requestId, { workspaceId, path, resolve: (entries) => {
      window.clearTimeout(timeout)
      resolve(entries)
    } })
    try {
      sendFilesList(workspaceId, requestId, path)
    } catch {
      window.clearTimeout(timeout)
      directoryRequests.delete(requestId)
      resolve(null)
    }
  })
}

function waitForUpload(workspaceId: string, requestId: string): Promise<void> {
  if (!removeUploadListener) {
    removeUploadListener = onEvent('files:upload_result', (data: FilesUploadResultEvent) => {
      const request = uploadRequests.get(data.request_id)
      if (!request || request.workspaceId !== data.workspace_id) return
      uploadRequests.delete(data.request_id)
      request.resolve(data.error || data.status === 'error' ? (data.error || 'The file could not be uploaded.') : null)
    })
  }
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      uploadRequests.delete(requestId)
      reject(new Error('Upload timed out. Please retry.'))
    }, 30_000)
    uploadRequests.set(requestId, { workspaceId, resolve: (error) => {
      window.clearTimeout(timeout)
      if (error) reject(new Error(error))
      else resolve()
    } })
  })
}

function searchWorkspaceFiles(workspaceId: string, query: string): Promise<string[] | null> {
  if (!removeFindListener) {
    removeFindListener = onEvent('files:find_result', (data: FilesFindResultEvent) => {
      const request = findRequests.get(data.request_id)
      if (!request || request.workspaceId !== data.workspace_id) return
      findRequests.delete(data.request_id)
      request.resolve(data.error ? null : data.paths.map((entry) => entry.path))
    })
  }
  if (!workspaceSubscriptions.has(workspaceId)) workspaceSubscriptions.set(workspaceId, subscribeToWorkspace(workspaceId))
  const requestId = `scheduled-task-find-${++directoryRequestCounter}-${Date.now()}`
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      findRequests.delete(requestId)
      resolve(null)
    }, 15_000)
    findRequests.set(requestId, { workspaceId, resolve: (paths) => {
      window.clearTimeout(timeout)
      resolve(paths)
    } })
    if (!sendFilesFind(workspaceId, requestId, query, 40)) {
      window.clearTimeout(timeout)
      findRequests.delete(requestId)
      resolve(null)
    }
  })
}
async function searchFiles(): Promise<void> {
  if (!workspaceCanManageFiles.value || !form.value.workspace_id) return
  const request = ++fileSearchRequest.value
  const workspaceId = form.value.workspace_id
  fileSearching.value = true
  fileMatches.value = []
  try {
    const matches = await searchWorkspaceFiles(workspaceId, fileQuery.value.trim())
    if (request === fileSearchRequest.value && workspaceId === form.value.workspace_id) {
      if (matches === null) notifications.error('Could not search workspace files', 'The file search failed or timed out. Please try again.')
      else fileMatches.value = matches
    }
  } catch (error) {
    if (request === fileSearchRequest.value) {
      fileMatches.value = []
      notifications.error('Could not search workspace files', error instanceof Error ? error.message : 'Please try again.')
    }
  } finally {
    if (request === fileSearchRequest.value) fileSearching.value = false
  }
}
function addFileMention(path: string): void {
  const token = `@${`file:${path}`}`
  if (hasUploadMention(form.value.prompt, path)) return
  form.value.prompt = form.value.prompt ? `${form.value.prompt}${form.value.prompt.endsWith('\n') ? '' : '\n'}${token} ` : `${token} `
  dirty.value = true
}
async function uploadFiles(fileList: FileList | File[] | null): Promise<void> {
  if (!fileList || !workspaceCanManageFiles.value || !form.value.workspace_id) return
  const files = Array.from(fileList)
  uploading.value = true
  const generation = formGeneration
  try {
    const workspaceId = form.value.workspace_id
    // The Pinia explorer tree is shared with workspace tools; force-refresh it
    // for this workspace instead of trusting a potentially stale other tree.
    const rootEntries = await fetchWorkspaceListing(workspaceId, '/workspace')
    if (form.value.workspace_id !== workspaceId || !rootEntries) {
      notifications.error('Upload failed', 'Could not load the selected workspace files. Please retry.')
      return
    }
    const metadataDir = rootEntries.find((entry) => entry.path === '/workspace/.opencuria' && entry.type === 'directory')
    const metadataEntries = metadataDir ? await fetchWorkspaceListing(workspaceId, '/workspace/.opencuria') : []
    if (formGeneration !== generation || form.value.workspace_id !== workspaceId) return
    if (metadataDir && metadataEntries === null) {
      notifications.error('Upload failed', 'Could not check existing workspace uploads. Please retry.')
      return
    }
    const uploadDir = metadataEntries?.find((entry) => entry.path === CHAT_UPLOAD_DIR && entry.type === 'directory')
    const uploadEntries = uploadDir ? await fetchWorkspaceListing(workspaceId, CHAT_UPLOAD_DIR) : []
    if (formGeneration !== generation || form.value.workspace_id !== workspaceId) return
    if (uploadEntries === null) {
      notifications.error('Upload failed', 'Could not load existing upload names. Please retry.')
      return
    }
    const taken = new Set(uploadEntries.map((entry) => entry.name))
    const uploaded: string[] = []
    for (const file of files) {
      if (file.size > UPLOAD_MAX_BYTES) {
        notifications.error('Upload failed', `“${file.name}” is larger than the 10 MB limit.`)
        continue
      }
      const name = resolveUniqueFilename(taken, sanitizeUploadFilename(file.name))
      const path = uploadTargetPath(name)
      if (hasUploadMention(form.value.prompt, path)) continue
      const content = await fileToBase64(file)
      if (formGeneration !== generation || form.value.workspace_id !== workspaceId) return
      const requestId = nextChatUploadRequestId(name)
      const tracked = waitForUpload(workspaceId, requestId)
      try { sendFilesUpload(workspaceId, requestId, CHAT_UPLOAD_DIR, name, content, false) }
      catch (error) {
        uploadRequests.get(requestId)?.resolve(error instanceof Error ? error.message : 'The file could not be uploaded.')
      }
      await tracked
      uploaded.push(path)
    }
    if (uploaded.length && formGeneration === generation && form.value.workspace_id === workspaceId) {
      form.value.prompt = appendUploadMentions(form.value.prompt, uploaded)
      dirty.value = true
      notifications.success('Files uploaded', `${uploaded.length} file${uploaded.length === 1 ? '' : 's'} added to the prompt.`)
    }
  } catch (error) {
    notifications.error('Upload failed', error instanceof Error ? error.message : 'Could not upload files.')
  } finally {
    uploading.value = false
    if (uploadInput.value) uploadInput.value.value = ''
  }
}

watch(() => form.value.workspace_id, () => {
  fileSearchRequest.value += 1
  fileSearching.value = false
  fileMatches.value = []
  fileQuery.value = ''
})
const runsRefreshTimer = window.setInterval(() => {
  if (selectedId.value && runs.value.some((run) => run.status === 'running' || run.status === 'claimed')) {
    void loadRuns(selectedId.value)
  }
}, 10_000)
onUnmounted(() => {
  window.clearInterval(runsRefreshTimer)
  removeDirectoryListener?.()
  removeDirectoryListener = null
  removeFindListener?.()
  removeFindListener = null
  removeUploadListener?.()
  removeUploadListener = null
  findRequests.clear()
  uploadRequests.clear()
  for (const unsubscribe of workspaceSubscriptions.values()) unsubscribe()
  workspaceSubscriptions.clear()
  directoryRequests.clear()
})
onMounted(async () => {
  const jobs: Promise<unknown>[] = [refresh()]
  if (!workspaceStore.workspaces.length) jobs.push(workspaceStore.fetchWorkspaces())
  if (!skillStore.skills.length) jobs.push(skillStore.fetchSkills())
  jobs.push(loadProviderModelsCached().then((result) => { models.value = result }).catch(() => { models.value = [] }).finally(() => { modelLoading.value = false }))
  await Promise.all(jobs)
})
</script>

<template>
  <div class="mx-auto w-full max-w-[1440px] pb-10">
    <section class="relative mb-6 overflow-hidden rounded-3xl border border-border bg-card px-5 py-6 shadow-sm sm:px-8 sm:py-8">
      <div class="pointer-events-none absolute -right-14 -top-24 size-72 rounded-full bg-primary/5 blur-3xl" />
      <div class="relative flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex items-start gap-4">
          <div class="flex size-12 shrink-0 items-center justify-center rounded-2xl bg-primary/10 text-primary"><CalendarClock class="size-6" /></div>
          <div>
            <p class="mb-1 text-xs font-semibold uppercase tracking-[0.18em] text-primary">Automation</p>
            <h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">Scheduled tasks</h1>
            <p class="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">Let your agent take care of routine work. Choose a workspace, set a cadence, and each run opens its own chat.</p>
          </div>
        </div>
        <Button class="shrink-0 rounded-xl" data-testid="create-task" @click="createNew"><Plus class="mr-2 size-4" />Create schedule</Button>
      </div>
      <div class="relative mt-6 flex flex-wrap gap-2 text-xs">
        <span class="rounded-full border border-border bg-background/70 px-3 py-1.5"><strong class="mr-1 text-foreground">{{ tasks.length }}</strong><span class="text-muted-foreground">total</span></span>
        <span class="rounded-full border border-emerald-500/20 bg-emerald-500/5 px-3 py-1.5 text-emerald-700 dark:text-emerald-300"><strong class="mr-1">{{ taskStats.active }}</strong>active</span>
        <span class="rounded-full border border-border bg-background/70 px-3 py-1.5 text-muted-foreground"><strong class="mr-1 text-foreground">{{ taskStats.paused }}</strong>paused</span>
      </div>
    </section>

    <div v-if="loading" class="flex min-h-64 items-center justify-center rounded-3xl border border-border bg-card"><Loader2 class="size-6 animate-spin text-primary" /><span class="ml-3 text-sm text-muted-foreground">Loading your schedules…</span></div>
    <div v-else-if="loadError" class="rounded-2xl border border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive"><CircleAlert class="mr-2 inline size-4" />{{ loadError }} <Button variant="outline" size="sm" class="ml-3" @click="refresh"><RefreshCw class="mr-1 size-3.5" />Try again</Button></div>

    <div v-else class="grid min-w-0 gap-5 xl:grid-cols-[minmax(280px,0.78fr)_minmax(0,1.65fr)]">
      <aside class="min-w-0 rounded-3xl border border-border bg-card p-3 shadow-sm sm:p-4">
        <div class="mb-3 flex items-center justify-between px-1">
          <div><h2 class="text-sm font-semibold">Your schedules</h2><p class="mt-0.5 text-xs text-muted-foreground">{{ tasks.length ? `${tasks.length} personal task${tasks.length === 1 ? '' : 's'}` : 'Ready when you are' }}</p></div>
          <Button variant="ghost" size="icon" class="size-9 rounded-xl" aria-label="Create schedule" @click="createNew"><Plus class="size-4" /></Button>
        </div>
        <div v-if="tasks.length" class="space-y-2" data-testid="task-list">
          <button v-for="task in tasks" :key="task.id" type="button" class="group w-full rounded-2xl border p-3 text-left transition-all hover:border-primary/30 hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-primary sm:p-4" :class="selectedId === task.id ? 'border-primary/40 bg-primary/[0.04] shadow-sm' : 'border-transparent bg-muted/25'" :data-testid="`task-card-${task.id}`" @click="selectTask(task)">
            <div class="flex items-start gap-3">
              <span class="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl" :class="task.enabled ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'"><CalendarClock class="size-4" /></span>
              <div class="min-w-0 flex-1">
                <div class="flex items-start justify-between gap-2"><span class="truncate text-sm font-semibold">{{ task.name }}</span><span class="mt-1.5 size-2 shrink-0 rounded-full" :class="task.enabled ? 'bg-emerald-500' : 'bg-muted-foreground/40'" :title="task.enabled ? 'Active' : 'Paused'" /></div>
                <p class="mt-1 truncate text-xs text-muted-foreground">{{ taskWorkspaceName(task.workspace_id) }}</p>
                <div class="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground"><span>{{ taskSchedule(task) }}</span><span aria-hidden="true">·</span><span>{{ task.enabled ? `Next ${formatDate(task.next_run_at, task.timezone_name)}` : 'Paused' }}</span></div>
                <p v-if="lastRunsByTask[task.id]" class="mt-1.5 truncate text-[11px] text-muted-foreground" :data-testid="`last-run-${task.id}`">Last {{ formatRunStatus(lastRunsByTask[task.id]!.status).toLowerCase() }} · {{ formatDate(lastRunsByTask[task.id]!.started_at || lastRunsByTask[task.id]!.scheduled_for) }}</p>
                <p v-else-if="selectedId === task.id && runs.length === 0" class="mt-1.5 text-[11px] text-muted-foreground">No runs yet</p>
              </div>
            </div>
          </button>
        </div>
        <div v-else class="rounded-2xl border border-dashed border-border px-5 py-9 text-center">
          <span class="mx-auto flex size-11 items-center justify-center rounded-2xl bg-primary/10 text-primary"><Clock3 class="size-5" /></span>
          <h3 class="mt-4 text-sm font-semibold">Nothing on the calendar yet</h3>
          <p class="mx-auto mt-1.5 max-w-56 text-xs leading-relaxed text-muted-foreground">Create a recurring task and your agent will start a fresh chat for every run.</p>
          <Button size="sm" class="mt-4 rounded-xl" @click="createNew"><Plus class="mr-1.5 size-3.5" />Create your first task</Button>
        </div>
        <div class="mt-4 rounded-2xl bg-muted/40 p-3.5"><p class="text-xs font-medium">A fresh chat, every time</p><p class="mt-1 text-[11px] leading-relaxed text-muted-foreground">Runs never hijack an open conversation. Follow each result from its run history.</p></div>
      </aside>

      <main class="min-w-0 space-y-5">
        <section class="rounded-3xl border border-border bg-card shadow-sm">
          <div class="flex flex-wrap items-start justify-between gap-4 border-b border-border px-5 py-5 sm:px-7">
            <div class="flex min-w-0 items-start gap-3">
              <span class="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted text-foreground"><Rocket class="size-4" /></span>
              <div class="min-w-0"><p class="text-xs font-medium text-primary">{{ isNew ? 'NEW SCHEDULE' : 'TASK SETTINGS' }}</p><h2 class="mt-1 text-lg font-semibold">{{ isNew ? 'Set up a scheduled task' : form.name || 'Untitled task' }}</h2><p class="mt-1 text-xs text-muted-foreground">Configure when it runs and what your agent should do.</p></div>
            </div>
            <div v-if="!isNew" class="flex w-full items-center gap-2 sm:w-auto">
              <Button variant="outline" size="sm" class="flex-1 rounded-xl sm:flex-none" :disabled="actionBusy || saving" data-testid="run-now" @click="runNow(selectedTask!)"><Play class="mr-1.5 size-3.5" />Run now</Button>
              <Button variant="outline" size="sm" class="rounded-xl" :disabled="actionBusy || saving" :data-testid="selectedTask?.enabled ? 'pause-task' : 'resume-task'" @click="toggleEnabled(selectedTask!)"><Pause v-if="selectedTask?.enabled" class="mr-1.5 size-3.5" /><Play v-else class="mr-1.5 size-3.5" />{{ selectedTask?.enabled ? 'Pause' : 'Resume' }}</Button>
              <Button variant="ghost" size="icon" class="size-9 rounded-xl text-muted-foreground hover:text-destructive" aria-label="Delete schedule" data-testid="delete-task" @click="deleteOpen = true"><Trash2 class="size-4" /></Button>
            </div>
          </div>

          <div class="space-y-7 px-5 py-6 sm:px-7">
            <div class="grid gap-4 sm:grid-cols-2">
              <label class="block space-y-2"><span class="text-xs font-medium">Task name</span><Input :model-value="form.name" maxlength="255" placeholder="e.g. Weekly dependency review" data-testid="task-name" @update:model-value="updateForm('name', String($event))" /><span v-if="errors.name" class="text-xs text-destructive">{{ errors.name }}</span></label>
              <label class="block space-y-2"><span class="text-xs font-medium">Workspace</span>
                <Select :model-value="form.workspace_id || undefined" :disabled="!isNew" @update:model-value="updateForm('workspace_id', String($event))">
                  <SelectTrigger class="h-10 w-full rounded-xl bg-background" data-testid="task-workspace"><SelectValue placeholder="Select a workspace…" /></SelectTrigger>
                  <SelectContent><SelectItem v-for="workspace in visibleWorkspaces" :key="workspace.id" :value="workspace.id" :data-testid="`workspace-option-${workspace.id}`"><span class="flex w-full items-center justify-between gap-5"><span>{{ workspace.name }}</span><span class="text-xs text-muted-foreground">{{ workspaceStatus(workspace) }}</span></span></SelectItem></SelectContent>
                </Select><span v-if="errors.workspace_id" class="text-xs text-destructive">{{ errors.workspace_id }}</span>
                <span v-else-if="chosenWorkspace?.status === WorkspaceStatus.STOPPED" class="text-[11px] text-muted-foreground">Stopped workspaces resume automatically when a task runs.</span>
              </label>
            </div>

            <div class="rounded-2xl border border-border bg-background/60 p-4 sm:p-5">
              <div class="mb-4 flex items-start justify-between gap-3"><div><h3 class="text-sm font-semibold">Schedule</h3><p class="mt-1 text-xs text-muted-foreground">{{ scheduleSummary }} · {{ form.timezone_name || 'Choose a time zone' }}</p></div><Badge variant="secondary" class="rounded-lg text-[10px]">One run per day</Badge></div>
              <div class="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(180px,0.8fr)]">
                <div><span class="mb-2 block text-xs font-medium">Repeat</span><div class="flex rounded-xl bg-muted p-1"><button type="button" class="flex-1 rounded-lg px-3 py-2 text-xs font-medium transition-colors" :class="form.recurrence === 'daily' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'" data-testid="recurrence-daily" @click="updateForm('recurrence', 'daily')">Every day</button><button type="button" class="flex-1 rounded-lg px-3 py-2 text-xs font-medium transition-colors" :class="form.recurrence === 'weekly' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'" data-testid="recurrence-weekly" @click="updateForm('recurrence', 'weekly')">Selected days</button></div></div>
                <label class="block space-y-2"><span class="text-xs font-medium">Time</span><Input type="time" :model-value="form.local_time" data-testid="task-time" @update:model-value="updateForm('local_time', String($event))" /><span v-if="errors.local_time" class="text-xs text-destructive">{{ errors.local_time }}</span></label>
              </div>
              <div v-if="form.recurrence === 'weekly'" class="mt-4"><span class="mb-2 block text-xs font-medium">Run on</span><div class="flex flex-wrap gap-2"><button v-for="day in WEEKDAYS" :key="day.value" type="button" class="min-w-12 rounded-xl border px-3 py-2 text-xs font-medium transition-colors" :class="form.weekdays.includes(day.value) ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-background text-muted-foreground hover:bg-muted'" :aria-pressed="form.weekdays.includes(day.value)" :data-testid="`weekday-${day.value}`" @click="toggleWeekday(day.value)">{{ day.label }}</button><button type="button" class="rounded-xl border border-border px-3 py-2 text-xs font-medium text-muted-foreground hover:bg-muted" data-testid="weekdays-weekdays" @click="setWeekdays([0,1,2,3,4])">Weekdays</button></div><span v-if="errors.weekdays" class="mt-2 block text-xs text-destructive">{{ errors.weekdays }}</span></div>
              <div class="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(180px,0.8fr)]">
                <label class="block space-y-2"><span class="text-xs font-medium">Time zone</span><Input v-model="form.timezone_name" placeholder="Europe/Berlin" data-testid="task-timezone" @update:model-value="dirty = true" /><span class="text-[11px] text-muted-foreground">IANA zone · DST adjusts automatically</span><span v-if="form.timezone_name && !isValidTimezone" class="block text-xs text-destructive">{{ errors.timezone_name || 'Enter a valid IANA time zone.' }}</span></label>
                <div class="rounded-xl bg-primary/[0.06] px-3.5 py-3"><p class="text-[10px] font-semibold uppercase tracking-wide text-primary">Next local run</p><p class="mt-1.5 text-sm font-medium">{{ previewText }}</p><p class="mt-1 truncate text-[11px] text-muted-foreground">{{ form.timezone_name || 'Time zone not set' }}</p></div>
              </div>
            </div>

            <div class="space-y-4">
              <div class="flex flex-wrap items-center justify-between gap-3"><div><h3 class="text-sm font-semibold">Agent instructions</h3><p class="mt-1 text-xs text-muted-foreground">This prompt starts a new chat each time the schedule runs.</p></div><DropdownMenu><DropdownMenuTrigger as-child><Button variant="outline" size="sm" class="rounded-xl" data-testid="task-mode"><span>{{ form.mode === 'build' ? 'Build mode' : 'Plan mode' }}</span><ChevronDown class="ml-2 size-3.5" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem data-testid="task-mode-build" @click="setMode('build')">Build{{ form.mode === 'build' ? ' ✓' : '' }}</DropdownMenuItem><DropdownMenuItem data-testid="task-mode-plan" @click="setMode('plan')">Plan{{ form.mode === 'plan' ? ' ✓' : '' }}</DropdownMenuItem></DropdownMenuContent></DropdownMenu></div>
              <div class="overflow-hidden rounded-2xl border border-border bg-background focus-within:border-primary/60 focus-within:ring-2 focus-within:ring-primary/10"><ComposerRichEditor v-model="form.prompt" :disabled="false" placeholder="Describe the recurring work for your agent… Use @file:path to reference workspace files." data-testid="task-prompt" aria-label="Scheduled task prompt" :aria-invalid="Boolean(errors.prompt)" aria-describedby="task-prompt-error" @update:model-value="dirty = true" /><p v-if="errors.prompt" id="task-prompt-error" data-testid="task-prompt-error" role="alert" class="border-t border-destructive/20 bg-destructive/5 px-3 py-2 text-xs text-destructive">{{ errors.prompt }}</p><div class="flex flex-wrap items-center gap-1 border-t border-border px-2 py-1.5"><HarnessModelPicker :model="form.model" :effort="form.reasoning_effort" :models="models" :recent-models="recentCatalogModels(models)" :recent-efforts="recentEffortEntries" :loading="modelLoading" @update:model="updateForm('model', $event)" @update:effort="updateForm('reasoning_effort', $event)" /><span v-if="!form.model" class="hidden text-[11px] text-muted-foreground sm:inline">Using {{ form.mode }} agent defaults at run time</span><Button v-if="form.model" variant="ghost" size="sm" class="h-8 text-xs text-muted-foreground" data-testid="use-agent-default" @click="form.model = ''; form.reasoning_effort = ''; dirty = true">Use agent default</Button></div></div>
              <div v-if="skillStore.skills.length" class="space-y-2"><p class="text-xs font-medium">Skills <span class="font-normal text-muted-foreground">(optional)</span></p><div class="flex flex-wrap gap-2"><button v-for="skill in skillStore.skills" :key="skill.id" type="button" class="rounded-xl border px-3 py-1.5 text-xs transition-colors" :class="form.skill_ids.includes(skill.id) ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border bg-background text-muted-foreground hover:bg-muted'" :aria-pressed="form.skill_ids.includes(skill.id)" :data-testid="`skill-${skill.id}`" @click="form.skill_ids = form.skill_ids.includes(skill.id) ? form.skill_ids.filter((id) => id !== skill.id) : [...form.skill_ids, skill.id]; dirty = true">{{ form.skill_ids.includes(skill.id) ? '✓ ' : '' }}{{ skill.name }}</button></div></div>

              <div class="rounded-2xl border border-border bg-muted/20 p-3.5">
                <div class="flex flex-wrap items-center justify-between gap-3"><div class="flex items-start gap-2.5"><FolderSearch class="mt-0.5 size-4 text-muted-foreground" /><div><p class="text-xs font-medium">Workspace files</p><p class="mt-1 text-[11px] text-muted-foreground">Search and mention files, or upload new ones.</p></div></div><div class="flex gap-2"><input ref="uploadInput" type="file" multiple class="hidden" data-testid="upload-input" @change="uploadFiles(($event.target as HTMLInputElement).files)" /><Button variant="outline" size="sm" class="rounded-lg" :disabled="!workspaceCanManageFiles || uploading" data-testid="upload-files" :title="workspaceCanManageFiles ? 'Upload files to this workspace' : 'File upload is available when the workspace is running and its runner is online'" @click="uploadInput?.click()"><Loader2 v-if="uploading" class="mr-1.5 size-3.5 animate-spin" /><FilePlus2 v-else class="mr-1.5 size-3.5" />Upload</Button></div></div>
                <p v-if="!workspaceCanManageFiles" class="mt-3 rounded-xl bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-800 dark:text-amber-200" data-testid="files-disabled-hint">{{ chosenWorkspace ? `File browse and upload need a running workspace and online runner. ${chosenWorkspace.status === WorkspaceStatus.STOPPED ? 'This stopped workspace will resume automatically when the task runs.' : ''} You can still edit the prompt and keep existing file mentions.` : 'Select a running workspace to browse files or upload attachments. You can still edit the prompt.' }}</p>
                <div v-else class="mt-3 flex flex-col gap-2 sm:flex-row"><div class="flex min-w-0 flex-1 gap-2"><Input v-model="fileQuery" placeholder="Search workspace files…" class="h-9" data-testid="file-search" @keydown.enter.prevent="searchFiles" /><Button variant="outline" size="sm" class="h-9 shrink-0 rounded-lg" :disabled="fileSearching" data-testid="browse-files" @click="searchFiles"><Loader2 v-if="fileSearching" class="mr-1.5 size-3.5 animate-spin" /><FolderSearch v-else class="mr-1.5 size-3.5" />Browse</Button></div></div>
                <div v-if="fileMatches.length" class="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-xl border border-border bg-background p-1.5" data-testid="file-results"><button v-for="path in fileMatches" :key="path" type="button" class="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs hover:bg-muted" @click="addFileMention(path)"><span class="min-w-0 truncate font-mono">{{ path.replace('/workspace/', '') }}</span><span class="shrink-0 text-primary">Add mention</span></button></div>
              </div>
            </div>

            <div class="flex flex-col-reverse gap-3 border-t border-border pt-5 sm:flex-row sm:items-center sm:justify-between">
              <div class="flex items-center gap-3"><Switch :model-value="form.enabled" data-testid="task-enabled" aria-label="Enable scheduled task" @update:model-value="updateForm('enabled', $event)" /><div><p class="text-xs font-medium">{{ form.enabled ? 'Schedule active' : 'Start paused' }}</p><p class="text-[11px] text-muted-foreground">{{ form.enabled ? 'Runs automatically at the next scheduled time.' : 'Save now and enable it whenever you’re ready.' }}</p></div></div>
              <div class="flex gap-2"><Button v-if="dirty && !isNew" variant="ghost" class="rounded-xl" @click="applyTask(selectedTask!)">Discard changes</Button><Button class="rounded-xl" :disabled="saving || !dirty" data-testid="save-task" @click="save"><Loader2 v-if="saving" class="mr-2 size-4 animate-spin" /><Check v-else class="mr-2 size-4" />{{ isNew ? 'Create schedule' : 'Save changes' }}</Button></div>
            </div>
          </div>
        </section>

        <section class="overflow-hidden rounded-3xl border border-border bg-card shadow-sm">
          <div class="flex items-center justify-between gap-3 border-b border-border px-5 py-4 sm:px-7"><div><h2 class="text-sm font-semibold">Run history</h2><p class="mt-1 text-xs text-muted-foreground">Each run opens a separate chat in this workspace.</p></div><Button v-if="selectedId" variant="ghost" size="icon" class="size-8 rounded-lg" aria-label="Refresh run history" @click="loadRuns(selectedId!)"><RefreshCw class="size-3.5" /></Button></div>
          <div v-if="!selectedId" class="px-5 py-8 text-center text-xs text-muted-foreground">Save this schedule to see run history.</div>
          <div v-else-if="runs.length" class="divide-y divide-border" data-testid="run-history">
            <div v-for="run in runs" :key="run.id" class="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:px-7">
              <span class="flex size-9 shrink-0 items-center justify-center rounded-xl" :class="run.status === 'error' ? 'bg-destructive/10 text-destructive' : 'bg-muted text-muted-foreground'"><CircleAlert v-if="run.status === 'error'" class="size-4" /><Check v-else-if="run.status === 'succeeded'" class="size-4 text-emerald-600" /><Loader2 v-else-if="run.status === 'running' || run.status === 'claimed'" class="size-4 animate-spin text-primary" /><Clock3 v-else class="size-4" /></span>
              <div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-2"><span class="text-xs font-semibold">{{ formatRunStatus(run.status) }}</span><Badge variant="secondary" class="rounded-md px-1.5 py-0 text-[10px]" :class="runStatusClass(run.status)">{{ run.status === 'running' ? 'In progress' : formatRunStatus(run.status) }}</Badge><span v-if="run.reason" class="text-[10px] text-muted-foreground">{{ run.reason.replace(/_/g, ' ') }}</span></div><p class="mt-1 text-[11px] text-muted-foreground">{{ formatDate(run.started_at || run.scheduled_for) }}<span v-if="run.finished_at"> · Finished {{ formatDate(run.finished_at) }}</span></p><p v-if="run.error || run.assistant_error" class="mt-1 break-words text-xs text-destructive">{{ run.assistant_error || run.error }}</p><p v-else-if="run.assistant_finish && run.assistant_finish !== 'stop'" class="mt-1 text-[11px] text-muted-foreground">{{ run.assistant_finish }}</p></div>
              <Button v-if="run.session_id" variant="outline" size="sm" class="w-full shrink-0 rounded-xl sm:w-auto" :data-testid="`open-run-${run.id}`" @click="router.push(chatPath(run.session_id, selectedTask?.workspace_id ?? form.workspace_id))">Open chat<ChevronDown class="ml-1.5 size-3.5 -rotate-90" /></Button>
            </div>
          </div>
          <div v-else class="px-5 py-8 text-center"><span class="mx-auto flex size-10 items-center justify-center rounded-xl bg-muted text-muted-foreground"><Clock3 class="size-4" /></span><p class="mt-3 text-xs font-medium">No runs yet</p><p class="mt-1 text-[11px] text-muted-foreground">Your first run will appear here, along with a direct link to its chat.</p></div>
        </section>
      </main>
    </div>

    <Dialog :open="deleteOpen" @update:open="(open) => { if (!open) deleteOpen = false }"><DialogContent><DialogHeader><DialogTitle>Delete this schedule?</DialogTitle><DialogDescription><strong>{{ selectedTask?.name }}</strong> will be permanently deleted. Existing chats and run history are not removed.</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" :disabled="deleting" @click="deleteOpen = false">Cancel</Button><Button variant="destructive" :disabled="deleting" data-testid="confirm-delete" @click="confirmDelete"><Loader2 v-if="deleting" class="mr-2 size-4 animate-spin" /><Trash2 v-else class="mr-2 size-4" />Delete schedule</Button></DialogFooter></DialogContent></Dialog>
  </div>
</template>
