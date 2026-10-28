import { describe, expect, it } from "vitest";
import type { HarnessMessage, HarnessPart } from "../src/harness";
import {
  applySubtaskFinished,
  applySubtaskStarted,
} from "../src/harnessReducer";
import { resolveChildSessionId } from "../src/harnessSubtaskActivity";

const part: HarnessPart = {
  id: "part-1",
  session_id: "parent",
  type: "subtask",
  state: "running",
  title: "Find renderer",
  output: "",
};

describe("explicit child chat links", () => {
  it.each([
    [{ meta: { child_session_id: "child" } }, "child"],
    [{ display: { child_session_id: "child" } }, "child"],
    [
      {
        meta: { child_session_id: "child" },
        display: { child_session_id: "other" },
      },
      "child",
    ],
    [{}, null],
    [
      {
        meta: { child_session_id: "" },
        display: { child_session_id: "other" },
      },
      null,
    ],
    [{ meta: { child_session_id: 42 } }, null],
  ] as const)("reads only the backend association: %j", (fields, expected) => {
    expect(resolveChildSessionId({ ...part, ...fields })).toBe(expected);
  });

  it("retains the child link from streamed start through completion", () => {
    const message: HarnessMessage = {
      id: "message-1",
      session_id: "parent",
      role: "assistant",
      content: "",
      parts: [],
    };
    const started = applySubtaskStarted(message, "parent", {
      workspace_id: "workspace-1",
      session_id: "parent",
      message_id: message.id,
      part_id: part.id,
      subtask_id: "task-1",
      child_session_id: "child",
      agent: "explore",
      description: part.title,
    });
    expect(resolveChildSessionId(started)).toBe("child");
    const finished = applySubtaskFinished(message, {
      workspace_id: "workspace-1",
      session_id: "parent",
      message_id: message.id,
      subtask_id: "task-1",
      status: "completed",
      summary: "Found renderer",
    });
    expect(resolveChildSessionId(finished!)).toBe("child");
  });
});
