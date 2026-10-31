import type { OpenCuriaClient } from "@opencuria/client";
import {
  base64ToBytes,
  createUtf8StreamDecoder,
  encodeTerminalInput,
} from "../utf8";

export type TerminalStatus =
  | "idle"
  | "starting"
  | "running"
  | "ended"
  | "error";

export interface TerminalSnapshot {
  status: TerminalStatus;
  error: string | null;
  terminalId: string | null;
  /** Bumped whenever the retained output is replaced (new shell, clear). */
  generation: number;
}

export interface TerminalGrid {
  cols: number;
  rows: number;
}

/** Retained output replayed into a remounted terminal view. */
export const MAX_TERMINAL_HISTORY = 512 * 1024;
const DEFAULT_GRID: TerminalGrid = { cols: 80, rows: 24 };
const NOTICE_ENDED = "\r\n\u001b[33m[Terminal session ended]\u001b[0m\r\n";
const NOTICE_RECONNECTED =
  "\r\n\u001b[33m[Connection restored — started a new shell]\u001b[0m\r\n";

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "Failed to start terminal";
}

/** Drop the oldest output at a line start so replay stays readable. */
export function trimTerminalHistory(
  history: string,
  max = MAX_TERMINAL_HISTORY,
): string {
  if (history.length <= max) return history;
  const cut = history.length - max;
  const lineStart = history.indexOf("\n", cut);
  return history.slice(lineStart === -1 ? cut : lineStart + 1);
}

/**
 * One workspace PTY for the mobile terminal. The session outlives the tools
 * sheet: closing and reopening it shows the same shell and its retained
 * output. Output arrives as base64 bytes on the workspace room and is
 * decoded as a UTF-8 stream; all events are filtered by this terminal's id.
 */
export class TerminalSession {
  private snapshot: TerminalSnapshot = {
    status: "idle",
    error: null,
    terminalId: null,
    generation: 0,
  };
  private readonly listeners = new Set<() => void>();
  private readonly outputListeners = new Set<(data: string) => void>();
  private readonly decoder = createUtf8StreamDecoder();
  private history = "";
  private grid: TerminalGrid = DEFAULT_GRID;
  private sentGrid: TerminalGrid | null = null;
  private taskId: string | null = null;
  private startAttempt = 0;
  /** `terminal:started`/`workspace:error` that arrived before the start POST resolved. */
  private early = new Map<string, { terminalId?: string; error?: string }>();
  private readonly cleanups: Array<() => void> = [];
  private disposed = false;

  constructor(
    private readonly client: OpenCuriaClient,
    readonly workspaceId: string,
  ) {
    const realtime = client.realtime;
    this.cleanups.push(
      realtime.subscribeWorkspace(workspaceId),
      realtime.on("terminal:started", (event) => {
        if (event.workspace_id !== workspaceId || !event.task_id) return;
        this.onStarted(event.task_id, event.terminal_id);
      }),
      realtime.on("workspace:error", (event) => {
        if (event.workspace_id !== workspaceId || !event.task_id) return;
        this.onStartError(event.task_id, event.error);
      }),
      realtime.on("terminal:output", (event) => {
        if (
          event.workspace_id !== workspaceId ||
          event.terminal_id !== this.snapshot.terminalId
        )
          return;
        this.append(this.decoder.decode(base64ToBytes(event.data)));
      }),
      realtime.on("terminal:closed", (event) => {
        if (
          event.workspace_id !== workspaceId ||
          event.terminal_id !== this.snapshot.terminalId
        )
          return;
        this.append(NOTICE_ENDED);
        this.set({ status: "ended", terminalId: null });
      }),
      realtime.onReconnect(() => {
        // The server-side PTY does not survive a reconnect.
        const { status } = this.snapshot;
        if (status !== "running" && status !== "starting") return;
        this.append(NOTICE_RECONNECTED);
        this.launch();
      }),
    );
  }

  readonly getSnapshot = (): TerminalSnapshot => this.snapshot;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Live output after `getHistory()`; replay history on generation changes. */
  onOutput(listener: (data: string) => void): () => void {
    this.outputListeners.add(listener);
    return () => this.outputListeners.delete(listener);
  }

  getHistory(): string {
    return this.history;
  }

