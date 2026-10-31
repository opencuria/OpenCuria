import { describe, expect, it } from "vitest";

import {
  buildActivityBlocks,
  buildActivityMessageBlocks,
  findTodoAnchor,
  summarizeActivity,
} from "@opencuria/chat-core/harnessActivity";
import type { HarnessMessage, HarnessPart } from "@opencuria/chat-core/harness";

const part = (
  id: string,
  type: HarnessPart["type"],
  extra: Partial<HarnessPart> = {},
): HarnessPart => ({
  id,
  session_id: "s1",
  type,
  state: "completed",
  title: "",
  output: "",
  ...extra,
});

describe("buildActivityMessageBlocks", () => {
  it("leaves streaming work flat and folds only through the last finished work", () => {
    const parts = [
      text("intro"),
      tool("read", "read"),
      text("update"),
      part("thought", "reasoning", { output: "Checking" }),
      text("answer"),
    ];
    expect(
      buildActivityMessageBlocks(parts).map((block) => block.kind),
    ).toEqual(["text", "activity", "text", "activity", "text"]);
    const blocks = buildActivityMessageBlocks(parts, { finished: true });
    expect(blocks.map((block) => block.kind)).toEqual(["workedFor", "text"]);
    expect(blocks[0]).toMatchObject({
      kind: "workedFor",
      blocks: [
        { kind: "text", part: { id: "intro" } },
        { kind: "activity", parts: [{ id: "read" }] },
        { kind: "text", part: { id: "update" } },
        { kind: "activity", parts: [{ id: "thought" }] },
      ],
    });
  });

  it("keeps steers visible and compaction in chronological order", () => {
    const blocks = buildActivityMessageBlocks(
      [
        part("leading", "compaction"),
        text("intro"),
        tool("read", "read"),
        part("steer", "steer", { output: "Use pnpm" }),
        text("update"),
        part("middle", "compaction"),
        tool("grep", "grep"),
        text("answer"),
      ],
      { finished: true },
    );
    expect(blocks.map((block) => block.kind)).toEqual([
      "activity",
      "workedFor",
      "steer",
      "workedFor",
      "text",
    ]);
    expect(blocks[3]).toMatchObject({
      kind: "workedFor",
      blocks: [
        { kind: "text", part: { id: "update" } },
        { kind: "activity", parts: [{ id: "middle" }] },
        { kind: "activity", parts: [{ id: "grep" }] },
      ],
    });
  });

  it("folds answered questions and anchored todos but keeps pending questions hidden", () => {
    const blocks = buildActivityMessageBlocks(
      [
        text("intro"),
        tool("open", "question", { state: "running" }),
        tool("answered", "question"),
        tool("todo", "todowrite"),
        text("answer"),
      ],
      { finished: true, todoPartId: "todo" },
    );
    expect(blocks[0]).toMatchObject({
      kind: "workedFor",
      blocks: [
        { kind: "text", part: { id: "intro" } },
        { kind: "question", part: { id: "answered" } },
        { kind: "todos", part: { id: "todo" } },
      ],
    });
    expect(blocks[1]).toMatchObject({ kind: "text", part: { id: "answer" } });
  });

  it("does not invent work for text-only or compaction-only turns", () => {
    expect(
      buildActivityMessageBlocks([text("answer")], { finished: true }),
    ).toEqual([{ kind: "text", part: text("answer") }]);
    expect(
      buildActivityMessageBlocks([part("compact", "compaction")], {
        finished: true,
      }),
    ).toEqual([{ kind: "activity", parts: [part("compact", "compaction")] }]);
  });
});
const tool = (
  id: string,
  name: string,
  extra: Partial<HarnessPart> = {},
): HarnessPart => part(id, "tool", { tool: name, ...extra });
const text = (id: string, output = id) => part(id, "text", { output });

