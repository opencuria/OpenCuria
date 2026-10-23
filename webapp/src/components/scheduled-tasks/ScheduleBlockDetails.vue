<!--
  ScheduleBlockDetails — popover body for a planned run, a past run, or a
  series of runs in the schedule calendar.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { CirclePlay, MessageSquare, Pause, Pencil, Play } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { describeSchedule } from '@/lib/scheduledTasks'
import { describeSeries, formatMinutes, wallClock } from '@/lib/scheduleCalendar'
import type { CalendarBlock } from '@/lib/scheduleCalendar'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'

const props = defineProps<{
  block: CalendarBlock
  task: ScheduledTask
  workspaceName: string
  color: string
  movable: boolean
  busy: boolean
  /** Zone the calendar shows times in. */
  zone: string
}>()
const emit = defineEmits<{
  edit: []
  run: []
  toggle: []
  openChat: [run: ScheduledTaskRun]
}>()

const STATUS_LABELS: Record<string, string> = {
  claimed: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  error: 'Failed',
  skipped: 'Skipped',
  interrupted: 'Interrupted',
}
const STATUS_CLASSES: Record<string, string> = {
  succeeded: 'text-success',
  error: 'text-destructive',
  interrupted: 'text-warning',
  running: 'text-primary',
  claimed: 'text-primary',
}

const first = computed(() => props.block.occurrences[0]!)
const isSeries = computed(() => props.block.occurrences.length > 1)
const singleRun = computed(() => (isSeries.value ? null : (first.value.run ?? null)))

const when = computed(() => {
  const day = new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: props.zone,
  }).format(first.value.at)
  if (!isSeries.value) return `${day} · ${formatMinutes(props.block.start)}`
  return `${day} · ${formatMinutes(props.block.start)}–${formatMinutes(props.block.end)} · ${describeSeries(props.block)}`
})

/** The task's own wall-clock time when it differs from the shown zone. */
const taskTime = computed(() => {
  if (isSeries.value || props.task.timezone_name === props.zone) return ''
  return `${wallClock(first.value.at, props.task.timezone_name).time} ${props.task.timezone_name}`
})

const runSummary = computed(() => {
  if (props.block.kind !== 'run') return []
  const counts = new Map<string, number>()
  for (const item of props.block.occurrences) {
    const status = item.run?.status ?? ''
    counts.set(status, (counts.get(status) ?? 0) + 1)
  }
  return [...counts].map(([status, count]) => ({
    status,
    text: isSeries.value
      ? `${count} ${(STATUS_LABELS[status] ?? status).toLowerCase()}`
      : (STATUS_LABELS[status] ?? status),
    class: STATUS_CLASSES[status] ?? 'text-muted-foreground',
  }))
})

const runError = computed(() => {
  const run = singleRun.value
  if (run) return run.assistant_error || run.error || ''
  return ''
})
</script>

<template>
  <div class="space-y-3" data-testid="schedule-block-details">
    <div class="flex items-start gap-2.5">
      <span class="mt-1.5 size-2.5 shrink-0 rounded-full" :style="{ backgroundColor: color }" />
      <div class="min-w-0 flex-1">
        <p class="truncate text-sm font-semibold">{{ task.name }}</p>
        <p class="truncate text-xs text-muted-foreground">
          {{ workspaceName }} · {{ describeSchedule(task) }}
        </p>
      </div>
    </div>

    <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
      <dt class="text-muted-foreground">When</dt>
      <dd class="min-w-0">{{ when }}</dd>
      <template v-if="taskTime">
        <dt class="text-muted-foreground">Task time</dt>
        <dd class="min-w-0 truncate">{{ taskTime }}</dd>
      </template>
      <template v-if="runSummary.length">
        <dt class="text-muted-foreground">{{ isSeries ? 'Runs' : 'Status' }}</dt>
        <dd class="flex min-w-0 flex-wrap gap-x-2" data-testid="schedule-block-status">
          <span v-for="item in runSummary" :key="item.status" :class="item.class">{{
            item.text
          }}</span>
        </dd>
      </template>
    </dl>

    <p v-if="runError" class="line-clamp-3 rounded-md bg-error-muted px-2 py-1.5 text-xs text-error">
      {{ runError }}
    </p>
    <p v-else-if="block.kind === 'planned' && !task.enabled" class="text-xs text-muted-foreground">
      Paused · this run will not start.
    </p>
    <p v-else-if="movable" class="text-xs text-muted-foreground">
      Drag in the calendar to change the time.
    </p>

    <div class="flex flex-wrap items-center gap-1.5 border-t border-border pt-3">
      <Button
        v-if="singleRun?.session_id"
        size="sm"
        data-testid="schedule-open-chat"
        @click="emit('openChat', singleRun)"
        ><MessageSquare />Open chat</Button
      >
      <Button
        size="sm"
        :variant="singleRun?.session_id ? 'outline' : 'default'"
        data-testid="schedule-edit-task"
        @click="emit('edit')"
        ><Pencil />Edit</Button
      >
      <Button
        size="sm"
        variant="ghost"
        :disabled="busy"
        data-testid="schedule-run-now"
        @click="emit('run')"
        ><Play />Run now</Button
      >
      <Button
        v-if="block.kind === 'planned'"
        size="sm"
        variant="ghost"
        :disabled="busy"
        data-testid="schedule-toggle-task"
        @click="emit('toggle')"
        ><component :is="task.enabled ? Pause : CirclePlay" />{{
          task.enabled ? 'Pause' : 'Resume'
        }}</Button
      >
    </div>
  </div>
</template>
