import { describe, expect, it } from "vitest";
import {
  defaultScheduleDraft,
  describeScheduleWeekdays,
  isScheduleRunActive,
  nextScheduleTime,
  onceScheduleError,
  scheduleDraftWithMode,
  scheduleRunStatusLabel,
  scheduleRunWorkspaceId,
  upcomingScheduleRuns,
  type ScheduleDraft,
} from "../src/scheduledTasks";

const ZONE = "Europe/Berlin";
/** Tuesday, 3 November 2026, 11:00 in Berlin. */
const NOW = new Date("2026-11-03T10:00:00Z");

function draft(overrides: Partial<ScheduleDraft>): ScheduleDraft {
  return { ...defaultScheduleDraft(ZONE), ...overrides };
}

const iso = (dates: Date[]): string[] =>
  dates.map((date) => date.toISOString());

describe("schedule editor transitions", () => {
  it("carries the current cadence into the cron editor and keeps an existing expression", () => {
    const times = draft({
      mode: "times",
      weekdays: [0, 1, 2, 3, 4],
      times: ["14:30", "09:00"],
    });
    expect(
      scheduleDraftWithMode(times, "cron", ZONE, NOW).cron_expression,
    ).toBe("0 9 * * 1-5; 30 14 * * 1-5");
    const edited = { ...times, cron_expression: "*/5 * * * *" };
    expect(
      scheduleDraftWithMode(edited, "cron", ZONE, NOW).cron_expression,
    ).toBe("*/5 * * * *");
    expect(scheduleDraftWithMode(times, "times", ZONE, NOW)).toBe(times);
  });

  it("moves a one-time run that has passed to the next occurrence of its time", () => {
    const past = draft({ mode: "times", date: "2026-01-01", times: ["09:00"] });
    expect(scheduleDraftWithMode(past, "once", ZONE, NOW)).toMatchObject({
      mode: "once",
      date: "2026-11-04",
    });
    const ahead = draft({ mode: "cron", date: "2026-12-24", times: ["18:00"] });
    expect(scheduleDraftWithMode(ahead, "once", ZONE, NOW).date).toBe(
      "2026-12-24",
    );
  });

  it("adds the next time an hour after the latest one, else at noon", () => {
    expect(nextScheduleTime([])).toBe("09:00");
    expect(nextScheduleTime(["10:15", "09:00"])).toBe("11:15");
    expect(nextScheduleTime(["09:30", "23:15"])).toBe("12:00");
  });
});

describe("schedule timing", () => {
  it("rejects one-time runs inside a DST gap or, while enabled, in the past", () => {
    const gap = draft({ date: "2027-03-28", times: ["02:30"] });
    expect(onceScheduleError(gap, ZONE, true, NOW)).toBe(
      "This time does not exist on that date.",
    );
    const past = draft({ date: "2026-11-03", times: ["10:59"] });
    expect(onceScheduleError(past, ZONE, true, NOW)).toBe(
      "Choose a future date and time.",
    );
    // A paused or ended task may keep its past run.
    expect(onceScheduleError(past, ZONE, false, NOW)).toBe("");
    expect(
      onceScheduleError({ ...past, times: ["11:01"] }, ZONE, true, NOW),
    ).toBe("");
    expect(onceScheduleError({ ...past, mode: "times" }, ZONE, true, NOW)).toBe(
      "",
    );
  });

  it("previews upcoming runs and lets the saved next run lead", () => {
    const once = draft({ date: "2026-11-04", times: ["09:00"] });
    expect(iso(upcomingScheduleRuns(once, ZONE, { now: NOW }))).toEqual([
      "2026-11-04T08:00:00.000Z",
    ]);
    expect(
      upcomingScheduleRuns({ ...once, date: "2026-11-02" }, ZONE, { now: NOW }),
    ).toEqual([]);

    const weekdays = draft({
      mode: "times",
      weekdays: [0, 1, 2, 3, 4],
      times: ["09:00"],
    });
    expect(
      iso(upcomingScheduleRuns(weekdays, ZONE, { now: NOW, count: 3 })),
    ).toEqual([
      "2026-11-04T08:00:00.000Z",
      "2026-11-05T08:00:00.000Z",
      "2026-11-06T08:00:00.000Z",
    ]);
    expect(
      iso(
        upcomingScheduleRuns(weekdays, ZONE, {
          now: NOW,
          count: 2,
          nextRunAt: "2026-11-05T08:00:00Z",
        }),
      ),
    ).toEqual(["2026-11-05T08:00:00.000Z", "2026-11-06T08:00:00.000Z"]);
    expect(
      upcomingScheduleRuns(weekdays, "Mars/Olympus", { now: NOW }),
    ).toEqual([]);
    expect(
      upcomingScheduleRuns({ ...weekdays, weekdays: [] }, ZONE, { now: NOW }),
    ).toEqual([]);
  });
});

describe("schedule labels", () => {
  it("names run states and weekday sets", () => {
    expect(scheduleRunStatusLabel("error")).toBe("Failed");
    expect(scheduleRunStatusLabel("claimed")).toBe("Queued");
    expect(scheduleRunStatusLabel("future_state")).toBe("future_state");
    expect(isScheduleRunActive({ status: "claimed" })).toBe(true);
    expect(isScheduleRunActive({ status: "succeeded" })).toBe(false);
    expect(describeScheduleWeekdays([0, 1, 2, 3, 4])).toBe("Weekdays");
    expect(describeScheduleWeekdays([5, 6])).toBe("Weekends");
    expect(describeScheduleWeekdays([0, 1, 2, 3, 4, 5, 6])).toBe("Daily");
    expect(describeScheduleWeekdays([2, 0])).toBe("Mon, Wed");
  });

  it("opens a run's chat in the workspace it ran in", () => {
    const task = { workspace_id: "ws-now" };
    expect(
      scheduleRunWorkspaceId(
        { configuration_snapshot: { workspace_id: "ws-then" } },
        task,
      ),
    ).toBe("ws-then");
    expect(scheduleRunWorkspaceId({ configuration_snapshot: {} }, task)).toBe(
      "ws-now",
    );
    expect(scheduleRunWorkspaceId({}, task)).toBe("ws-now");
  });
});
