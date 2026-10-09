<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Check, Loader2, MoreHorizontal, Play, RefreshCw, Trash2, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import { runScheduledTaskNow, listScheduledTaskRuns } from '@/services/scheduledTasks.api'
import type {
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskRun,
} from '@/services/scheduledTasks.api'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useAuthStore } from '@/stores/auth'
import { useSkillStore } from '@/stores/skills'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import { WorkspaceStatus } from '@/types'
import type { HarnessId } from '@/types/harness'
import {
  isValidTimeZone,
  nextLocalOccurrence,
  validateSchedule,
  WEEKDAYS,
} from '@/lib/scheduledTasks'
import { useNotificationStore } from '@/stores/notifications'
import { usePolling } from '@/composables/usePolling'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const authStore = useAuthStore()
const skillStore = useSkillStore()
const taskStore = useScheduledTaskStore()
const notifications = useNotificationStore()

const runs = ref<ScheduledTaskRun[]>([])
const loadingRuns = ref(false)
const runsError = ref('')
const selectedId = ref<string | null>(taskStore.selectedTaskId)
const saving = ref(false)
const actionBusy = ref(false)
const deleting = ref(false)
const deleteOpen = ref(false)
const deleteError = ref('')
const discardOpen = ref(false)
const dirty = ref(false)
const tab = ref<'settings' | 'runs'>('settings')
const uploading = ref(false)

interface FormState {
  name: string
  workspace_id: string
  prompt: string
  mode: 'plan' | 'build'
  harness_id: HarnessId
  model: string
  reasoning_effort: string
  skill_ids: string[]
  weekdays: number[]
  local_time: string
  timezone_name: string
  enabled: boolean
}
function blankForm(): FormState {
  return {
    name: '',
    workspace_id: '',
    prompt: '',
    mode: 'build',
    harness_id: 'native',
    model: '',
    reasoning_effort: '',
    skill_ids: [],
    weekdays: WEEKDAYS.map((day) => day.value),
    local_time: '09:00',
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    enabled: true,
  }
}
const form = ref<FormState>(blankForm())
const recurrence = computed(() =>
  WEEKDAYS.every((day) => form.value.weekdays.includes(day.value)) ? 'daily' : 'weekly',
)
const errors = ref<Record<string, string>>({})
let formBaseline = JSON.stringify(form.value)
watch(
  form,
  () => {
    dirty.value = JSON.stringify(form.value) !== formBaseline
  },
  { deep: true },
)
const apiError = ref('')
const selectedTask = computed(
  () => taskStore.tasks.find((task) => task.id === selectedId.value) ?? null,
)
const isNew = computed(() => selectedId.value === null)
const taskUnavailable = computed(
  () => !isNew.value && (!selectedTask.value || selectedId.value === 'latest'),
)
const unavailableMessage = computed(
  () =>
    taskStore.loadError ||
    (selectedId.value === 'latest'
      ? 'No scheduled tasks are available.'
      : 'This task is no longer available.'),
)
const visibleWorkspaces = computed(() =>
  workspaceStore.workspaces.filter(
    (workspace) =>
      ![
        WorkspaceStatus.REMOVED,
        WorkspaceStatus.DELETED,
        WorkspaceStatus.DELETING,
        WorkspaceStatus.PENDING_DELETION,
      ].includes(workspace.status),
  ),
)
const chosenWorkspace = computed(
  () =>
    visibleWorkspaces.value.find((workspace) => workspace.id === form.value.workspace_id) ?? null,
)
const workspaceCanManageFiles = computed(
  () =>
    chosenWorkspace.value?.status === WorkspaceStatus.RUNNING &&
    chosenWorkspace.value.runner_online &&
    !chosenWorkspace.value.active_operation,
)
const timezoneValid = computed(() => isValidTimeZone(form.value.timezone_name))
const nextPreview = computed(() => {
  if (!timezoneValid.value) return null
  if (!dirty.value && selectedTask.value) {
    const authoritative = new Date(selectedTask.value.next_run_at)
    if (!Number.isNaN(authoritative.getTime())) return authoritative
  }
  return nextLocalOccurrence(
    recurrence.value,
    form.value.weekdays,
    form.value.local_time,
    form.value.timezone_name,
  )
})
const previewText = computed(() =>
  nextPreview.value
    ? new Intl.DateTimeFormat(undefined, {
        timeZone: form.value.timezone_name,
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }).format(nextPreview.value)
    : 'Choose a valid time',
)
const busy = computed(() => saving.value || uploading.value || actionBusy.value || deleting.value)
let postDiscardAction: (() => void) | null = null
let disposed = false
let lastOrganizationId = authStore.activeOrganizationId

