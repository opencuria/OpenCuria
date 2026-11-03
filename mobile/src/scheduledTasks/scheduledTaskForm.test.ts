import { describe, expect, it } from "vitest";
import type { ScheduledTask } from "@opencuria/chat-core/scheduledTasks";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";
import type { NativeFormRow } from "../ui/NativeForm.types";
import {
  agentSummary,
  blankTaskForm,
  createTaskPayload,
  taskFormEnabled,
  taskFormErrors,
  taskFormFromTask,
  taskFormPatch,
  timeZoneGroups,
  withAgent,
  withModel,
  type AgentCatalog,
} from "./scheduledTaskForm";
import { scheduleFormSection } from "./scheduleFormSection";

const ZONE = "Europe/Berlin";
/** Tuesday, 3 November 2026, 11:00 in Berlin. */
const NOW = new Date("2026-11-03T10:00:00Z");

const model = (id: string, efforts: string[], name = id): ProviderModel => ({
  id,
  name,
  reasoning_efforts: efforts,
  default_effort: efforts[0] ?? "",
  supports_tools: true,
  context_length: 0,
  max_output_tokens: 0,
});

const catalog: AgentCatalog = {
  engines: [
    {
      id: "native",
      name: "OpenCuria",
      modes: ["build", "plan"],
      connected: true,
      default_model: null,
      default_effort: null,
    },
    {
      id: "codex",
      name: "Codex",
      modes: ["build"],
      connected: true,
      default_model: "openai/gpt-6",
      default_effort: "high",
    },
  ],
  models: [model("sonnet", ["low", "high"], "Sonnet"), model("haiku", [])],
  engineModels: { codex: [model("openai/gpt-6", ["medium", "high"], "GPT-6")] },
  agentConfigs: [
    { agent: "build", model: "sonnet", effort: "high" },
    { agent: "plan", model: "haiku", effort: "" },
  ],
  recentModelIds: [],
  error: null,
};

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "task-1",
    name: "Dependency review",
    workspace_id: "ws-1",
    prompt: "Check dependencies",
    mode: "build",
    harness_id: "native",
    model: "sonnet",
    reasoning_effort: "high",
    skill_ids: [],
    recurrence: "weekly",
    weekdays: [0, 1, 2, 3, 4],
    cron_expression: "",
    run_date: null,
    local_time: "14:30",
    timezone_name: ZONE,
    enabled: true,
    is_running: false,
    next_run_at: "2026-11-03T13:30:00Z",
    created_at: "2026-10-01T09:00:00Z",
    updated_at: "2026-10-01T09:00:00Z",
    ...overrides,
  };
}

