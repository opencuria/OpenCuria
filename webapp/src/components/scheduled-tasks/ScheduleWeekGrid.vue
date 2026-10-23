<!--
  ScheduleWeekGrid — time grid for scheduled-task runs in a display time zone.
  Click an empty slot to plan a task there, click a run for details, drag a
  set-time run to move it.
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { CircleAlert, CircleCheck, CircleMinus, CircleX, Loader2, Plus } from '@lucide/vue'
import type { ScheduledTask } from '@/services/scheduledTasks.api'
import {
  blockVisualEnd,
  canMoveBlock,
  describeSeries,
  formatMinutes,
  isSameDay,
  isSeries,
  MINUTES_PER_DAY,
  zonedTime,
  zoneOffsetLabel,
} from '@/lib/scheduleCalendar'
import type { CalendarBlock, CalendarDay } from '@/lib/scheduleCalendar'

const HOUR_PX = 48
const SNAP_MINUTES = 15
const SLOT_MINUTES = 30
const DRAG_THRESHOLD_PX = 4
const MAX_SERIES_TICKS = 96
/** Edge of a series band kept visible beside single runs drawn over it. */
const SERIES_INSET_PX = 14
const HOURS = Array.from({ length: 24 }, (_, hour) => hour)

const props = defineProps<{
  days: CalendarDay[]
  blocks: CalendarBlock[][]
  tasks: Map<string, ScheduledTask>
  colors: Map<string, string>
  now: Date
  /** IANA zone all times and days are shown in. */
  zone: string
  focusTaskId: string | null
  movableTaskIds: Set<string>
}>()
const emit = defineEmits<{
  create: [day: CalendarDay, minute: number]
  select: [block: CalendarBlock, element: HTMLElement]
  move: [block: CalendarBlock, day: CalendarDay, minute: number]
}>()

const scroller = ref<HTMLElement | null>(null)
const columnsEl = ref<HTMLElement | null>(null)
const hover = ref<{ day: number; minute: number } | null>(null)
const drag = ref<{
  block: CalendarBlock
  originX: number
  originY: number
  day: number
  minute: number
  moved: boolean
} | null>(null)
let suppressClick = false

const weekdayFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', timeZone: 'UTC' })
/** Offset of the shown week (it may differ from today's around DST). */
const zoneLabel = computed(() =>
  props.days[0] ? zoneOffsetLabel(props.days[0], props.zone) : props.zone,
)
const nowInZone = computed(() => zonedTime(props.now, props.zone))
const columnsStyle = computed(() => ({
  gridTemplateColumns: `repeat(${props.days.length}, minmax(0, 1fr))`,
}))
const hourLines = {
  backgroundImage: 'linear-gradient(to bottom, var(--border) 1px, transparent 1px)',
  backgroundSize: `100% ${HOUR_PX}px`,
}

const px = (minutes: number): string => `${(minutes / 60) * HOUR_PX}px`
const isToday = (day: CalendarDay): boolean => isSameDay(day, nowInZone.value.day)
const isWeekend = (day: CalendarDay): boolean => day.getUTCDay() === 0 || day.getUTCDay() === 6

function taskOf(block: CalendarBlock): ScheduledTask | undefined {
  return props.tasks.get(block.taskId)
}

function colorOf(block: CalendarBlock): string {
  const task = taskOf(block)
  return (task && props.colors.get(task.workspace_id)) || 'var(--muted-foreground)'
}

function isPaused(block: CalendarBlock): boolean {
  return block.kind === 'planned' && taskOf(block)?.enabled === false
}

function isMovable(block: CalendarBlock): boolean {
  return canMoveBlock(block, props.movableTaskIds)
}

function surfaceStyle(block: CalendarBlock, color: string): Record<string, string> {
  if (isPaused(block)) {
    return {
      backgroundColor: 'var(--card)',
      border: `1px dashed color-mix(in oklch, ${color} 55%, transparent)`,
      borderLeft: `3px dashed ${color}`,
    }
  }
  const tint = isSeries(block) ? 7 : block.kind === 'run' ? 10 : 18
  return {
    backgroundColor: `color-mix(in oklch, ${color} ${tint}%, var(--card))`,
    border: `1px solid color-mix(in oklch, ${color} 24%, transparent)`,
    borderLeft: `3px solid ${color}`,
  }
}

