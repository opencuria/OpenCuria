/**
 * Framework-neutral owner for one mobile harness chat view.
 *
 * `subscribe` and `getSnapshot` follow React's `useSyncExternalStore` contract:
 * snapshots are referentially stable until the controller publishes a change.
 * Stream updates copy only the affected message and its parts, so a previous
 * snapshot is never mutated and unrelated transcript rows retain their identity.
 */

import type { OpenCuriaClient } from "@opencuria/client";
import type {
  HarnessId,
  HarnessMessage,
  HarnessPart,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSessionMode,
  HarnessTodo,
  HarnessPartUpdatedEvent,
  HarnessSessionStatusEvent,
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodoUpdatedEvent,
  HarnessPermissionResolvedEvent,
  HarnessQuestionResolvedEvent,
} from "@opencuria/chat-core/harness";
import {
  applyPartDeltaImmutable,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureBusyAssistant,
  mergeBusyFetchedMessages,
  routeAssistantMessage,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
} from "@opencuria/chat-core/harnessReducer";
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
} from "@opencuria/chat-core/harnessSubtaskActivity";
import { assertHarnessSessionSelection } from "@opencuria/chat-core/harnessModels";
import { hydrateHarnessPart } from "@opencuria/chat-core/toolDisplay";

const EMPTY_MESSAGES = Object.freeze([]) as unknown as HarnessMessage[];
const DETAIL_CACHE_LIMIT = 100;
const INACTIVE_TRANSCRIPT_LIMIT = 4;
const INACTIVE_TRANSCRIPT_BUDGET_BYTES = 16 * 1024 * 1024;

/** Options used when creating a conversation or sending/editing a turn. */
export interface ChatRunOptions {
  harnessId?: HarnessId;
  mode?: HarnessSessionMode;
  model?: string;
  reasoningEffort?: string;
  skillIds?: string[];
}

/** Immutable-by-convention view consumed by React's `useSyncExternalStore`. */
export interface ChatControllerSnapshot {
  sessions: HarnessSession[];
  /** Transcript of `activeSessionId`, ordered as persisted by chat-core. */
  messages: HarnessMessage[];
  todos: HarnessTodo[];
  /** Pending gates for the active session and its descendants. */
  permissions: HarnessPermissionRequest[];
  questions: HarnessQuestionRequest[];
  activeSessionId: string | null;
  activeSession: HarnessSession | null;
  /** Root-to-active-session breadcrumb, inclusive. */
  activeSessionLineage: HarnessSession[];
  workspaceId: string | null;
  loading: boolean;
  error: string | null;
  connected: boolean;
  /** The active session is running, or its send/create request is in flight. */
  busy: boolean;
}

interface SelectionContext {
  workspaceId: string;
  workspaceGeneration: number;
  selectionGeneration: number;
}

interface TimelineFlight extends SelectionContext {
  promise: Promise<void>;
  revision: number;
  sessionStatusAtRequest: HarnessSession["status"] | undefined;
}

interface TodoFlight extends SelectionContext {
  promise: Promise<void>;
  revision: number;
}

interface CachedDetail {
  sessionId: string;
  part: HarnessPart;
}