describe("buildActivityBlocks", () => {
  it("groups every contiguous kind of work and splits runs only at reader content", () => {
    const blocks = buildActivityBlocks([
      part("think", "reasoning", { output: "Plan" }),
      tool("read", "read"),
      tool("bash", "bash", { state: "error" }),
      tool("edit", "edit", { call_id: "c1" }),
      part("patch", "patch", { call_id: "c1" }),
      part("sub", "subtask", { display: { child_session_id: "child" } }),
      part("step", "step-finish"),
      text("between"),
      tool("grep", "grep"),
      part("steer", "steer", { output: "Use pnpm" }),
      tool("question", "question", { input: { tool: "question" } }),
      tool("glob", "glob"),
    ]);

    expect(
      blocks.map((block) =>
        block.kind === "activity"
          ? block.parts.map((item) => item.id)
          : `${block.kind}:${block.part.id}`,
      ),
    ).toEqual([
      // The edit call is represented by its patch and the step is hidden.
      ["think", "read", "bash", "patch", "sub"],
      "text:between",
      ["grep"],
      "steer:steer",
      "question:question",
      ["glob"],
    ]);
  });

  it("renders the anchored to-do call in place and keeps other updates as work", () => {
    const blocks = buildActivityBlocks(
      [
        tool("todo-1", "todowrite"),
        tool("read", "read"),
        tool("todo-2", "todowrite"),
        tool("grep", "grep"),
      ],
      { todoPartId: "todo-2" },
    );

    expect(blocks.map((block) => block.kind)).toEqual([
      "activity",
      "todos",
      "activity",
    ]);
    expect(blocks[0]).toEqual({
      kind: "activity",
      parts: [expect.objectContaining({ id: "todo-1" }), expect.anything()],
    });
  });

  it("keeps compaction dividers standalone and hides open questions", () => {
    const blocks = buildActivityBlocks([
      part("compact", "compaction"),
      tool("read", "read"),
      tool("open", "question", { state: "running" }),
      tool("grep", "grep"),
    ]);

    expect(blocks).toEqual([
      { kind: "activity", parts: [expect.objectContaining({ id: "compact" })] },
      {
        kind: "activity",
        parts: [
          expect.objectContaining({ id: "read" }),
          expect.objectContaining({ id: "grep" }),
        ],
      },
    ]);
  });
});

describe("summarizeActivity", () => {
  it("counts exploration like the native activity label", () => {
    const parts = [
      ...Array.from({ length: 9 }, (_, index) => tool(`r${index}`, "read")),
      tool("g", "grep"),
      tool("w", "websearch"),
      tool("f", "webfetch"),
      tool("m", "mcp_notion_search"),
      part("think", "reasoning"),
    ];

    expect(summarizeActivity(parts)).toEqual({
      title: "Explored",
      detail: "9 files, 2 searches, 2 other tools",
      running: false,
    });
  });

  it("joins edits, commands, delegated tasks and failures", () => {
    expect(
      summarizeActivity([
        tool("read", "read"),
        part("patch", "patch"),
        tool("edit-failed", "edit", { state: "error" }),
        tool("bash", "bash"),
        part("sub", "subtask"),
        tool("todo", "todowrite"),
        tool("fetch", "webfetch"),
      ]),
    ).toEqual({
      title: "Explored",
      detail:
        "1 file, edited 2 files, ran 1 command, delegated 1 task, updated to-dos, 1 other tool, 1 failed",
      running: false,
    });
  });

  it("titles the first phrase and uses the progressive form while running", () => {
    expect(
      summarizeActivity([
        tool("bash-1", "bash"),
        tool("bash-2", "bash", { state: "running" }),
      ]),
    ).toEqual({ title: "Running", detail: "2 commands", running: true });
    expect(summarizeActivity([tool("x", "process_start")]).title).toBe("Used");
    expect(summarizeActivity([tool("x", "process_start")]).detail).toBe(
      "1 tool",
    );
  });

  it("labels runs without tools by their reasoning or compaction", () => {
    expect(summarizeActivity([part("think", "reasoning")])).toEqual({
      title: "Thought",
      detail: "",
      running: false,
    });
    expect(
      summarizeActivity([part("think", "reasoning", { state: "running" })])
        .title,
    ).toBe("Thinking");
    expect(summarizeActivity([part("compact", "compaction")]).title).toBe(
      "Compacted context",
    );
  });

  it("reads tool names of detail-less timeline parts from their projection", () => {
    expect(
      summarizeActivity([
        part("lazy", "tool", {
          detail_loaded: false,
          display: { tool: "read" },
        }),
      ]).detail,
    ).toBe("1 file");
  });
});

describe("findTodoAnchor", () => {
  const message = (
    id: string,
    role: HarnessMessage["role"],
    parts: HarnessPart[] = [],
  ): HarnessMessage => ({ id, session_id: "s1", role, content: "", parts });

  it("anchors at the latest successful to-do call across ordered messages", () => {
    expect(
      findTodoAnchor([
        message("a1", "assistant", [tool("old", "todowrite", { position: 1 })]),
        message("u1", "user"),
        message("a2", "assistant", [
          tool("late", "todowrite", { position: 5 }),
          tool("early", "todowrite", { position: 2 }),
          tool("failed", "todowrite", { position: 6, state: "error" }),
        ]),
        message("a3", "assistant", [text("no todos")]),
      ]),
    ).toEqual({ messageId: "a2", partId: "late" });
  });

  it("falls back to the latest assistant message without a to-do call", () => {
    expect(
      findTodoAnchor([
        message("a1", "assistant"),
        message("a2", "assistant", [text("plan arrived as an event")]),
        message("u1", "user"),
      ]),
    ).toEqual({ messageId: "a2", partId: null });
    expect(findTodoAnchor([message("u1", "user")])).toBeNull();
  });
});
