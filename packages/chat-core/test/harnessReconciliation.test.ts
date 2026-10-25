import { describe, expect, it } from "vitest";

import type { HarnessMessage, HarnessPart } from "../src/harness";
import {
  mergeBusyFetchedMessages,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
} from "../src/harnessReducer";

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

describe("timeline ordering and settlement", () => {
  it("settles leftover running text and reasoning parts without touching tools", () => {
    const messages: HarnessMessage[] = [
      {
        id: "msg-1",
        session_id: "session-1",
        role: "assistant",
        content: "done",
        parts: [
          {
            id: "r1",
            session_id: "session-1",
            type: "reasoning",
            state: "running",
            title: "",
            output: "planning",
          },
          {
            id: "t1",
            session_id: "session-1",
            type: "text",
            state: "pending",
            title: "",
            output: "done",
          },
          {
            id: "tool-1",
            session_id: "session-1",
            type: "tool",
            state: "running",
            title: "bash",
            output: "",
          },
        ],
      },
    ];

    const settled = settleOpenStreamParts(messages);
    expect(settled[0]!.parts[0]!.state).toBe("completed");
    expect(settled[0]!.parts[0]!.output).toBe("planning");
    expect(settled[0]!.parts[1]!.state).toBe("completed");
    expect(settled[0]!.parts[2]!.state).toBe("running");
  });

  it("sorts messages by position with creation-order fallback", () => {
    const messages: HarnessMessage[] = [
      {
        id: "b",
        session_id: "s",
        role: "user",
        content: "b",
        parts: [],
        position: 1,
        created_at: "2026-03-29T10:00:01.000Z",
      },
      {
        id: "a",
        session_id: "s",
        role: "user",
        content: "a",
        parts: [],
        position: 0,
        created_at: "2026-03-29T10:00:02.000Z",
      },
      { id: "local", session_id: "s", role: "user", content: "c", parts: [] },
    ];
    expect(sortHarnessMessages(messages).map((m) => m.id)).toEqual([
      "a",
      "b",
      "local",
    ]);
  });

  it("sorts parts by position while preserving live observed order", () => {
    const parts = [
      {
        id: "p2",
        session_id: "s",
        type: "tool" as const,
        state: "completed" as const,
        title: "",
        output: "",
        position: 1,
      },
      {
        id: "p1",
        session_id: "s",
        type: "text" as const,
        state: "completed" as const,
        title: "",
        output: "",
        position: 0,
      },
      {
        id: "live-1",
        session_id: "s",
        type: "text" as const,
        state: "running" as const,
        title: "",
        output: "",
      },
    ];
    expect(sortHarnessParts(parts).map((p) => p.id)).toEqual([
      "p1",
      "p2",
      "live-1",
    ]);
  });

  it("carries the optimistic follow-up user across a busy fetch until echoed", () => {
    const previous: HarnessMessage[] = [
      {
        id: "msg-user-1",
        session_id: "session-1",
        role: "user",
        content: "first",
        parts: [],
        position: 0,
      },
      {
        id: "assistant-1",
        session_id: "session-1",
        role: "assistant",
        content: "done",
        parts: [],
        position: 1,
        completed_at: "2026-03-29T10:00:00.000Z",
      },
      {
        id: "local-user-session-1-9",
        session_id: "session-1",
        role: "user",
        content: "follow up",
        parts: [],
      },
    ];
    const incoming: HarnessMessage[] = [
      {
        id: "msg-user-1",
        session_id: "session-1",
        role: "user",
        content: "first",
        parts: [],
        position: 0,
      },
      {
        id: "assistant-1",
        session_id: "session-1",
        role: "assistant",
        content: "done",
        parts: [],
        position: 1,
        completed_at: "2026-03-29T10:00:00.000Z",
      },
    ];

    const merged = mergeBusyFetchedMessages(previous, incoming);
    expect(merged.some((m) => m.id === "local-user-session-1-9")).toBe(true);

    // Server echo arrives: the optimistic row drops (no duplicate).
    const echoed = mergeBusyFetchedMessages(previous, [
      ...incoming,
      {
        id: "msg-user-2",
        session_id: "session-1",
        role: "user",
        content: "follow up",
        parts: [],
        position: 2,
      },
    ]);
    expect(
      echoed.filter((m) => m.role === "user" && m.content === "follow up"),
    ).toHaveLength(1);
    expect(echoed.some((m) => m.id === "local-user-session-1-9")).toBe(false);
  });
});
