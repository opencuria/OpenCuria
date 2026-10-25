<!--
  ScheduledTasksView — full week schedule per workspace with recorded run
  outcomes and direct planning in the grid.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useLocalStorage, useMediaQuery, useNow } from '@vueuse/core'
import { CalendarClock, ChevronLeft, ChevronRight, Globe, Plus, RefreshCw } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import EmptyState from '@/components/common/EmptyState.vue'
import ScheduleBlockDetails from '@/components/scheduled-tasks/ScheduleBlockDetails.vue'
import ScheduleTaskRail from '@/components/scheduled-tasks/ScheduleTaskRail.vue'
import ScheduleWeekGrid from '@/components/scheduled-tasks/ScheduleWeekGrid.vue'
import { usePolling } from '@/composables/usePolling'
import { COMMON_TIME_ZONES, describeSchedule, isValidTimeZone } from '@/lib/scheduledTasks'
import {
  addDays,
  browserTimeZone,
  buildCalendarBlocks,
  calendarOccurrences,
  canMoveBlock,
  canReschedule,
  currentSchedulePatch,
  formatMinutes,
  formatWeekLabel,
  isoDate,
  isSameDay,
  rescheduleTask,
  startOfWeek,
  weekdayIndex,
  workspaceColors,
  zonedInstant,
  zonedTime,
} from '@/lib/scheduleCalendar'
import type { CalendarBlock, CalendarDay, SchedulePatch } from '@/lib/scheduleCalendar'
import { listScheduledTaskRuns, runScheduledTaskNow } from '@/services/scheduledTasks.api'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'
import { useNotificationStore } from '@/stores/notifications'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import { useWorkspaceStore } from '@/stores/workspaces'

const ALL_WORKSPACES = 'all'
const BROWSER_ZONE = 'browser'

const route = useRoute()
const router = useRouter()
const taskStore = useScheduledTaskStore()
const workspaceStore = useWorkspaceStore()
const notifications = useNotificationStore()
const isMobile = useMediaQuery('(max-width: 767px)')
const now = useNow({ interval: 60_000 })

/** Times show in the browser's zone unless the user picked another one. */
const zoneChoice = useLocalStorage('opencuria:schedule-time-zone', BROWSER_ZONE)
const browserZone = browserTimeZone()
const zone = computed(() =>
  zoneChoice.value !== BROWSER_ZONE && isValidTimeZone(zoneChoice.value)
    ? zoneChoice.value
    : browserZone,
)
const today = computed(() => zonedTime(now.value, zone.value).day)
const anchor = ref<CalendarDay>(zonedTime(new Date(), zone.value).day)
const workspaceFilter = ref(ALL_WORKSPACES)
const focusTaskId = ref<string | null>(null)
const runsByTask = ref<Record<string, ScheduledTaskRun[]>>({})
/** Optimistic edits shown until the server confirms them. */
const overrides = ref<Record<string, Partial<ScheduledTask>>>({})
const busyTaskIds = ref(new Set<string>())
const selection = ref<{ block: CalendarBlock; element: HTMLElement } | null>(null)

const tasks = computed(() =>
  taskStore.tasks.map((task) =>
    overrides.value[task.id] ? { ...task, ...overrides.value[task.id] } : task,
  ),
)
const tasksById = computed(() => new Map(tasks.value.map((task) => [task.id, task])))
const workspaceNames = computed(
  () => new Map(workspaceStore.workspaces.map((workspace) => [workspace.id, workspace.name])),
)
const scheduledWorkspaces = computed(() =>
  [...new Set(tasks.value.map((task) => task.workspace_id))]
    .map((id) => ({ id, name: workspaceNames.value.get(id) ?? 'Unknown workspace' }))
    .sort((a, b) => a.name.localeCompare(b.name)),
)
const colors = computed(() => workspaceColors(scheduledWorkspaces.value))
const zoneOptions = computed(() =>
  [...new Set([...COMMON_TIME_ZONES, ...tasks.value.map((task) => task.timezone_name)])]
    .filter((option) => option !== browserZone && isValidTimeZone(option))
    .sort(),
)
const visibleTasks = computed(() =>
  workspaceFilter.value === ALL_WORKSPACES
    ? tasks.value
    : tasks.value.filter((task) => task.workspace_id === workspaceFilter.value),
)
const movableTaskIds = computed(
  () => new Set(visibleTasks.value.filter(canReschedule).map((task) => task.id)),
)