function blockStyle(block: CalendarBlock): Record<string, string> {
  const inset = block.inset ? SERIES_INSET_PX : 0
  return {
    ...surfaceStyle(block, colorOf(block)),
    top: `calc(${px(block.start)} + 1px)`,
    height: `calc(${px(blockVisualEnd(block) - block.start)} - 2px)`,
    left: `calc(${inset}px + (100% - ${inset}px) * ${block.column / block.columns} + 2px)`,
    width: `calc((100% - ${inset}px) / ${block.columns} - 4px)`,
  }
}

const ghostStyle = computed(() => {
  const state = drag.value
  if (!state) return {}
  return {
    ...surfaceStyle(state.block, colorOf(state.block)),
    top: `calc(${px(state.minute)} + 1px)`,
    height: `calc(${px(blockVisualEnd({ start: state.minute, end: state.minute, step: 0 }) - state.minute)} - 2px)`,
  }
})

/** Worst status first, so a failing series stands out. */
const STATUS_RANK = ['error', 'interrupted', 'running', 'claimed', 'skipped', 'succeeded']

function runStatus(block: CalendarBlock): string | null {
  if (block.kind !== 'run') return null
  const statuses = block.occurrences.map((item) => item.run?.status ?? '')
  return STATUS_RANK.find((status) => statuses.includes(status)) ?? statuses[0] ?? null
}

function statusIcon(status: string | null) {
  switch (status) {
    case 'succeeded':
      return { icon: CircleCheck, class: 'text-success', label: 'Succeeded' }
    case 'error':
      return { icon: CircleX, class: 'text-destructive', label: 'Failed' }
    case 'running':
    case 'claimed':
      return { icon: Loader2, class: 'animate-spin text-primary', label: 'Running' }
    case 'skipped':
      return { icon: CircleMinus, class: 'text-muted-foreground', label: 'Skipped' }
    case 'interrupted':
      return { icon: CircleAlert, class: 'text-warning', label: 'Interrupted' }
    default:
      return null
  }
}

