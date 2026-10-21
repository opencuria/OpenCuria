import { describe, expect, it, vi } from "vitest";
import type { OpenCuriaClient } from "@opencuria/client";
import type {
  HarnessMessage,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
} from "@opencuria/chat-core/harness";
import { ChatController } from "./chatController";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function session(
  id: string,
  options: Partial<HarnessSession> = {},
): HarnessSession {
  return {
    id,
    workspace_id: options.workspace_id ?? "workspace-a",
    parent_id: options.parent_id ?? null,
    harness_id: "native",
    title: id,
    mode: "build",
    agent_name: options.agent_name ?? "build",
    model: options.model ?? "model-v1",
    reasoning_effort: "medium",
    status: options.status ?? "idle",
    unread: options.unread ?? false,
    manual_unread: options.manual_unread ?? false,
    cost: 0,
    tokens: {},
    ...options,
  };
}

function message(
  id: string,
  role: "user" | "assistant",
  content: string,
): HarnessMessage {
  return {
    id,
    session_id: "root",
    role,
    content,
    parts: [],
    created_at: `2026-01-01T00:00:0${id === "u1" ? "1" : "2"}.000Z`,
  };
}

interface TimelineFixture {
  session: HarnessSession;
  messages: HarnessMessage[];
  permissions?: HarnessPermissionRequest[];
  questions?: HarnessQuestionRequest[];
}

function timeline(
  owner: HarnessSession,
  messages: HarnessMessage[] = [],
  permissions: HarnessPermissionRequest[] = [],
  questions: HarnessQuestionRequest[] = [],
): TimelineFixture {
  return { session: owner, messages, permissions, questions };
}

class FakeRealtime {
  isConnected = false;
  readonly handlers = new Map<string, Set<(payload: any) => void>>();
  readonly releases = vi.fn();
  readonly subscribeWorkspace = vi.fn(() => this.releases);
  readonly onReconnect = vi.fn((callback: () => void) => {
    this.reconnect = callback;
    return () => {
      this.reconnect = null;
    };
  });
  reconnect: (() => void) | null = null;

  on(event: string, callback: (payload: any) => void): () => void {
    const callbacks = this.handlers.get(event) ?? new Set();
    callbacks.add(callback);
    this.handlers.set(event, callbacks);
    return () => callbacks.delete(callback);
  }

  emit(event: string, payload: unknown): void {
    for (const callback of this.handlers.get(event) ?? []) callback(payload);
  }
}

function makeClient(initialSessions: HarnessSession[] = []) {
  const realtime = new FakeRealtime();
  const rows = new Map(initialSessions.map((item) => [item.id, item]));
  const harnessApi = {
    listSessions: vi.fn(async (workspaceId: string) =>
      [...rows.values()].filter((row) => row.workspace_id === workspaceId),
    ),
    createSession: vi.fn(async (workspaceId: string, input: any) => {
      const result = session(`created-${rows.size + 1}`, {
        workspace_id: workspaceId,
        status: "busy",
        model: input.model,
      });
      rows.set(result.id, result);
      return result;
    }),
    send: vi.fn(async (id: string) => rows.get(id)!),
    timeline: vi.fn(async (id: string) =>
      timeline(rows.get(id) ?? session(id)),
    ),
    getPart: vi.fn(async () => ({
      id: "detail-part",
      session_id: "root",
      type: "tool",
      state: "completed",
      title: "Detail",
      output: "body",
      input: {},
      detail_loaded: true,
    })),
    abort: vi.fn(async (id: string) => rows.get(id)!),
    edit: vi.fn(async (id: string) => rows.get(id)!),
    fork: vi.fn(async (id: string) => {
      const source = rows.get(id)!;
      const result = session(`${id}-fork`, {
        workspace_id: source.workspace_id,
        status: "idle",
      });
      rows.set(result.id, result);
      return result;
    }),
    rename: vi.fn(async (id: string, title: string) => ({
      ...rows.get(id)!,
      title,
    })),
    deleteSession: vi.fn(async (id: string) => {
      rows.delete(id);
    }),
    markRead: vi.fn(async () => undefined),
    markUnread: vi.fn(async () => undefined),
    dismissNotice: vi.fn(async () => undefined),
    resolvePermission: vi.fn(async () => ({ decision: "once" })),
    resolveQuestion: vi.fn(async () => ({ status: "answered" })),
  };
  const api = { get: vi.fn(async () => [] as any[]) };
  return {
    client: {
      harness: harnessApi,
      api,
      realtime,
    } as unknown as OpenCuriaClient,
    realtime,
    rows,
  };
}

