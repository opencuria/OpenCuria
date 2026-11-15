import type {
  HarnessMessage,
  HarnessSession,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import type {
  ConversationWorkspace,
  WorkspaceConversationGroup,
} from "@opencuria/chat-core/conversationGroups";
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
} from "@opencuria/chat-core/harnessSubtaskActivity";
import type { HarnessConversation } from "@opencuria/client";
import type { ChatControllerSnapshot } from "../state/chatController";
import type {
  OfflineAccount,
  SavedChat,
  SavedTranscript,
} from "./offlineStore";

/** A chat that is opened is saved soon, so it is there when the network goes. */
export const SAVE_DELAY_MS = 1_000;
/** A streaming reply is saved this often; its end is saved right away. */
export const STREAMING_SAVE_DELAY_MS = 10_000;

const NONE = Object.freeze([]) as unknown as never[];

const EMPTY: ChatControllerSnapshot = Object.freeze({
  sessions: NONE,
  messages: NONE,
  todos: NONE,
  permissions: NONE,
  questions: NONE,
  activeSessionId: null,
  activeSession: null,
  activeSessionLineage: NONE,
  workspaceId: null,
  loading: false,
  error: null,
  connected: false,
  busy: false,
});

/**
 * Saved chats as chat list rows, as they were saved. Offline nothing runs
 * or waits for an answer, and there is nothing to mark as read.
 */
export function savedConversations(chats: SavedChat[]): HarnessConversation[] {
  return chats.flatMap((chat) =>
    chat.conversation
      ? [
          {
            ...chat.conversation,
            status: "idle" as const,
            unread: false,
            manual_unread: false,
            needs_attention: false,
          },
        ]
      : [],
  );
}

/**
 * Offline, every workspace with saved chats is a group, whatever its state
 * was: newest chats first, groups by name.
 */
export function groupSavedConversations<W extends ConversationWorkspace>(
  workspaces: readonly W[],
  conversations: readonly HarnessConversation[],
): WorkspaceConversationGroup<W>[] {
  const groups = new Map<string, WorkspaceConversationGroup<W>>();
  const newestFirst = [...conversations].sort(
    (a, b) =>
      Date.parse(b.last_message_at) - Date.parse(a.last_message_at) ||
      a.session_id.localeCompare(b.session_id),
  );
  for (const conversation of newestFirst) {
    let group = groups.get(conversation.workspace_id);
    if (!group) {
      const workspace =
        workspaces.find((item) => item.id === conversation.workspace_id) ??
        null;
      group = {
        workspaceId: conversation.workspace_id,
        name:
          workspace?.name.trim() ||
          conversation.workspace_name.trim() ||
          `Workspace ${conversation.workspace_id.slice(0, 8)}`,
        workspace,
        online: false,
        conversations: [],
      };
      groups.set(conversation.workspace_id, group);
    }
    group.conversations.push(conversation);
  }
  return [...groups.values()].sort(
    (a, b) =>
      a.name.localeCompare(b.name, undefined, {
        sensitivity: "base",
        numeric: true,
      }) || a.workspaceId.localeCompare(b.workspaceId),
  );
}

/** A chat list row for a chat the list did not contain yet. */
export function conversationFromSession(
  session: HarnessSession,
  workspaceName: string,
): HarnessConversation {
  const at =
    session.updated_at ?? session.created_at ?? new Date().toISOString();
  return {
    session_id: session.id,
    harness_id: session.harness_id,
    workspace_id: session.workspace_id,
    workspace_name: workspaceName,
    title: session.title,
    status: "idle",
    mode: session.mode,
    agent_name: session.agent_name,
    model: session.model,
    reasoning_effort: session.reasoning_effort,
    unread: false,
    updated_at: at,
    last_message_at: at,
  };
}

function freezeTree<T>(value: T): T {
  if (!value || typeof value !== "object" || Object.isFrozen(value))
    return value;
  for (const child of Object.values(value as Record<string, unknown>))
    freezeTree(child);
  return Object.freeze(value);
}

function snapshotOf(transcript: SavedTranscript): ChatControllerSnapshot {
  // Offline nothing runs: a reply that was streaming when saved ends there.
  const idle = (session: HarnessSession): HarnessSession => ({
    ...session,
    status: "idle",
  });
  const session = idle(transcript.session);
  const sessions = transcript.sessions.some((item) => item.id === session.id)
    ? transcript.sessions.map(idle)
    : [session, ...transcript.sessions.map(idle)];
  return freezeTree({
    ...EMPTY,
    sessions,
    messages: transcript.messages,
    todos: transcript.todos,
    activeSessionId: session.id,
    activeSession: session,
    activeSessionLineage: collectAncestorSessions(session.id, sessions),
    workspaceId: session.workspace_id,
  });
}

/**
 * A saved chat in the shape of the live chat (`ChatController`), for reading
 * offline: nothing runs, nothing waits for an answer and nothing can be sent.
 * Snapshots follow React's `useSyncExternalStore` contract.
 */
export class SavedChatView {
  private readonly listeners = new Set<() => void>();
  private snapshot = EMPTY;
  private generation = 0;

