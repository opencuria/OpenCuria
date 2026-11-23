import { describe, expect, it } from "vitest";

import {
  buildComposerSheets,
  optionLetter,
  runNotice,
  type RunNoticeMessage,
} from "../src/composerSheets";
import type {
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessScheduledMessage,
  HarnessSteer,
  HarnessTodo,
} from "../src/harness";

function makeQuestion(id: string): HarnessQuestionRequest {
  return {
    request_id: id,
    session_id: "session-1",
    workspace_id: "ws-1",
    questions: [{ question: `Question ${id}` }],
  };
}

function makePermission(id: string): HarnessPermissionRequest {
  return {
    request_id: id,
    session_id: "session-1",
    workspace_id: "ws-1",
    tool: "bash",
    pattern: "ls",
    title: "Run ls",
  };
}

function makeTodo(id: string): HarnessTodo {
  return { id, content: id, status: "pending", priority: "medium", order: 0 };
}

function makeSteer(id: string): HarnessSteer {
  return { id, content: `Steer ${id}`, created_at: "2026-10-16T10:00:00Z" };
}

function makeScheduled(id: string, sendAt: string): HarnessScheduledMessage {
  return {
    id,
    session_id: "session-1",
    content: `Later ${id}`,
    send_at: sendAt,
    status: "pending",
    created_at: "2026-10-16T10:00:00Z",
  };
}

describe("buildComposerSheets", () => {
  it("orders sheets by priority: mention > processes > question > permission > notice > context > todos", () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo("t1")],
      permissions: [makePermission("p1")],
      questions: [makeQuestion("q1")],
      notice: { messageId: "msg-1", text: "Run stopped by user", tone: "info" },
      mention: {
        candidates: [{ kind: "file", label: "a.ts", insert: "file:a.ts" }],
        activeIndex: 0,
      },
      processesOpen: true,
      contextOpen: true,
      context: { used: 1000, limit: 10_000, percent: 10 },
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      "mention",
      "processes",
      "question",
      "permission",
      "notice",
      "context",
      "todos",
    ]);
  });

  it("puts user-opened processes above agent gates and notices", () => {
    const sheets = buildComposerSheets({
      permissions: [makePermission("p1")],
      questions: [makeQuestion("q1")],
      notice: { messageId: "msg-1", text: "Run stopped by user", tone: "info" },
      processesOpen: true,
      todos: [makeTodo("t1")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      "processes",
      "question",
      "permission",
      "notice",
      "todos",
    ]);
  });

  it("omits the context sheet when it is closed", () => {
    const sheets = buildComposerSheets({
      contextOpen: false,
      context: { used: 1000, limit: 10_000, percent: 10 },
      todos: [makeTodo("t1")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["todos"]);
  });

  it("omits the processes sheet when it is closed", () => {
    const sheets = buildComposerSheets({
      processesOpen: false,
      todos: [makeTodo("t1")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["todos"]);
  });

  it("orders sheets by priority: mention > processes > question > permission > todos when context is closed", () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo("t1")],
      permissions: [makePermission("p1")],
      questions: [makeQuestion("q1")],
      processesOpen: true,
      mention: {
        candidates: [{ kind: "file", label: "a.ts", insert: "file:a.ts" }],
        activeIndex: 0,
      },
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      "mention",
      "processes",
      "question",
      "permission",
      "todos",
    ]);
  });

  it("includes a notice sheet when a run error is present", () => {
    const sheets = buildComposerSheets({
      notice: { messageId: "msg-1", text: "boom", tone: "error" },
      todos: [makeTodo("t1")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["notice", "todos"]);
    expect(sheets[0]?.notice?.text).toBe("boom");
  });

  it("skips empty sources instead of rendering placeholder sheets", () => {
    const sheets = buildComposerSheets({
      mention: { candidates: [], activeIndex: 0 },
      questions: [],
      permissions: [],
      todos: [makeTodo("t1")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["todos"]);
  });

  it("keeps multiple pending permissions on one sheet for the pager", () => {
    const sheets = buildComposerSheets({
      permissions: [makePermission("p1"), makePermission("p2")],
    });

    expect(sheets).toHaveLength(1);
    expect(sheets[0]?.kind).toBe("permission");
    expect(sheets[0]?.permissions).toHaveLength(2);
    expect(sheets[0]?.permission?.request_id).toBe("p1");
  });
});

describe("buildComposerSheets queue", () => {
  it("lets gates win the primary slot and ranks the queue above notices and todos", () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo("t1")],
      notice: { messageId: "msg-1", text: "Run stopped by user", tone: "info" },
      queue: [makeSteer("s1")],
      permissions: [makePermission("p1")],
      questions: [makeQuestion("q1")],
      processesOpen: true,
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      "processes",
      "question",
      "permission",
      "queue",
      "notice",
      "todos",
    ]);
  });

  it("shows the queue on top of todos while nothing else needs attention", () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo("t1")],
      queue: [makeSteer("s1"), makeSteer("s2")],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["queue", "todos"]);
    expect(sheets[0]?.queue?.map((steer) => steer.id)).toEqual(["s1", "s2"]);
  });

  it("contributes no sheet for an empty or absent queue", () => {
    expect(buildComposerSheets({ queue: [] })).toEqual([]);
    expect(buildComposerSheets({ queue: [], scheduled: [] })).toEqual([]);
    expect(buildComposerSheets({})).toEqual([]);
  });

  it("carries scheduled messages in the queue sheet, earliest first", () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo("t1")],
      scheduled: [
        makeScheduled("late", "2026-10-18T09:00:00Z"),
        makeScheduled("soon", "2026-10-16T11:00:00Z"),
      ],
    });

    expect(sheets.map((sheet) => sheet.kind)).toEqual(["queue", "todos"]);
    expect(sheets[0]?.queue).toEqual([]);
    expect(sheets[0]?.scheduled?.map((message) => message.id)).toEqual([
      "soon",
      "late",
    ]);
  });
});

