/**
 * The "Schedule" section of the task editor: one segmented choice for how a
 * task repeats, the native controls of that mode, the time zone and a
 * preview of the next runs.
 */
import {
  CRON_PRESETS,
  FREQUENT_RUNS_PER_DAY,
  INTERVAL_OPTIONS,
  MAX_SCHEDULE_TIMES,
  describeScheduleWeekdays,
  maxRunsPerDay,
  nextScheduleTime,
  scheduleDraftToCron,
  scheduleDraftWithMode,
  upcomingScheduleRuns,
  type ScheduleDraft,
  type ScheduleMode,
} from "@opencuria/chat-core/scheduledTasks";
import type {
  NativeFormRow,
  NativeFormSection,
  NativeFormTone,
} from "../ui/NativeForm.types";
import { deviceTimeZone, timeZoneGroups } from "./scheduledTaskForm";
import { formatRunClock, relativeRunTime } from "./scheduledTaskPresentation";

const MODES: Array<{
  value: ScheduleMode;
  label: string;
  accessibilityLabel: string;
}> = [
  { value: "once", label: "Once", accessibilityLabel: "Once" },
  { value: "times", label: "At times", accessibilityLabel: "At set times" },
  { value: "interval", label: "Interval", accessibilityLabel: "Repeating" },
  { value: "cron", label: "Cron", accessibilityLabel: "Custom cron schedule" },
];

const hourLabel = (hour: number): string =>
  `${String(hour).padStart(2, "0")}:00`;

