<!--
  ScheduleTaskRail — scheduled tasks grouped by workspace. Hover highlights a
  task in the calendar, click opens its settings, the switch pauses it.
  Ended one-time tasks need a new date before they can run again.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Switch } from '@/components/ui/switch'
import { describeSchedule, isScheduleEnded, scheduleStateLabel } from '@/lib/scheduledTasks'
import type { ScheduledTask } from '@/services/scheduledTasks.api'

const props = defineProps<{
  tasks: ScheduledTask[]
  workspaceNames: Map<string, string>
  colors: Map<string, string>
  focusTaskId: string | null
  busyTaskIds: Set<string>
}>()
const emit = defineEmits<{
  focus: [taskId: string | null]
  open: [taskId: string]
  toggle: [task: ScheduledTask, enabled: boolean]
}>()

const groups = computed(() => {
  const byWorkspace = new Map<string, ScheduledTask[]>()
  for (const task of props.tasks) {
    byWorkspace.set(task.workspace_id, [...(byWorkspace.get(task.workspace_id) ?? []), task])
  }
  return [...byWorkspace]
    .map(([workspaceId, tasks]) => ({
      workspaceId,
      name: props.workspaceNames.get(workspaceId) ?? 'Unknown workspace',
      color: props.colors.get(workspaceId) ?? 'var(--muted-foreground)',
      tasks: [...tasks].sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
})
</script>

<template>
  <div class="flex flex-col gap-4 p-3" data-testid="schedule-task-rail" @mouseleave="emit('focus', null)">
    <section v-for="group in groups" :key="group.workspaceId" class="space-y-1">
      <h3
        class="flex items-center gap-2 px-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase"
      >
        <span class="size-2 shrink-0 rounded-full" :style="{ backgroundColor: group.color }" />
        <span class="truncate">{{ group.name }}</span>
      </h3>
      <div
        v-for="task in group.tasks"
        :key="task.id"
        class="group flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-muted"
        :class="focusTaskId === task.id ? 'bg-muted' : ''"
        :data-testid="`schedule-rail-task-${task.id}`"
        @mouseenter="emit('focus', task.id)"
      >
        <button
          type="button"
          class="min-w-0 flex-1 text-left focus-visible:outline-2 focus-visible:outline-primary"
          :aria-label="`Open scheduled task ${task.name}`"
          @focus="emit('focus', task.id)"
          @blur="emit('focus', null)"
          @click="emit('open', task.id)"
        >
          <span
            class="block truncate text-[13px]"
            :class="task.enabled ? 'text-foreground' : 'text-muted-foreground'"
            >{{ task.name }}</span
          >
          <span class="block truncate text-[11px] text-muted-foreground">{{
            [scheduleStateLabel(task), describeSchedule(task)].filter(Boolean).join(' · ')
          }}</span>
        </button>
        <Switch
          :model-value="task.enabled"
          :disabled="busyTaskIds.has(task.id) || isScheduleEnded(task)"
          class="scale-75"
          :aria-label="`${task.enabled ? 'Pause' : 'Resume'} ${task.name}`"
          :data-testid="`schedule-rail-toggle-${task.id}`"
          @update:model-value="emit('toggle', task, $event)"
        />
      </div>
    </section>
  </div>
</template>