function makeMessage(
  id: string,
  role: RunNoticeMessage["role"],
  fields: Partial<RunNoticeMessage> = {},
): RunNoticeMessage {
  return { id, role, ...fields };
}

describe("runNotice", () => {
  it("reports the newest run error after the last prompt", () => {
    const notice = runNotice([
      makeMessage("u1", "user"),
      makeMessage("a1", "assistant", { error: "Old failure" }),
      makeMessage("a2", "assistant", {
        finish: "error",
        error: "Rate limited",
      }),
    ]);

    expect(notice).toEqual({
      messageId: "a2",
      text: "Rate limited",
      tone: "error",
    });
  });

  it("reads a stop by the user as information", () => {
    const notice = runNotice([
      makeMessage("u1", "user"),
      makeMessage("a1", "assistant", {
        finish: "aborted",
        error: "aborted by user",
      }),
    ]);

    expect(notice).toEqual({
      messageId: "a1",
      text: "Run stopped by user",
      tone: "info",
    });
  });

  it("clears once a newer prompt follows the failed run", () => {
    expect(
      runNotice([
        makeMessage("a1", "assistant", { error: "Rate limited" }),
        makeMessage("u2", "user"),
        makeMessage("a2", "assistant"),
      ]),
    ).toBeNull();
  });

  it("skips notices dismissed on the server or optimistically", () => {
    const messages = [
      makeMessage("u1", "user"),
      makeMessage("a1", "assistant", { error: "First" }),
      makeMessage("a2", "assistant", {
        error: "Second",
        notice_dismissed_at: "2026-11-23T10:00:00Z",
      }),
    ];

    expect(runNotice(messages)?.messageId).toBe("a1");
    expect(runNotice(messages, { a1: true })).toBeNull();
  });
});

describe("optionLetter", () => {
  it("labels the first options A through E", () => {
    expect([0, 1, 2, 3, 4].map(optionLetter)).toEqual([
      "A",
      "B",
      "C",
      "D",
      "E",
    ]);
  });

  it("continues past Z with double letters", () => {
    expect(optionLetter(25)).toBe("Z");
    expect(optionLetter(26)).toBe("AA");
  });
});
