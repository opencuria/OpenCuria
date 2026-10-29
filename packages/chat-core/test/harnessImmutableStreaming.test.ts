import { describe, expect, it, vi } from "vitest";

import type { HarnessMessage, HarnessPartDelta } from "../src/harness";
import {
  applyPartDelta,
  applyPartDeltaImmutable,
  type HarnessPartDeltaOptions,
} from "../src/harnessReducer";

function freezeTree<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(freezeTree);
    Object.freeze(value);
  }
  return value;
}

function fixture(): HarnessMessage[] {
  return [
    {
      id: "assistant",
      session_id: "session",
      role: "assistant",
      content: "Before",
      parts: [
        {
          id: "text",
          session_id: "session",
          message_id: "assistant",
          position: 1,
          type: "text",
          state: "running",
          title: "",
          output: "Before",
        },
        {
          id: "reasoning",
          session_id: "session",
          message_id: "assistant",
          position: 0,
          type: "reasoning",
          state: "pending",
          title: "",
          output: "Thinking",
        },
        {
          id: "tool",
          session_id: "session",
          message_id: "assistant",
          position: 2,
          type: "tool",
          state: "running",
          title: "Read",
          output: "Full details",
          input: { tool: "read", arguments: { path: "/workspace/a" } },
          meta: { step: 1, attachments: [{ mime: "text/plain", url: "test" }] },
          display: { tool: "read", summary: "Reading" },
          detail_loaded: true,
        },
      ],
    },
  ];
}

describe("immutable streaming", () => {
  const deltas: Array<[string, string | null, HarnessPartDelta]> = [
    ["text", "text", { text: " after" }],
    ["reasoning", "reasoning", { reasoning: " more" }],
    ["queued tool", "tool", { tool_started: "read", state: "pending" }],
    [
      "running tool",
      "tool",
      { tool_started: "read", state: "running", call_id: "call" },
    ],
    [
      "completed tool",
      "tool",
      { tool_completed: "read", display: { summary: "Read" } },
    ],
    ["tool failure", "tool", { tool_error: "Failed" }],
    ["step start", "step", { step_start: 2 }],
    ["step finish", "finish", { step_finish: 2, cost: 0.1 }],
    [
      "live context",
      "step",
      { context: { prompt_tokens: 40, completion_tokens: 10 } },
    ],
    [
      "patch",
      "patch",
      { patch: "Updated file", display: { summary: "Patch" } },
    ],
    ["compaction", "compaction", { compaction: true }],
    ["steer", "steer", { steer: "Focus here", steer_id: "steer-id" }],
    ["missing part id", null, { text: "ignored" }],
  ];

  it.each(deltas)(
    "matches the mutable reducer for %s on frozen snapshots",
    (_name, partId, delta) => {
      const input = freezeTree(fixture());
      const original = structuredClone(input);
      const expected = structuredClone(input);
      const options = {
        messageId: "assistant",
        partId,
        partPosition: 4,
        step: 2,
      };
      applyPartDelta(expected, "session", delta, options);
      const output = applyPartDeltaImmutable(input, "session", delta, options);
      expect(output).toEqual(expected);
      expect(input).toEqual(original);
    },
  );

  it("preserves completed parts and hydrated detail identity across text chunks", () => {
    const input = fixture();
    input[0]!.parts[1]!.state = "completed";
    input[0]!.parts[2]!.state = "completed";
    freezeTree(input);
    const options = { messageId: "assistant", partId: "text" };
    const output = applyPartDeltaImmutable(
      input,
      "session",
      { text: " after" },
      options,
    );
    expect(output).not.toBe(input);
    expect(output[0]).not.toBe(input[0]);
    expect(output[0]!.parts[0]).not.toBe(input[0]!.parts[0]);
    expect(output[0]!.parts[1]).toBe(input[0]!.parts[1]);
    expect(output[0]!.parts[2]).toBe(input[0]!.parts[2]);
    expect(output[0]!.parts[2]!.input).toBe(input[0]!.parts[2]!.input);
  });

  it("matches an ordered stream with replayed tools, boundaries and position changes", () => {
    let snapshot = freezeTree(fixture());
    const mutable = structuredClone(snapshot);
    const events: Array<[string, HarnessPartDelta, number]> = [
      ["tool", { tool_started: "read", state: "pending" }, 2],
      ["tool", { tool_started: "read", state: "running" }, 2],
      ["tool-2", { tool_started: "bash", state: "running" }, 5],
      ["tool", { tool_completed: "read" }, 2],
      ["tool", { tool_completed: "read" }, 2],
      ["tool-2", { tool_error: "Failed" }, 5],
      ["text-2", { text: "Next" }, 6],
      ["text-2", { text: " chunk" }, 3],
      ["steer", { steer: "Follow-up", steer_id: "user-steer" }, 7],
      ["text-3", { text: "Responding" }, 8],
    ];
    for (const [partId, delta, partPosition] of events) {
      const previous = structuredClone(snapshot);
      const options: HarnessPartDeltaOptions = {
        messageId: "assistant",
        partId,
        partPosition,
      };
      applyPartDelta(mutable, "session", delta, options);
      const next = applyPartDeltaImmutable(snapshot, "session", delta, options);
      expect(next).toEqual(mutable);
      expect(snapshot).toEqual(previous);
      snapshot = freezeTree(next);
    }
  });

  it("routes missing messages without changing existing rows", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
      const input = freezeTree(fixture());
      const expected = structuredClone(input);
      const options = { messageId: "new", partId: "new-text" };
      applyPartDelta(expected, "session", { text: "Hello" }, options);
      const output = applyPartDeltaImmutable(
        input,
        "session",
        { text: "Hello" },
        options,
      );
      expect(output).toEqual(expected);
      expect(output[0]).toBe(input[0]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("leaves completed turns frozen when late deltas arrive", () => {
    const input = fixture();
    input[0]!.completed_at = "2026-01-01T00:00:00Z";
    freezeTree(input);
    const output = applyPartDeltaImmutable(
      input,
      "session",
      { text: "late" },
      {
        messageId: "assistant",
        partId: "text",
      },
    );
    expect(output[0]).toBe(input[0]);
  });
});