  constructor(private readonly account: OfflineAccount | null) {}

  readonly getSnapshot = (): ChatControllerSnapshot => this.snapshot;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Show a saved chat, or a workspace without a chat when `sessionId` is missing. */
  async open(workspaceId: string, sessionId?: string): Promise<void> {
    const generation = ++this.generation;
    if (!sessionId) {
      this.publish({ ...EMPTY, workspaceId });
      return;
    }
    this.publish({
      ...EMPTY,
      workspaceId,
      activeSessionId: sessionId,
      loading: true,
    });
    const transcript = this.account
      ? await this.account.loadChat(sessionId).catch(() => null)
      : null;
    if (generation !== this.generation) return;
    if (!transcript) {
      this.publish({
        ...EMPTY,
        workspaceId,
        activeSessionId: sessionId,
        error: "This chat is not saved on this device.",
      });
      return;
    }
    // Reading a chat offline keeps it saved for another week.
    void this.account?.markOpened(sessionId).catch(() => undefined);
    this.publish(snapshotOf(transcript));
  }

  private publish(snapshot: ChatControllerSnapshot): void {
    this.snapshot = Object.freeze(snapshot);
    for (const listener of [...this.listeners]) listener();
  }
}

interface ChatSource {
  subscribe(listener: () => void): () => void;
  getSnapshot(): ChatControllerSnapshot;
}

interface Pending {
  transcript: SavedTranscript;
  conversation: HarnessConversation | null;
  timer: ReturnType<typeof setTimeout>;
}

/** The chat's own sessions: its parents for the path, its subtasks for their cards. */
function familyOf(
  session: HarnessSession,
  sessions: readonly HarnessSession[],
): HarnessSession[] {
  const ids = new Set([
    ...collectAncestorSessions(session.id, [...sessions]).map(
      (item) => item.id,
    ),
    ...collectDescendantSessionIds(session.id, [...sessions]),
  ]);
  const family = sessions.filter((item) => ids.has(item.id));
  return family.some((item) => item.id === session.id)
    ? family
    : [session, ...family];
}

/** Whether two transcripts differ; unchanged messages keep their identity. */
function sameMessages(
  left: readonly HarnessMessage[],
  right: readonly HarnessMessage[],
): boolean {
  return (
    left.length === right.length &&
    left.every((message, index) => message === right[index])
  );
}

function sameValue(
  left: HarnessSession | HarnessTodo[],
  right: HarnessSession | HarnessTodo[],
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

/**
 * Saves the chat the user is reading, as the live chat shows it, so it opens
 * offline later. A streaming reply is saved now and then; switching chats or
 * stopping saves what is pending at once.
 */
export class SavedChatWriter {
  private pending: Pending | null = null;
  private saved: {
    sessionId: string;
    session: HarnessSession;
    messages: readonly HarnessMessage[];
    todos: HarnessTodo[];
  } | null = null;

  constructor(
    private readonly source: ChatSource,
    private readonly account: OfflineAccount,
    /** The chat's list row; null for subtasks. */
    private readonly conversation: (
      session: HarnessSession,
    ) => HarnessConversation | null,
  ) {}

  /** Follow the live chat; returns the stop function, which saves what is pending. */
  start(): () => void {
    const unsubscribe = this.source.subscribe(() => this.observe());
    this.observe();
    return () => {
      unsubscribe();
      this.flush();
    };
  }

  private observe(): void {
    const chat = this.source.getSnapshot();
    const session = chat.activeSession;
    if (
      chat.loading ||
      !session ||
      chat.workspaceId !== session.workspace_id ||
      !chat.messages.length
    )
      return;
    const saved = this.saved;
    const opened = saved?.sessionId !== session.id;
    if (
      !opened &&
      sameMessages(saved!.messages, chat.messages) &&
      sameValue(saved!.todos, chat.todos) &&
      sameValue(saved!.session, session)
    )
      return;
    this.saved = {
      sessionId: session.id,
      session,
      messages: chat.messages,
      todos: chat.todos,
    };
    if (this.pending && this.pending.transcript.session.id !== session.id)
      this.flush();
    const transcript: SavedTranscript = {
      session,
      sessions: familyOf(session, chat.sessions),
      messages: chat.messages,
      todos: chat.todos,
    };
    const conversation = session.parent_id ? null : this.conversation(session);
    if (this.pending) {
      // Waiting already: the save takes the newest state when it runs.
      this.pending = { ...this.pending, transcript, conversation };
      if (!chat.busy) {
        clearTimeout(this.pending.timer);
        this.pending.timer = setTimeout(() => this.flush(), SAVE_DELAY_MS);
      }
      return;
    }
    this.pending = {
      transcript,
      conversation,
      // A chat just opened is saved soon even while its reply streams.
      timer: setTimeout(
        () => this.flush(),
        chat.busy && !opened ? STREAMING_SAVE_DELAY_MS : SAVE_DELAY_MS,
      ),
    };
  }

  private flush(): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    void this.account
      .saveChat(pending.transcript, pending.conversation)
      .catch(() => undefined);
  }
}
