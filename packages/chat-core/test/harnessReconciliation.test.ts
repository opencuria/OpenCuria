import { describe, expect, it } from "vitest";

import type { HarnessMessage, HarnessPart } from "../src/harness";
import { mergeBusyFetchedMessages } from "../src/harnessReducer";

function part(id: string, overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id,
    session_id: "session-1",
    type: "text",
    state: "running",
    title: "",
    output: "",
    ...overrides,
  };
}

function message(
  parts: HarnessPart[],
  overrides: Partial<HarnessMessage> = {},
): HarnessMessage {
  return {
    id: "assistant-1",
    session_id: "session-1",
    role: "assistant",
    content: "",
    parts,
    ...overrides,
  };
}

describe("busy snapshot reconciliation", () => {
  it.each(["text", "reasoning"] as const)(
    "preserves streamed %s and completed state across a stale snapshot",
    (type) => {
      const live = message(
        [part("part-1", { type, output: "Hello world", state: "completed" })],
        { content: "Hello world" },
      );
      const snapshot = message([
        part("part-1", { type, output: "Hello", position: 2 }),
      ]);

      const [merged] = mergeBusyFetchedMessages([live], [snapshot]);

      expect(merged?.content).toBe("Hello world");
      expect(merged?.parts).toEqual([
        expect.objectContaining({
          id: "part-1",
          output: "Hello world",
          state: "completed",
          position: 2,
        }),
      ]);
    },
  );

  it.each(["text", "reasoning", "agent", "step-start", "step-finish"] as const)(
    "keeps distinct %s IDs even when their content and step match",
    (type) => {
      const shared = { type, output: "same content", meta: { step: 1 } };
      const live = message([part("part-1", shared), part("part-2", shared)]);
      const snapshot = message([part("part-1", shared)]);

      const [merged] = mergeBusyFetchedMessages([live], [snapshot]);

      expect(merged?.parts.map((entry) => entry.id)).toEqual([
        "part-1",
        "part-2",
      ]);
    },
  );

  it("keeps live-only parts in server position order and merges live metadata", () => {
    const live = message([
      part("agent-1", {
        type: "agent",
        position: 1,
        meta: { step: 1, agent_meta: { action: "click" } },
      }),
      part("tool-1", { type: "tool", position: 0 }),
    ]);
    const snapshot = message([
      part("agent-1", { type: "agent", position: 1, meta: { step: 1 } }),
    ]);

    const [merged] = mergeBusyFetchedMessages([live], [snapshot]);

    expect(merged?.parts.map((entry) => entry.id)).toEqual([
      "tool-1",
      "agent-1",
    ]);
    expect(merged?.parts[1]?.meta).toEqual({
      step: 1,
      agent_meta: { action: "click" },
    });
  });

  it("retains hydrated tool details and errors when the snapshot has only a projection", () => {
    const detail = part("tool-1", {
      type: "tool",
      state: "error",
      input: { tool: "read", arguments: { path: "/a.txt" } },
      output: "File not found",
      meta: { step: 1 },
      detail_loaded: true,
    });
    const snapshot = message([
      part("tool-1", {
        type: "tool",
        state: "completed",
        display: { tool: "read", summary: "Read file" },
        detail_loaded: false,
      }),
    ]);

    const [merged] = mergeBusyFetchedMessages([message([detail])], [snapshot]);

    expect(merged?.parts[0]).toMatchObject({
      ...detail,
      display: { tool: "read", summary: "Read file" },
    });
  });

  it("keeps a live turn missing from the snapshot separate from an identical older turn", () => {
    const live = message([part("part-2", { output: "Hello" })], {
      id: "assistant-2",
      content: "Hello",
      position: 2,
    });
    const older = message([part("part-1", { output: "Hello" })], {
      content: "Hello",
      position: 1,
    });

    const merged = mergeBusyFetchedMessages(
      [older, live],
      [structuredClone(older)],
    );

    expect(merged.map((entry) => entry.id)).toEqual([
      "assistant-1",
      "assistant-2",
    ]);
    expect(merged[1]?.parts).toEqual(live.parts);
  });
});