const weekStart = computed(() => startOfWeek(anchor.value))
const weekDays = computed(() =>
  Array.from({ length: 7 }, (_, index) => addDays(weekStart.value, index)),
)
const weekBlocks = computed(() =>
  buildCalendarBlocks(
    calendarOccurrences(
      visibleTasks.value,
      runsByTask.value,
      zonedInstant(weekStart.value, 0, zone.value),
      zonedInstant(addDays(weekStart.value, 7), 0, zone.value),
    ),
    weekStart.value,
    7,
    zone.value,
  ),
)
const anchorIndex = computed(() => weekDays.value.findIndex((day) => isSameDay(day, anchor.value)))
const gridDays = computed(() => (isMobile.value ? [anchor.value] : weekDays.value))
const gridBlocks = computed(() =>
  isMobile.value
    ? [(weekBlocks.value[anchorIndex.value] ?? []).map((block) => ({ ...block, day: 0 }))]
    : weekBlocks.value,
)
const rangeLabel = computed(() =>
  isMobile.value
    ? new Intl.DateTimeFormat(undefined, {
        month: 'long',
        year: 'numeric',
        timeZone: 'UTC',
      }).format(anchor.value)
    : formatWeekLabel(weekStart.value, addDays(weekStart.value, 6)),
)
const showsToday = computed(() => gridDays.value.some((day) => isSameDay(day, today.value)))
const narrowWeekday = new Intl.DateTimeFormat(undefined, { weekday: 'narrow', timeZone: 'UTC' })

const selectedTask = computed(() =>
  selection.value ? (tasksById.value.get(selection.value.block.taskId) ?? null) : null,
)

function navigate(direction: -1 | 1): void {
  selection.value = null
  anchor.value = addDays(anchor.value, direction * (isMobile.value ? 1 : 7))
}

function goToday(): void {
  selection.value = null
  anchor.value = today.value
}

function filteredWorkspaceId(): string | undefined {
  return workspaceFilter.value === ALL_WORKSPACES ? undefined : workspaceFilter.value
}

function openNew(): void {
  taskStore.openNew({ workspace_id: filteredWorkspaceId(), timezone_name: zone.value })
}

/** New tasks use the shown zone, so a 09:00 slot becomes a 09:00 task. */
function createAt(day: CalendarDay, minute: number): void {
  selection.value = null
  taskStore.openNew({
    workspace_id: filteredWorkspaceId(),
    date: isoDate(day),
    weekdays: [weekdayIndex(day)],
    times: [formatMinutes(minute)],
    timezone_name: zone.value,
  })
}

function openTask(taskId: string): void {
  selection.value = null
  taskStore.openTask(taskId)
}

function setBusy(taskId: string, busy: boolean): void {
  const next = new Set(busyTaskIds.value)
  if (busy) next.add(taskId)
  else next.delete(taskId)
  busyTaskIds.value = next
}

function clearOverride(taskId: string): void {
  const rest = { ...overrides.value }
  delete rest[taskId]
  overrides.value = rest
}