let formGeneration = 0
const composerResetKey = ref(0)
const runsRequestGeneration = ref(0)

function applyTask(task: ScheduledTask): void {
  formGeneration += 1
  composerResetKey.value = formGeneration
  form.value = {
    name: task.name,
    workspace_id: task.workspace_id,
    prompt: task.prompt,
    mode: task.mode,
    harness_id: task.harness_id ?? 'native',
    model: task.model,
    reasoning_effort: task.reasoning_effort,
    skill_ids: [...task.skill_ids],
    weekdays: task.recurrence === 'daily' ? WEEKDAYS.map((day) => day.value) : [...task.weekdays],
    local_time: task.local_time,
    timezone_name: task.timezone_name,
    enabled: task.enabled,
  }
  dirty.value = false
  errors.value = {}
  apiError.value = ''
  formBaseline = JSON.stringify(form.value)
}
function applyBlank(): void {
  formGeneration += 1
  composerResetKey.value = formGeneration
  form.value = blankForm()
  if (visibleWorkspaces.value.length === 1) form.value.workspace_id = visibleWorkspaces.value[0]!.id
  dirty.value = false
  errors.value = {}
  apiError.value = ''
  runs.value = []
  formBaseline = JSON.stringify(form.value)
}
function syncSelection(): void {
  selectedId.value = taskStore.selectedTaskId
  runsRequestGeneration.value += 1
  runs.value = []
  if (selectedId.value && selectedTask.value) applyTask(selectedTask.value)
  else if (!selectedId.value) applyBlank()
  else {
    formGeneration += 1
    form.value = blankForm()
    dirty.value = false
    errors.value = {}
    apiError.value = taskStore.loadError || 'This task is not available yet.'
  }
  tab.value = 'settings'
}
function openDialog(): void {
  syncSelection()
  if (taskStore.loadError) void taskStore.refresh(true).catch(() => undefined)
  else if (!taskStore.tasks.length && !taskStore.loading)
    void taskStore.refresh().catch(() => undefined)
}
function closeDialog(): void {
  if (busy.value) return
  emit('update:open', false)
  taskStore.closeDialog()
}
function requestClose(afterDiscard?: () => void): void {
  if (busy.value) return
  const action = typeof afterDiscard === 'function' ? afterDiscard : closeDialog
  if (dirty.value) {
    postDiscardAction = action
    discardOpen.value = true
  } else {
    action()
  }
}
function discardChanges(): void {
  if (busy.value) return
  discardOpen.value = false
  dirty.value = false
  const action = typeof postDiscardAction === 'function' ? postDiscardAction : closeDialog
  postDiscardAction = null
  action()
}
function toggleWeekday(day: number): void {
  const days = new Set(form.value.weekdays)
  if (days.has(day)) days.delete(day)
  else days.add(day)
  form.value.weekdays = [...days].sort((a, b) => a - b)
  dirty.value = true
}
function updateForm<K extends keyof FormState>(key: K, value: FormState[K]): void {
  if (JSON.stringify(form.value[key]) === JSON.stringify(value)) return
  form.value[key] = value
  dirty.value = true
  apiError.value = ''
}
function validate(): boolean {
  const result = validateSchedule({ ...form.value, recurrence: recurrence.value })
  errors.value = { ...result.errors }
  if (isNew.value && !form.value.workspace_id && visibleWorkspaces.value.length === 0)
    errors.value.workspace_id = 'Create a workspace before scheduling a task.'
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(form.value.local_time))
    errors.value.local_time = 'Enter a valid time.'
  return (
    result.valid &&
    !errors.value.local_time &&
    !(isNew.value && !form.value.workspace_id && visibleWorkspaces.value.length === 0)
  )
}
async function save(): Promise<void> {
  if (busy.value || taskUnavailable.value || !validate()) return
  if (!dirty.value) return
  saving.value = true
  const generation = formGeneration
  const organizationId = authStore.activeOrganizationId
  apiError.value = ''
  try {
    const payload: ScheduledTaskInput = {
      ...form.value,
      name: form.value.name.trim(),
      prompt: form.value.prompt.trim(),
      recurrence: recurrence.value,
      weekdays: recurrence.value === 'daily' ? [] : form.value.weekdays,
    }
    const saved = selectedId.value
      ? await taskStore.update(selectedId.value, payload)
      : await taskStore.create(payload)
    if (
      disposed ||
      generation !== formGeneration ||
      organizationId !== authStore.activeOrganizationId ||
      organizationId !== lastOrganizationId
    )
      return
    selectedId.value = saved.id
    taskStore.selectedTaskId = saved.id
    applyTask(saved)
    notifications.success('Schedule saved', `${saved.name} is ready to run.`)
  } catch (error) {
    if (
      !disposed &&
      organizationId === authStore.activeOrganizationId &&
      organizationId === lastOrganizationId
    ) {
      apiError.value = error instanceof Error ? error.message : 'Please try again.'
    }
  } finally {
    saving.value = false
  }
}
async function loadRuns(id: string): Promise<void> {
  const request = ++runsRequestGeneration.value
  loadingRuns.value = true
  runsError.value = ''
  try {
    const result = await listScheduledTaskRuns(id)
    if (
      request === runsRequestGeneration.value &&
      selectedId.value === id &&
      props.open &&
      tab.value === 'runs'
    )
      runs.value = result
  } catch (error) {
    if (
      request === runsRequestGeneration.value &&
      selectedId.value === id &&
      props.open &&
      tab.value === 'runs'
    ) {
      runsError.value = error instanceof Error ? error.message : 'Could not load run history.'
      runs.value = []
    }
  } finally {
    if (request === runsRequestGeneration.value && props.open && tab.value === 'runs')
      loadingRuns.value = false
  }
}
watch(tab, (value) => {
  if (value === 'runs' && props.open) {
    if (selectedId.value) void loadRuns(selectedId.value)
    startRunsPolling()
  } else stopRunsPolling()
})
async function runNow(): Promise<void> {
  if (!selectedTask.value || dirty.value || busy.value || taskUnavailable.value) return
  actionBusy.value = true
  const id = selectedTask.value.id
  const organizationId = authStore.activeOrganizationId
  try {
    const run = await runScheduledTaskNow(id)
    if (
      disposed ||
      organizationId !== authStore.activeOrganizationId ||
      organizationId !== lastOrganizationId
    )
      return
    if (run.status === 'running' || run.status === 'claimed')
      notifications.info(
        'Run started',
        run.status === 'running' ? 'A new chat was created for this run.' : 'Your task is queued.',
      )
    else notifications.error('Task could not run', run.error || 'The run was skipped.')
    if (tab.value === 'runs') void loadRuns(id)
  } catch (error) {
    if (
      !disposed &&
      organizationId === authStore.activeOrganizationId &&
      organizationId === lastOrganizationId
    ) {
      apiError.value = error instanceof Error ? error.message : 'Could not run task.'
    }
  } finally {
    actionBusy.value = false
  }
}
async function confirmDelete(): Promise<void> {
  const task = selectedTask.value
  if (!task || deleting.value || busy.value) return
  deleting.value = true
  deleteError.value = ''
  const organizationId = authStore.activeOrganizationId
  try {
    await taskStore.remove(task.id)
    if (
      disposed ||
      organizationId !== authStore.activeOrganizationId ||
      organizationId !== lastOrganizationId
    )
      return
    deleteOpen.value = false
    notifications.success('Schedule deleted', `${task.name} was removed.`)
    emit('update:open', false)
    taskStore.closeDialog()
  } catch (error) {
    if (
      !disposed &&
      organizationId === authStore.activeOrganizationId &&
      organizationId === lastOrganizationId
    ) {
      deleteError.value = error instanceof Error ? error.message : 'Could not delete schedule.'
    }
  } finally {
    deleting.value = false
  }
}
function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}
function formatRunStatus(status: string): string {
  return (
    (
      {
        claimed: 'Queued',
        running: 'Running',
        succeeded: 'Succeeded',
        error: 'Failed',
        skipped: 'Skipped',
        interrupted: 'Interrupted',
      } as Record<string, string>
    )[status] ?? status
  )
}
function chatPath(sessionId: string, workspaceId: string): string {
  return `/workspaces/${workspaceId}?session=${encodeURIComponent(sessionId)}`
}
async function openRunChat(run: ScheduledTaskRun): Promise<void> {
  if (!run.session_id || busy.value) return
  requestClose(async () => {
    const path = chatPath(
      run.session_id!,
      selectedTask.value?.workspace_id ?? form.value.workspace_id,
    )
    closeDialog()
    await nextTick()
    await router.push(path)
  })
}

