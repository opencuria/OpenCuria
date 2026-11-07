import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  HarnessMessage,
  HarnessSession,
} from "@opencuria/chat-core/harness";
import type { HarnessConversation, Workspace } from "@opencuria/client";
import type { ChatControllerSnapshot } from "../state/chatController";
import type { OfflineAccount, SavedTranscript } from "./offlineStore";
import {
  SAVE_DELAY_MS,
  STREAMING_SAVE_DELAY_MS,
  SavedChatView,
  SavedChatWriter,
  conversationFromSession,
  groupSavedConversations,
  savedConversations,
} from "./savedChats";

function session(
  id: string,
  extra: Partial<HarnessSession> = {},
): HarnessSession {
  return {
    id,
    workspace_id: "ws-1",
    parent_id: null,
    title: `Chat ${id}`,
    mode: "build",
    agent_name: "build",
    model: "m",
    status: "idle",
    cost: 0,
    tokens: {},
    ...extra,
  };
}

function message(id: string, content = id): HarnessMessage {
  return {
    id,
    session_id: "s1",
    role: "assistant",
    content,
    parts: [],
  } as HarnessMessage;
}

function conversation(
  id: string,
  extra: Partial<HarnessConversation> = {},
): HarnessConversation {
  return {
    session_id: id,
    workspace_id: "ws-1",
    workspace_name: "dev",
    title: `Chat ${id}`,
    status: "idle",
    mode: "build",
    agent_name: "build",
    model: "m",
    unread: false,
    updated_at: "2026-11-07T10:00:00Z",
    last_message_at: "2026-11-07T10:00:00Z",
    ...extra,
  };
}

function account(transcripts: Record<string, SavedTranscript> = {}) {
  return {
    loadChat: vi.fn(async (id: string) => transcripts[id] ?? null),
    markOpened: vi.fn(async () => undefined),
    saveChat: vi.fn(async () => undefined),
  };
}

describe("saved chat list", () => {
  it("lists saved chats without live states and groups them by workspace", () => {
    const rows = savedConversations([
      {
        sessionId: "s1",
        workspaceId: "ws-1",
        conversation: conversation("s1", {
          status: "busy",
          unread: true,
          needs_attention: true,
        }),
        openedAt: "",
        savedAt: "",
      },
      {
        sessionId: "child",
        workspaceId: "ws-1",
        conversation: null,
        openedAt: "",
        savedAt: "",
      },
    ]);
    expect(rows).toEqual([
      expect.objectContaining({
        session_id: "s1",
        status: "idle",
        unread: false,
        needs_attention: false,
      }),
    ]);

    const stopped = { id: "ws-2", name: "Stopped", status: "stopped" };
    const groups = groupSavedConversations(
      [stopped as Workspace],
      [
        conversation("a", { workspace_id: "ws-2" }),
        conversation("b", {
          workspace_id: "ws-3",
          workspace_name: "Archive",
          last_message_at: "2026-11-07T11:00:00Z",
        }),
        conversation("c", {
          workspace_id: "ws-3",
          workspace_name: "Archive",
          last_message_at: "2026-11-07T12:00:00Z",
        }),
      ],
    );
    expect(
      groups.map((group) => [
        group.name,
        group.online,
        group.conversations.map((row) => row.session_id),
      ]),
    ).toEqual([
      ["Archive", false, ["c", "b"]],
      ["Stopped", false, ["a"]],
    ]);
  });

  it("makes a list row for a chat the list did not know yet", () => {
    expect(
      conversationFromSession(
        session("s1", { updated_at: "2026-11-07T09:00:00Z" }),
        "dev",
      ),
    ).toMatchObject({
      session_id: "s1",
      workspace_id: "ws-1",
      workspace_name: "dev",
      title: "Chat s1",
      status: "idle",
      last_message_at: "2026-11-07T09:00:00Z",
    });
  });
});