  private set(patch: Partial<TerminalSnapshot>): void {
    if (this.disposed) return;
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  private append(data: string): void {
    if (!data || this.disposed) return;
    this.history = trimTerminalHistory(this.history + data);
    for (const listener of this.outputListeners) listener(data);
  }

  private replaceHistory(text: string): void {
    this.history = text;
    this.decoder.reset();
    this.set({ generation: this.snapshot.generation + 1 });
  }

  /** Start a shell unless one is running or starting. */
  start(): void {
    const { status } = this.snapshot;
    if (status === "running" || status === "starting" || this.disposed) return;
    if (status === "ended" || status === "error") this.replaceHistory("");
    this.launch();
  }

  /** Close the current shell and start a fresh one. */
  restart(): void {
    if (this.disposed) return;
    const terminalId = this.snapshot.terminalId;
    if (terminalId) this.client.terminal.close(this.workspaceId, terminalId);
    this.replaceHistory("");
    this.launch();
  }

  private launch(): void {
    const attempt = ++this.startAttempt;
    this.taskId = null;
    this.sentGrid = null;
    this.set({ status: "starting", error: null, terminalId: null });
    const { cols, rows } = this.grid;
    this.client.terminal
      .start(this.workspaceId, cols, rows)
      .then(({ task_id: taskId }) => {
        if (attempt !== this.startAttempt || this.disposed) return;
        this.taskId = taskId;
        const early = this.early.get(taskId);
        this.early.clear();
        if (early?.terminalId) this.onStarted(taskId, early.terminalId);
        else if (early?.error) this.onStartError(taskId, early.error);
      })
      .catch((error: unknown) => {
        if (attempt !== this.startAttempt || this.disposed) return;
        this.set({ status: "error", error: errorMessage(error) });
      });
  }

  private onStarted(taskId: string, terminalId: string): void {
    if (this.snapshot.status !== "starting") return;
    if (this.taskId === null) {
      this.early.set(taskId, { terminalId });
      return;
    }
    if (taskId !== this.taskId) return;
    this.set({ status: "running", terminalId, error: null });
    this.flushResize();
  }

  private onStartError(taskId: string, error: string): void {
    if (this.snapshot.status !== "starting") return;
    if (this.taskId === null) {
      this.early.set(taskId, { error });
      return;
    }
    if (taskId !== this.taskId) return;
    this.set({ status: "error", error: error || "Failed to start terminal" });
  }

  /** Send keyboard input; false while no shell is running. */
  write(text: string): boolean {
    const { status, terminalId } = this.snapshot;
    if (!text || status !== "running" || !terminalId) return false;
    return this.client.terminal.input(
      this.workspaceId,
      terminalId,
      encodeTerminalInput(text),
    );
  }

  /** The view measured a new grid; the PTY follows once it runs. */
  resize(grid: TerminalGrid): void {
    const cols = Math.max(1, Math.floor(grid.cols));
    const rows = Math.max(1, Math.floor(grid.rows));
    this.grid = { cols, rows };
    this.flushResize();
  }

  get size(): TerminalGrid {
    return this.grid;
  }

  private flushResize(): void {
    const { terminalId, status } = this.snapshot;
    if (status !== "running" || !terminalId) return;
    if (
      this.sentGrid?.cols === this.grid.cols &&
      this.sentGrid.rows === this.grid.rows
    )
      return;
    this.sentGrid = this.grid;
    this.client.terminal.resize(
      this.workspaceId,
      terminalId,
      this.grid.cols,
      this.grid.rows,
    );
  }

  /** Clear the screen and retained output, then let the shell redraw its prompt. */
  clear(): void {
    this.replaceHistory("");
    this.write("\u000c");
  }

  /** Close the PTY and stop listening; the session cannot be reused. */
  dispose(): void {
    if (this.disposed) return;
    const terminalId = this.snapshot.terminalId;
    if (terminalId) this.client.terminal.close(this.workspaceId, terminalId);
    this.disposed = true;
    this.startAttempt += 1;
    for (const cleanup of this.cleanups.splice(0)) cleanup();
    this.listeners.clear();
    this.outputListeners.clear();
  }
}

const registry = new WeakMap<OpenCuriaClient, Map<string, TerminalSession>>();

/** The workspace's terminal session for this client, created on first use. */
export function getTerminalSession(
  client: OpenCuriaClient,
  workspaceId: string,
): TerminalSession {
  let sessions = registry.get(client);
  if (!sessions) {
    sessions = new Map();
    registry.set(client, sessions);
  }
  let session = sessions.get(workspaceId);
  if (!session) {
    session = new TerminalSession(client, workspaceId);
    sessions.set(workspaceId, session);
  }
  return session;
}

/** Close every terminal of a client (sign-out, organization or server change). */
export function releaseTerminalSessions(client: OpenCuriaClient): void {
  const sessions = registry.get(client);
  if (!sessions) return;
  registry.delete(client);
  for (const session of sessions.values()) {
    try {
      session.dispose();
    } catch {
      /* A disposed realtime client already dropped its listeners. */
    }
  }
}