describe("scheduled task form", () => {
  it("starts new tasks once at the next 09:00 with the agent's defaults or the chat's choice", () => {
    const blank = blankTaskForm(catalog, { workspaceId: "ws-1" }, ZONE);
    expect(blank).toMatchObject({
      workspaceId: "ws-1",
      harnessId: "native",
      mode: "build",
      model: "sonnet",
      effort: "high",
      timezone: ZONE,
    });
    expect(blank.schedule).toMatchObject({ mode: "once", times: ["09:00"] });
    expect(
      blankTaskForm(
        catalog,
        {
          prompt: "Ship it",
          harnessId: "codex",
          model: "openai/gpt-6",
          effort: "medium",
        },
        ZONE,
      ),
    ).toMatchObject({
      prompt: "Ship it",
      model: "openai/gpt-6",
      effort: "medium",
    });
    expect(
      createTaskPayload({ ...blank, name: "  Nightly  ", prompt: " Test " }),
    ).toMatchObject({
      name: "Nightly",
      prompt: "Test",
      enabled: true,
      recurrence: "once",
    });
  });

  it("sends only changed fields and the whole cadence when the schedule changes", () => {
    const saved = task();
    const form = taskFormFromTask(saved);
    expect(taskFormPatch(saved, form, NOW)).toEqual({});
    expect(taskFormPatch(saved, { ...form, name: "Deps" }, NOW)).toEqual({
      name: "Deps",
    });
    expect(
      taskFormPatch(
        saved,
        { ...form, schedule: { ...form.schedule, times: ["15:00"] } },
        NOW,
      ),
    ).toEqual({
      recurrence: "weekly",
      weekdays: [0, 1, 2, 3, 4],
      local_time: "15:00",
      cron_expression: "",
      run_date: null,
      timezone_name: ZONE,
    });
  });

  it("re-arms an ended one-time task only when its run moves, and keeps a paused task paused", () => {
    const ended = task({
      recurrence: "once",
      weekdays: [],
      run_date: "2026-11-01",
      local_time: "07:30",
      enabled: false,
    });
    const form = taskFormFromTask(ended);
    expect(taskFormEnabled(ended, { ...form, name: "Renamed" }, NOW)).toBe(
      false,
    );
    expect(taskFormErrors(ended, { ...form, name: "Renamed" }, NOW)).toEqual(
      {},
    );
    const moved = {
      ...form,
      schedule: { ...form.schedule, date: "2026-11-10" },
    };
    expect(taskFormPatch(ended, moved, NOW)).toMatchObject({
      enabled: true,
      run_date: "2026-11-10",
    });
    const stillPast = {
      ...form,
      schedule: { ...form.schedule, date: "2026-11-02" },
    };
    expect(taskFormErrors(ended, stillPast, NOW).schedule).toBe(
      "Choose a future date and time.",
    );
    const paused = task({ enabled: false });
    expect(
      taskFormPatch(
        paused,
        { ...taskFormFromTask(paused), prompt: "New" },
        NOW,
      ),
    ).toEqual({ prompt: "New" });
  });

  it("reports the first schedule problem and required fields", () => {
    const fresh = blankTaskForm(catalog, {}, ZONE);
    const blank = {
      ...fresh,
      schedule: { ...fresh.schedule, date: "2026-11-04" },
    };
    expect(taskFormErrors(null, blank, NOW)).toEqual({
      name: "Give this schedule a name.",
      workspace: "Choose a workspace.",
      prompt: "Add a prompt for the agent.",
    });
    const filled = {
      ...blank,
      name: "Review",
      workspaceId: "ws-1",
      prompt: "Go",
    };
    expect(
      taskFormErrors(
        null,
        { ...filled, schedule: { ...filled.schedule, date: "2026-11-02" } },
        NOW,
      ).schedule,
    ).toBe("Choose a future date and time.");
    expect(
      taskFormErrors(
        null,
        {
          ...filled,
          schedule: { ...filled.schedule, mode: "times", weekdays: [] },
        },
        NOW,
      ).schedule,
    ).toBe("Choose at least one day.");
    expect(
      taskFormErrors(
        null,
        {
          ...filled,
          schedule: {
            ...filled.schedule,
            mode: "cron",
            cron_expression: "61 * * * *",
          },
        },
        NOW,
      ).schedule,
    ).toMatch(/minute/i);
  });

  it("follows the mode's defaults until a model is chosen and resets them for another engine", () => {
    const form = blankTaskForm(catalog, {}, ZONE);
    expect(withAgent(form, "native", "plan", catalog, false)).toMatchObject({
      mode: "plan",
      model: "haiku",
      effort: "",
    });
    const chosen = withModel(form, "haiku", catalog);
    expect(chosen).toMatchObject({ model: "haiku", effort: "" });
    expect(withAgent(chosen, "native", "plan", catalog, true)).toMatchObject({
      mode: "plan",
      model: "haiku",
    });
    const codex = withAgent(chosen, "codex", "build", catalog, true);
    expect(codex).toMatchObject({
      harnessId: "codex",
      model: "openai/gpt-6",
      effort: "high",
    });
    expect(agentSummary({ ...codex, mode: "plan" }, catalog)).toBe(
      "Codex · Plan · GPT-6 High",
    );
    expect(agentSummary(form, catalog)).toBe("Sonnet High");
  });

  it("offers the device's zone first without duplicates", () => {
    expect(timeZoneGroups("Asia/Tokyo", "Europe/Berlin")).toEqual([
      { title: "This device", zones: ["Europe/Berlin"] },
      {
        title: "Other time zones",
        zones: [
          "Asia/Tokyo",
          "UTC",
          "Europe/London",
          "America/New_York",
          "America/Los_Angeles",
        ],
      },
    ]);
  });
});