async function openRoot(controller: ChatController): Promise<void> {
  await controller.openWorkspace("workspace-a");
  await controller.openSession("root");
}

describe("ChatController", () => {
  it("resumes Codex and permits same-provider send/edit while rejecting engine or provider changes", async () => {
    const root = session("root", {
      harness_id: "codex",
      model: "chatgpt/gpt-6.1-sol",
      reasoning_effort: "low",
    });
    const { client } = makeClient([root]);
    const controller = new ChatController(client);
    await openRoot(controller);
    expect(controller.getSnapshot().activeSession?.harness_id).toBe("codex");
    await expect(
      controller.send("wrong engine", { harnessId: "claude" }),
    ).rejects.toThrow("harness cannot be changed");
    await expect(
      controller.send("wrong provider", { model: "openrouter/vendor/model" }),
    ).rejects.toThrow("provider cannot be changed");
    await expect(
      controller.edit("u1", "wrong provider", {
        model: "openrouter/vendor/model",
      }),
    ).rejects.toThrow("provider cannot be changed");
    expect(client.harness.send).not.toHaveBeenCalled();
    expect(client.harness.edit).not.toHaveBeenCalled();
    await controller.send("continue", {
      harnessId: "codex",
      model: "chatgpt/gpt-6.1-sol",
      reasoningEffort: "ultra",
    });
    await controller.edit("u1", "edit", {
      harnessId: "codex",
      model: "chatgpt/gpt-6.1-sol",
      reasoningEffort: "low",
    });
    expect(client.harness.edit).toHaveBeenCalledWith(
      "root",
      "u1",
      expect.objectContaining({
        model: "chatgpt/gpt-6.1-sol",
        reasoning_effort: "low",
      }),
    );
    controller.dispose();
  });

  it("ignores late workspace responses after a workspace switch", async () => {
    const a = deferred<HarnessSession[]>();
    const b = deferred<HarnessSession[]>();
    const { client } = makeClient();
    vi.mocked(client.harness.listSessions)
      .mockImplementationOnce(() => a.promise)
      .mockImplementationOnce(() => b.promise);
    const controller = new ChatController(client);

    const openingA = controller.openWorkspace("workspace-a");
    const openingB = controller.openWorkspace("workspace-b");
    b.resolve([session("b", { workspace_id: "workspace-b" })]);
    await openingB;
    a.resolve([session("a")]);
    await openingA;

    expect(controller.getSnapshot().workspaceId).toBe("workspace-b");
    expect(controller.getSnapshot().sessions.map((row) => row.id)).toEqual([
      "b",
    ]);
    controller.dispose();
  });

  it("reduces streamed text/tool deltas in order without mutating prior snapshots", async () => {
    const root = session("root");
    const { client, realtime } = makeClient([root]);
    vi.mocked(client.harness.timeline).mockResolvedValue(timeline(root));
    const controller = new ChatController(client);
    await openRoot(controller);
    realtime.emit("harness.session_status", {
      workspace_id: "workspace-a",
      session_id: "root",
      status: "busy",
      message_id: "assistant-1",
    });
    const before = controller.getSnapshot();
    realtime.emit("harness.part_updated", {
      workspace_id: "workspace-a",
      session_id: "root",
      message_id: "assistant-1",
      part_id: "text-1",
      part_position: 1,
      delta: { text: "Hello" },
    });
    const afterText = controller.getSnapshot();
    realtime.emit("harness.part_updated", {
      workspace_id: "workspace-a",
      session_id: "root",
      message_id: "assistant-1",
      part_id: "tool-1",
      part_position: 2,
      delta: { tool_started: "shell", state: "running" },
    });
    const afterTool = controller.getSnapshot();

    expect(before.messages[0]?.content).toBe("");
    expect(before.messages[0]?.parts).toHaveLength(0);
    expect(afterText.messages[0]?.content).toBe("Hello");
    expect(afterTool.messages[0]?.parts.map((part) => part.id)).toEqual([
      "text-1",
      "tool-1",
    ]);
    expect(afterText.messages[0]?.parts).toHaveLength(1);
    expect(controller.getSnapshot()).toBe(afterTool);
    controller.dispose();
  });

  it("refetches the timeline only for patch deltas without a preview", async () => {
    const root = session("root");
    const { client, realtime } = makeClient([root]);
    vi.mocked(client.harness.timeline).mockResolvedValue(timeline(root));
    const controller = new ChatController(client);
    await openRoot(controller);
    const event = {
      workspace_id: "workspace-a",
      session_id: "root",
      message_id: "assistant-1",
    };
    realtime.emit("harness.session_status", { ...event, status: "busy" });
    const loads = vi.mocked(client.harness.timeline).mock.calls.length;

    realtime.emit("harness.part_updated", {
      ...event,
      part_id: "patch-1",
      delta: { patch: "a.ts", display: { path: "a.ts", preview: [] } },
    });
    expect(client.harness.timeline).toHaveBeenCalledTimes(loads);
    expect(controller.getSnapshot().messages[0]?.parts[0]?.display?.path).toBe(
      "a.ts",
    );

    realtime.emit("harness.part_updated", {
      ...event,
      part_id: "patch-2",
      delta: { patch: "b.ts" },
    });
    await vi.waitFor(() =>
      expect(client.harness.timeline).toHaveBeenCalledTimes(loads + 1),
    );
    controller.dispose();
  });

  it("loads child session timelines and exposes child gates with parent breadcrumbs", async () => {
    const root = session("root");
    const child = session("child", {
      parent_id: "root",
      agent_name: "explore",
    });
    const permission: HarnessPermissionRequest = {
      request_id: "gate-1",
      session_id: "child",
      workspace_id: "workspace-a",
      tool: "shell",
      pattern: "rm -rf /tmp",
      title: "Clean temporary files",
      status: "pending",
    };
    const question: HarnessQuestionRequest = {
      request_id: "question-1",
      session_id: "child",
      workspace_id: "workspace-a",
      questions: [{ question: "Which branch?" }],
      status: "pending",
    };
    const { client } = makeClient([root, child]);
    vi.mocked(client.harness.timeline).mockImplementation(async (id) =>
      timeline(
        id === "root" ? root : child,
        [],
        id === "child" ? [permission] : [],
        id === "child" ? [question] : [],
      ),
    );
    const controller = new ChatController(client);
    await openRoot(controller);

    expect(client.harness.timeline).toHaveBeenCalledWith("child");
    expect(
      controller.getSnapshot().activeSessionLineage.map((row) => row.id),
    ).toEqual(["root"]);
    expect(
      controller.getSnapshot().permissions.map((row) => row.request_id),
    ).toEqual(["gate-1"]);
    expect(
      controller.getSnapshot().questions.map((row) => row.request_id),
    ).toEqual(["question-1"]);
    controller.dispose();
  });

  it("waits for an in-flight busy timeline before taking the post-idle snapshot", async () => {
    const root = session("root", { status: "busy" });
    const first = deferred<TimelineFixture>();
    const final = timeline(root, [
      message("answer", "assistant", "final answer"),
    ]);
    const { client, realtime } = makeClient([root]);
    vi.mocked(client.harness.timeline)
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(final);
    const controller = new ChatController(client);
    await controller.openWorkspace("workspace-a");
    const opening = controller.openSession("root");
    await Promise.resolve();
    realtime.emit("harness.session_status", {
      workspace_id: "workspace-a",
      session_id: "root",
      status: "idle",
    });
    first.resolve(timeline(root));
    await opening;
    await vi.waitFor(() =>
      expect(client.harness.timeline).toHaveBeenCalledTimes(2),
    );
    await vi.waitFor(() =>
      expect(controller.getSnapshot().messages[0]?.content).toBe(
        "final answer",
      ),
    );
    controller.dispose();
  });

  it("reconciles the current workspace after a realtime reconnect", async () => {
    const root = session("root");
    const { client, realtime } = makeClient([root]);
    const controller = new ChatController(client);
    await openRoot(controller);
    vi.mocked(client.harness.listSessions).mockResolvedValue([
      session("root", { title: "Updated title" }),
    ]);
    realtime.reconnect?.();
    await vi.waitFor(() =>
      expect(controller.getSnapshot().activeSession?.title).toBe(
        "Updated title",
      ),
    );
    expect(client.harness.timeline).toHaveBeenCalledTimes(2);
    controller.dispose();
  });

  it("releases realtime resources on dispose and ignores late responses", async () => {
    const pending = deferred<HarnessSession[]>();
    const { client, realtime } = makeClient();
    vi.mocked(client.harness.listSessions).mockReturnValue(pending.promise);
    const controller = new ChatController(client);
    let notifications = 0;
    controller.subscribe(() => {
      notifications += 1;
    });
    const opening = controller.openWorkspace("workspace-a");
    controller.dispose();
    const disposedNotifications = notifications;
    pending.resolve([session("late")]);
    await opening;

    expect(realtime.releases).toHaveBeenCalledOnce();
    expect(notifications).toBe(disposedNotifications);
    expect(controller.getSnapshot().sessions).toEqual([]);
  });

  it("keeps failed-send transcripts server-owned and does not add a duplicate prompt", async () => {
    const root = session("root");
    const { client } = makeClient([root]);
    vi.mocked(client.harness.timeline).mockResolvedValue(
      timeline(root, [message("u1", "user", "existing")]),
    );
    const controller = new ChatController(client);
    await openRoot(controller);
    const before = controller.getSnapshot().messages;
    vi.mocked(client.harness.send).mockRejectedValueOnce(
      new Error("network timeout"),
    );

    await expect(controller.send("possibly persisted")).rejects.toThrow(
      "network timeout",
    );
    expect(controller.getSnapshot().messages).toEqual(before);
    expect(
      controller
        .getSnapshot()
        .messages.some((row) => row.content === "possibly persisted"),
    ).toBe(false);
    expect(controller.getSnapshot().error).toBe("network timeout");
    controller.dispose();
  });

  it("forwards edit options and forks including a selected assistant answer", async () => {
    const root = session("root");
    const answer = {
      ...message("a1", "assistant", "answer"),
      completed_at: "2026-01-01T00:01:00Z",
    };
    const { client } = makeClient([root]);
    vi.mocked(client.harness.timeline).mockResolvedValue(
      timeline(root, [message("u1", "user", "prompt"), answer]),
    );
    const controller = new ChatController(client);
    await openRoot(controller);

    await controller.edit("u1", "edited prompt", {
      mode: "plan",
      model: "model-v2",
      reasoningEffort: "high",
      skillIds: ["skill-1"],
    });
    expect(client.harness.edit).toHaveBeenCalledWith("root", "u1", {
      prompt: "edited prompt",
      mode: "plan",
      model: "model-v2",
      reasoning_effort: "high",
      skill_ids: ["skill-1"],
    });
    await controller.fork("a1");
    expect(client.harness.fork).toHaveBeenCalledWith("root", {
      message_id: "a1",
      include_message: true,
    });
    expect(controller.getSnapshot().activeSessionId).toBe("root-fork");
    controller.dispose();
  });

  it("does not mark manual-unread sessions as read when opened", async () => {
    const root = session("root", { manual_unread: true, unread: true });
    const { client } = makeClient([root]);
    const controller = new ChatController(client);
    await openRoot(controller);
    expect(client.harness.markRead).not.toHaveBeenCalled();
    controller.dispose();
  });
});
