/**
 * "Send later" choices shared by the web and native composers.
 *
 * Times are the user's local wall clock; the server stores absolute instants.
 * The presets follow mail and chat apps: a short delay, the next morning and
 * the start of the next work week. Labels use English words and the device's
 * clock and calendar conventions.
 */

import type { HarnessId, HarnessSessionMode } from "./harness";
import type { ScheduledTaskInput } from "./scheduledTasks";

/** Local hour of the morning presets. */
export const SEND_LATER_MORNING_HOUR = 9;

/** How far ahead a message may be scheduled (the server allows a little more). */
export const SEND_LATER_MAX_DAYS = 365;

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

/** One preset of the "Send later" menu. */
export interface SendLaterOption {
  id: "in-1-hour" | "tomorrow" | "monday";
  /** What the preset means, e.g. "Tomorrow". */
  label: string;
  /** When it sends, e.g. "09:00" or "9:00 AM". */
  detail: string;
  at: Date;
}

/** Presets for a message scheduled at `now` (in the device's time zone). */
export function sendLaterOptions(now: Date = new Date()): SendLaterOption[] {
  const inOneHour = new Date(now.getTime() + HOUR_MS);
  inOneHour.setSeconds(0, 0);
  const tomorrow = morningAfter(now, 1);
  const options: SendLaterOption[] = [
    {
      id: "in-1-hour",
      label: "In 1 hour",
      detail: clock(inOneHour),
      at: inOneHour,
    },
    {
      id: "tomorrow",
      label: "Tomorrow",
      detail: clock(tomorrow),
      at: tomorrow,
    },
  ];
  // Days until the next Monday (1–7); a Monday tomorrow is "Tomorrow".
  const toMonday = (8 - now.getDay()) % 7 || 7;
  if (toMonday > 1) {
    const monday = morningAfter(now, toMonday);
    options.push({
      id: "monday",
      label: toMonday === 7 ? "Next Monday" : "Monday",
      detail: clock(monday),
      at: monday,
    });
  }
  return options;
}

/**
 * When a scheduled message sends, relative to `now`: "Today, 14:30",
 * "Tomorrow, 9:00 AM", "Friday, 09:00" or "Mar 3, 09:00".
 */
export function formatSendTime(
  at: Date | string,
  now: Date = new Date(),
): string {
  const date = typeof at === "string" ? new Date(at) : at;
  if (Number.isNaN(date.getTime())) return "";
  const time = clock(date);
  const days = calendarDays(now, date);
  if (days === 0) return `Today, ${time}`;
  if (days === 1) return `Tomorrow, ${time}`;
  if (days === -1) return `Yesterday, ${time}`;
  if (days > 1 && days < 7)
    return `${format(date, { weekday: "long" })}, ${time}`;
  const day = format(date, {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  return `${day}, ${time}`;
}

/** Why `at` cannot be chosen, or `null` when it can. */
export function sendLaterError(
  at: Date | null,
  now: Date = new Date(),
): string | null {
  if (!at || Number.isNaN(at.getTime())) return "Choose a date and time.";
  if (at.getTime() <= now.getTime()) return "Choose a time in the future.";
  if (at.getTime() > now.getTime() + SEND_LATER_MAX_DAYS * DAY_MS)
    return "Choose a time within a year.";
  return null;
}

/** Initial value of a custom time picker: a full hour at least 15 min away. */
export function sendLaterDefault(now: Date = new Date()): Date {
  const at = new Date(now.getTime() + 15 * 60 * 1000);
  at.setMinutes(0, 0, 0);
  at.setHours(at.getHours() + 1);
  return at;
}

/** Local `YYYY-MM-DD` and `HH:MM` fields of `at`, e.g. for date/time inputs. */
export function sendLaterFields(at: Date): { date: string; time: string } {
  const pad = (value: number) => String(value).padStart(2, "0");
  return {
    date: `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`,
    time: `${pad(at.getHours())}:${pad(at.getMinutes())}`,
  };
}

/** The local instant of `YYYY-MM-DD` and `HH:MM` fields, or `null`. */
export function sendLaterFromFields(date: string, time: string): Date | null {
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const hour = /^(\d{2}):(\d{2})$/.exec(time);
  if (!day || !hour) return null;
  const [year, month, dayOfMonth] = day.slice(1).map(Number) as [
    number,
    number,
    number,
  ];
  const [hours, minutes] = hour.slice(1).map(Number) as [number, number];
  if (hours > 23 || minutes > 59) return null;
  const at = new Date(year, month - 1, dayOfMonth, hours, minutes);
  // Reject overflow such as February 30.
  return at.getMonth() === month - 1 && at.getDate() === dayOfMonth ? at : null;
}

/** Longest name of a scheduled task created from a prompt. */
const TASK_NAME_CHARS = 60;

/**
 * A new chat scheduled with "Send later" is a one-time scheduled task: it
 * starts the chat with `prompt` and the composer's run options at `at`.
 */
export function sendLaterTaskInput(input: {
  workspaceId: string;
  prompt: string;
  at: Date;
  mode: HarnessSessionMode;
  harnessId: HarnessId;
  model: string;
  reasoningEffort: string;
  skillIds: string[];
  timeZone?: string;
}): ScheduledTaskInput {
  const firstLine =
    input.prompt
      .split("\n")
      .map((line) => line.trim())
      .find(Boolean) ?? "Scheduled chat";
  const name =
    firstLine.length > TASK_NAME_CHARS
      ? `${firstLine.slice(0, TASK_NAME_CHARS - 1).trimEnd()}…`
      : firstLine;
  const { date, time } = sendLaterFields(input.at);
  return {
    name,
    workspace_id: input.workspaceId,
    prompt: input.prompt,
    mode: input.mode,
    harness_id: input.harnessId,
    model: input.model,
    reasoning_effort: input.reasoningEffort,
    skill_ids: [...input.skillIds],
    recurrence: "once",
    weekdays: [],
    run_date: date,
    local_time: time,
    timezone_name:
      input.timeZone ||
      Intl.DateTimeFormat().resolvedOptions().timeZone ||
      "UTC",
    enabled: true,
  };
}

function morningAfter(now: Date, days: number): Date {
  return new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() + days,
    SEND_LATER_MORNING_HOUR,
  );
}

/** Whole calendar days from `from` to `to` in the device's zone. */
function calendarDays(from: Date, to: Date): number {
  const start = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  const end = new Date(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((end.getTime() - start.getTime()) / DAY_MS);
}

function clock(date: Date): string {
  return format(date, { hour: "numeric", minute: "2-digit" });
}

function format(date: Date, options: Intl.DateTimeFormatOptions): string {
  try {
    return new Intl.DateTimeFormat(undefined, options).format(date);
  } catch {
    return date.toISOString();
  }
}