function seriesLabel(block: CalendarBlock): string {
  if (block.occurrences.length < 2) return ''
  const failed = block.occurrences.filter((item) => item.run?.status === 'error').length
  return [
    `${formatMinutes(block.start)}–${formatMinutes(block.end)}`,
    describeSeries(block).toLowerCase(),
    failed ? `${failed} failed` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

function seriesTicks(block: CalendarBlock): number[] {
  if (block.occurrences.length < 2 || block.occurrences.length > MAX_SERIES_TICKS) return []
  return block.occurrences.map((item) => zonedTime(item.at, props.zone).minute - block.start)
}

function blockLabel(block: CalendarBlock): string {
  const task = taskOf(block)
  const status = statusIcon(runStatus(block))?.label
  const when =
    block.occurrences.length > 1
      ? `${formatMinutes(block.start)} to ${formatMinutes(block.end)}, ${describeSeries(block)}`
      : formatMinutes(block.start)
  return [task?.name ?? 'Task', when, status, isPaused(block) ? 'Paused' : '']
    .filter(Boolean)
    .join(' · ')
}

function minuteAt(event: PointerEvent | MouseEvent, column: HTMLElement): number {
  const offset = event.clientY - column.getBoundingClientRect().top
  const slot = Math.floor(((offset / HOUR_PX) * 60) / SLOT_MINUTES) * SLOT_MINUTES
  return Math.min(Math.max(slot, 0), MINUTES_PER_DAY - SLOT_MINUTES)
}

function onColumnHover(event: PointerEvent, day: number): void {
  if (drag.value || event.pointerType !== 'mouse') return
  if ((event.target as HTMLElement).closest('[data-block]')) {
    hover.value = null
    return
  }
  const minute = minuteAt(event, event.currentTarget as HTMLElement)
  if (hover.value?.day !== day || hover.value.minute !== minute) hover.value = { day, minute }
}

function onColumnClick(event: MouseEvent, day: number): void {
  if ((event.target as HTMLElement).closest('[data-block]')) return
  emit('create', props.days[day]!, minuteAt(event, event.currentTarget as HTMLElement))
}

function onBlockPointerDown(event: PointerEvent, block: CalendarBlock): void {
  if (event.button !== 0 || event.pointerType === 'touch' || !isMovable(block)) return
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  hover.value = null
  drag.value = {
    block,
    originX: event.clientX,
    originY: event.clientY,
    day: block.day,
    minute: block.start,
    moved: false,
  }
}

function onBlockPointerMove(event: PointerEvent): void {
  const state = drag.value
  if (!state || !columnsEl.value) return
  const dx = event.clientX - state.originX
  const dy = event.clientY - state.originY
  if (!state.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return
  state.moved = true
  const rect = columnsEl.value.getBoundingClientRect()
  const columnWidth = rect.width / props.days.length
  state.day = Math.min(
    Math.max(Math.floor((event.clientX - rect.left) / columnWidth), 0),
    props.days.length - 1,
  )
  const delta = Math.round(((dy / HOUR_PX) * 60) / SNAP_MINUTES) * SNAP_MINUTES
  state.minute = Math.min(Math.max(state.block.start + delta, 0), MINUTES_PER_DAY - SNAP_MINUTES)
}

function onBlockPointerUp(block: CalendarBlock): void {
  const state = drag.value
  drag.value = null
  if (!state?.moved) return
  suppressClick = true
  if (state.day !== block.day || state.minute !== block.start)
    emit('move', block, props.days[state.day]!, state.minute)
}

function onBlockClick(event: MouseEvent, block: CalendarBlock): void {
  if (suppressClick) {
    suppressClick = false
    return
  }
  emit('select', block, event.currentTarget as HTMLElement)
}

/** Scroll so `minute` sits near the top of the visible grid. */
function scrollToMinute(minute: number): void {
  if (!scroller.value) return
  scroller.value.scrollTop = Math.max(0, (minute / 60) * HOUR_PX - 12)
}

// Follow the first run of the shown days (no later than 08:00) while tasks and
// run history load, until the user scrolls the grid themselves.
let userScrolled = false
const firstStart = computed(() => Math.min(8 * 60, ...props.blocks.flat().map((block) => block.start)))
function markUserScroll(): void {
  userScrolled = true
}
onMounted(() => scrollToMinute(firstStart.value - 30))
watch(firstStart, async (minute) => {
  if (userScrolled) return
  await nextTick()
  scrollToMinute(minute - 30)
})

defineExpose({ scrollToMinute })
</script>

<template>
  <div
    ref="scroller"
    class="relative min-h-0 flex-1 overflow-auto"
    data-testid="schedule-grid"
    @wheel.passive="markUserScroll"
    @touchmove.passive="markUserScroll"
    @keydown="markUserScroll"
    @pointerdown="markUserScroll"
  >
    <div :class="days.length > 1 ? 'min-w-[46rem]' : ''">
      <div
        v-if="days.length > 1"
        class="sticky top-0 z-30 flex border-b border-border bg-card/95 backdrop-blur"
      >
        <div
          class="flex w-14 shrink-0 items-end justify-end pr-2 pb-1.5 text-[10px] text-muted-foreground"
          :title="`Times shown in your time zone (${zoneLabel})`"
        >
          {{ zoneLabel }}
        </div>
        <div class="grid flex-1" :style="columnsStyle">
          <div
            v-for="day in days"
            :key="day.getTime()"
            class="flex flex-col items-center gap-0.5 border-l border-border py-2"
            :data-testid="`schedule-day-header-${day.getUTCDate()}`"
          >
            <span
              class="text-[11px] font-medium tracking-wide uppercase"
              :class="isToday(day) ? 'text-primary' : 'text-muted-foreground'"
              >{{ weekdayFormat.format(day) }}</span
            >
            <span
              class="flex size-7 items-center justify-center rounded-full text-sm font-semibold tabular-nums"
              :class="isToday(day) ? 'bg-primary text-primary-foreground' : 'text-foreground'"
              >{{ day.getUTCDate() }}</span
            >
          </div>
        </div>
      </div>

      <div class="flex" :style="{ height: `${24 * HOUR_PX}px` }">
        <div class="relative w-14 shrink-0" aria-hidden="true">
          <span
            v-for="hour in HOURS.slice(1)"
            :key="hour"
            class="absolute right-2 -translate-y-1/2 text-[10px] text-muted-foreground tabular-nums"
            :style="{ top: px(hour * 60) }"
            >{{ formatMinutes(hour * 60) }}</span
          >
        </div>
        <div ref="columnsEl" class="grid flex-1" :style="columnsStyle">
          <div
            v-for="(day, dayIndex) in days"
            :key="day.getTime()"
            class="relative cursor-pointer border-l border-border select-none"
            :class="isWeekend(day) ? 'bg-muted/40' : ''"
            :style="hourLines"
            :data-testid="`schedule-day-${dayIndex}`"
            @pointermove="onColumnHover($event, dayIndex)"
            @pointerleave="hover = null"
            @click="onColumnClick($event, dayIndex)"
          >
            <div
              v-if="hover?.day === dayIndex && !drag"
              class="pointer-events-none absolute inset-x-1 flex items-start gap-0.5 rounded-md border border-dashed border-primary/50 bg-primary/5 px-1.5 py-1 text-[10px] font-medium text-primary"
              :style="{ top: px(hover.minute), height: px(SLOT_MINUTES) }"
              data-testid="schedule-hover-slot"
            >
              <Plus class="size-3" />{{ formatMinutes(hover.minute) }}
            </div>

            <button
              v-for="block in blocks[dayIndex]"
              :key="block.key"
              type="button"
              data-block
              class="absolute flex overflow-hidden rounded-md text-left text-[11px] leading-tight transition-opacity duration-150 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary"
              :class="[
                isSeries(block)
                  ? 'z-[5]'
                  : 'z-10 hover:z-20 hover:shadow-md focus-visible:z-20',
                focusTaskId && focusTaskId !== block.taskId ? 'opacity-25' : '',
                drag?.moved && drag.block.key === block.key ? 'opacity-40' : '',
                isMovable(block) ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer',
                isPaused(block) ? 'text-muted-foreground' : 'text-foreground',
              ]"
              :style="blockStyle(block)"
              :aria-label="blockLabel(block)"
              :data-testid="`schedule-block-${block.taskId}`"
              :data-kind="block.kind"
              @pointerdown="onBlockPointerDown($event, block)"
              @pointermove="onBlockPointerMove"
              @pointerup="onBlockPointerUp(block)"
              @pointercancel="drag = null"
              @click.stop="onBlockClick($event, block)"
            >
              <span
                v-if="isSeries(block)"
                class="flex h-fit min-w-0 flex-1 items-center gap-1 py-[3px] pr-1.5 pl-2.5"
                :title="seriesLabel(block)"
              >
                <span class="min-w-0 truncate"
                  ><span class="font-medium">{{ taskOf(block)?.name }}</span
                  ><span class="text-muted-foreground">
                    · {{ describeSeries(block).toLowerCase() }}</span
                  ></span
                >
                <component
                  :is="statusIcon(runStatus(block))!.icon"
                  v-if="statusIcon(runStatus(block))"
                  class="ml-auto size-3 shrink-0"
                  :class="statusIcon(runStatus(block))!.class"
                />
              </span>
              <span v-else class="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-[3px]">
                <span class="shrink-0 font-medium text-muted-foreground tabular-nums">{{
                  formatMinutes(block.start)
                }}</span>
                <span class="min-w-0 truncate font-medium">{{ taskOf(block)?.name }}</span>
                <component
                  :is="statusIcon(runStatus(block))!.icon"
                  v-if="statusIcon(runStatus(block))"
                  class="ml-auto size-3 shrink-0"
                  :class="statusIcon(runStatus(block))!.class"
                />
              </span>
              <span
                v-for="tick in seriesTicks(block)"
                :key="tick"
                class="pointer-events-none absolute left-0 h-px w-1.5 opacity-70"
                :style="{ top: px(tick), backgroundColor: colorOf(block) }"
              />
            </button>

            <div
              v-if="drag?.moved && drag.day === dayIndex"
              class="pointer-events-none absolute inset-x-0.5 z-40 flex items-start rounded-md px-1.5 py-[3px] text-[11px] leading-tight font-medium text-foreground shadow-lg ring-2 ring-primary"
              :style="ghostStyle"
              data-testid="schedule-drag-ghost"
            >
              <span class="mr-1 tabular-nums">{{ formatMinutes(drag.minute) }}</span>
              <span class="truncate">{{ taskOf(drag.block)?.name }}</span>
            </div>

            <div
              v-if="isToday(day)"
              class="pointer-events-none absolute inset-x-0 z-20 h-0.5 bg-primary"
              :style="{ top: px(nowInZone.minute) }"
              data-testid="schedule-now-line"
            >
              <span class="absolute -top-[3px] -left-1 size-2 rounded-full bg-primary" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
