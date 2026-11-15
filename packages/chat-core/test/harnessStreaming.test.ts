import { describe, expect, it } from "vitest";

import type { HarnessMessage, HarnessPartDelta } from "../src/harness";
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureBusyAssistant,
  routeAssistantMessage,
} from "../src/harnessReducer";

function stream(
  messages: HarnessMessage[],
  partId: string,
  delta: HarnessPartDelta,
  step = 1,
) {
  return applyPartDelta(messages, "session-1", delta, {
    messageId: "assistant-1",
    partId,
    step,
  });
}

describe("current harness streaming contract", () => {
  it("updates live context without closing streams or accumulating snapshots", () => {
    const messages: HarnessMessage[] = [];
    stream(messages, "step", { step_start: 1 });
    const assistant = stream(messages, "text", { text: "Working" });
    const context = {
      prompt_tokens: 40,
      completion_tokens: 10,
      context_length: 258400,
    };
    for (let index = 0; index < 2; index += 1)
      stream(messages, "step", { context });
    expect(assistant.parts).toHaveLength(2);
    expect(assistant.parts[0]?.meta).toEqual({ step: 1, context });
    expect(assistant.parts[1]?.state).toBe("running");
    stream(messages, "finish", { step_finish: 1, context });
    expect(assistant.parts[2]?.meta?.context).toEqual(context);
  });

  it("keeps text chunks in their persisted part and preserves the parts array", () => {
    const messages: HarnessMessage[] = [];
    const assistant = stream(messages, "text-1", { text: "Hello " });
    const parts = assistant.parts;
    stream(messages, "text-1", { text: "world" });
    // A second persisted text row stays separate even without a tool boundary.
    stream(messages, "text-2", { text: "!" });
    expect(assistant.parts).toBe(parts);
    expect(assistant.content).toBe("Hello world!");
    expect(parts.map(({ id, output }) => [id, output])).toEqual([
      ["text-1", "Hello world"],
      ["text-2", "!"],
    ]);
  });

  it("keeps reasoning, parallel tools, and following text in observed order", () => {
    const messages: HarnessMessage[] = [];
    stream(messages, "text-1", { text: "Before" });
    stream(messages, "reasoning-1", { reasoning: "Thinking" }, 2);
    const assistant = stream(messages, "tool-1", {
      tool_started: "read",
      state: "pending",
      call_id: "provisional",
    });
    stream(messages, "tool-2", { tool_started: "bash", state: "pending" });
    stream(messages, "tool-1", {
      tool_started: "read",
      state: "running",
      call_id: "actual",
    });
    expect(assistant.parts[2]).toMatchObject({
      state: "running",
      call_id: "actual",
    });
    stream(messages, "tool-2", {
      tool_completed: "bash",
      state: "completed",
      display: { tool: "bash", summary: "Done" },
    });
    stream(messages, "tool-1", {
      tool_error: "Tool failed",
      state: "error",
      display: { tool: "read", summary: "File missing" },
    });
    stream(messages, "text-2", { text: "After" });
    expect(assistant.content).toBe("BeforeAfter");
    expect(assistant.parts.map(({ id, state }) => [id, state])).toEqual([
      ["text-1", "completed"],
      ["reasoning-1", "completed"],
      ["tool-1", "error"],
      ["tool-2", "completed"],
      ["text-2", "running"],
    ]);
    expect(assistant.parts[1]?.meta).toEqual({ step: 2 });
    expect(assistant.parts[2]).toMatchObject({
      input: { tool: "read" },
      output: "Tool failed",
      detail_loaded: false,
      display: { summary: "File missing" },
    });
  });

  it.each(["completed", "error"] as const)(
    "creates a %s projection when the start was missed",
    (state) => {
      const messages: HarnessMessage[] = [];
      const display = { tool: "read", summary: "Read cat.png" };
      const delta =
        state === "completed"
          ? { tool_completed: "read" }
          : { tool_error: "Tool failed" };
      const assistant = stream(messages, "tool-1", {
        ...delta,
        state,
        display,
      });
      stream(messages, "tool-1", { ...delta, state, display });
      expect(assistant.parts).toEqual([
        expect.objectContaining({
          id: "tool-1",
          state,
          input: { tool: "read" },
          output: state === "error" ? "Tool failed" : "",
          display,
          detail_loaded: false,
        }),
      ]);
    },
  );

  it("keeps REST-loaded tool details when a completion event arrives later", () => {
    const messages: HarnessMessage[] = [];
    const assistant = stream(messages, "tool-1", {
      tool_started: "read",
      state: "running",
    });
    Object.assign(assistant.parts[0]!, {
      input: { tool: "read", arguments: { path: "/workspace/cat.png" } },
      output: "Image read successfully",
      detail_loaded: true,
      meta: {
        attachments: [
          {
            type: "file",
            mime: "image/png",
            url: "data:image/png;base64,YQ==",
          },
        ],
      },
    });
    stream(messages, "tool-1", {
      tool_completed: "read",
      state: "completed",
      display: { tool: "read" },
    });
    expect(assistant.parts[0]).toMatchObject({
      output: "Image read successfully",
      detail_loaded: true,
      input: { arguments: { path: "/workspace/cat.png" } },
      meta: { attachments: [expect.objectContaining({ mime: "image/png" })] },
    });
  });

  it("uses compact patch projections and loads details separately", () => {
    const messages: HarnessMessage[] = [];
    const patch = {
      path: "a.ts",
      preview: [{ type: "add" as const, oldNo: null, newNo: 1, content: "x" }],
    };
    const assistant = stream(messages, "patch-1", {
      patch: "a.ts",
      display: patch,
    });
    expect(assistant.content).toBe("");
    expect(assistant.parts).toEqual([
      expect.objectContaining({
        id: "patch-1",
        display: patch,
        output: "",
        detail_loaded: false,
      }),
    ]);
  });

  it("stores step usage and closes the reasoning stream at the step boundary", () => {
    const messages: HarnessMessage[] = [];
    stream(messages, "start-1", { step_start: 1 });
    stream(messages, "reasoning-1", { reasoning: "Thinking" });
    const assistant = stream(messages, "finish-1", {
      step_finish: 1,
      cost: 0.01,
      tokens: { prompt_tokens: 10, completion_tokens: 2 },
    });
    expect(assistant.parts[1]?.state).toBe("completed");
    expect(assistant.parts[2]?.meta).toEqual({
      step: 1,
      cost: 0.01,
      tokens: { prompt_tokens: 10, completion_tokens: 2 },
    });
  });

  it("keeps server position order when a lower-positioned tool arrives late", () => {
    const messages: HarnessMessage[] = [];
    const assistant = applyPartDelta(
      messages,
      "session-1",
      { text: "Answer" },
      {
        messageId: "assistant-1",
        partId: "text-1",
        partPosition: 1,
      },
    );
    applyPartDelta(
      messages,
      "session-1",
      { tool_started: "read", state: "running" },
      {
        messageId: "assistant-1",
        partId: "tool-1",
        partPosition: 0,
      },
    );
    expect(assistant.parts.map((part) => part.id)).toEqual([
      "tool-1",
      "text-1",
    ]);
  });

  it("routes fresh turns by message ID and ignores deltas for completed turns", () => {
    const messages: HarnessMessage[] = [];
    const old = stream(messages, "text-1", { text: "Done" });
    old.completed_at = "2026-10-25T10:00:00Z";
    const fresh = routeAssistantMessage(messages, "session-1", "assistant-2");
    stream(messages, "text-1", { text: "Stale" });
    expect(old.content).toBe("Done");
    expect(fresh.content).toBe("");
    expect(messages.map((message) => message.id)).toEqual([
      "assistant-1",
      "assistant-2",
    ]);
    expect(routeAssistantMessage(messages, "session-1", "assistant-2")).toBe(
      fresh,
    );
  });

  it("does not invent a part ID for notices without a persisted row", () => {
    const messages: HarnessMessage[] = [];
    const assistant = applyPartDelta(
      messages,
      "session-1",
      {},
      { messageId: "assistant-1" },
    );
    expect(assistant.parts).toEqual([]);
  });

  it("anchors busy turns after the optimistic user while the server echo is pending", () => {
    const messages: HarnessMessage[] = [
      {
        id: "local-user-1",
        session_id: "session-1",
        role: "user",
        content: "Continue",
        parts: [],
      },
    ];
    const assistant = ensureBusyAssistant(
      messages,
      "session-1",
      "assistant-1",
      "server-user-1",
    );
    expect(messages.map((message) => message.id)).toEqual([
      "local-user-1",
      "assistant-1",
    ]);
    expect(
      ensureBusyAssistant(
        messages,
        "session-1",
        "assistant-1",
        "server-user-1",
      ),
    ).toBe(assistant);
  });

  it.each(["completed", "error"])(
    "routes subtask %s to its persisted part",
    (status) => {
      const messages: HarnessMessage[] = [];
      const assistant = stream(messages, "text-1", { text: "Before" });
      const event = {
        workspace_id: "workspace-1",
        session_id: "session-1",
        message_id: "assistant-1",
        subtask_id: "subtask-1",
        part_id: "subtask-part-1",
        part_position: 1,
        agent: "explore",
        description: "Inspect files",
        child_session_id: "child-1",
        model: "test/model",
      };
      applySubtaskStarted(assistant, "session-1", event);
      applySubtaskStarted(assistant, "session-1", event);
      applySubtaskFinished(assistant, {
        ...event,
        status,
        summary: "Found files",
      });
      expect(assistant.parts).toHaveLength(2);
      expect(assistant.parts[0]?.state).toBe("completed");
      expect(assistant.parts[1]).toMatchObject({
        id: "subtask-part-1",
        state: status,
        output: "Found files",
        meta: { child_session_id: "child-1", model: "test/model", status },
      });
    },
  );

  it("replaces the todo snapshot without retaining removed items", () => {
    const todos = [
      {
        id: "todo-1",
        content: "Inspect",
        status: "pending" as const,
        priority: "high" as const,
      },
    ];
    expect(applyTodoUpdate(todos, { todos: [] })).toEqual([]);
    expect(todos).toHaveLength(1);
  });
});