/** Latest run history per task (the API returns each task's recent runs). */
let runsGeneration = 0
async function loadRuns(): Promise<void> {
  const generation = ++runsGeneration
  const ids = taskStore.tasks.map((task) => task.id)
  const results = await Promise.allSettled(ids.map((id) => listScheduledTaskRuns(id)))
  if (generation !== runsGeneration) return
  const next: Record<string, ScheduledTaskRun[]> = {}
  results.forEach((result, index) => {
    const id = ids[index]!
    if (result.status === 'fulfilled') next[id] = result.value
    else if (runsByTask.value[id]) next[id] = runsByTask.value[id]
  })
  runsByTask.value = next
}

async function applySchedule(
  task: ScheduledTask,
  patch: SchedulePatch,
  undo?: SchedulePatch,
): Promise<void> {
  overrides.value = { ...overrides.value, [task.id]: patch }
  setBusy(task.id, true)
  try {
    const saved = await taskStore.update(task.id, patch)
    notifications.success(
      undo ? 'Schedule moved' : 'Move undone',
      `${saved.name} · ${describeSchedule(saved)}`,
      undo ? { label: 'Undo', onClick: () => void applySchedule(saved, undo) } : undefined,
    )
  } catch (error) {
    notifications.error(
      'Could not change schedule',
      error instanceof Error ? error.message : 'Please try again.',
    )
  } finally {
    clearOverride(task.id)
    setBusy(task.id, false)
  }
}

function moveBlock(block: CalendarBlock, day: CalendarDay, minute: number): void {
  const task = tasksById.value.get(block.taskId)
  const from = block.occurrences[0]?.at
  if (!task || !from || busyTaskIds.value.has(task.id)) return
  selection.value = null
  const patch = rescheduleTask(task, from, zonedInstant(day, minute, zone.value))
  if (patch) void applySchedule(task, patch, currentSchedulePatch(task))
}

async function setEnabled(task: ScheduledTask, enabled: boolean): Promise<void> {
  if (busyTaskIds.value.has(task.id)) return
  overrides.value = { ...overrides.value, [task.id]: { enabled } }
  setBusy(task.id, true)
  try {
    await taskStore.update(task.id, { enabled })
  } catch (error) {
    notifications.error(
      enabled ? 'Could not resume task' : 'Could not pause task',
      error instanceof Error ? error.message : 'Please try again.',
    )
  } finally {
    clearOverride(task.id)
    setBusy(task.id, false)
  }
}

async function runNow(task: ScheduledTask): Promise<void> {
  if (busyTaskIds.value.has(task.id)) return
  selection.value = null
  setBusy(task.id, true)
  try {
    const run = await runScheduledTaskNow(task.id)
    if (run.status === 'running' || run.status === 'claimed')
      notifications.info('Run started', `${task.name} is running in a new chat.`)
    else notifications.error('Task could not run', run.error || 'The run was skipped.')
    await loadRuns()
  } catch (error) {
    notifications.error(
      'Could not run task',
      error instanceof Error ? error.message : 'Please try again.',
    )
  } finally {
    setBusy(task.id, false)
  }
}

function openChat(task: ScheduledTask, run: ScheduledTaskRun): void {
  if (!run.session_id) return
  selection.value = null
  void router.push({ path: `/workspaces/${task.workspace_id}`, query: { session: run.session_id } })
}

function selectBlock(block: CalendarBlock, element: HTMLElement): void {
  selection.value = { block, element }
}

const { start: startRunsPolling } = usePolling(loadRuns, 60_000)

watch(
  () => taskStore.tasks.map((task) => task.id).join(','),
  () => void loadRuns(),
)
watch(weekBlocks, () => {
  if (selection.value && !selection.value.element.isConnected) selection.value = null
})
watch(scheduledWorkspaces, (workspaces) => {
  if (
    workspaceFilter.value !== ALL_WORKSPACES &&
    !workspaces.some((workspace) => workspace.id === workspaceFilter.value)
  )
    workspaceFilter.value = ALL_WORKSPACES
})