function localDay(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

/** "Then 14:30, Wed 09:00 and Wed 14:30" for the runs after the next one. */
function laterRuns(runs: Date[]): string {
  const labels = runs
    .slice(1)
    .map((date, index) =>
      localDay(date) === localDay(runs[index]!)
        ? formatRunClock(date)
        : `${new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(date)} ${formatRunClock(date)}`,
    );
  if (!labels.length) return "";
  const last = labels.pop()!;
  return `Then ${labels.length ? `${labels.join(", ")} and ${last}` : last}.`;
}

function capitalize(text: string): string {
  return text ? `${text[0]!.toUpperCase()}${text.slice(1)}` : text;
}

export interface ScheduleSectionOptions {
  schedule: ScheduleDraft;
  timezone: string;
  /** Saving keeps the task paused; it will not run. */
  paused: boolean;
  /** The saved task's next run, authoritative while its schedule is unchanged. */
  nextRunAt: string | null;
  /** First problem of the schedule from the form validation. */
  error?: string;
  onSchedule: (schedule: ScheduleDraft) => void;
  onTimezone: (timezone: string) => void;
  disabled?: boolean;
  now?: Date;
}

export function scheduleFormSection({
  schedule,
  timezone,
  paused,
  nextRunAt,
  error,
  onSchedule,
  onTimezone,
  disabled,
  now = new Date(),
}: ScheduleSectionOptions): NativeFormSection {
  const set = (next: Partial<ScheduleDraft>) =>
    onSchedule({ ...schedule, ...next });
  const weekdaysRow: NativeFormRow = {
    kind: "weekdays",
    id: "schedule-days",
    label: "Days",
    summary: schedule.weekdays.length
      ? describeScheduleWeekdays(schedule.weekdays)
      : "None",
    value: schedule.weekdays,
    onChange: (weekdays) => set({ weekdays }),
    disabled,
  };

  const modeRows: NativeFormRow[] =
    schedule.mode === "once"
      ? [
          {
            kind: "date",
            id: "schedule-date",
            label: "Date",
            mode: "date",
            value: schedule.date,
            onChange: (date) => set({ date }),
            disabled,
          },
          {
            kind: "date",
            id: "schedule-time",
            label: "Time",
            mode: "time",
            value: schedule.times[0] ?? "09:00",
            onChange: (time) => set({ times: [time] }),
            disabled,
          },
        ]
      : schedule.mode === "times"
        ? [
            weekdaysRow,
            ...schedule.times.map(
              (time, index): NativeFormRow => ({
                kind: "date",
                id: `schedule-time-${index}`,
                label: schedule.times.length > 1 ? `Time ${index + 1}` : "Time",
                mode: "time",
                value: time,
                onChange: (value) =>
                  set({
                    times: schedule.times.map((entry, position) =>
                      position === index ? value : entry,
                    ),
                  }),
                onRemove:
                  schedule.times.length > 1
                    ? () =>
                        set({
                          times: schedule.times.filter(
                            (_, position) => position !== index,
                          ),
                        })
                    : undefined,
                disabled,
              }),
            ),
            {
              kind: "button",
              id: "schedule-add-time",
              title: "Add time",
              symbol: "plus",
              disabled: disabled || schedule.times.length >= MAX_SCHEDULE_TIMES,
              onPress: () =>
                set({
                  times: [...schedule.times, nextScheduleTime(schedule.times)],
                }),
            },
          ]
        : schedule.mode === "interval"
          ? [
              weekdaysRow,
              {
                kind: "picker",
                id: "schedule-interval",
                label: "Every",
                value: schedule.interval,
                groups: [
                  {
                    options: INTERVAL_OPTIONS.map((option) => ({
                      value: option.value,
                      label: option.label,
                    })),
                  },
                ],
                onChange: (interval) => set({ interval }),
                disabled,
              },
              {
                kind: "toggle",
                id: "schedule-window",
                label: "Only between",
                value: schedule.window_enabled,
                onChange: (windowEnabled) =>
                  set({ window_enabled: windowEnabled }),
                disabled,
              },
              ...(schedule.window_enabled
                ? [
                    {
                      kind: "picker" as const,
                      id: "schedule-window-start",
                      label: "From",
                      value: String(schedule.window_start),
                      groups: [
                        {
                          options: Array.from({ length: 24 }, (_, hour) => ({
                            value: String(hour),
                            label: hourLabel(hour),
                          })),
                        },
                      ],
                      onChange: (value: string) =>
                        set({ window_start: Number(value) }),
                      disabled,
                    },
                    {
                      kind: "picker" as const,
                      id: "schedule-window-end",
                      label: "Until",
                      value: String(schedule.window_end),
                      groups: [
                        {
                          options: Array.from({ length: 24 }, (_, index) => ({
                            value: String(index + 1),
                            label: hourLabel(index + 1),
                          })),
                        },
                      ],
                      onChange: (value: string) =>
                        set({ window_end: Number(value) }),
                      disabled,
                    },
                  ]
                : []),
            ]
          : [
              {
                kind: "text",
                id: "schedule-cron",
                label: "Cron expression",
                placeholder: "*/30 9-17 * * 1-5",
                value: schedule.cron_expression,
                monospace: true,
                onChange: (cronExpression) =>
                  set({ cron_expression: cronExpression }),
                disabled,
              },
              {
                kind: "picker",
                id: "schedule-cron-preset",
                label: "Preset",
                value:
                  CRON_PRESETS.find(
                    (preset) =>
                      preset.expression === schedule.cron_expression.trim(),
                  )?.expression ?? "",
                groups: [
                  {
                    options: [
                      ...(CRON_PRESETS.some(
                        (preset) =>
                          preset.expression === schedule.cron_expression.trim(),
                      )
                        ? []
                        : [{ value: "", label: "Custom" }]),
                      ...CRON_PRESETS.map((preset) => ({
                        value: preset.expression,
                        label: preset.label,
                      })),
                    ],
                  },
                ],
                onChange: (expression) => {
                  if (expression) set({ cron_expression: expression });
                },
                disabled,
              },
            ];

  const runs = error
    ? []
    : upcomingScheduleRuns(schedule, timezone, { now, nextRunAt });
  const perDay =
    error || schedule.mode === "once"
      ? 0
      : maxRunsPerDay(scheduleDraftToCron(schedule), now);
  const nextValue: { text: string; tone: NativeFormTone } = error
    ? { text: "—", tone: "muted" }
    : paused
      ? { text: "Paused", tone: "muted" }
      : runs[0]
        ? { text: capitalize(relativeRunTime(runs[0], now)), tone: "accent" }
        : { text: "None", tone: "muted" };
  const zoneNote =
    timezone !== deviceTimeZone() ? `Times above are in ${timezone}.` : "";
  const footer = error
    ? { text: error, tone: "danger" as const }
    : perDay > FREQUENT_RUNS_PER_DAY
      ? {
          text: `Up to ${perDay} runs a day. Each run starts a new chat.`,
          tone: "warning" as const,
        }
      : {
          text: [
            schedule.mode === "cron"
              ? "Five fields: minute, hour, day, month, weekday (0 is Sunday). Separate several with “;”."
              : "",
            paused ? "" : laterRuns(runs),
            zoneNote,
          ]
            .filter(Boolean)
            .join(" "),
          tone: "muted" as const,
        };

  return {
    id: "schedule",
    title: "Schedule",
    rows: [
      {
        kind: "segmented",
        id: "schedule-mode",
        label: "Repeat",
        value: schedule.mode,
        options: MODES,
        onChange: (mode) =>
          onSchedule(
            scheduleDraftWithMode(
              schedule,
              mode as ScheduleMode,
              timezone,
              now,
            ),
          ),
        disabled,
      },
      ...modeRows,
      {
        kind: "picker",
        id: "schedule-timezone",
        label: "Time zone",
        value: timezone,
        groups: timeZoneGroups(timezone).map((group) => ({
          title: group.title,
          options: group.zones.map((zone) => ({ value: zone, label: zone })),
        })),
        onChange: onTimezone,
        disabled,
      },
      {
        kind: "value",
        id: "schedule-next-run",
        label: "Next run",
        value: nextValue.text,
        tone: nextValue.tone,
      },
    ],
    footer: footer.text || undefined,
    footerTone: footer.tone,
  };
}