watch(
  () => taskStore.selectedTaskId,
  (id) => {
    if (!props.open) return
    selectedId.value = id
    runsRequestGeneration.value += 1
    runs.value = []
    if (id && selectedTask.value) applyTask(selectedTask.value)
    else if (!id) applyBlank()
    else {
      formGeneration += 1
      composerResetKey.value = formGeneration
      form.value = blankForm()
      dirty.value = false
      errors.value = {}
      apiError.value = taskStore.loadError || 'This task is not available yet.'
    }
    tab.value = 'settings'
  },
)
watch(selectedTask, (task) => {
  if (!props.open || !task || task.id !== selectedId.value || dirty.value) return
  if (!form.value.workspace_id || taskUnavailable.value) applyTask(task)
})
watch(
  () => authStore.activeOrganizationId,
  (id) => {
    lastOrganizationId = id
    formGeneration += 1
    composerResetKey.value = formGeneration
    runsRequestGeneration.value += 1
    runs.value = []
    syncSelection()
  },
)
const { start: startRunsPolling, stop: stopRunsPolling } = usePolling(async () => {
  if (
    !props.open ||
    tab.value !== 'runs' ||
    !selectedId.value ||
    !runs.value.some((run) => run.status === 'running' || run.status === 'claimed')
  )
    return
  await loadRuns(selectedId.value)
}, 10_000)
watch(
  () => props.open,
  (open) => {
    if (open) {
      openDialog()
      if (tab.value === 'runs' && selectedId.value) void loadRuns(selectedId.value)
      startRunsPolling()
    } else {
      runsRequestGeneration.value += 1
      loadingRuns.value = false
      stopRunsPolling()
      formGeneration += 1
      composerResetKey.value = formGeneration
      uploading.value = false
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  disposed = true
  stopRunsPolling()
  runsRequestGeneration.value += 1
  formGeneration += 1
})
onMounted(async () => {
  if (props.open) openDialog()
  const jobs: Promise<unknown>[] = []
  if (!workspaceStore.workspaces.length) jobs.push(workspaceStore.fetchWorkspaces())
  if (!skillStore.skills.length) jobs.push(skillStore.fetchSkills())
  await Promise.all(jobs)
})
</script>

<template>
  <Dialog
    :open="props.open"
    @update:open="
      (open) => {
        if (open) emit('update:open', true)
        else requestClose()
      }
    "
  >
    <DialogContent
      :show-close-button="false"
      class="flex max-h-[min(54rem,calc(100dvh-2rem))] w-[calc(100%-2rem)] max-w-[40rem] flex-col gap-0 overflow-hidden rounded-xl p-0 sm:max-w-[40rem]"
      data-testid="scheduled-task-dialog"
    >
      <Tabs
        :model-value="tab"
        class="flex min-h-0 flex-1 flex-col"
        @update:model-value="tab = $event as 'settings' | 'runs'"
        @keydown.esc.stop.prevent="requestClose()"
      >
        <DialogHeader class="shrink-0 border-b border-border px-4 py-4 sm:px-6">
          <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
              <DialogTitle class="text-base font-semibold">{{
                isNew ? 'New scheduled task' : form.name || 'Task settings'
              }}</DialogTitle>
              <DialogDescription class="mt-1 text-xs"
                >Work and schedule · each run starts a new chat.</DialogDescription
              >
            </div>
            <div class="flex shrink-0 items-center gap-1">
              <DropdownMenu v-if="!isNew">
                <DropdownMenuTrigger as-child
                  ><Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Task actions"
                    data-testid="task-actions"
                    ><MoreHorizontal class="size-4" /></Button
                ></DropdownMenuTrigger>
                <DropdownMenuContent align="end"
                  ><DropdownMenuItem
                    variant="destructive"
                    data-testid="delete-task"
                    :disabled="busy"
                    @select.prevent="((deleteError = ''), (deleteOpen = true))"
                    ><Trash2 class="mr-2 size-4" />Delete schedule</DropdownMenuItem
                  ></DropdownMenuContent
                >
              </DropdownMenu>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Close task settings"
                data-testid="close-task-dialog"
                :disabled="busy"
                @click="requestClose()"
                ><X class="size-4"
              /></Button>
            </div>
          </div>
          <TabsList variant="line" class="mt-2 h-8 w-full justify-start gap-4">
            <TabsTrigger
              value="settings"
              class="h-8 max-w-fit flex-none px-0 text-xs data-active:after:bottom-[-1px]"
              data-testid="task-settings-tab"
              @click="tab = 'settings'"
              >Settings</TabsTrigger
            >
            <TabsTrigger
              v-if="!isNew"
              value="runs"
              class="h-8 max-w-fit flex-none px-0 text-xs data-active:after:bottom-[-1px]"
              data-testid="task-runs-tab"
              @click="tab = 'runs'"
              >Runs</TabsTrigger
            >
          </TabsList>
        </DialogHeader>

        <TabsContent
          value="settings"
          class="flex min-h-0 flex-1 flex-col overflow-hidden"
          data-testid="task-settings-panel"
        >
          <div
            class="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6"
            :inert="busy"
            :aria-busy="busy"
          >
            <div
              v-if="taskUnavailable"
              class="flex items-center justify-between gap-3 py-5 text-sm text-muted-foreground"
              role="alert"
              data-testid="task-unavailable"
            >
              <span>{{ unavailableMessage }}</span>
              <Button
                variant="outline"
                size="sm"
                @click="
                  taskStore
                    .refresh(true)
                    .then(() => {
                      if (selectedTask) applyTask(selectedTask)
                    })
                    .catch(() => undefined)
                "
                ><RefreshCw class="mr-1.5 size-3.5" />Retry</Button
              >
            </div>
            <div v-else class="space-y-4">
              <div class="grid gap-3 sm:grid-cols-2">
                <label class="block space-y-1.5"
                  ><span class="block text-xs font-medium">Task name</span
                  ><Input
                    :model-value="form.name"
                    maxlength="255"
                    placeholder="e.g. Weekly dependency review"
                    class="rounded-lg"
                    data-testid="task-name"
                    @update:model-value="updateForm('name', String($event))"
                  /><span v-if="errors.name" class="text-xs text-destructive">{{
                    errors.name
                  }}</span></label
                >
                <label class="block space-y-1.5"
                  ><span class="block text-xs font-medium">Workspace</span>
                  <Select
                    :model-value="form.workspace_id || undefined"
                    :disabled="!isNew || busy"
                    @update:model-value="updateForm('workspace_id', String($event))"
                  >
                    <SelectTrigger class="h-9 w-full rounded-lg" data-testid="task-workspace"
                      ><SelectValue placeholder="Select a workspace…">{{
                        chosenWorkspace?.name
                      }}</SelectValue>
                    </SelectTrigger>
                    <SelectContent
                      ><SelectItem
                        v-for="workspace in visibleWorkspaces"
                        :key="workspace.id"
                        :value="workspace.id"
                        :data-testid="`workspace-option-${workspace.id}`"
                        ><span class="flex w-full items-center justify-between gap-5"
                          ><span>{{ workspace.name }}</span
                          ><span class="text-xs text-muted-foreground">{{
                            String(workspace.status).replace(/_/g, ' ')
                          }}</span></span
                        ></SelectItem
                      ></SelectContent
                    >
                  </Select>
                  <span v-if="errors.workspace_id" class="text-xs text-destructive">{{
                    errors.workspace_id
                  }}</span>
                  <span
                    v-else-if="chosenWorkspace?.status === WorkspaceStatus.STOPPED"
                    class="text-xs text-muted-foreground"
                    >Stopped workspace resumes automatically at run time.</span
                  >
                </label>
              </div>

              <section class="space-y-2" aria-labelledby="instructions-heading">
                <h3 id="instructions-heading" class="text-xs font-semibold">Instructions</h3>
                <HarnessChatInput
                  :prompt="form.prompt"
                  :mode="form.mode"
                  :harness-id="form.harness_id"
                  :model="form.model"
                  :effort="form.reasoning_effort"
                  :skill-ids="form.skill_ids"
                  @update:prompt="updateForm('prompt', $event)"
                  @update:mode="updateForm('mode', $event)"
                  @update:harness-id="updateForm('harness_id', $event)"
                  @update:model="updateForm('model', $event)"
                  @update:effort="updateForm('reasoning_effort', $event)"
                  @update:skill-ids="updateForm('skill_ids', $event)"
                  variant="scheduled-task"
                  :reset-key="composerResetKey"
                  :workspace-id="form.workspace_id"
                  :skill-options="skillStore.skills"
                  :files-available="workspaceCanManageFiles"
                  :disabled="busy || taskUnavailable"
                  :prompt-error="errors.prompt"
                  @uploading="uploading = $event"
                />
                <p
                  v-if="!workspaceCanManageFiles && chosenWorkspace"
                  class="px-4 text-xs text-muted-foreground"
                  data-testid="files-disabled-hint"
                >
                  File search and upload need a running workspace and online runner. Existing file
                  mentions remain available.
                </p>
              </section>

              <section
                class="space-y-3 border-t border-border pt-3"
                aria-labelledby="schedule-heading"
              >
                <div>
                  <h3 id="schedule-heading" class="text-xs font-semibold">Schedule</h3>
                </div>
                <div class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_130px]">
                  <div class="space-y-1.5">
                    <span class="block text-xs font-medium">Days</span>
                    <div class="flex flex-wrap items-center gap-1.5" data-testid="weekday-picker">
                      <Button
                        v-for="day in WEEKDAYS"
                        :key="day.value"
                        variant="outline"
                        size="sm"
                        class="h-9 min-w-9 rounded-md px-2 text-xs"
                        :class="
                          form.weekdays.includes(day.value)
                            ? 'border-primary/50 bg-primary/10 text-primary'
                            : 'border-border text-muted-foreground'
                        "
                        :aria-pressed="form.weekdays.includes(day.value)"
                        :data-testid="`weekday-${day.value}`"
                        @click="toggleWeekday(day.value)"
                      >
                        {{ day.label }}</Button
                      ><Button
                        variant="ghost"
                        size="sm"
                        class="h-8 rounded-lg px-2 text-xs"
                        data-testid="weekdays-weekdays"
                        @click="updateForm('weekdays', [0, 1, 2, 3, 4])"
                        >Mon–Fri</Button
                      >
                    </div>
                    <span v-if="errors.weekdays" class="text-xs text-destructive">{{
                      errors.weekdays
                    }}</span>
                  </div>
                  <label class="block space-y-1.5"
                    ><span class="block text-xs font-medium">Time</span
                    ><Input
                      type="time"
                      :model-value="form.local_time"
                      class="h-9 rounded-lg"
                      data-testid="task-time"
                      @update:model-value="updateForm('local_time', String($event))"
                    /><span v-if="errors.local_time" class="text-xs text-destructive">{{
                      errors.local_time
                    }}</span></label
                  >
                </div>
                <div class="grid gap-3 sm:grid-cols-2">
                  <label class="block space-y-1.5"
                    ><span class="block text-xs font-medium">Time zone</span
                    ><Input
                      v-model="form.timezone_name"
                      list="scheduled-timezones"
                      placeholder="Europe/Berlin"
                      class="h-9 rounded-lg"
                      data-testid="task-timezone"
                      @update:model-value="dirty = true"
                    /><datalist id="scheduled-timezones">
                      <option value="UTC" />
                      <option value="Europe/London" />
                      <option value="Europe/Berlin" />
                      <option value="America/New_York" />
                      <option value="America/Los_Angeles" />
                      <option value="Asia/Tokyo" /></datalist
                    ><span class="block text-xs text-muted-foreground"
                      >Local time, including daylight saving.</span
                    ><span
                      v-if="form.timezone_name && !timezoneValid"
                      class="block text-xs text-destructive"
                      >{{ errors.timezone_name || 'Enter a valid IANA time zone.' }}</span
                    ></label
                  >
                  <p class="self-center text-xs text-muted-foreground sm:pt-5">
                    Next run · {{ previewText }} in
                    {{ form.timezone_name || 'the selected time zone' }}
                  </p>
                </div>
                <div class="flex items-center justify-between gap-4 border-t border-border pt-3">
                  <div>
                    <p class="text-xs font-medium">Enabled</p>
                    <p class="text-xs text-muted-foreground">
                      {{
                        form.enabled
                          ? 'Run automatically on this schedule.'
                          : 'Paused · no runs will start'
                      }}
                    </p>
                  </div>
                  <Switch
                    :model-value="form.enabled"
                    data-testid="task-enabled"
                    aria-label="Enable scheduled task"
                    @update:model-value="updateForm('enabled', $event)"
                  />
                </div>
              </section>
            </div>
          </div>
          <DialogFooter
            class="!flex !flex-row !items-center !justify-between gap-2 border-t border-border px-4 py-3 sm:px-6"
          >
            <Button
              v-if="!isNew"
              variant="ghost"
              size="sm"
              class="h-9 rounded-lg px-2 text-xs"
              :disabled="taskUnavailable || dirty || busy"
              data-testid="run-now"
              @click="runNow"
              ><Play class="mr-1.5 size-3.5" />Run now</Button
            ><span v-else />
            <div class="flex shrink-0 gap-2">
              <Button
                variant="outline"
                size="sm"
                class="h-9 rounded-lg"
                :disabled="busy"
                data-testid="cancel-task"
                @click="requestClose()"
                >Cancel</Button
              ><Button
                size="sm"
                class="h-9 rounded-lg"
                :disabled="taskUnavailable || busy || !dirty"
                data-testid="save-task"
                @click="save"
                ><Loader2 v-if="saving" class="mr-1.5 size-3.5 animate-spin" /><Check
                  v-else
                  class="mr-1.5 size-3.5"
                />{{ isNew ? 'Create task' : 'Save' }}</Button
              >
            </div>
          </DialogFooter>
        </TabsContent>

        <TabsContent
          v-if="!isNew"
          value="runs"
          class="flex min-h-0 flex-1 flex-col overflow-hidden"
          data-testid="task-runs-panel"
        >
          <div class="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
            <div
              v-if="loadingRuns"
              class="flex items-center gap-2 py-8 text-sm text-muted-foreground"
            >
              <Loader2 class="size-4 animate-spin" />Loading runs…
            </div>
            <div
              v-else-if="runsError"
              class="flex items-center justify-between gap-3 py-4 text-xs text-destructive"
              role="alert"
              data-testid="task-runs-error"
            >
              <span>{{ runsError }}</span
              ><Button variant="outline" size="sm" @click="selectedId && loadRuns(selectedId)"
                ><RefreshCw class="mr-1 size-3.5" />Retry</Button
              >
            </div>
            <div v-else-if="runs.length" class="divide-y divide-border" data-testid="run-history">
              <div v-for="run in runs" :key="run.id" class="flex items-start gap-3 py-3">
                <span
                  class="mt-0.5 size-2 rounded-full"
                  :class="
                    run.status === 'error'
                      ? 'bg-destructive'
                      : run.status === 'succeeded'
                        ? 'bg-emerald-500'
                        : 'bg-muted-foreground'
                  "
                />
                <div class="min-w-0 flex-1">
                  <p class="text-xs font-medium">
                    {{ formatRunStatus(run.status)
                    }}<span class="ml-2 font-normal text-muted-foreground">{{
                      formatDate(run.started_at || run.scheduled_for)
                    }}</span>
                  </p>
                  <p
                    v-if="run.error || run.assistant_error"
                    class="mt-1 break-words text-xs text-destructive"
                  >
                    {{ run.assistant_error || run.error }}
                  </p>
                  <p v-if="run.reason" class="mt-1 text-xs text-muted-foreground">
                    {{ run.reason.replace(/_/g, ' ') }}
                  </p>
                </div>
                <Button
                  v-if="run.session_id"
                  variant="outline"
                  size="sm"
                  class="h-8 shrink-0 rounded-lg"
                  :data-testid="`open-run-${run.id}`"
                  @click="openRunChat(run)"
                  >Open chat</Button
                >
              </div>
            </div>
            <p v-else class="py-8 text-center text-sm text-muted-foreground">
              No runs yet. Your first run will appear here.
            </p>
          </div>
          <DialogFooter
            class="!flex !flex-row !items-center !justify-end gap-2 border-t border-border px-4 py-3 sm:px-6"
            ><Button
              variant="outline"
              size="sm"
              class="h-9 rounded-lg"
              :disabled="taskUnavailable || dirty || busy"
              @click="runNow"
              ><Play class="mr-1.5 size-3.5" />Run now</Button
            ></DialogFooter
          >
        </TabsContent>
      </Tabs>
    </DialogContent>
  </Dialog>

  <Dialog
    :open="discardOpen"
    @update:open="
      (open) => {
        if (!open) {
          discardOpen = false
          postDiscardAction = null
        }
      }
    "
    ><DialogContent class="max-w-sm"
      ><DialogHeader
        ><DialogTitle>Discard unsaved changes?</DialogTitle
        ><DialogDescription
          >Your changes to this scheduled task will be lost.</DialogDescription
        ></DialogHeader
      ><DialogFooter
        ><Button
          variant="outline"
          :disabled="busy"
          data-testid="keep-editing"
          @click="((discardOpen = false), (postDiscardAction = null))"
          >Keep editing</Button
        ><Button
          variant="destructive"
          :disabled="busy"
          data-testid="discard-changes"
          @click="discardChanges"
          >Discard changes</Button
        ></DialogFooter
      ></DialogContent
    ></Dialog
  >
  <Dialog
    :open="deleteOpen"
    @update:open="
      (open) => {
        if (!open) deleteOpen = false
      }
    "
    ><DialogContent class="max-w-sm"
      ><DialogHeader
        ><DialogTitle>Delete this task?</DialogTitle
        ><DialogDescription
          ><strong>{{ selectedTask?.name }}</strong> will be permanently deleted. Existing chats and
          run history are not removed.</DialogDescription
        ></DialogHeader
      >
      <p
        v-if="deleteError"
        class="text-sm text-destructive"
        role="alert"
        data-testid="delete-task-error"
      >
        {{ deleteError }}
      </p>
      <DialogFooter
        ><Button variant="outline" :disabled="deleting" @click="deleteOpen = false">Cancel</Button
        ><Button
          variant="destructive"
          :disabled="deleting"
          data-testid="confirm-delete"
          @click="confirmDelete"
          ><Loader2 v-if="deleting" class="mr-2 size-4 animate-spin" /><Trash2
            v-else
            class="mr-2 size-4"
          />Delete task</Button
        ></DialogFooter
      ></DialogContent
    ></Dialog
  >
</template>
