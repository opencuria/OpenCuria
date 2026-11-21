import { describe, expect, it } from "vitest";

import {
  formatSendTime,
  sendLaterDefault,
  sendLaterError,
  sendLaterFields,
  sendLaterFromFields,
  sendLaterOptions,
  sendLaterTaskInput,
} from "../src/sendLater";

/** Local wall-clock date, independent of the test machine's zone. */
function local(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
): Date {
  return new Date(year, month - 1, day, hour, minute);
}

describe("sendLaterOptions", () => {
  it("offers an hour from now, tomorrow morning and next Monday morning", () => {
    // Wednesday, 18 Nov 2026, 14:23:41.
    const now = local(2026, 11, 18, 14, 23);
    now.setSeconds(41);
    const options = sendLaterOptions(now);

    expect(options.map(({ id, label }) => [id, label])).toEqual([
      ["in-1-hour", "In 1 hour"],
      ["tomorrow", "Tomorrow"],
      ["monday", "Monday"],
    ]);
    expect(options.map((option) => option.at)).toEqual([
      local(2026, 11, 18, 15, 23),
      local(2026, 11, 19, 9),
      local(2026, 11, 23, 9),
    ]);
    expect(options.every((option) => option.detail.length > 0)).toBe(true);
  });

  it("folds Monday into Tomorrow on Sundays and says Next Monday on Mondays", () => {
    const sunday = sendLaterOptions(local(2026, 11, 22, 20));
    expect(sunday.map((option) => option.id)).toEqual([
      "in-1-hour",
      "tomorrow",
    ]);
    expect(sunday[1]?.at).toEqual(local(2026, 11, 23, 9));

    const monday = sendLaterOptions(local(2026, 11, 23, 8));
    expect(monday[2]).toMatchObject({
      label: "Next Monday",
      at: local(2026, 11, 30, 9),
    });
  });
});

describe("formatSendTime", () => {
  const now = local(2026, 11, 18, 14, 0);

  it("names nearby days and dates further away", () => {
    expect(formatSendTime(local(2026, 11, 18, 16, 30), now)).toMatch(
      /^Today, /,
    );
    expect(formatSendTime(local(2026, 11, 19, 9), now)).toMatch(/^Tomorrow, /);
    expect(formatSendTime(local(2026, 11, 17, 9), now)).toMatch(/^Yesterday, /);
    const friday = formatSendTime(local(2026, 11, 20, 9), now);
    expect(friday).not.toMatch(/^(Today|Tomorrow)/);
    expect(friday).toContain(", ");
    expect(formatSendTime(local(2027, 1, 5, 9), now)).toContain("2027");
    expect(formatSendTime(local(2026, 11, 19, 9).toISOString(), now)).toMatch(
      /^Tomorrow, /,
    );
    expect(formatSendTime("not a date", now)).toBe("");
  });
});

describe("custom send times", () => {
  const now = local(2026, 11, 18, 14, 50);

  it("starts at a full hour at least a quarter hour away", () => {
    expect(sendLaterDefault(now)).toEqual(local(2026, 11, 18, 16));
    expect(sendLaterDefault(local(2026, 11, 18, 14, 10))).toEqual(
      local(2026, 11, 18, 15),
    );
  });

  it("accepts only future times within a year", () => {
    expect(sendLaterError(local(2026, 11, 18, 15), now)).toBeNull();
    expect(sendLaterError(local(2026, 11, 18, 14, 50), now)).toBe(
      "Choose a time in the future.",
    );
    expect(sendLaterError(local(2027, 12, 1), now)).toBe(
      "Choose a time within a year.",
    );
    expect(sendLaterError(null, now)).toBe("Choose a date and time.");
  });

  it("round-trips local date and time fields", () => {
    const at = local(2026, 3, 9, 7, 5);
    expect(sendLaterFields(at)).toEqual({ date: "2026-03-09", time: "07:05" });
    expect(sendLaterFromFields("2026-03-09", "07:05")).toEqual(at);
    expect(sendLaterFromFields("2026-02-30", "07:05")).toBeNull();
    expect(sendLaterFromFields("2026-03-09", "24:00")).toBeNull();
    expect(sendLaterFromFields("", "07:05")).toBeNull();
  });
});

describe("sendLaterTaskInput", () => {
  it("turns a new chat into a one-time scheduled task at the local time", () => {
    const task = sendLaterTaskInput({
      workspaceId: "ws-1",
      prompt:
        "\n  Review the open pull requests and summarize them for the team  \nDetails",
      at: local(2026, 11, 19, 9, 30),
      mode: "plan",
      harnessId: "claude",
      model: "m-1",
      reasoningEffort: "high",
      skillIds: ["s-1"],
      timeZone: "Europe/Berlin",
    });

    expect(task).toEqual({
      name: "Review the open pull requests and summarize them for the te…",
      workspace_id: "ws-1",
      prompt:
        "\n  Review the open pull requests and summarize them for the team  \nDetails",
      mode: "plan",
      harness_id: "claude",
      model: "m-1",
      reasoning_effort: "high",
      skill_ids: ["s-1"],
      recurrence: "once",
      weekdays: [],
      run_date: "2026-11-19",
      local_time: "09:30",
      timezone_name: "Europe/Berlin",
      enabled: true,
    });
    expect(task.name.length).toBe(60);
  });
});