onMounted(async () => {
  startRunsPolling()
  // Legacy links (/scheduled-tasks?task=<id>) open that task's settings.
  const legacyTask = route.query.task
  if (typeof legacyTask !== 'string' || !legacyTask) return
  await taskStore.refresh().catch(() => undefined)
  taskStore.openTask(legacyTask)
  const query = { ...route.query }
  delete query.task
  void router.replace({ path: route.path, query }).catch(() => undefined)
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col gap-4 p-4 lg:p-6" data-testid="scheduled-tasks-view">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0">
        <h2 class="text-xl font-semibold text-foreground">Scheduled tasks</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          See when agents run on which workspace. Click a free slot to plan a run.
        </p>
      </div>
      <Button data-testid="schedule-new-task" @click="openNew()"><Plus />New task</Button>
    </header>

    <div
      class="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card shadow-xs"
    >
      <aside class="hidden w-60 shrink-0 flex-col border-r border-border lg:flex">
        <div class="flex h-12 shrink-0 items-center gap-1.5 border-b border-border px-4">
          <h3 class="text-sm font-medium">Tasks</h3>
          <span class="text-sm text-muted-foreground tabular-nums">{{ visibleTasks.length }}</span>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto">
          <div
            v-if="taskStore.loading && !taskStore.tasks.length"
            class="space-y-2 p-4"
            aria-label="Loading scheduled tasks"
          >
            <div class="h-10 animate-pulse rounded-lg bg-muted/60" />
            <div class="h-10 w-3/4 animate-pulse rounded-lg bg-muted/40" />
          </div>
          <div
            v-else-if="taskStore.loadError"
            class="flex items-center justify-between gap-2 p-4 text-xs text-destructive"
            role="alert"
          >
            <span class="min-w-0 truncate">Couldn’t load tasks</span>
            <Button
              variant="outline"
              size="sm"
              @click="taskStore.refresh(true).catch(() => undefined)"
              ><RefreshCw />Retry</Button
            >
          </div>
          <EmptyState
            v-else-if="!visibleTasks.length"
            :icon="CalendarClock"
            title="Nothing scheduled"
            description="Click a time in the calendar to plan your first run."
          />
          <ScheduleTaskRail
            v-else
            :tasks="visibleTasks"
            :workspace-names="workspaceNames"
            :colors="colors"
            :focus-task-id="focusTaskId"
            :busy-task-ids="busyTaskIds"
            @focus="focusTaskId = $event"
            @open="openTask"
            @toggle="setEnabled"
          />
        </div>
      </aside>

      <section class="flex min-w-0 flex-1 flex-col">
        <div
          class="flex min-h-12 shrink-0 flex-wrap items-center gap-1.5 border-b border-border px-3 py-2"
        >
          <Button
            variant="outline"
            size="sm"
            :disabled="showsToday"
            data-testid="schedule-today"
            @click="goToday"
            >Today</Button
          >
          <Button
            variant="ghost"
            size="icon-sm"
            :aria-label="isMobile ? 'Previous day' : 'Previous week'"
            data-testid="schedule-prev"
            @click="navigate(-1)"
            ><ChevronLeft
          /></Button>
          <Button
            variant="ghost"
            size="icon-sm"
            :aria-label="isMobile ? 'Next day' : 'Next week'"
            data-testid="schedule-next"
            @click="navigate(1)"
            ><ChevronRight
          /></Button>
          <h3 class="ml-1 min-w-0 truncate text-sm font-semibold" data-testid="schedule-range">
            {{ rangeLabel }}
          </h3>
          <div class="flex w-full items-center gap-1.5 sm:ml-auto sm:w-auto">
            <Select v-model="zoneChoice">
              <SelectTrigger
                size="sm"
                class="min-w-0 flex-1 rounded-lg sm:w-44 sm:flex-none"
                :title="`Times shown in ${zone}`"
                aria-label="Time zone"
                data-testid="schedule-time-zone"
                ><Globe class="size-3.5 text-muted-foreground" /><SelectValue
              /></SelectTrigger>
              <SelectContent>
                <SelectItem :value="BROWSER_ZONE">Browser · {{ browserZone }}</SelectItem>
                <SelectItem v-for="option in zoneOptions" :key="option" :value="option">{{
                  option
                }}</SelectItem>
              </SelectContent>
            </Select>
            <Select v-if="scheduledWorkspaces.length > 1" v-model="workspaceFilter">
              <SelectTrigger
                size="sm"
                class="min-w-0 flex-1 rounded-lg sm:w-48 sm:flex-none"
                aria-label="Filter by workspace"
                data-testid="schedule-workspace-filter"
                ><SelectValue
              /></SelectTrigger>
              <SelectContent>
                <SelectItem :value="ALL_WORKSPACES">All workspaces</SelectItem>
                <SelectItem
                  v-for="workspace in scheduledWorkspaces"
                  :key="workspace.id"
                  :value="workspace.id"
                  ><span class="flex min-w-0 items-center gap-2"
                    ><span
                      class="size-2 shrink-0 rounded-full"
                      :style="{ backgroundColor: colors.get(workspace.id) }"
                    /><span class="truncate">{{ workspace.name }}</span></span
                  ></SelectItem
                >
              </SelectContent>
            </Select>
          </div>
        </div>

        <div
          v-if="isMobile"
          class="grid shrink-0 grid-cols-7 gap-1 border-b border-border px-2 py-1.5"
          data-testid="schedule-day-strip"
        >
          <button
            v-for="(day, index) in weekDays"
            :key="day.getTime()"
            type="button"
            class="flex flex-col items-center gap-0.5 rounded-lg py-1 text-[11px] transition-colors"
            :class="
              index === anchorIndex
                ? 'bg-primary text-primary-foreground'
                : isSameDay(day, today)
                  ? 'text-primary'
                  : 'text-muted-foreground hover:bg-muted'
            "
            :aria-pressed="index === anchorIndex"
            @click="anchor = day"
          >
            <span>{{ narrowWeekday.format(day) }}</span>
            <span class="text-sm font-semibold tabular-nums">{{ day.getUTCDate() }}</span>
            <span
              class="size-1 rounded-full"
              :class="
                weekBlocks[index]?.length
                  ? index === anchorIndex
                    ? 'bg-primary-foreground'
                    : 'bg-primary'
                  : 'bg-transparent'
              "
            />
          </button>
        </div>

        <ScheduleWeekGrid
          :days="gridDays"
          :blocks="gridBlocks"
          :tasks="tasksById"
          :colors="colors"
          :now="now"
          :zone="zone"
          :focus-task-id="focusTaskId"
          :movable-task-ids="movableTaskIds"
          @create="createAt"
          @select="selectBlock"
          @move="moveBlock"
        />
      </section>
    </div>

    <Popover
      v-if="selection && selectedTask"
      :open="true"
      @update:open="
        (open) => {
          if (!open) selection = null
        }
      "
    >
      <PopoverAnchor :reference="selection.element" />
      <PopoverContent
        :side="isMobile ? 'bottom' : 'right'"
        align="start"
        class="w-[21rem]"
        @open-auto-focus.prevent
      >
        <ScheduleBlockDetails
          :block="selection.block"
          :task="selectedTask"
          :workspace-name="workspaceNames.get(selectedTask.workspace_id) ?? 'Unknown workspace'"
          :zone="zone"
          :color="colors.get(selectedTask.workspace_id) ?? 'var(--muted-foreground)'"
          :movable="canMoveBlock(selection.block, movableTaskIds)"
          :busy="busyTaskIds.has(selectedTask.id)"
          @edit="openTask(selectedTask.id)"
          @run="runNow(selectedTask)"
          @toggle="setEnabled(selectedTask, !selectedTask.enabled)"
          @open-chat="openChat(selectedTask, $event)"
        />
      </PopoverContent>
    </Popover>
  </div>
</template>