describe("SavedChatView", () => {
  it("shows a saved chat as it was, with nothing running", async () => {
    const saved = account({
      s1: {
        session: session("s1", { status: "busy" }),
        sessions: [
          session("root"),
          session("s1", { status: "busy", parent_id: "root" }),
        ],
        messages: [message("m1")],
        todos: [],
      },
    });
    const view = new SavedChatView(saved as unknown as OfflineAccount);
    const listener = vi.fn();
    view.subscribe(listener);

    const opening = view.open("ws-1", "s1");
    expect(view.getSnapshot()).toMatchObject({
      loading: true,
      activeSessionId: "s1",
      workspaceId: "ws-1",
    });
    await opening;

    const chat = view.getSnapshot();
    expect(chat).toMatchObject({
      loading: false,
      busy: false,
      connected: false,
      error: null,
      activeSessionId: "s1",
      activeSession: { id: "s1", status: "idle" },
      workspaceId: "ws-1",
      permissions: [],
      questions: [],
    });
    expect(chat.messages.map((row) => row.id)).toEqual(["m1"]);
    expect(chat.activeSessionLineage.map((row) => row.id)).toEqual([
      "root",
      "s1",
    ]);
    expect(Object.isFrozen(chat.messages)).toBe(true);
    expect(saved.markOpened).toHaveBeenCalledWith("s1");
    expect(listener).toHaveBeenCalled();
  });

  it("says when a chat is not saved and shows an empty workspace", async () => {
    const view = new SavedChatView(account() as unknown as OfflineAccount);
    await view.open("ws-1", "missing");
    expect(view.getSnapshot()).toMatchObject({
      activeSessionId: "missing",
      activeSession: null,
      error: "This chat is not saved on this device.",
    });
    await view.open("ws-2");
    expect(view.getSnapshot()).toMatchObject({
      workspaceId: "ws-2",
      activeSessionId: null,
      error: null,
      messages: [],
    });
  });

  it("keeps only the chat opened last", async () => {
    let finish: (value: SavedTranscript | null) => void = () => undefined;
    const saved = {
      loadChat: vi
        .fn()
        .mockImplementationOnce(
          () => new Promise((resolve) => (finish = resolve)),
        )
        .mockResolvedValue(null),
      markOpened: vi.fn(async () => undefined),
    };
    const view = new SavedChatView(saved as unknown as OfflineAccount);
    const first = view.open("ws-1", "s1");
    await view.open("ws-1", "s2");
    finish({
      session: session("s1"),
      sessions: [],
      messages: [message("m1")],
      todos: [],
    });
    await first;
    expect(view.getSnapshot().activeSessionId).toBe("s2");
  });
});

describe("SavedChatWriter", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function source(initial: Partial<ChatControllerSnapshot>) {
    const listeners = new Set<() => void>();
    let snapshot = {
      sessions: [],
      messages: [],
      todos: [],
      permissions: [],
      questions: [],
      activeSessionId: null,
      activeSession: null,
      activeSessionLineage: [],
      workspaceId: "ws-1",
      loading: false,
      error: null,
      connected: true,
      busy: false,
      ...initial,
    } as ChatControllerSnapshot;
    return {
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      getSnapshot: () => snapshot,
      update(next: Partial<ChatControllerSnapshot>) {
        snapshot = { ...snapshot, ...next };
        listeners.forEach((listener) => listener());
      },
    };
  }

  it("saves the chat being read soon after it opens, with its list row", () => {
    const s1 = session("s1");
    const chat = source({
      activeSessionId: "s1",
      activeSession: s1,
      sessions: [s1, session("other")],
      messages: [message("m1")],
    });
    const saved = account();
    const row = conversation("s1");
    const stop = new SavedChatWriter(
      chat,
      saved as unknown as OfflineAccount,
      () => row,
    ).start();

    expect(saved.saveChat).not.toHaveBeenCalled();
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledTimes(1);
    expect(saved.saveChat).toHaveBeenCalledWith(
      {
        session: s1,
        sessions: [s1],
        messages: [message("m1")],
        todos: [],
      },
      row,
    );

    // Unchanged content (a new snapshot with the same rows) is not saved again.
    chat.update({ connected: false });
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledTimes(1);
    stop();
  });

  it("saves a streaming reply now and then and switching chats at once", () => {
    const s1 = session("s1");
    const first = [message("m1")];
    const chat = source({
      activeSessionId: "s1",
      activeSession: s1,
      sessions: [s1],
      messages: first,
      busy: true,
    });
    const saved = account();
    const stop = new SavedChatWriter(
      chat,
      saved as unknown as OfflineAccount,
      () => null,
    ).start();
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledTimes(1);

    chat.update({ messages: [first[0]!, message("m2")] });
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(STREAMING_SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledTimes(2);

    chat.update({ messages: [first[0]!, message("m2", "more")] });
    const s2 = session("s2");
    chat.update({
      activeSessionId: "s2",
      activeSession: s2,
      sessions: [s2],
      messages: [message("x1")],
      busy: false,
    });
    // The pending save of the previous chat ran right away.
    expect(saved.saveChat).toHaveBeenCalledTimes(3);
    expect(saved.saveChat.mock.calls[2]![0]).toMatchObject({
      session: { id: "s1" },
    });

    // Stopping saves what is pending.
    stop();
    expect(saved.saveChat).toHaveBeenCalledTimes(4);
    expect(saved.saveChat.mock.calls[3]![0]).toMatchObject({
      session: { id: "s2" },
    });
  });

  it("does not save a chat that is still loading or empty, and no row for subtasks", () => {
    const child = session("child", { parent_id: "s1" });
    const chat = source({
      activeSessionId: "child",
      activeSession: child,
      sessions: [child],
      messages: [],
    });
    const saved = account();
    const describe = vi.fn(() => conversation("child"));
    const stop = new SavedChatWriter(
      chat,
      saved as unknown as OfflineAccount,
      describe,
    ).start();
    chat.update({ loading: true, messages: [message("m1")] });
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).not.toHaveBeenCalled();

    chat.update({ loading: false });
    vi.advanceTimersByTime(SAVE_DELAY_MS);
    expect(saved.saveChat).toHaveBeenCalledWith(
      expect.objectContaining({ session: child }),
      null,
    );
    expect(describe).not.toHaveBeenCalled();
    stop();
  });
});
