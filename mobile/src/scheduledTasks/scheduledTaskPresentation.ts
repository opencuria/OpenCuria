/**
 * How scheduled tasks read on a phone: an agenda grouped by the day of each
 * task's next run, state tiles, and run outcomes. Times are shown in the
 * device's zone; schedules keep their own zone for the rules.
 */
import {
  describeSchedule,
  isScheduleEnded,
  isScheduleRunActive,
  scheduleRunStatusLabel,
  type ScheduledTask,
  type ScheduledTaskRun,
} from "@opencuria/chat-core/scheduledTasks";
import type { NativeSymbol } from "../ui/controls.types";
import type { NativeFormTone } from "../ui/NativeForm.types";
import type { OpenCuriaTheme } from "../ui/theme";
import { tileColor } from "../ui/tileColors";

const DAY_MS = 86_400_000;

export type TaskState = "running" | "scheduled" | "paused" | "ended";

/** Run state first, then a passed one-time run, then the enabled flag. */
export function taskState(task: ScheduledTask, now = new Date()): TaskState {
  if (task.is_running) return "running";
  if (isScheduleEnded(task, now)) return "ended";
  return task.enabled ? "scheduled" : "paused";
}

function startOfDay(date: Date): number {
  return new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  ).getTime();
}

/** Whole calendar days from `now` to `date` in the device's zone. */
function dayOffset(date: Date, now: Date): number {
  return Math.round((startOfDay(date) - startOfDay(now)) / DAY_MS);
}

function format(date: Date, options: Intl.DateTimeFormatOptions): string {
  try {
    return new Intl.DateTimeFormat(undefined, options).format(date);
  } catch {
    return date.toISOString();
  }
}

/** Clock time of a run, e.g. "09:00" or "9:00 AM". */
export function formatRunClock(date: Date): string {
  return format(date, { hour: "numeric", minute: "2-digit" });
}

/**
 * Clock time split for an agenda column: "8:00" with its day period "AM"
 * in 12-hour locales, the whole time otherwise.
 */
export function clockParts(date: Date): { time: string; period: string } {
  const text = formatRunClock(date);
  const match = /^(.*\d)[\s\u00a0\u202f]+([^\d\s]+)$/u.exec(text);
  return match
    ? { time: match[1]!, period: match[2]! }
    : { time: text, period: "" };
}

