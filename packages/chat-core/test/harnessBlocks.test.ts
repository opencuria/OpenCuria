import { describe, expect, it } from "vitest";

import type { HarnessPart } from "../src/harness";
import {
  buildMessageBlocks,
  type MessageRenderBlock,
  type RenderBlock,
} from "../src/harnessBlocks";

function makePart(
  id: string,
  type: HarnessPart["type"],
  overrides: Partial<HarnessPart> = {},
): HarnessPart {
  return {
    id,
    session_id: "session-1",
    type,
    state: "completed",
    title: "",
    output: "",
    ...overrides,
  };
}

function tool(id: string, overrides: Partial<HarnessPart> = {}): HarnessPart {
  return makePart(id, "tool", { tool: "bash", ...overrides });
}

/** Intro text, a two-tool group, reasoning and the live answer. */
function turn(): HarnessPart[] {
  return [
    makePart("intro", "text", { output: "Looking around." }),
    tool("ls"),
    tool("cat"),
    makePart("think", "reasoning", { output: "Plan" }),
    makePart("live", "text", { state: "running", output: "Answer" }),
  ];
}

function innerBlocks(block: MessageRenderBlock | undefined): RenderBlock[] {
  if (block?.kind !== "workedFor") throw new Error("expected a workedFor card");
  return block.blocks;
}

describe("buildMessageBlocks previous reuse", () => {
  it("returns the previous blocks when a streamed token changes no structure", () => {
    const parts = turn();
    const previous = buildMessageBlocks(parts);

    parts[4]!.output += " continues";

    expect(buildMessageBlocks(parts, { previous })).toBe(previous);
  });

  it("keeps untouched blocks and rebuilds the blocks a new part changes", () => {
    const parts = turn().slice(0, 3);
    const previous = buildMessageBlocks(parts);

    parts.push(tool("grep", { state: "running" }));
    const next = buildMessageBlocks(parts, { previous });

    expect(next[0]).toBe(previous[0]);
    expect(next[1]).not.toBe(previous[1]);
    expect(next[1]).toEqual({ kind: "group", parts: parts.slice(1) });
  });

  it("rebuilds blocks whose part changes kind", () => {
    const parts = turn();
    const previous = buildMessageBlocks(parts);

    parts[2]!.state = "error";
    const next = buildMessageBlocks(parts, { previous });

    expect(next.map((block) => block.kind)).toEqual([
      "text",
      "single",
      "single",
      "single",
      "text",
    ]);
    expect(next[0]).toBe(previous[0]);
    expect(next[1]).not.toBe(previous[1]);
  });

  it("rebuilds blocks for replaced part objects from immutable snapshots", () => {
    const parts = turn();
    const previous = buildMessageBlocks(parts);

    const snapshot = parts.map((part) =>
      part.id === "cat" ? { ...part, title: "cat a.txt" } : part,
    );
    const next = buildMessageBlocks(snapshot, { previous });

    expect(next[0]).toBe(previous[0]);
    expect(next[1]).toEqual({ kind: "group", parts: snapshot.slice(1, 3) });
    expect(next[2]).toBe(previous[2]);
    expect(next[3]).toBe(previous[3]);
  });

  it("reuses finished work cards and their unchanged inner blocks", () => {
    const parts = turn();
    const previous = buildMessageBlocks(parts, { finished: true });
    expect(previous.map((block) => block.kind)).toEqual(["workedFor", "text"]);
    expect(buildMessageBlocks(parts, { finished: true, previous })).toBe(
      previous,
    );

    parts.splice(3, 0, tool("rm"));
    const next = buildMessageBlocks(parts, { finished: true, previous });
    const before = innerBlocks(previous[0]);
    const after = innerBlocks(next[0]);

    expect(next[1]).toBe(previous[1]);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).toEqual({ kind: "group", parts: parts.slice(1, 4) });
    expect(after[2]).toBe(before[2]);
  });

  it("wraps finished work correctly from streaming blocks", () => {
    const parts = turn();
    const previous = buildMessageBlocks(parts);

    const next = buildMessageBlocks(parts, { finished: true, previous });

    expect(next).toEqual(buildMessageBlocks(parts, { finished: true }));
    expect(innerBlocks(next[0]).map((block) => block.kind)).toEqual([
      "text",
      "group",
      "single",
    ]);
  });
});