describe("schedule section", () => {
  const rowsOf = (rows: NativeFormRow[]) =>
    rows.map((row) => `${row.kind}:${row.id}`);
  const base = blankTaskForm(catalog, {}, ZONE).schedule;

  it("shows each mode's native controls and keeps the last time", () => {
    const changes: unknown[] = [];
    const section = (schedule = base) =>
      scheduleFormSection({
        schedule,
        timezone: ZONE,
        paused: false,
        nextRunAt: null,
        onSchedule: (next) => changes.push(next),
        onTimezone: () => undefined,
        now: NOW,
      });
    expect(rowsOf(section().rows)).toEqual([
      "segmented:schedule-mode",
      "date:schedule-date",
      "date:schedule-time",
      "picker:schedule-timezone",
      "value:schedule-next-run",
    ]);
    const times = { ...base, mode: "times" as const, times: ["09:00"] };
    const timesRows = section(times).rows;
    expect(rowsOf(timesRows)).toEqual([
      "segmented:schedule-mode",
      "weekdays:schedule-days",
      "date:schedule-time-0",
      "button:schedule-add-time",
      "picker:schedule-timezone",
      "value:schedule-next-run",
    ]);
    expect(timesRows.find((row) => row.id === "schedule-time-0")).toMatchObject(
      { label: "Time", onRemove: undefined },
    );
    const add = timesRows.find((row) => row.id === "schedule-add-time");
    if (add?.kind === "button") add.onPress();
    expect(changes.at(-1)).toMatchObject({ times: ["09:00", "10:00"] });
    const mode = section(times).rows[0];
    if (mode?.kind === "segmented") mode.onChange("cron");
    expect(changes.at(-1)).toMatchObject({
      mode: "cron",
      cron_expression: "0 9 * * *",
    });
    expect(
      rowsOf(section({ ...base, mode: "interval", window_enabled: true }).rows),
    ).toContain("picker:schedule-window-end");
  });

  it("previews the next runs, warns about frequent runs and shows errors", () => {
    const weekdays = {
      ...base,
      mode: "times" as const,
      weekdays: [0, 1, 2, 3, 4],
      times: ["09:00", "14:30"],
    };
    const preview = scheduleFormSection({
      schedule: weekdays,
      timezone: ZONE,
      paused: false,
      nextRunAt: null,
      onSchedule: () => undefined,
      onTimezone: () => undefined,
      now: NOW,
    });
    expect(preview.rows.at(-1)).toMatchObject({
      value: expect.stringMatching(/^Today at /),
      tone: "accent",
    });
    expect(preview.footer).toMatch(/^Then .+ and .+\./);
    const frequent = scheduleFormSection({
      schedule: { ...base, mode: "interval", interval: "5m" },
      timezone: ZONE,
      paused: false,
      nextRunAt: null,
      onSchedule: () => undefined,
      onTimezone: () => undefined,
      now: NOW,
    });
    expect(frequent).toMatchObject({
      footer: "Up to 288 runs a day. Each run starts a new chat.",
      footerTone: "warning",
    });
    const failing = scheduleFormSection({
      schedule: weekdays,
      timezone: ZONE,
      paused: false,
      nextRunAt: null,
      error: "Choose at least one day.",
      onSchedule: () => undefined,
      onTimezone: () => undefined,
      now: NOW,
    });
    expect(failing).toMatchObject({
      footer: "Choose at least one day.",
      footerTone: "danger",
    });
    expect(failing.rows.at(-1)).toMatchObject({ value: "—" });
    expect(
      scheduleFormSection({
        schedule: weekdays,
        timezone: ZONE,
        paused: true,
        nextRunAt: null,
        onSchedule: () => undefined,
        onTimezone: () => undefined,
        now: NOW,
      }).rows.at(-1),
    ).toMatchObject({ value: "Paused", tone: "muted" });
  });
});
