import { describe, expect, it } from "vitest";

import type { HarnessMessage } from "../src/harness";
import {
  isOptimisticPrompt,
  mergeBusyFetchedMessages,
  reconcileOptimisticPrompts,
} from "../src/harnessReducer";
import {
  createOptimisticPrompt,
  messageRenderKey,
  withoutOptimisticPrompt,
} from "../src/optimisticPrompts";

function user(id: string, content: string, position?: number): HarnessMessage {
  return {
    id,
    session_id: "s",
    role: "user",
    content,
    parts: [],
    ...(position === undefined ? {} : { position }),
  };
}

function answer(id: string, position?: number): HarnessMessage {
  return {
    id,
    session_id: "s",
    role: "assistant",
    content: "done",
    parts: [],
    completed_at: "2026-11-17T10:00:00.000Z",
    ...(position === undefined ? {} : { position }),
  };
}

const history = [user("u1", "first", 0), answer("a1", 1)];

describe("optimistic prompts", () => {
  it("creates a local row that keeps its own render key", () => {
    const prompt = createOptimisticPrompt({
      sessionId: "s",
      content: "  ship it \n",
      skillIds: ["skill"],
      harnessId: "codex",
      now: new Date("2026-11-17T10:00:00.000Z"),
    });
    expect(prompt).toMatchObject({
      session_id: "s",
      role: "user",
      content: "ship it",
      skill_ids: ["skill"],
      harness_id: "codex",
      parts: [],
      created_at: "2026-11-17T10:00:00.000Z",
    });
    expect(prompt.id).toMatch(/^local-user-/);
    expect(isOptimisticPrompt(prompt)).toBe(true);
    expect(messageRenderKey(prompt)).toBe(prompt.id);
    expect(messageRenderKey(user("u1", "x"))).toBe("u1");
    expect(
      createOptimisticPrompt({ sessionId: "s", content: "x" }).id,
    ).not.toBe(prompt.id);
  });

  it("hands the render key to the server's copy of the prompt", () => {
    const prompt = createOptimisticPrompt({ sessionId: "s", content: "next" });
    const next = reconcileOptimisticPrompts(
      [...history, prompt],
      [...history, user("u2", "next", 2), answer("a2", 3)],
    );
    expect(next.map((message) => message.id)).toEqual(["u1", "a1", "u2", "a2"]);
    expect(next.map(messageRenderKey)).toEqual(["u1", "a1", prompt.id, "a2"]);

    // Later snapshots keep the key by id.
    const later = reconcileOptimisticPrompts(next, [
      ...history,
      user("u2", "next", 2),
      answer("a2", 3),
    ]);
    expect(messageRenderKey(later[2]!)).toBe(prompt.id);
  });

  it("keeps a prompt the snapshot does not hold yet, after the history", () => {
    const prompt = createOptimisticPrompt({ sessionId: "s", content: "first" });
    // The older identical prompt is no echo of the new one.
    const next = reconcileOptimisticPrompts([...history, prompt], history);
    expect(next.map((message) => message.id)).toEqual(["u1", "a1", prompt.id]);
    expect(next[2]).not.toBe(prompt);
    expect(next[2]).toEqual(prompt);
  });

  it("pairs the first prompt of a new chat with its echo", () => {
    const prompt = createOptimisticPrompt({
      sessionId: "s",
      content: "look\n@file:/workspace/a.png ",
    });
    const next = reconcileOptimisticPrompts(
      [prompt],
      [user("u1", "look\n@file:/workspace/a.png", 0), answer("a1", 1)],
    );
    expect(next.map((message) => message.id)).toEqual(["u1", "a1"]);
    expect(messageRenderKey(next[0]!)).toBe(prompt.id);
  });

  it("keeps prompts through a busy merge until their echo arrives", () => {
    const prompt = createOptimisticPrompt({ sessionId: "s", content: "next" });
    const shell = { ...answer("a2"), completed_at: null, content: "" };
    const stale = mergeBusyFetchedMessages(
      [...history, prompt, shell],
      [...history],
    );
    expect(stale.map((message) => message.id)).toEqual([
      "u1",
      "a1",
      prompt.id,
      "a2",
    ]);

    const echoed = mergeBusyFetchedMessages(
      [...history, prompt, shell],
      [...history, user("u2", "next", 2), { ...shell, position: 3 }],
    );
    expect(echoed.map((message) => message.id)).toEqual([
      "u1",
      "a1",
      "u2",
      "a2",
    ]);
    expect(messageRenderKey(echoed[2]!)).toBe(prompt.id);
  });

  it("drops only a prompt the server did not store", () => {
    const prompt = createOptimisticPrompt({ sessionId: "s", content: "next" });
    const messages = [...history, prompt];
    expect(withoutOptimisticPrompt(messages, prompt.id)).toEqual(history);
    expect(withoutOptimisticPrompt(history, "u1")).toBe(history);
  });
});
