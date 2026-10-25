import { describe, expect, it } from "vitest";

import { buildRenderBlocks, wrapFinishedWork } from "../src/harnessBlocks";
import type { HarnessMessage, HarnessPart } from "../src/harness";
import {
  applyPartDelta,
  hasDeliveredSteer,
  mergeBusyFetchedMessages,
  steerPartId,
} from "../src/harnessReducer";

function makePart(
  type: HarnessPart["type"],
  overrides: Partial<HarnessPart> = {},
): HarnessPart {
  return {
    id: `part-${type}`,
    session_id: "session-1",
    type,
    state: "completed",
    title: "",
    output: "",
    ...overrides,
  };
}

function makeMessage(id: string, parts: HarnessPart[] = []): HarnessMessage {
  return {
    id,
    session_id: "session-1",
    role: "assistant",
    content: "",
    parts,
  };
}

describe("shared harness steering", () => {
  it("reduces a delivered steer as a distinct, idempotent timeline part", () => {
    const messages: HarnessMessage[] = [
      {
        id: "user-1",
        session_id: "session-1",
        role: "user",
        content: "Start",
        parts: [],
      },
    ];

    applyPartDelta(
      messages,
      "session-1",
      { text: "Before" },
      {
        messageId: "assistant-1",
        partId: "text-1",
        partPosition: 0,
      },
    );
    applyPartDelta(
      messages,
      "session-1",
      { reasoning: "Thinking" },
      {
        messageId: "assistant-1",
        partId: "reasoning-1",
        partPosition: 1,
      },
    );
    applyPartDelta(
      messages,
      "session-1",
      { steer: "Use pnpm", steer_id: "steer-1" },
      { messageId: "assistant-1", partId: "steer-part-1", partPosition: 2 },
    );
    const assistant = applyPartDelta(
      messages,
      "session-1",
      { text: "After" },
      {
        messageId: "assistant-1",
        partId: "text-2",
        partPosition: 3,
      },
    );

    expect(assistant.content).toBe("BeforeAfter");
    expect(
      assistant.parts.map(({ id, type, state }) => [id, type, state]),
    ).toEqual([
      ["text-1", "text", "completed"],
      ["reasoning-1", "reasoning", "completed"],
      ["steer-part-1", "steer", "completed"],
      ["text-2", "text", "running"],
    ]);
    const steer = assistant.parts[2]!;
    expect(steer).toMatchObject({
      output: "Use pnpm",
      meta: { steer_id: "steer-1" },
      position: 2,
    });
    expect(steerPartId(steer)).toBe("steer-1");
    expect(hasDeliveredSteer(messages, "steer-1")).toBe(true);
    expect(hasDeliveredSteer(messages, "missing")).toBe(false);

    applyPartDelta(
      messages,
      "session-1",
      { steer: "Use pnpm", steer_id: "steer-1" },
      { messageId: "assistant-1", partId: "steer-part-1" },
    );
    expect(
      assistant.parts.filter((part) => part.type === "steer"),
    ).toHaveLength(1);
  });

  it("reconciles a live steer with its server-backed part", () => {
    const livePart = makePart("steer", {
      id: "server-steer-1",
      output: "Use pnpm",
      meta: { steer_id: "steer-1" },
    });
    const live = makeMessage("assistant-1", [livePart]);
    const server = makeMessage("assistant-1", [
      makePart("steer", {
        id: "server-steer-1",
        output: "Use pnpm",
        meta: { steer_id: "steer-1" },
        position: 3,
      }),
    ]);

    const merged = mergeBusyFetchedMessages([live], [server]);

    expect(merged[0]?.parts).toHaveLength(1);
    expect(merged[0]?.parts[0]).toMatchObject({
      id: "server-steer-1",
      type: "steer",
      output: "Use pnpm",
      meta: { steer_id: "steer-1" },
    });
  });

  it("keeps delivered steers outside work groups and finished work cards", () => {
    const parts = [
      makePart("text", { id: "text-1", output: "Working" }),
      makePart("tool", { id: "tool-1", input: { tool: "read" } }),
      makePart("tool", { id: "tool-2", input: { tool: "write" } }),
      makePart("steer", {
        id: "steer-1",
        output: "Use pnpm",
        meta: { steer_id: "steer-1" },
      }),
      makePart("tool", { id: "tool-3", input: { tool: "read" } }),
      makePart("tool", { id: "tool-4", input: { tool: "write" } }),
      makePart("text", { id: "text-2", output: "Done" }),
    ];

    const flat = buildRenderBlocks(parts);
    expect(flat.map((block) => block.kind)).toEqual([
      "text",
      "group",
      "steer",
      "group",
      "text",
    ]);

    const wrapped = wrapFinishedWork(flat);
    expect(wrapped.map((block) => block.kind)).toEqual([
      "workedFor",
      "steer",
      "workedFor",
      "text",
    ]);
    for (const block of wrapped) {
      if (block.kind === "workedFor") {
        expect(block.blocks.some((inner) => inner.kind === "steer")).toBe(
          false,
        );
      }
    }
  });
});