type SessionEvent = { workspace_id: string; session_id: string };
type RootScopedSessionEvent = SessionEvent & { root_session_id?: string };
type PermissionEvent = SessionEvent & {
  root_session_id?: string;
  request_id?: string;
  tool: string;
  pattern: string;
  title: string;
  call_id?: string;
  decision?: string;
  remember?: string;
  agent_name?: string;
};
type QuestionEvent = SessionEvent & {
  root_session_id?: string;
  request_id: string;
  questions: HarnessQuestionRequest["questions"];
  call_id?: string;
  status?: string;
  agent_name?: string;
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function clonePart(part: HarnessPart): HarnessPart {
  return {
    ...part,
    ...(part.input ? { input: { ...part.input } } : {}),
    ...(part.meta ? { meta: { ...part.meta } } : {}),
    ...(part.display
      ? {
          display: {
            ...part.display,
            ...(part.display.preview
              ? { preview: part.display.preview.map((line) => ({ ...line })) }
              : {}),
            ...(part.display.question_rows
              ? {
                  question_rows: part.display.question_rows.map((row) => ({
                    ...row,
                    ...(row.options
                      ? {
                          options: row.options.map((option) => ({ ...option })),
                        }
                      : {}),
                  })),
                }
              : {}),
          },
        }
      : {}),
  };
}

function cloneMessage(message: HarnessMessage): HarnessMessage {
  return {
    ...message,
    ...(message.skill_ids ? { skill_ids: [...message.skill_ids] } : {}),
    parts: message.parts.map(clonePart),
  };
}

function cloneMessages(messages: HarnessMessage[]): HarnessMessage[] {
  return messages.map(cloneMessage);
}

function freezeTree<T>(value: T): T {
  if (!value || typeof value !== "object" || Object.isFrozen(value))
    return value;
  for (const child of Object.values(value as Record<string, unknown>))
    freezeTree(child);
  return Object.freeze(value);
}

function freezeMessages(messages: HarnessMessage[]): HarnessMessage[] {
  for (const message of messages) freezeTree(message);
  return Object.freeze(messages) as HarnessMessage[];
}

function cloneSession(session: HarnessSession): HarnessSession {
  return {
    ...session,
    tokens: { ...session.tokens },
    ...(session.skill_ids ? { skill_ids: [...session.skill_ids] } : {}),
  };
}

function cloneTodo(todo: HarnessTodo): HarnessTodo {
  return { ...todo };
}

function cloneQuestionRequest(
  request: HarnessQuestionRequest,
): HarnessQuestionRequest {
  return {
    ...request,
    questions: request.questions.map((question) => ({
      ...question,
      ...(question.options
        ? { options: question.options.map((option) => ({ ...option })) }
        : {}),
    })),
  };
}

function sessionByteEstimate(messages: HarnessMessage[]): number {
  // Avoid a JSON serialization (and its temporary allocation) in the stream path.
  let bytes = 0;
  for (const message of messages) {
    bytes += (message.content.length + message.id.length) * 2 + 128;
    for (const part of message.parts) {
      bytes +=
        (part.output.length + part.title.length + part.id.length) * 2 + 160;
      bytes += part.detail_loaded ? 512 : 0;
    }
  }
  return bytes;
}

/**
 * Stateful harness controller bound to an authenticated shared client.
 * The parent owns workspace navigation, auth, and native foreground events.
 */
export class ChatController {
  private readonly listeners = new Set<() => void>();
  private readonly detachRealtime: Array<() => void> = [];
  private releaseWorkspace: (() => void) | null = null;
  private disposed = false;

  private sessions: HarnessSession[] = [];
  private readonly messagesBySession = new Map<string, HarnessMessage[]>();
  private readonly todosBySession = new Map<string, HarnessTodo[]>();
  private readonly permissionsById = new Map<
    string,
    HarnessPermissionRequest
  >();
  private readonly questionsById = new Map<string, HarnessQuestionRequest>();
  private readonly timelineFlights = new Map<string, TimelineFlight>();
  private readonly todoFlights = new Map<string, TodoFlight>();
  private readonly detailFlights = new Map<string, Promise<HarnessPart>>();
  private readonly pinnedDetailKeys = new Set<string>();
  private readonly detailCache = new Map<string, CachedDetail>();
  private readonly sessionRevisions = new Map<string, number>();
  private readonly sessionTombstones = new Set<string>();
  private readonly ownedSessionIds = new Set<string>();
  /** Child ids explicitly announced by an owned parent, before list refresh commits them. */
  private readonly trustedChildSessionIds = new Set<string>();
  private readonly pendingSends = new Set<string>();
  private readonly sendFlights = new Map<string, Promise<HarnessSession>>();
  private readonly createFlights = new Map<string, Promise<HarnessSession>>();
  private readonly readFlights = new Map<string, Promise<void>>();
  private readonly unreadFlights = new Map<string, Promise<void>>();
  private readonly dismissedNotices = new Map<string, string>();
  private readonly transcriptLru = new Map<string, number>();
  private readonly timelineRevisions = new Map<string, number>();
  private readonly todoRevisions = new Map<string, number>();
  private readonly detailRevisions = new Map<string, number>();

  private workspaceId: string | null = null;
  private activeSessionId: string | null = null;
  private loading = false;
  private error: string | null = null;
  private connected: boolean;
  private workspaceGeneration = 0;
  private selectionGeneration = 0;
  private activeWorkspaceSessionsLoaded = false;
  private sessionListGeneration = 0;
  private sessionsFlight: {
    workspaceId: string;
    workspaceGeneration: number;
    promise: Promise<void>;
  } | null = null;
  private snapshot: ChatControllerSnapshot;

  constructor(private readonly client: OpenCuriaClient) {
    this.connected = client.realtime.isConnected;
    this.snapshot = this.buildSnapshot();
    this.bindRealtime();
  }

  /** Stable snapshot getter suitable for `useSyncExternalStore`. */
  readonly getSnapshot = (): ChatControllerSnapshot => this.snapshot;

  /** Subscribe to snapshot replacements; the returned function is idempotent. */
  readonly subscribe = (listener: () => void): (() => void) => {
    if (this.disposed) return () => undefined;
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private buildSnapshot(): ChatControllerSnapshot {
    const activeSession =
      this.sessions.find((session) => session.id === this.activeSessionId) ??
      null;
    const lineage = activeSession
      ? freezeTree(
          collectAncestorSessions(activeSession.id, this.sessions).map(
            cloneSession,
          ),
        )
      : [];
    const descendantIds = activeSession
      ? new Set(collectDescendantSessionIds(activeSession.id, this.sessions))
      : new Set<string>();
    return Object.freeze({
      sessions: freezeTree(this.sessions.map(cloneSession)),
      messages: this.activeSessionId
        ? freezeMessages([
            ...(this.messagesBySession.get(this.activeSessionId) ??
              EMPTY_MESSAGES),
          ])
        : EMPTY_MESSAGES,
      todos: this.activeSessionId
        ? freezeTree(
            (this.todosBySession.get(this.activeSessionId) ?? EMPTY_TODOS).map(
              cloneTodo,
            ),
          )
        : EMPTY_TODOS,
      permissions: freezeTree(
        [...this.permissionsById.values()]
          .filter((request) => descendantIds.has(request.session_id))
          .map((request) => ({ ...request })),
      ),
      questions: freezeTree(
        [...this.questionsById.values()]
          .filter((request) => descendantIds.has(request.session_id))
          .map(cloneQuestionRequest),
      ),
      activeSessionId: this.activeSessionId,
      activeSession: activeSession
        ? freezeTree(cloneSession(activeSession))
        : null,
      activeSessionLineage: lineage,
      workspaceId: this.workspaceId,
      loading: this.loading,
      error: this.error,
      connected: this.connected,
      busy: Boolean(
        (this.workspaceId &&
          this.pendingSends.has(`new:${this.workspaceId}`)) ||
        (this.activeSessionId &&
          (activeSession?.status === "busy" ||
            this.pendingSends.has(this.activeSessionId))),
      ),
    });
  }

  private publish(): void {
    if (this.disposed) return;
    this.snapshot = this.buildSnapshot();
    for (const listener of [...this.listeners]) listener();
  }

  private setError(error: unknown, fallback: string): void {
    if (this.disposed) return;
    this.error = errorMessage(error, fallback);
    this.publish();
  }

  private clearError(): void {
    if (this.error !== null) {
      this.error = null;
      this.publish();
    }
  }

  private bindRealtime(): void {
    const on = this.client.realtime.on.bind(this.client.realtime);
    this.detachRealtime.push(
      on("connect", () => this.setConnected(true)),
      on("disconnect", () => this.setConnected(false)),
      this.client.realtime.onReconnect(() => {
        void this.refresh().catch(() => undefined);
      }),
      on("harness.part_updated", (event) => this.onPartUpdated(event)),
      on("harness.todo_updated", (event) => this.onTodoUpdated(event)),
      on("harness.subtask_started", (event) => this.onSubtaskStarted(event)),
      on("harness.subtask_finished", (event) => this.onSubtaskFinished(event)),
      on("harness.session_status", (event) => this.onSessionStatus(event)),
      on("harness.permission_required", (event) =>
        this.onPermissionRequired(event),
      ),
      on("harness.permission_resolved", (event) =>
        this.onPermissionResolved(event),
      ),
      on("harness.question_required", (event) =>
        this.onQuestionRequired(event),
      ),
      on("harness.question_resolved", (event) =>
        this.onQuestionResolved(event),
      ),
      on("harness.conversations_changed", (event) => {
        if (event.workspace_id === this.workspaceId)
          void this.refresh().catch(() => undefined);
      }),
    );
  }

  private setConnected(value: boolean): void {
    if (this.disposed || this.connected === value) return;
    this.connected = value;
    this.publish();
  }

  private ownsEvent(event: SessionEvent): boolean {
    if (
      this.disposed ||
      !this.workspaceId ||
      event.workspace_id !== this.workspaceId
    )
      return false;
    const session = this.sessions.find((item) => item.id === event.session_id);
    // Child timelines can emit before the parent event publishes the child
    // session. Accept unknown in-workspace ids, but reject known out-of-scope ids.
    return !session
      ? this.trustedChildSessionIds.has(event.session_id) ||
          this.isTrustedDescendant(event.session_id)
      : session.workspace_id === this.workspaceId;
  }

  private isTrustedDescendant(sessionId: string): boolean {
    let cursor = this.sessions.find((session) => session.id === sessionId);
    const seen = new Set<string>();
    while (cursor?.parent_id) {
      if (seen.has(cursor.id)) return false;
      seen.add(cursor.id);
      if (this.ownedSessionIds.has(cursor.parent_id)) return true;
      cursor = this.sessions.find(
        (session) => session.id === cursor?.parent_id,
      );
    }
    return false;
  }

  private eventMayBelongToWorkspace(event: RootScopedSessionEvent): boolean {
    if (this.ownsEvent(event)) return true;
    if (
      this.disposed ||
      !this.workspaceId ||
      event.workspace_id !== this.workspaceId
    )
      return false;
    // During child-session creation, a gate can beat the refreshed session list.
    const rootId = event.root_session_id;
    return (
      Boolean(
        rootId &&
        this.sessions.some(
          (session) =>
            session.id === rootId && session.workspace_id === this.workspaceId,
        ),
      ) ||
      this.trustedChildSessionIds.has(event.session_id) ||
      this.isTrustedDescendant(event.session_id)
    );
  }

  private captureSelection(): SelectionContext | null {
    if (!this.workspaceId) return null;
    return {
      workspaceId: this.workspaceId,
      workspaceGeneration: this.workspaceGeneration,
      selectionGeneration: this.selectionGeneration,
    };
  }

  private selectionIsCurrent(
    context: SelectionContext,
    sessionId?: string,
  ): boolean {
    if (
      this.disposed ||
      this.workspaceId !== context.workspaceId ||
      this.workspaceGeneration !== context.workspaceGeneration ||
      this.selectionGeneration !== context.selectionGeneration
    )
      return false;
    if (sessionId && this.activeSessionId !== sessionId) {
      return collectDescendantSessionIds(
        this.activeSessionId ?? "",
        this.sessions,
      ).includes(sessionId);
    }
    return true;
  }

  private bumpSessionRevision(sessionId: string): void {
    this.sessionRevisions.set(
      sessionId,
      (this.sessionRevisions.get(sessionId) ?? 0) + 1,
    );
  }

  private upsertSession(session: HarnessSession): void {
    if (
      !this.workspaceId ||
      session.workspace_id !== this.workspaceId ||
      this.disposed
    )
      return;
    const index = this.sessions.findIndex((item) => item.id === session.id);
    this.sessions =
      index < 0
        ? [session, ...this.sessions]
        : this.sessions.map((item, at) => (at === index ? session : item));
    this.ownedSessionIds.add(session.id);
    this.sessionTombstones.delete(session.id);
    this.bumpSessionRevision(session.id);
    this.publish();
  }

  private replaceSessions(
    incoming: HarnessSession[],
    requestRevisions: Map<string, number>,
  ): void {
    if (this.disposed || !this.workspaceId) return;
    const previous = this.sessions;
    const next: HarnessSession[] = [];
    const seen = new Set<string>();
    for (const serverSession of incoming) {
      if (
        serverSession.workspace_id !== this.workspaceId ||
        this.sessionTombstones.has(serverSession.id)
      )
        continue;
      seen.add(serverSession.id);
      this.ownedSessionIds.add(serverSession.id);
      const current = previous.find((item) => item.id === serverSession.id);
      const changedDuringRequest =
        (this.sessionRevisions.get(serverSession.id) ?? 0) >
        (requestRevisions.get(serverSession.id) ?? 0);
      next.push(
        changedDuringRequest && current
          ? { ...serverSession, ...current }
          : serverSession,
      );
    }
    // Preserve sessions created or otherwise updated locally after this list request began.
    for (const current of previous) {
      if (seen.has(current.id) || this.sessionTombstones.has(current.id))
        continue;
      if (
        (this.sessionRevisions.get(current.id) ?? 0) >
        (requestRevisions.get(current.id) ?? 0)
      ) {
        next.push(current);
      } else {
        this.forgetSession(current.id);
        this.ownedSessionIds.delete(current.id);
      }
    }
    this.sessions = next;
    const ids = new Set(next.map((session) => session.id));
    for (const childId of [...this.trustedChildSessionIds]) {
      if (ids.has(childId)) this.trustedChildSessionIds.delete(childId);
    }
    for (const previousSession of previous) {
      if (!ids.has(previousSession.id)) {
        this.forgetSession(previousSession.id);
        this.ownedSessionIds.delete(previousSession.id);
      }
    }
    if (this.activeSessionId && !ids.has(this.activeSessionId)) {
      this.activeSessionId = null;
      this.selectionGeneration += 1;
    }
    this.activeWorkspaceSessionsLoaded = true;
    this.publish();
  }

  private forgetSession(sessionId: string): void {
    this.messagesBySession.delete(sessionId);
    this.todosBySession.delete(sessionId);
    this.transcriptLru.delete(sessionId);
    this.timelineRevisions.set(
      sessionId,
      (this.timelineRevisions.get(sessionId) ?? 0) + 1,
    );
    this.todoRevisions.set(
      sessionId,
      (this.todoRevisions.get(sessionId) ?? 0) + 1,
    );
    this.detailRevisions.set(
      sessionId,
      (this.detailRevisions.get(sessionId) ?? 0) + 1,
    );
    this.timelineFlights.delete(sessionId);
    this.todoFlights.delete(sessionId);
    for (const key of this.readFlights.keys()) {
      if (key === sessionId) this.readFlights.delete(key);
    }
    for (const key of this.unreadFlights.keys()) {
      if (key === sessionId) this.unreadFlights.delete(key);
    }
    for (const key of this.sendFlights.keys()) {
      if (key === sessionId) this.sendFlights.delete(key);
    }
    this.pendingSends.delete(sessionId);
    for (const key of this.detailFlights.keys()) {
      if (key.startsWith(`${sessionId}:`)) this.detailFlights.delete(key);
    }
    for (const [id, detail] of this.detailCache) {
      if (detail.sessionId === sessionId) {
        this.detailCache.delete(id);
        this.pinnedDetailKeys.delete(id);
      }
    }
    for (const [id, request] of this.permissionsById) {
      if (request.session_id === sessionId) this.permissionsById.delete(id);
    }
    for (const [id, request] of this.questionsById) {
      if (request.session_id === sessionId) this.questionsById.delete(id);
    }
  }

  /** Load workspace session summaries and optionally open one session. */
  async openWorkspace(workspaceId: string, sessionId?: string): Promise<void> {
    this.assertLive();
    if (!workspaceId) throw new Error("A workspace id is required");
    if (this.workspaceId !== workspaceId) {
      this.releaseWorkspace?.();
      this.releaseWorkspace = null;
      this.workspaceGeneration += 1;
      this.selectionGeneration += 1;
      this.workspaceId = workspaceId;
      this.activeSessionId = null;
      this.sessions = [];
      this.ownedSessionIds.clear();
      this.trustedChildSessionIds.clear();
      this.messagesBySession.clear();
      this.todosBySession.clear();
      this.permissionsById.clear();
      this.questionsById.clear();
      this.timelineFlights.clear();
      this.todoFlights.clear();
      this.detailFlights.clear();
      this.detailCache.clear();
      this.pinnedDetailKeys.clear();
      this.sendFlights.clear();
      this.createFlights.clear();
      this.readFlights.clear();
      this.unreadFlights.clear();
      this.sessionRevisions.clear();
      this.sessionTombstones.clear();
      this.ownedSessionIds.clear();
      this.timelineRevisions.clear();
      this.todoRevisions.clear();
      this.detailRevisions.clear();
      this.pendingSends.clear();
      this.transcriptLru.clear();
      this.dismissedNotices.clear();
      this.activeWorkspaceSessionsLoaded = false;
      this.sessionListGeneration += 1;
      this.sessionsFlight = null;
      this.loading = true;
      this.error = null;
      this.releaseWorkspace =
        this.client.realtime.subscribeWorkspace(workspaceId);
      this.connected = this.client.realtime.isConnected;
      this.publish();
    }

    const workspaceGeneration = this.workspaceGeneration;
    if (!this.activeWorkspaceSessionsLoaded) {
      let flight = this.sessionsFlight;
      if (
        !flight ||
        flight.workspaceId !== workspaceId ||
        flight.workspaceGeneration !== workspaceGeneration
      ) {
        const revisions = new Map(this.sessionRevisions);
        const listGeneration = ++this.sessionListGeneration;
        const request = (async () => {
          try {
            const rows = await this.client.harness.listSessions(workspaceId);
            if (
              this.disposed ||
              workspaceGeneration !== this.workspaceGeneration ||
              this.workspaceId !== workspaceId ||
              listGeneration !== this.sessionListGeneration
            )
              return;
            this.replaceSessions(rows, revisions);
          } catch (error) {
            if (
              workspaceGeneration === this.workspaceGeneration &&
              listGeneration === this.sessionListGeneration &&
              !this.disposed
            ) {
              this.loading = false;
              this.setError(error, "Failed to load sessions");
            }
            throw error;
          } finally {
            if (
              workspaceGeneration === this.workspaceGeneration &&
              listGeneration === this.sessionListGeneration &&
              !this.disposed
            ) {
              this.loading = false;
              this.publish();
            }
          }
        })();
        flight = { workspaceId, workspaceGeneration, promise: request };
        this.sessionsFlight = flight;
      }
      await flight.promise;
    }

    if (sessionId) {
      await this.openSession(sessionId);
    }
  }

  /** Select a new empty composer state without creating a server session. */
  async newChat(workspaceId: string): Promise<void> {
    this.assertLive();
    if (this.workspaceId !== workspaceId) await this.openWorkspace(workspaceId);
    if (this.activeSessionId !== null) {
      if (this.messagesBySession.has(this.activeSessionId))
        this.touchTranscript(this.activeSessionId);
      this.activeSessionId = null;
      this.selectionGeneration += 1;
    }
    this.loading = false;
    this.error = null;
    this.publish();
  }

  /** Open a session transcript, todos, descendant timelines, and pending gates. */
  async openSession(sessionId: string): Promise<void> {
    this.assertLive();
    if (!this.workspaceId)
      throw new Error("Open a workspace before opening a session");
    if (!this.activeWorkspaceSessionsLoaded)
      await this.openWorkspace(this.workspaceId);
    if (this.disposed || !this.workspaceId) return;
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session || session.workspace_id !== this.workspaceId) {
      throw new Error("Session does not belong to the active workspace");
    }
    if (this.activeSessionId !== sessionId) {
      if (this.activeSessionId) this.touchTranscript(this.activeSessionId);
      this.selectionGeneration += 1;
      this.activeSessionId = sessionId;
    }
    const context = this.captureSelection()!;
    this.loading = !this.messagesBySession.has(sessionId);
    this.error = null;
    this.touchTranscript(sessionId);
    this.publish();
    this.maybeMarkRead(sessionId);
    try {
      const lineageIds = collectDescendantSessionIds(sessionId, this.sessions);
      await Promise.all([
        ...lineageIds.map((id) => this.fetchTimeline(id, context)),
        this.fetchTodos(sessionId, context),
      ]);
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to open session");
      throw error;
    } finally {
      if (this.selectionIsCurrent(context)) {
        this.loading = false;
        this.trimInactiveTranscripts();
        this.publish();
      }
    }
  }

  /** Reconcile server session summaries and the currently open lineage. */
  async refresh(): Promise<void> {
    this.assertLive();
    const workspaceId = this.workspaceId;
    if (!workspaceId) return;
    const workspaceGeneration = this.workspaceGeneration;
    const selectionGeneration = this.selectionGeneration;
    const activeSessionId = this.activeSessionId;
    const listGeneration = ++this.sessionListGeneration;
    const revisions = new Map(this.sessionRevisions);
    const initial = !this.activeWorkspaceSessionsLoaded;
    if (initial) this.loading = true;
    this.error = null;
    this.publish();
    try {
      const rows = await this.client.harness.listSessions(workspaceId);
      if (
        this.disposed ||
        this.workspaceId !== workspaceId ||
        this.workspaceGeneration !== workspaceGeneration ||
        this.sessionListGeneration !== listGeneration
      )
        return;
      this.replaceSessions(rows, revisions);
      if (
        this.selectionGeneration !== selectionGeneration ||
        this.activeSessionId !== activeSessionId ||
        !activeSessionId
      )
        return;
      const context = { workspaceId, workspaceGeneration, selectionGeneration };
      const lineageIds = collectDescendantSessionIds(
        activeSessionId,
        this.sessions,
      );
      await Promise.all([
        ...lineageIds.map((id) => this.fetchTimeline(id, context, true)),
        this.fetchTodos(activeSessionId, context, true),
      ]);
    } catch (error) {
      if (
        !this.disposed &&
        this.workspaceId === workspaceId &&
        this.workspaceGeneration === workspaceGeneration &&
        this.sessionListGeneration === listGeneration &&
        this.selectionGeneration === selectionGeneration
      ) {
        this.setError(error, "Failed to refresh chat");
      }
      throw error;
    } finally {
      if (
        !this.disposed &&
        this.workspaceGeneration === workspaceGeneration &&
        initial
      ) {
        this.loading = false;
        this.publish();
      }
    }
  }

  /** Create a new server conversation and seed its user row after POST success. */
  create(
    prompt: string,
    options: ChatRunOptions = {},
  ): Promise<HarnessSession> {
    this.assertLive();
    const workspaceId = this.workspaceId;
    if (!workspaceId)
      return Promise.reject(
        new Error("Open a workspace before creating a chat"),
      );
    if (!prompt.trim())
      return Promise.reject(new Error("Prompt cannot be empty"));
    const existing = this.createFlights.get(workspaceId);
    if (existing) return existing;
    const workspaceGeneration = this.workspaceGeneration;
    const mode = options.mode ?? "build";
    const harnessId = options.harnessId ?? "native";
    const flight = this.performCreate(
      workspaceId,
      workspaceGeneration,
      prompt,
      {
        ...options,
        mode,
        harnessId,
      },
    ).finally(() => {
      if (this.createFlights.get(workspaceId) === flight)
        this.createFlights.delete(workspaceId);
    });
    this.createFlights.set(workspaceId, flight);
    return flight;
  }

  private async performCreate(
    workspaceId: string,
    workspaceGeneration: number,
    prompt: string,
    options: ChatRunOptions & {
      mode: HarnessSessionMode;
      harnessId: HarnessId;
    },
  ): Promise<HarnessSession> {
    this.pendingSends.add(`new:${workspaceId}`);
    this.publish();
    try {
      const session = await this.client.harness.createSession(workspaceId, {
        prompt,
        mode: options.mode,
        agent_name: options.mode,
        model: options.model ?? "",
        reasoning_effort: options.reasoningEffort ?? "",
        skill_ids: options.skillIds ?? [],
        harness_id: options.harnessId,
      });
      if (
        this.disposed ||
        this.workspaceId !== workspaceId ||
        this.workspaceGeneration !== workspaceGeneration
      ) {
        return session;
      }
      this.upsertSession(session);
      this.selectSession(session.id);
      this.messagesBySession.set(
        session.id,
        freezeMessages([
          {
            id: `local-user-${session.id}`,
            session_id: session.id,
            harness_id: session.harness_id,
            role: "user",
            content: prompt,
            skill_ids: [...(options.skillIds ?? [])],
            parts: [],
            created_at: new Date().toISOString(),
          },
        ]) as HarnessMessage[],
      );
      this.touchTranscript(session.id, true);
      this.publish();
      const context = this.captureSelection()!;
      try {
        await Promise.all([
          ...collectDescendantSessionIds(session.id, this.sessions).map((id) =>
            this.fetchTimeline(id, context),
          ),
          this.fetchTodos(session.id, context),
        ]);
      } catch (error) {
        if (this.selectionIsCurrent(context)) {
          this.setError(
            error,
            "Chat was created but its transcript could not be refreshed",
          );
        }
      }
      return session;
    } catch (error) {
      if (!this.disposed && this.workspaceGeneration === workspaceGeneration) {
        this.setError(error, "Failed to create chat");
      }
      throw error;
    } finally {
      this.pendingSends.delete(`new:${workspaceId}`);
      this.publish();
    }
  }

  /** Send to the selected session, creating one when the composer is in new-chat mode. */
  send(prompt: string, options: ChatRunOptions = {}): Promise<HarnessSession> {
    this.assertLive();
    if (!prompt.trim())
      return Promise.reject(new Error("Prompt cannot be empty"));
    const sessionId = this.activeSessionId;
    if (!sessionId) return this.create(prompt, options);
    const existing = this.sendFlights.get(sessionId);
    if (existing) return existing;
    const workspaceGeneration = this.workspaceGeneration;
    const workspaceId = this.workspaceId;
    if (!workspaceId)
      return Promise.reject(
        new Error("Open a workspace before sending a message"),
      );
    const flight = this.performSend(
      sessionId,
      workspaceId,
      workspaceGeneration,
      prompt,
      options,
    ).finally(() => {
      if (this.sendFlights.get(sessionId) === flight)
        this.sendFlights.delete(sessionId);
    });
    this.sendFlights.set(sessionId, flight);
    return flight;
  }

  private async performSend(
    sessionId: string,
    workspaceId: string,
    workspaceGeneration: number,
    prompt: string,
    options: ChatRunOptions,
  ): Promise<HarnessSession> {
    const session = this.sessions.find((row) => row.id === sessionId);
    if (!session) throw new Error("The active session no longer exists");
    assertHarnessSessionSelection(session, options);
    const context = this.captureSelection()!;
    this.pendingSends.add(sessionId);
    this.publish();
    try {
      const updated = await this.client.harness.send(sessionId, {
        prompt,
        mode: options.mode,
        model: options.model,
        reasoning_effort: options.reasoningEffort,
        skill_ids: options.skillIds,
      });
      if (
        this.disposed ||
        this.workspaceId !== workspaceId ||
        this.workspaceGeneration !== workspaceGeneration
      ) {
        return updated;
      }
      this.upsertSession(updated);
      // The send endpoint persists the user row before returning. Do not add a
      // local prompt: retries after uncertain POST failures must not duplicate it.
      const reconcile = this.captureSelection();
      if (reconcile && this.activeSessionId === sessionId) {
        try {
          await this.fetchTimeline(sessionId, reconcile);
        } catch (error) {
          if (this.selectionIsCurrent(reconcile)) {
            this.setError(error, "Message sent but transcript refresh failed");
          }
        }
      }
      return updated;
    } catch (error) {
      if (!this.disposed && this.workspaceGeneration === workspaceGeneration) {
        this.setError(error, "Failed to send message");
      }
      throw error;
    } finally {
      this.pendingSends.delete(sessionId);
      if (!this.disposed) this.publish();
    }
  }

  /** Stop the selected session's current run. */
  async abort(
    sessionId = this.activeSessionId,
  ): Promise<HarnessSession | null> {
    if (!sessionId) return null;
    const context = this.captureSelection();
    if (!context) return null;
    try {
      const updated = await this.client.harness.abort(sessionId);
      if (
        this.workspaceGeneration !== context.workspaceGeneration ||
        this.disposed
      )
        return updated;
      this.upsertSession(updated);
      if (updated.status === "idle" && this.isInActiveLineage(sessionId)) {
        await this.refreshTimelineAfterIdle(
          sessionId,
          this.captureSelection()!,
        );
      }
      return updated;
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to stop the run");
      throw error;
    }
  }

  /** Edit a persisted user message and reconcile its server timeline. */
  async edit(
    messageId: string,
    prompt: string,
    options: ChatRunOptions = {},
    sessionId = this.activeSessionId,
  ): Promise<HarnessSession> {
    if (!sessionId) throw new Error("There is no active session to edit");
    if (!prompt.trim()) throw new Error("Prompt cannot be empty");
    const context = this.captureSelection();
    if (!context) throw new Error("Open a workspace before editing a message");
    const session = this.sessions.find(row => row.id === sessionId);
    if (session) assertHarnessSessionSelection(session, options);
    const details = this.editOptions(options);
    this.invalidatePartDetails(sessionId);
    try {
      const updated = await this.client.harness.edit(sessionId, messageId, {
        prompt,
        mode: details.mode,
        model: details.model,
        reasoning_effort: details.reasoning_effort,
        skill_ids: details.skill_ids,
      });
      if (this.selectionIsCurrent(context)) {
        this.upsertSession(updated);
        await this.fetchTimeline(sessionId, context, true).catch((error) => {
          this.setError(
            error,
            "Message was edited but transcript refresh failed",
          );
        });
      }
      return updated;
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to edit message");
      throw error;
    }
  }

  /** Fork from after a selected message. Assistant cutoffs include the answer. */
  async fork(
    afterMessageId?: string,
    sessionId = this.activeSessionId,
  ): Promise<HarnessSession | null> {
    if (!sessionId) return null;
    const context = this.captureSelection();
    if (!context) return null;
    const sourceMessage = this.messagesBySession
      .get(sessionId)
      ?.find((message) => message.id === afterMessageId);
    if (afterMessageId && !sourceMessage) {
      await this.fetchTimeline(sessionId, context, true);
    }
    const refreshedMessage = afterMessageId
      ? this.messagesBySession
          .get(sessionId)
          ?.find((message) => message.id === afterMessageId)
      : undefined;
    if (afterMessageId && !refreshedMessage)
      throw new Error("Selected fork message is no longer available");
    const input = afterMessageId
      ? {
          message_id: afterMessageId,
          ...(refreshedMessage?.role === "assistant"
            ? { include_message: true }
            : {}),
        }
      : {};
    try {
      const forked = await this.client.harness.fork(sessionId, input);
      if (
        this.disposed ||
        this.workspaceGeneration !== context.workspaceGeneration ||
        this.workspaceId !== context.workspaceId
      ) {
        return forked;
      }
      this.upsertSession(forked);
      this.selectSession(forked.id);
      const nextContext = this.captureSelection()!;
      try {
        await Promise.all([
          ...collectDescendantSessionIds(forked.id, this.sessions).map((id) =>
            this.fetchTimeline(id, nextContext),
          ),
          this.fetchTodos(forked.id, nextContext),
        ]);
      } catch (error) {
        if (this.selectionIsCurrent(nextContext))
          this.setError(
            error,
            "Conversation was forked but could not be loaded",
          );
      }
      return forked;
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to fork conversation");
      throw error;
    }
  }

  private editOptions(options: ChatRunOptions): {
    mode: HarnessSessionMode | "";
    model: string;
    reasoning_effort: string;
    skill_ids: string[];
  } {
    return {
      mode: options.mode ?? "",
      model: options.model ?? "",
      reasoning_effort: options.reasoningEffort ?? "",
      skill_ids: options.skillIds ?? [],
    };
  }

  /** Rename a conversation. */
  async rename(sessionId: string, title: string): Promise<HarnessSession> {
    const context = this.captureSelection();
    if (!context) throw new Error("Open a workspace before renaming a session");
    try {
      const updated = await this.client.harness.rename(sessionId, title);
      if (
        this.workspaceGeneration === context.workspaceGeneration &&
        !this.disposed
      )
        this.upsertSession(updated);
      return updated;
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to rename session");
      throw error;
    }
  }

  /** Delete a conversation and its cached transcript/gates. */
  async delete(sessionId: string): Promise<void> {
    const context = this.captureSelection();
    if (!context) throw new Error("Open a workspace before deleting a session");
    try {
      await this.client.harness.deleteSession(sessionId);
      if (
        this.disposed ||
        this.workspaceGeneration !== context.workspaceGeneration
      )
        return;
      this.sessionTombstones.add(sessionId);
      this.ownedSessionIds.delete(sessionId);
      this.trustedChildSessionIds.delete(sessionId);
      this.bumpSessionRevision(sessionId);
      this.sessions = this.sessions.filter(
        (session) => session.id !== sessionId,
      );
      this.forgetSession(sessionId);
      if (this.activeSessionId === sessionId) {
        this.activeSessionId = null;
        this.selectionGeneration += 1;
      }
      for (const childId of [...this.ownedSessionIds]) {
        if (this.sessions.some((session) => session.id === childId)) continue;
        this.ownedSessionIds.delete(childId);
      }
      this.publish();
    } catch (error) {
      if (this.selectionIsCurrent(context))
        this.setError(error, "Failed to delete session");
      throw error;
    }
  }

  /** Persist a read marker. Pass `force` to clear a deliberate manual-unread flag. */
  async markRead(
    sessionId = this.activeSessionId,
    force = false,
  ): Promise<void> {
    if (!sessionId) return;
    const existing = this.readFlights.get(sessionId);
    if (existing) return existing;
    const session = this.sessions.find((item) => item.id === sessionId);
    const context = this.captureSelection();
    if (session?.manual_unread && !force) return;
    const before = session
      ? { unread: session.unread, manual_unread: session.manual_unread }
      : null;
    if (session) {
      this.replaceSession(sessionId, {
        ...session,
        unread: false,
        manual_unread: force ? false : session.manual_unread,
      });
      this.bumpSessionRevision(sessionId);
      this.publish();
    }
    const flight = (async () => {
      try {
        await this.client.harness.markRead(sessionId);
      } catch (error) {
        if (
          before &&
          context &&
          !this.disposed &&
          this.workspaceGeneration === context.workspaceGeneration
        ) {
          const current = this.sessions.find((item) => item.id === sessionId);
          if (current)
            this.replaceSession(sessionId, { ...current, ...before });
          this.publish();
        }
        throw error;
      }
    })().finally(() => {
      if (this.readFlights.get(sessionId) === flight)
        this.readFlights.delete(sessionId);
    });
    this.readFlights.set(sessionId, flight);
    return flight;
  }

  /** Set the persistent manual-unread marker. */
  async markUnread(sessionId = this.activeSessionId): Promise<void> {
    if (!sessionId) return;
    const existing = this.unreadFlights.get(sessionId);
    if (existing) return existing;
    const session = this.sessions.find((item) => item.id === sessionId);
    const context = this.captureSelection();
    const before = session
      ? { unread: session.unread, manual_unread: session.manual_unread }
      : null;
    if (session) {
      this.replaceSession(sessionId, {
        ...session,
        unread: true,
        manual_unread: true,
      });
      this.bumpSessionRevision(sessionId);
      this.publish();
    }
    const flight = (async () => {
      try {
        await this.client.harness.markUnread(sessionId);
      } catch (error) {
        if (
          before &&
          context &&
          !this.disposed &&
          this.workspaceGeneration === context.workspaceGeneration
        ) {
          const current = this.sessions.find((item) => item.id === sessionId);
          if (current)
            this.replaceSession(sessionId, { ...current, ...before });
          this.publish();
        }
        throw error;
      }
    })().finally(() => {
      if (this.unreadFlights.get(sessionId) === flight)
        this.unreadFlights.delete(sessionId);
    });
    this.unreadFlights.set(sessionId, flight);
    return flight;
  }

  private maybeMarkRead(sessionId: string): void {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (
      session?.status === "idle" &&
      session.unread &&
      !session.manual_unread
    ) {
      void this.markRead(sessionId).catch(() => undefined);
    }
  }

  /** Dismiss the selected session's error/abort notice for one message. */
  async dismissNotice(
    messageId: string,
    sessionId = this.activeSessionId,
  ): Promise<void> {
    if (!sessionId) return;
    const messages = this.messagesBySession.get(sessionId);
    const messageIndex =
      messages?.findIndex((message) => message.id === messageId) ?? -1;
    const previous =
      messageIndex >= 0
        ? messages?.[messageIndex]?.notice_dismissed_at
        : undefined;
    const noticeKey = `${sessionId}:${messageId}`;
    this.dismissedNotices.set(noticeKey, new Date().toISOString());
    if (messageIndex >= 0 && messages) {
      const next = [...messages];
      const row = cloneMessage(messages[messageIndex]!);
      row.notice_dismissed_at = new Date().toISOString();
      next[messageIndex] = row;
      this.messagesBySession.set(sessionId, freezeMessages(next));
      this.publish();
    }
    if (messageId.startsWith("local-")) return;
    const context = this.captureSelection();
    try {
      await this.client.harness.dismissNotice(sessionId, messageId);
    } catch (error) {
      this.dismissedNotices.delete(noticeKey);
      const currentMessages = this.messagesBySession.get(sessionId);
      const index =
        currentMessages?.findIndex((message) => message.id === messageId) ?? -1;
      if (index >= 0 && currentMessages) {
        const next = [...currentMessages];
        const row = cloneMessage(currentMessages[index]!);
        row.notice_dismissed_at = previous ?? null;
        next[index] = row;
        this.messagesBySession.set(sessionId, freezeMessages(next));
        if (context && this.selectionIsCurrent(context)) this.publish();
      }
      if (context && this.selectionIsCurrent(context))
        this.setError(error, "Failed to dismiss notice");
      throw error;
    }
  }

  /** Resolve a permission request by id; its owner session is in the DTO. */
  async resolvePermission(
    requestId: string,
    response: HarnessPermissionResponse,
  ): Promise<void> {
    const request = this.permissionsById.get(requestId);
    if (!request) return;
    const context = this.captureSelection();
    try {
      const result = (await this.client.harness.resolvePermission(
        request.session_id,
        request,
        response,
      )) as { decision?: string } | undefined;
      if (
        this.disposed ||
        !context ||
        this.workspaceGeneration !== context.workspaceGeneration
      )
        return;
      this.permissionsById.delete(requestId);
      if (result?.decision) void result.decision;
      this.publish();
    } catch (error) {
      if (context && this.selectionIsCurrent(context))
        this.setError(error, "Failed to resolve permission");
      throw error;
    }
  }

  /** Submit or reject a pending question request. */
  async resolveQuestion(
    requestId: string,
    answers: string[],
    reject = false,
  ): Promise<void> {
    const request = this.questionsById.get(requestId);
    if (!request) return;
    const context = this.captureSelection();
    try {
      await this.client.harness.resolveQuestion(
        request.session_id,
        requestId,
        answers,
        reject,
      );
      if (
        this.disposed ||
        !context ||
        this.workspaceGeneration !== context.workspaceGeneration
      )
        return;
      this.questionsById.delete(requestId);
      this.publish();
    } catch (error) {
      if (context && this.selectionIsCurrent(context))
        this.setError(error, "Failed to resolve question");
      throw error;
    }
  }

  /** Hydrate one completed, lightweight timeline part; detail rows are LRU-bounded. */
  async requestPartDetail(
    sessionId: string,
    partId: string,
  ): Promise<HarnessPart> {
    this.assertLive();
    const context = this.captureSelection();
    if (!context || !this.isInActiveLineage(sessionId))
      throw new Error("Part is outside the active conversation");
    const key = `${sessionId}:${partId}`;
    const cached = this.detailCache.get(key);
    if (cached) {
      this.detailCache.delete(key);
      this.detailCache.set(key, cached);
      return clonePart(cached.part);
    }
    const existing = this.detailFlights.get(key);
    if (existing) {
      this.pinnedDetailKeys.add(key);
      return existing;
    }
    const current = this.findPart(sessionId, partId);
    if (!current) throw new Error("Part is no longer available");
    if (current.state === "pending" || current.state === "running") {
      throw new Error("Details are not available until the part is complete");
    }
    if (current.detail_loaded) {
      this.rememberDetail(key, current);
      return clonePart(current);
    }
    this.pinnedDetailKeys.add(key);
    const detailRevision = this.detailRevisions.get(sessionId) ?? 0;
    const flight = (async () => {
      try {
        const result = (await this.client.harness.getPart(
          sessionId,
          partId,
        )) as HarnessPart;
        if (
          !this.selectionIsCurrent(context, sessionId) ||
          detailRevision !== (this.detailRevisions.get(sessionId) ?? 0)
        )
          throw new Error("Conversation changed while loading part details");
        const message = this.messagesBySession
          .get(sessionId)
          ?.find((row) => row.parts.some((part) => part.id === partId));
        const part = message?.parts.find((row) => row.id === partId);
        if (
          !message ||
          !part ||
          part.state === "pending" ||
          part.state === "running"
        ) {
          throw new Error("Part changed while loading details");
        }
        const merged = {
          ...part,
          ...(result as Partial<HarnessPart>),
          session_id: sessionId,
          message_id: message.id,
          detail_loaded: true,
        } as HarnessPart;
        this.replacePart(sessionId, message.id, partId, merged);
        this.rememberDetail(key, merged);
        this.touchTranscript(sessionId, true);
        this.publish();
        return clonePart(merged);
      } catch (error) {
        if (this.selectionIsCurrent(context))
          this.setError(error, "Failed to load part details");
        throw error;
      }
    })().finally(() => {
      if (this.detailFlights.get(key) === flight)
        this.detailFlights.delete(key);
      this.pinnedDetailKeys.delete(key);
    });
    this.detailFlights.set(key, flight);
    return flight;
  }

  private findPart(sessionId: string, partId: string): HarnessPart | undefined {
    return this.messagesBySession
      .get(sessionId)
      ?.flatMap((message) => message.parts)
      .find((part) => part.id === partId);
  }

  private replacePart(
    sessionId: string,
    messageId: string,
    partId: string,
    replacement: HarnessPart,
  ): void {
    const messages = this.messagesBySession.get(sessionId);
    if (!messages) return;
    const messageIndex = messages.findIndex(
      (message) => message.id === messageId,
    );
    if (messageIndex < 0) return;
    const next = [...messages];
    const message = cloneMessage(messages[messageIndex]!);
    const partIndex = message.parts.findIndex((part) => part.id === partId);
    if (partIndex < 0) return;
    message.parts[partIndex] = clonePart(replacement);
    next[messageIndex] = message;
    this.messagesBySession.set(sessionId, next);
  }

  private rememberDetail(key: string, part: HarnessPart): void {
    this.detailCache.delete(key);
    this.detailCache.set(key, {
      sessionId: part.session_id,
      part: clonePart(part),
    });
    while (this.detailCache.size > DETAIL_CACHE_LIMIT) {
      const oldest = [...this.detailCache.keys()].find(
        (candidate) => !this.pinnedDetailKeys.has(candidate),
      );
      if (!oldest) break;
      const removed = this.detailCache.get(oldest);
      this.detailCache.delete(oldest);
      if (!removed) continue;
      const original = this.findPart(removed.sessionId, removed.part.id);
      if (!original?.detail_loaded) continue;
      const lightweight = clonePart(original);
      lightweight.input =
        lightweight.type === "tool" ? { tool: lightweight.tool ?? "" } : {};
      if (
        !(lightweight.state === "error" && lightweight.output === "Tool failed")
      )
        lightweight.output = "";
      lightweight.meta = {};
      lightweight.detail_loaded = false;
      const message = this.messagesBySession
        .get(removed.sessionId)
        ?.find((row) => row.parts.some((part) => part.id === lightweight.id));
      if (message)
        this.replacePart(
          removed.sessionId,
          message.id,
          lightweight.id,
          lightweight,
        );
    }
  }

  private invalidatePartDetails(sessionId: string): void {
    this.detailRevisions.set(
      sessionId,
      (this.detailRevisions.get(sessionId) ?? 0) + 1,
    );
    for (const key of this.detailFlights.keys()) {
      if (key.startsWith(`${sessionId}:`)) this.detailFlights.delete(key);
    }
    for (const [key, cached] of this.detailCache) {
      if (cached.sessionId === sessionId) {
        this.detailCache.delete(key);
        this.pinnedDetailKeys.delete(key);
      }
    }
  }

  private async fetchTimeline(
    sessionId: string,
    context: SelectionContext,
    refreshAfterExisting = false,
  ): Promise<void> {
    const existing = this.timelineFlights.get(sessionId);
    if (
      existing &&
      existing.workspaceGeneration === context.workspaceGeneration &&
      existing.selectionGeneration === context.selectionGeneration
    ) {
      if (!refreshAfterExisting) return existing.promise;
      await existing.promise.catch(() => undefined);
      if (!this.selectionIsCurrent(context, sessionId)) return;
      return this.fetchTimeline(sessionId, context);
    }
    if (!this.selectionIsCurrent(context, sessionId)) return;
    const revision = (this.timelineRevisions.get(sessionId) ?? 0) + 1;
    this.timelineRevisions.set(sessionId, revision);
    const flight: TimelineFlight = {
      ...context,
      revision,
      promise: Promise.resolve(),
      sessionStatusAtRequest: this.sessions.find(
        (session) => session.id === sessionId,
      )?.status,
    };
    flight.promise = (async () => {
      try {
        const timeline = await this.client.harness.timeline(sessionId);
        if (
          !this.selectionIsCurrent(context, sessionId) ||
          this.timelineRevisions.get(sessionId) !== revision ||
          !this.sessions.some(
            (session) =>
              session.id === sessionId &&
              session.workspace_id === context.workspaceId,
          )
        )
          return;
        const currentStatus = this.sessions.find(
          (session) => session.id === sessionId,
        )?.status;
        this.applyTimeline(
          sessionId,
          timeline,
          currentStatus === "busy" ? "busy" : flight.sessionStatusAtRequest,
        );
      } catch (error) {
        if (
          this.selectionIsCurrent(context, sessionId) &&
          this.timelineRevisions.get(sessionId) === revision
        ) {
          this.setError(error, "Failed to load conversation");
          throw error;
        }
      }
    })().finally(() => {
      if (this.timelineFlights.get(sessionId) === flight)
        this.timelineFlights.delete(sessionId);
      this.trimInactiveTranscripts();
    });
    this.timelineFlights.set(sessionId, flight);
    return flight.promise;
  }

  private applyTimeline(
    sessionId: string,
    timeline: Awaited<ReturnType<OpenCuriaClient["harness"]["timeline"]>>,
    sessionStatusAtRequest: HarnessSession["status"] | undefined,
  ): void {
    const previous = this.messagesBySession.get(sessionId) ?? [];
    const previousParts = new Map(
      previous.flatMap((message) =>
        message.parts.map((part) => [part.id, part] as const),
      ),
    );
    const incoming = sortHarnessMessages(
      (timeline.messages ?? []).map((message) => {
        const parts = (message.parts ?? []).map((rawPart) => {
          const prior = previousParts.get(rawPart.id);
          const cached = this.detailCache.get(
            `${sessionId}:${rawPart.id}`,
          )?.part;
          const source = prior?.detail_loaded ? prior : cached;
          const merged = source
            ? {
                ...rawPart,
                input: source.input,
                output: source.output,
                meta: source.meta,
                detail_loaded: true,
                ...(source.display ? { display: source.display } : {}),
              }
            : rawPart;
          return hydrateHarnessPart({
            ...merged,
            session_id: sessionId,
            message_id: message.id,
          });
        });
        const content =
          message.role === "assistant" && !message.content
            ? parts
                .filter((part) => part.type === "text")
                .map((part) => part.output)
                .join("")
            : message.content;
        return { ...message, content, session_id: sessionId, parts };
      }),
    );
    const session = this.sessions.find((item) => item.id === sessionId);
    const reconciled =
      session?.status === "busy"
        ? mergeBusyFetchedMessages(cloneMessages(previous), incoming)
        : sessionStatusAtRequest === "busy"
          ? this.preserveLatestStreamFromStaleIdle(previous, incoming)
          : settleOpenStreamParts(incoming);
    for (const message of reconciled) {
      message.session_id = sessionId;
      for (const part of message.parts) {
        part.session_id = sessionId;
        part.message_id = message.id;
        if (part.detail_loaded)
          this.rememberDetail(`${sessionId}:${part.id}`, part);
      }
    }
    this.messagesBySession.set(sessionId, freezeMessages(reconciled));
    this.touchTranscript(sessionId, true);

    // A timeline response is authoritative only for gates belonging to that
    // session. Never wipe a concurrently pending gate from a child/sibling.
    for (const [id, request] of this.permissionsById) {
      if (request.session_id === sessionId) this.permissionsById.delete(id);
    }
    for (const request of timeline.permissions ?? []) {
      if (request.session_id === sessionId) {
        const enriched = request.agent_name
          ? request
          : { ...request, agent_name: session?.agent_name };
        this.permissionsById.set(request.request_id, {
          ...enriched,
          status: "pending",
        });
      }
    }
    for (const [id, request] of this.questionsById) {
      if (request.session_id === sessionId) this.questionsById.delete(id);
    }
    for (const request of timeline.questions ?? []) {
      if (request.session_id === sessionId) {
        const enriched = request.agent_name
          ? request
          : { ...request, agent_name: session?.agent_name };
        this.questionsById.set(request.request_id, {
          ...enriched,
          status: "pending",
        });
      }
    }
    this.publish();
  }

  private preserveLatestStreamFromStaleIdle(
    previous: HarnessMessage[],
    incoming: HarnessMessage[],
  ): HarnessMessage[] {
    const priorLatest = [...previous]
      .reverse()
      .find((message) => message.role === "assistant");
    if (!priorLatest) return settleOpenStreamParts(incoming);
    const existingIndex = incoming.findIndex(
      (message) => message.id === priorLatest.id,
    );
    const next = [...incoming];
    if (existingIndex >= 0) {
      const server = cloneMessage(incoming[existingIndex]!);
      const live = cloneMessage(priorLatest);
      if (server.completed_at == null) {
        server.content =
          live.content.length > server.content.length
            ? live.content
            : server.content;
        const parts = new Map(server.parts.map((part) => [part.id, part]));
        for (const livePart of live.parts) {
          const stored = parts.get(livePart.id);
          if (!stored) {
            server.parts.push(livePart);
          } else if (stored.output.length < livePart.output.length) {
            Object.assign(stored, livePart);
          }
        }
        server.parts = sortHarnessParts(server.parts);
        server.model = server.model || live.model;
        server.reasoning_effort =
          server.reasoning_effort || live.reasoning_effort;
        next[existingIndex] = server;
      }
    } else if (priorLatest.completed_at == null) {
      next.push(cloneMessage(priorLatest));
    }
    return sortHarnessMessages(next);
  }

  private async fetchTodos(
    sessionId: string,
    context: SelectionContext,
    refreshAfterExisting = false,
  ): Promise<void> {
    const existing = this.todoFlights.get(sessionId);
    if (
      existing &&
      existing.workspaceGeneration === context.workspaceGeneration &&
      existing.selectionGeneration === context.selectionGeneration
    ) {
      if (!refreshAfterExisting) return existing.promise;
      await existing.promise.catch(() => undefined);
      if (!this.selectionIsCurrent(context, sessionId)) return;
      return this.fetchTodos(sessionId, context);
    }
    if (!this.selectionIsCurrent(context, sessionId)) return;
    const revision = (this.todoRevisions.get(sessionId) ?? 0) + 1;
    this.todoRevisions.set(sessionId, revision);
    const flight: TodoFlight = {
      ...context,
      revision,
      promise: Promise.resolve(),
    };
    flight.promise = (async () => {
      try {
        // The authenticated API client owns generic REST requests; match the
        // server endpoint used by the shared web harness service.
        const todos = await this.client.api.get<HarnessTodo[]>(
          `/harness/sessions/${encodeURIComponent(sessionId)}/todos`,
        );
        if (
          !this.selectionIsCurrent(context, sessionId) ||
          this.todoRevisions.get(sessionId) !== revision
        )
          return;
        this.todosBySession.set(sessionId, todos.map(cloneTodo));
        this.publish();
      } catch (error) {
        if (
          this.selectionIsCurrent(context, sessionId) &&
          this.todoRevisions.get(sessionId) === revision
        ) {
          this.setError(error, "Failed to load todos");
          throw error;
        }
      }
    })().finally(() => {
      if (this.todoFlights.get(sessionId) === flight)
        this.todoFlights.delete(sessionId);
      this.trimInactiveTranscripts();
    });
    this.todoFlights.set(sessionId, flight);
    return flight.promise;
  }

  private async refreshTimelineAfterIdle(
    sessionId: string,
    context: SelectionContext,
  ): Promise<void> {
    const ongoing = this.timelineFlights.get(sessionId);
    if (ongoing) await ongoing.promise.catch(() => undefined);
    if (!this.selectionIsCurrent(context, sessionId)) return;
    await this.fetchTimeline(sessionId, context);
  }

  private async refreshTodosAfterIdle(
    sessionId: string,
    context: SelectionContext,
  ): Promise<void> {
    const ongoing = this.todoFlights.get(sessionId);
    if (ongoing) await ongoing.promise.catch(() => undefined);
    if (!this.selectionIsCurrent(context, sessionId)) return;
    await this.fetchTodos(sessionId, context);
  }

  private isInActiveLineage(sessionId: string): boolean {
    return Boolean(
      this.activeSessionId &&
      collectDescendantSessionIds(this.activeSessionId, this.sessions).includes(
        sessionId,
      ),
    );
  }

  /** Find/create an event message in a copied messages array. */
  private prepareEventMessages(
    sessionId: string,
    messageId?: string,
  ): HarnessMessage[] {
    const current = this.messagesBySession.get(sessionId) ?? [];
    const next = [...current];
    const index = messageId
      ? current.findIndex((message) => message.id === messageId)
      : current.findIndex(
          (message, at) =>
            at === current.length - 1 &&
            message.role === "assistant" &&
            message.completed_at == null,
        );
    if (index >= 0) next[index] = cloneMessage(current[index]!);
    else if (!messageId) {
      const prior = current[current.length - 1];
      if (prior?.role === "assistant" && prior.completed_at != null) {
        const latest = cloneMessage(prior);
        next[next.length - 1] = latest;
      }
    }
    return next;
  }

  private commitEventMessages(
    sessionId: string,
    messages: HarnessMessage[],
  ): void {
    this.messagesBySession.set(sessionId, freezeMessages(messages));
    this.touchTranscript(sessionId);
    this.publish();
  }

  private onPartUpdated(data: HarnessPartUpdatedEvent): void {
    if (!this.ownsEvent(data)) return;
    const current = this.messagesBySession.get(data.session_id) ?? [];
    const matching = current.find((message) => message.id === data.message_id);
    if (
      matching &&
      (matching.role !== "assistant" || matching.completed_at != null)
    )
      return;
    const messages = applyPartDeltaImmutable(
      current,
      data.session_id,
      data.delta,
      {
        step: data.step,
        partId: data.part_id,
        partPosition: data.part_position,
        messageId: data.message_id,
      },
    );
    const session = this.sessions.find((item) => item.id === data.session_id);
    const assistant = messages.find(
      (message) => message.id === data.message_id,
    );
    if (assistant?.role === "assistant" && assistant.completed_at == null) {
      if (!assistant.model && session?.model) assistant.model = session.model;
      if (!assistant.reasoning_effort && session?.reasoning_effort)
        assistant.reasoning_effort = session.reasoning_effort;
    }
    this.commitEventMessages(data.session_id, messages);
    if (data.delta.compaction && this.isInActiveLineage(data.session_id)) {
      const context = this.captureSelection();
      if (context)
        void this.fetchTimeline(data.session_id, context, true).catch(
          () => undefined,
        );
    }
  }

  private onTodoUpdated(event: HarnessTodoUpdatedEvent): void {
    if (!this.ownsEvent(event)) return;
    const current = this.todosBySession.get(event.session_id) ?? [];
    this.todosBySession.set(
      event.session_id,
      applyTodoUpdate(current, { todos: event.todos }).map(cloneTodo),
    );
    this.publish();
  }

  private onSubtaskStarted(event: HarnessSubtaskStartedEvent): void {
    if (!this.ownsEvent(event)) return;
    const messages = this.prepareEventMessages(
      event.session_id,
      event.message_id,
    );
    const message = messages.find((row) => row.id === event.message_id);
    const target =
      message?.role === "assistant"
        ? message
        : routeAssistantMessage(messages, event.session_id, event.message_id);
    applySubtaskStarted(target, event.session_id, {
      ...event,
      workspace_id: this.workspaceId ?? event.workspace_id,
    });
    target.parts = sortHarnessParts(target.parts);
    const session = this.sessions.find((item) => item.id === event.session_id);
    if (!target.model && session?.model) target.model = session.model;
    if (!target.reasoning_effort && session?.reasoning_effort)
      target.reasoning_effort = session.reasoning_effort;
    this.commitEventMessages(event.session_id, messages);
    if (event.child_session_id) {
      this.trustedChildSessionIds.add(event.child_session_id);
      const shouldLoadChild = this.isInActiveLineage(event.session_id);
      void this.refresh()
        .then(() => {
          const context = this.captureSelection();
          if (
            shouldLoadChild &&
            context &&
            this.isInActiveLineage(event.session_id) &&
            this.sessions.some(
              (session) => session.id === event.child_session_id,
            )
          ) {
            return this.fetchTimeline(event.child_session_id!, context);
          }
        })
        .catch(() => undefined);
    }
  }

  private onSubtaskFinished(event: HarnessSubtaskFinishedEvent): void {
    if (!this.ownsEvent(event)) return;
    const messages = this.prepareEventMessages(
      event.session_id,
      event.message_id,
    );
    const message = messages.find((row) => row.id === event.message_id);
    if (
      !message ||
      message.role !== "assistant" ||
      message.completed_at != null
    )
      return;
    applySubtaskFinished(message, {
      ...event,
      workspace_id: this.workspaceId ?? event.workspace_id,
    });
    this.commitEventMessages(event.session_id, messages);
  }

  private onSessionStatus(event: HarnessSessionStatusEvent): void {
    if (!this.ownsEvent(event)) return;
    const session = this.sessions.find((item) => item.id === event.session_id);
    if (session) {
      const next = {
        ...session,
        status: event.status,
        ...(event.model ? { model: event.model } : {}),
        ...(event.reasoning_effort !== undefined
          ? { reasoning_effort: event.reasoning_effort }
          : {}),
        ...(!session.manual_unread && event.status === "busy"
          ? { unread: false }
          : {}),
      };
      this.replaceSession(event.session_id, next);
      this.bumpSessionRevision(event.session_id);
    }
    if (event.status === "busy") {
      if (event.message_id) {
        const messages = this.prepareEventMessages(
          event.session_id,
          event.message_id,
        );
        ensureBusyAssistant(
          messages,
          event.session_id,
          event.message_id,
          event.user_message_id,
        );
        const target = messages.find(
          (message) => message.id === event.message_id,
        );
        if (target && target.role === "assistant") {
          if (event.model) target.model = event.model;
          else if (!target.model && session?.model)
            target.model = session.model;
          if (event.reasoning_effort !== undefined)
            target.reasoning_effort = event.reasoning_effort;
          else if (!target.reasoning_effort && session?.reasoning_effort)
            target.reasoning_effort = session.reasoning_effort;
        }
        this.messagesBySession.set(event.session_id, freezeMessages(messages));
        this.touchTranscript(event.session_id);
      }
      this.publish();
      return;
    }

    if (event.status === "idle") {
      const messages = this.prepareEventMessages(
        event.session_id,
        event.message_id,
      );
      const target = event.message_id
        ? messages.find((message) => message.id === event.message_id)
        : [...messages]
            .reverse()
            .find(
              (message) =>
                message.role === "assistant" && message.completed_at == null,
            );
      if (target?.role === "assistant" && target.completed_at == null)
        target.completed_at = new Date().toISOString();
      this.messagesBySession.set(event.session_id, freezeMessages(messages));
      this.touchTranscript(event.session_id, true);
      const latest = this.sessions.find((item) => item.id === event.session_id);
      if (latest && !latest.manual_unread) {
        this.replaceSession(event.session_id, { ...latest, unread: true });
        this.bumpSessionRevision(event.session_id);
      }
      this.publish();
      const context = this.captureSelection();
      if (context && this.isInActiveLineage(event.session_id)) {
        void this.refreshTimelineAfterIdle(event.session_id, context).catch(
          () => undefined,
        );
        void this.refreshTodosAfterIdle(event.session_id, context).catch(
          () => undefined,
        );
      }
      if (
        this.activeSessionId === event.session_id &&
        latest &&
        !latest.manual_unread
      ) {
        this.maybeMarkRead(event.session_id);
      }
      return;
    }
    this.publish();
  }

  private onPermissionRequired(event: PermissionEvent): void {
    if (!this.eventMayBelongToWorkspace(event) || !event.request_id) return;
    if (event.decision) {
      this.permissionsById.delete(event.request_id);
      this.publish();
      return;
    }
    const session = this.sessions.find((item) => item.id === event.session_id);
    this.permissionsById.set(event.request_id, {
      request_id: event.request_id,
      session_id: event.session_id,
      workspace_id: event.workspace_id,
      tool: event.tool,
      pattern: event.pattern,
      title: event.title,
      call_id: event.call_id,
      status: "pending",
      agent_name: event.agent_name ?? session?.agent_name,
    });
    this.publish();
  }

  private onPermissionResolved(event: HarnessPermissionResolvedEvent): void {
    if (!this.eventMayBelongToWorkspace(event)) return;
    this.permissionsById.delete(event.request_id);
    this.publish();
  }

  private onQuestionRequired(event: QuestionEvent): void {
    if (!this.eventMayBelongToWorkspace(event) || !event.request_id) return;
    if (event.status && event.status !== "pending") {
      this.questionsById.delete(event.request_id);
      this.publish();
      return;
    }
    const session = this.sessions.find((item) => item.id === event.session_id);
    this.questionsById.set(event.request_id, {
      request_id: event.request_id,
      session_id: event.session_id,
      workspace_id: event.workspace_id,
      questions: (event.questions ?? []).map((question) => ({ ...question })),
      call_id: event.call_id,
      status: "pending",
      agent_name: event.agent_name ?? session?.agent_name,
    });
    this.publish();
  }

  private onQuestionResolved(event: HarnessQuestionResolvedEvent): void {
    if (!this.eventMayBelongToWorkspace(event)) return;
    this.questionsById.delete(event.request_id);
    this.publish();
  }

  private replaceSession(sessionId: string, updated: HarnessSession): void {
    const index = this.sessions.findIndex(
      (session) => session.id === sessionId,
    );
    if (index < 0) return;
    this.sessions = this.sessions.map((session, at) =>
      at === index ? updated : session,
    );
  }

  private selectSession(sessionId: string | null): void {
    if (this.activeSessionId === sessionId) return;
    if (this.activeSessionId) this.touchTranscript(this.activeSessionId);
    this.activeSessionId = sessionId;
    this.selectionGeneration += 1;
    if (sessionId) this.touchTranscript(sessionId);
    this.publish();
  }

  private touchTranscript(sessionId: string, recalculate = false): void {
    const messages = this.messagesBySession.get(sessionId);
    if (!messages) return;
    const bytes =
      recalculate || !this.transcriptLru.has(sessionId)
        ? sessionByteEstimate(messages)
        : this.transcriptLru.get(sessionId)!;
    this.transcriptLru.delete(sessionId);
    this.transcriptLru.set(sessionId, bytes);
  }

  private trimInactiveTranscripts(): void {
    let count = 0;
    let bytes = 0;
    for (const [sessionId, estimate] of this.transcriptLru) {
      if (sessionId === this.activeSessionId) continue;
      const session = this.sessions.find((row) => row.id === sessionId);
      const hasGate = [
        ...this.permissionsById.values(),
        ...this.questionsById.values(),
      ].some((request) => request.session_id === sessionId);
      if (session?.status === "busy" || hasGate) continue;
      count += 1;
      bytes += estimate;
    }
    if (
      count <= INACTIVE_TRANSCRIPT_LIMIT &&
      bytes <= INACTIVE_TRANSCRIPT_BUDGET_BYTES
    )
      return;
    for (const sessionId of [...this.transcriptLru.keys()]) {
      if (
        count <= INACTIVE_TRANSCRIPT_LIMIT &&
        bytes <= INACTIVE_TRANSCRIPT_BUDGET_BYTES
      )
        break;
      if (sessionId === this.activeSessionId) continue;
      const session = this.sessions.find((row) => row.id === sessionId);
      const hasGate = [
        ...this.permissionsById.values(),
        ...this.questionsById.values(),
      ].some((request) => request.session_id === sessionId);
      if (session?.status === "busy" || hasGate) continue;
      const estimate = this.transcriptLru.get(sessionId) ?? 0;
      this.forgetSession(sessionId);
      count -= 1;
      bytes -= estimate;
    }
  }

  private assertLive(): void {
    if (this.disposed) throw new Error("Chat controller has been disposed");
  }

  /** Release realtime room/listeners and invalidate any in-flight work. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.workspaceGeneration += 1;
    this.selectionGeneration += 1;
    this.releaseWorkspace?.();
    this.releaseWorkspace = null;
    for (const detach of this.detachRealtime.splice(0)) detach();
    this.listeners.clear();
    this.timelineFlights.clear();
    this.todoFlights.clear();
    this.detailFlights.clear();
    this.pinnedDetailKeys.clear();
    this.trustedChildSessionIds.clear();
    this.sendFlights.clear();
    this.createFlights.clear();
    this.readFlights.clear();
    this.unreadFlights.clear();
  }
}

const EMPTY_TODOS = Object.freeze([]) as unknown as HarnessTodo[];