/** "Today", "Tomorrow" or a date such as "Thursday, Nov 6". */
export function agendaDayTitle(date: Date, now = new Date()): string {
  const offset = dayOffset(date, now);
  if (offset <= 0) return "Today";
  if (offset === 1) return "Tomorrow";
  return format(date, {
    weekday: "long",
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

/** "today at 09:00", "Thursday at 09:00" or "Nov 12 at 09:00". */
export function relativeRunTime(date: Date, now = new Date()): string {
  const offset = dayOffset(date, now);
  const day =
    offset <= 0
      ? "today"
      : offset === 1
        ? "tomorrow"
        : offset < 7
          ? format(date, { weekday: "long" })
          : format(date, {
              month: "short",
              day: "numeric",
              ...(date.getFullYear() !== now.getFullYear()
                ? { year: "numeric" }
                : {}),
            });
  return `${day} at ${formatRunClock(date)}`;
}

function nextRun(task: ScheduledTask): Date | null {
  const at = new Date(task.next_run_at);
  return Number.isNaN(at.getTime()) ? null : at;
}

export interface AgendaGroup {
  id: string;
  title: string;
  tasks: ScheduledTask[];
}

/**
 * Running tasks first, then one group per day of the next run, then paused
 * and, on request, ended tasks. An overdue run counts as today.
 */
export function scheduleAgenda(
  tasks: ScheduledTask[],
  { now = new Date(), showEnded = false }: { now?: Date; showEnded?: boolean },
): AgendaGroup[] {
  const running: ScheduledTask[] = [];
  const paused: ScheduledTask[] = [];
  const ended: ScheduledTask[] = [];
  const days = new Map<number, { date: Date; tasks: ScheduledTask[] }>();
  for (const task of tasks) {
    const state = taskState(task, now);
    if (state === "running") running.push(task);
    else if (state === "paused") paused.push(task);
    else if (state === "ended") ended.push(task);
    else {
      const at = nextRun(task) ?? now;
      const date = at < now ? now : at;
      const key = startOfDay(date);
      const day = days.get(key) ?? { date, tasks: [] };
      day.tasks.push(task);
      days.set(key, day);
    }
  }
  const byNextRun = (a: ScheduledTask, b: ScheduledTask) =>
    a.next_run_at.localeCompare(b.next_run_at);
  const byName = (a: ScheduledTask, b: ScheduledTask) =>
    a.name.localeCompare(b.name);
  return [
    ...(running.length
      ? [
          {
            id: "running",
            title: "Running now",
            tasks: running.sort(byName),
          },
        ]
      : []),
    ...[...days]
      .sort(([a], [b]) => a - b)
      .map(([key, day]) => ({
        id: `day-${key}`,
        title: agendaDayTitle(day.date, now),
        tasks: day.tasks.sort(byNextRun),
      })),
    ...(paused.length
      ? [{ id: "paused", title: "Paused", tasks: paused.sort(byName) }]
      : []),
    ...(showEnded && ended.length
      ? [
          {
            id: "ended",
            title: "Ended",
            tasks: ended.sort((a, b) =>
              b.updated_at.localeCompare(a.updated_at),
            ),
          },
        ]
      : []),
  ];
}

/** Visible in the sidebar count: everything except ended one-time tasks. */
export function activeTaskCount(
  tasks: ScheduledTask[],
  now = new Date(),
): number {
  return tasks.filter((task) => taskState(task, now) !== "ended").length;
}

/** Leading tile of a row or header; scheduled rows lead with their time. */
export function taskTile(
  state: TaskState,
  theme: OpenCuriaTheme,
): { symbol: NativeSymbol; tint: string } {
  switch (state) {
    case "running":
      return { symbol: "play.fill", tint: theme.primary };
    case "paused":
      return { symbol: "pause.fill", tint: tileColor("gray", theme) };
    case "ended":
      return { symbol: "checkmark", tint: tileColor("gray", theme) };
    default:
      return {
        symbol: "calendar.badge.clock",
        tint: tileColor("indigo", theme),
      };
  }
}

/** "Berlin" for "Europe/Berlin", "New York" for "America/New_York". */
export function zoneCity(zone: string): string {
  return (zone.split("/").pop() ?? zone).replaceAll("_", " ");
}

/**
 * Cadence and workspace under a task's name. The cadence names its zone
 * when it differs from the device's, whose times lead the agenda.
 */
export function taskSubtitle(
  task: ScheduledTask,
  workspaceName: string | undefined,
  deviceZone: string,
): string {
  const zone =
    task.timezone_name === deviceZone ? "" : ` ${zoneCity(task.timezone_name)}`;
  return [`${describeSchedule(task)}${zone}`, workspaceName]
    .filter(Boolean)
    .join(" · ");
}

/** The detail header's status line. */
export function taskStatusLine(
  task: ScheduledTask,
  now = new Date(),
): { text: string; tone: NativeFormTone } {
  const state = taskState(task, now);
  if (state === "running") return { text: "Running now", tone: "accent" };
  if (state === "ended") return { text: "Ended", tone: "muted" };
  if (state === "paused") return { text: "Paused", tone: "muted" };
  const at = nextRun(task);
  return at
    ? { text: `Next run ${relativeRunTime(at, now)}`, tone: "muted" }
    : { text: "Scheduled", tone: "muted" };
}

export interface RunPresentation {
  title: string;
  subtitle: string;
  subtitleTone: NativeFormTone;
  symbol: NativeSymbol;
  tint: string;
  busy: boolean;
}

/** Outcome tile, label and time (or the reason it failed) of one run. */
export function runPresentation(
  run: ScheduledTaskRun,
  theme: OpenCuriaTheme,
  now = new Date(),
): RunPresentation {
  const at = new Date(run.started_at ?? run.scheduled_for);
  const when = Number.isNaN(at.getTime())
    ? ""
    : dayOffset(at, now) === 0
      ? `Today, ${formatRunClock(at)}`
      : format(at, {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
  const problem = run.assistant_error || run.error;
  const reason = run.reason ? run.reason.replaceAll("_", " ") : "";
  const detail =
    run.status === "error" || run.status === "interrupted"
      ? problem
      : run.status === "skipped"
        ? reason || problem
        : "";
  const manual = run.trigger === "manual" ? "Run now" : "";
  const tile = runTile(run, theme);
  return {
    title: scheduleRunStatusLabel(run.status),
    subtitle: [when, detail || manual].filter(Boolean).join(" · "),
    subtitleTone: run.status === "error" ? "danger" : "muted",
    ...tile,
    busy: isScheduleRunActive(run),
  };
}

function runTile(
  run: ScheduledTaskRun,
  theme: OpenCuriaTheme,
): { symbol: NativeSymbol; tint: string } {
  switch (run.status) {
    case "succeeded":
      return {
        symbol: "checkmark.circle.fill",
        tint: tileColor("green", theme),
      };
    case "error":
      return { symbol: "xmark.circle.fill", tint: tileColor("red", theme) };
    case "interrupted":
      return {
        symbol: "exclamationmark.triangle.fill",
        tint: tileColor("orange", theme),
      };
    case "skipped":
      return { symbol: "minus.circle.fill", tint: tileColor("gray", theme) };
    default:
      return { symbol: "clock", tint: theme.primary };
  }
}

/** The newest run that has finished, for "Last run failed" and the latest chat. */
export function latestSettledRun(
  runs: ScheduledTaskRun[],
): ScheduledTaskRun | undefined {
  return runs.find((run) => !isScheduleRunActive(run));
}
