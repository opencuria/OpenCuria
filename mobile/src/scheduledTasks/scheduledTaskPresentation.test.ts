import { describe, expect, it } from "vitest";
import type {
  ScheduledTask,
  ScheduledTaskRun,
} from "@opencuria/chat-core/scheduledTasks";
import type { OpenCuriaTheme } from "../ui/theme";
import {
  activeTaskCount,
  agendaDayTitle,
  clockParts,
  formatRunClock,
  latestSettledRun,
  relativeRunTime,
  runPresentation,
  scheduleAgenda,
  taskState,
  taskStatusLine,
  taskSubtitle,
} from "./scheduledTaskPresentation";

/** Tuesday, 3 November 2026, 11:00 on the test machine's clock. */
const NOW = new Date(2026, 10, 3, 11, 0);
const at = (day: number, hour: number, minute = 0) =>
  new Date(2026, 10, day, hour, minute).toISOString();

function task(overrides: Partial<ScheduledTask>): ScheduledTask {
  return {
    id: "task",
    name: "Task",
    workspace_id: "ws-1",
    prompt: "Do it",
    mode: "build",
    harness_id: "native",
    model: "",
    reasoning_effort: "",
    skill_ids: [],
    recurrence: "daily",
    weekdays: [],
    cron_expression: "",
    run_date: null,
    local_time: "09:00",
    timezone_name: "UTC",
    enabled: true,
    is_running: false,
    next_run_at: at(4, 9),
    created_at: at(1, 9),
    updated_at: at(1, 9),
    ...overrides,
  };
}

function run(overrides: Partial<ScheduledTaskRun>): ScheduledTaskRun {
  return {
    id: "run",
    scheduled_for: at(2, 9),
    trigger: "scheduled",
    status: "succeeded",
    session_id: "session",
    started_at: at(2, 9),
    finished_at: at(2, 9, 6),
    error: "",
    reason: "",
    assistant_finish: "",
    assistant_error: "",
    ...overrides,
  };
}

describe("scheduled task agenda", () => {
  it("orders running tasks, each day's runs by time, then paused and ended tasks", () => {
    const tasks = [
      task({ id: "later", name: "Later", next_run_at: at(9, 9) }),
      task({ id: "evening", name: "Evening", next_run_at: at(3, 17) }),
      task({ id: "paused", name: "Paused", enabled: false }),
      task({ id: "running", name: "Running", is_running: true }),
      task({ id: "overdue", name: "Overdue", next_run_at: at(3, 10, 59) }),
      task({ id: "tomorrow", name: "Tomorrow", next_run_at: at(4, 9) }),
      task({
        id: "ended",
        name: "Ended",
        recurrence: "once",
        run_date: "2026-11-01",
        enabled: false,
      }),
    ];
    const groups = scheduleAgenda(tasks, { now: NOW });
    expect(
      groups.map((group) => [group.title, group.tasks.map((item) => item.id)]),
    ).toEqual([
      ["Running now", ["running"]],
      ["Today", ["overdue", "evening"]],
      ["Tomorrow", ["tomorrow"]],
      [agendaDayTitle(new Date(at(9, 9)), NOW), ["later"]],
      ["Paused", ["paused"]],
    ]);
    expect(
      scheduleAgenda(tasks, { now: NOW, showEnded: true }).at(-1),
    ).toMatchObject({ title: "Ended", tasks: [{ id: "ended" }] });
    expect(activeTaskCount(tasks, NOW)).toBe(6);
  });

  it("names days relative to today and runs by their clock time", () => {
    expect(agendaDayTitle(new Date(at(3, 23)), NOW)).toBe("Today");
    expect(agendaDayTitle(new Date(at(4, 1)), NOW)).toBe("Tomorrow");
    expect(agendaDayTitle(new Date(at(6, 9)), NOW)).toMatch(/Friday/);
    const clock = formatRunClock(new Date(at(4, 9)));
    expect(relativeRunTime(new Date(at(3, 14)), NOW)).toBe(
      `today at ${formatRunClock(new Date(at(3, 14)))}`,
    );
    expect(relativeRunTime(new Date(at(4, 9)), NOW)).toBe(
      `tomorrow at ${clock}`,
    );
    expect(relativeRunTime(new Date(at(6, 9)), NOW)).toMatch(/^Friday at /);
    expect(relativeRunTime(new Date(at(20, 9)), NOW)).toMatch(/Nov 20/);
  });

  it("names the schedule's zone only when it differs from the device's", () => {
    const berlin = task({ timezone_name: "Europe/Berlin" });
    expect(taskSubtitle(berlin, "docs", "Europe/Berlin")).toBe(
      "Daily · 09:00 · docs",
    );
    expect(
      taskSubtitle(
        task({ timezone_name: "America/New_York" }),
        undefined,
        "Europe/Berlin",
      ),
    ).toBe("Daily · 09:00 New York");
  });

  it("splits a 12-hour clock into the time and its day period", () => {
    const date = new Date(at(3, 14, 30));
    const { time, period } = clockParts(date);
    expect([time, period].filter(Boolean).join(" ")).toBe(
      formatRunClock(date).replace(/[\u00a0\u202f]/g, " "),
    );
    expect(time).toMatch(/\d$/);
  });

  it("puts run state before the enabled flag and an ended one-time run", () => {
    expect(taskState(task({ is_running: true, enabled: false }), NOW)).toBe(
      "running",
    );
    expect(
      taskState(
        task({
          recurrence: "once",
          run_date: "2026-11-03",
          local_time: "10:00",
        }),
        NOW,
      ),
    ).toBe("ended");
    expect(taskState(task({ enabled: false }), NOW)).toBe("paused");
    expect(taskStatusLine(task({ next_run_at: at(4, 9) }), NOW)).toEqual({
      text: `Next run tomorrow at ${formatRunClock(new Date(at(4, 9)))}`,
      tone: "muted",
    });
    expect(taskStatusLine(task({ enabled: false }), NOW).text).toBe("Paused");
  });
});

describe("scheduled task runs", () => {
  const theme = { scheme: "light", primary: "#007AFF" } as OpenCuriaTheme;

  it("shows why a run failed or was skipped and marks manual runs", () => {
    const failed = runPresentation(
      run({ status: "error", error: "Workspace did not start." }),
      theme,
      NOW,
    );
    expect(failed).toMatchObject({
      title: "Failed",
      subtitleTone: "danger",
      symbol: "xmark.circle.fill",
      busy: false,
    });
    expect(failed.subtitle).toMatch(/· Workspace did not start\.$/);
    expect(
      runPresentation(
        run({ status: "skipped", reason: "workspace_busy", session_id: null }),
        theme,
        NOW,
      ).subtitle,
    ).toMatch(/· workspace busy$/);
    expect(
      runPresentation(run({ trigger: "manual" }), theme, NOW).subtitle,
    ).toMatch(/· Run now$/);
    const today = runPresentation(
      run({ status: "running", started_at: at(3, 10) }),
      theme,
      NOW,
    );
    expect(today).toMatchObject({ title: "Running", busy: true });
    expect(today.subtitle).toBe(
      `Today, ${formatRunClock(new Date(at(3, 10)))}`,
    );
  });

  it("finds the newest settled run", () => {
    const runs = [
      run({ id: "active", status: "running" }),
      run({ id: "failed", status: "error" }),
      run({ id: "older" }),
    ];
    expect(latestSettledRun(runs)?.id).toBe("failed");
    expect(latestSettledRun([run({ status: "claimed" })])).toBeUndefined();
  });
});
