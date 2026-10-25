<script setup lang="ts">
import { nextTick } from 'vue'
import { CalendarCheck, CalendarClock, CalendarDays, CirclePause, Plus } from '@lucide/vue'
import { useSidebar } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import { describeSchedule, scheduleStateLabel } from '@/lib/scheduledTasks'
import type { ScheduledTask } from '@/services/scheduledTasks.api'

defineProps<{ tasks: ScheduledTask[] }>()
const emit = defineEmits<{ 'open-all': [] }>()
const tasksStore = useScheduledTaskStore()
const { isMobile, setOpenMobile } = useSidebar()

async function afterMobileDrawerCloses(): Promise<void> {
  await nextTick()
  await new Promise<void>((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
  )
}

async function openNew(): Promise<void> {
  if (isMobile.value) {
    setOpenMobile(false)
    await afterMobileDrawerCloses()
  }
  tasksStore.openNew()
}

async function openTask(id: string): Promise<void> {
  if (isMobile.value) {
    setOpenMobile(false)
    await afterMobileDrawerCloses()
  }
  tasksStore.openTask(id)
}

function cadence(task: ScheduledTask): string {
  return describeSchedule(task)
}
</script>

<template>
  <section class="px-2 pt-2" data-testid="scheduled-tasks-section">
    <div class="flex h-7 items-center gap-1 px-2">
      <h2 class="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        Scheduled tasks
      </h2>
      <Button
        variant="ghost"
        size="icon-xs"
        class="ml-auto text-muted-foreground"
        aria-label="New scheduled task"
        data-testid="new-scheduled-task"
        @click="openNew"
      >
        <Plus />
      </Button>
    </div>
    <div
      v-if="tasksStore.loading && !tasks.length"
      class="space-y-1 px-2 py-1"
      aria-label="Loading scheduled tasks"
      data-testid="scheduled-tasks-loading"
    >
      <div class="h-8 animate-pulse rounded-lg bg-muted/60" />
      <div class="h-8 w-3/4 animate-pulse rounded-lg bg-muted/40" />
    </div>
    <div v-else-if="tasks.length" class="flex flex-col gap-0.5">
      <button
        v-for="task in tasks"
        :key="task.id"
        type="button"
        class="flex min-h-11 min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-muted focus-visible:outline-2 focus-visible:outline-primary"
        :aria-label="
          [`Open scheduled task ${task.name}`, cadence(task), scheduleStateLabel(task)]
            .filter(Boolean)
            .join(' · ')
        "
        :data-testid="`scheduled-task-${task.id}`"
        @click="openTask(task.id)"
      >
        <CalendarClock class="size-4 shrink-0 text-muted-foreground" />
        <span class="min-w-0 flex-1">
          <span class="block truncate text-[13px]">{{ task.name }}</span>
          <span class="block truncate text-[11px] text-muted-foreground">{{ cadence(task) }}</span>
        </span>
        <CalendarCheck
          v-if="scheduleStateLabel(task) === 'Ended'"
          class="size-3.5 shrink-0 text-muted-foreground"
          aria-label="Ended"
        />
        <CirclePause
          v-else-if="!task.enabled"
          class="size-3.5 shrink-0 text-muted-foreground"
          :aria-label="`Paused`"
        />
      </button>
    </div>
    <p
      v-else-if="!tasksStore.loading && !tasksStore.loadError"
      class="px-2 py-1 text-[11px] text-muted-foreground"
    >
      No scheduled tasks
    </p>
    <div v-if="tasksStore.loadError" class="flex items-center justify-between gap-2 px-2 py-1">
      <p class="min-w-0 truncate text-[11px] text-destructive">Couldn’t load tasks</p>
      <Button
        variant="ghost"
        size="sm"
        class="h-7 px-2 text-xs"
        data-testid="retry-scheduled-tasks"
        @click="tasksStore.refresh(true).catch(() => undefined)"
        >Retry</Button
      >
    </div>
    <Button
      variant="ghost"
      size="sm"
      data-testid="open-schedule-calendar"
      class="mt-1 h-7 w-full justify-start rounded-xl px-2 text-xs font-normal text-muted-foreground"
      @click="emit('open-all')"
    >
      <CalendarDays class="size-3.5" />Calendar
    </Button>
  </section>
</template>
