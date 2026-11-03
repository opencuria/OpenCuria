/**
 * Wall-clock values for native date and time pickers. Pickers work with
 * device-local `Date`s while schedules store zone-free "YYYY-MM-DD" and
 * "HH:MM" strings, so the fields map one to one in the device's zone.
 */

const pad = (value: number): string => String(value).padStart(2, "0");

/** Days of a week row, Monday first, with their spoken names. */
export const WEEKDAY_TOGGLES = [
  { value: 0, short: "M", name: "Monday" },
  { value: 1, short: "T", name: "Tuesday" },
  { value: 2, short: "W", name: "Wednesday" },
  { value: 3, short: "T", name: "Thursday" },
  { value: 4, short: "F", name: "Friday" },
  { value: 5, short: "S", name: "Saturday" },
  { value: 6, short: "S", name: "Sunday" },
] as const;

/** Toggle one day of a weekday selection, keeping it sorted. */
export function toggleWeekday(days: number[], day: number): number[] {
  return days.includes(day)
    ? days.filter((entry) => entry !== day)
    : [...days, day].sort((a, b) => a - b);
}

/**
 * Local `Date` showing `value`; dates sit at noon so that no DST change
 * moves them to another day.
 */
export function wallClockDate(mode: "date" | "time", value: string): Date {
  if (mode === "date") {
    const [year, month, day] = value.split("-").map(Number);
    return year && month && day
      ? new Date(year, month - 1, day, 12, 0)
      : new Date();
  }
  const [hour, minute] = value.split(":").map(Number);
  const date = new Date(2000, 0, 1, 0, 0);
  if (Number.isFinite(hour) && Number.isFinite(minute))
    date.setHours(hour!, minute!);
  return date;
}

/** The "YYYY-MM-DD" or "HH:MM" a picker's local `Date` shows. */
export function wallClockValue(mode: "date" | "time", date: Date): string {
  return mode === "date"
    ? `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    : `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Whether the device locale shows times on a 24-hour clock. */
export function uses24HourClock(): boolean {
  try {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric" })
      .format(new Date(2000, 0, 1, 13))
      .includes("13");
  } catch {
    return true;
  }
}

/** Locale label of a wall-clock value, e.g. "Nov 4, 2026" or "9:00 AM". */
export function formatWallClock(mode: "date" | "time", value: string): string {
  const date = wallClockDate(mode, value);
  try {
    return new Intl.DateTimeFormat(
      undefined,
      mode === "date"
        ? { month: "short", day: "numeric", year: "numeric" }
        : { hour: "numeric", minute: "2-digit" },
    ).format(date);
  } catch {
    return value;
  }
}
