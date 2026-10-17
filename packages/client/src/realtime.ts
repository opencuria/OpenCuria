import { io, type Socket } from "socket.io-client";
import {
  CHUNK_TRANSFER_TIMEOUT_MS,
  FILE_CHUNK_B64_SIZE,
  MAX_READ_CHUNKS_PER_TRANSFER,
  UPLOAD_MAX_BYTES,
  createChunkedTransferStore,
  decodedBase64Size,
  splitBase64Chunks,
  stripBase64Whitespace,
} from "@opencuria/chat-core/fileChunks";
import type {
  HarnessPartUpdatedEvent,
  HarnessPermissionRequiredEvent,
  HarnessPermissionResolvedEvent,
  HarnessQuestionRequiredEvent,
  HarnessQuestionResolvedEvent,
  HarnessSessionStatusEvent,
  HarnessSessionSummaryEvent,
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodoUpdatedEvent,
} from "@opencuria/chat-core/harness";
import type {
  FileChunkEvent,
  FileContentResult,
  FileDownloadResult,
  FileFindResult,
  FileFindResultEvent,
  FileListResult,
  FileUploadResult,
} from "./types";

export interface RealtimeEventMap {
  "workspace:status_changed": {
    workspace_id: string;
    status: string;
    [key: string]: unknown;
  };
  "workspace:operation_changed": {
    workspace_id: string;
    active_operation: string | null;
    [key: string]: unknown;
  };
  "workspace:error": { workspace_id: string; task_id: string; error: string };
  "harness.part_updated": HarnessPartUpdatedEvent;
  "harness.permission_required": HarnessPermissionRequiredEvent;
  "harness.permission_resolved": HarnessPermissionResolvedEvent;
  "harness.question_required": HarnessQuestionRequiredEvent;
  "harness.question_resolved": HarnessQuestionResolvedEvent;
  "harness.session_status": HarnessSessionStatusEvent;
  "harness.session_summary": HarnessSessionSummaryEvent;
  "harness.conversations_changed": { workspace_id: string };
  "harness.todo_updated": HarnessTodoUpdatedEvent;
  "harness.subtask_started": HarnessSubtaskStartedEvent;
  "harness.subtask_finished": HarnessSubtaskFinishedEvent;
  "files:list_result": FileListResult;
  "files:find_result": FileFindResultEvent;
  "files:content_result": FileContentResult;
  "files:content_chunk": FileChunkEvent;
  "files:upload_result": FileUploadResult;
  "files:download_result": FileDownloadResult;
  "files:download_chunk": FileChunkEvent;
}

export type RealtimeEventName = keyof RealtimeEventMap | (string & {});
export type SocketAuth = (
  callback: (data: Record<string, string>) => void,
) => void;

/** Small Socket.IO subset that also permits deterministic mock sockets in tests. */
export interface RealtimeSocket {
  connected: boolean;
  active?: boolean;
  volatile?: { emit(event: string, ...args: any[]): RealtimeSocket };
  on(event: string, callback: (...args: any[]) => void): this;
  off(event: string, callback?: (...args: any[]) => void): this;
  emit(event: string, ...args: any[]): this;
  connect(): this;
  disconnect(): this;
}

export type SocketFactory = (
  uri: string,
  options: {
    path: string;
    transports: string[];
    reconnection: boolean;
    reconnectionDelay: number;
    reconnectionDelayMax: number;
    auth: SocketAuth;
  },
) => RealtimeSocket;

export interface RealtimeOptions {
  wsBaseUrl: string;
  getAccessToken: () => string | null;
  /** Context string changes when the API base URL, org, or auth generation changes. */
  getContextKey: () => string;
  onContextChange?: (callback: () => void) => () => void;
  refreshAccessToken: () => Promise<boolean>;
  socketFactory?: SocketFactory;
  timeoutMs?: number;
  createRequestId?: () => string;
}

export class FileTransferError extends Error {
  readonly code: string;
  constructor(message: string, code = "file_transfer_error") {
    super(message);
    this.name = "FileTransferError";
    this.code = code;
  }
}

type AnyCallback = (data: unknown) => void;
type PendingFile = {
  requestId: string;
  workspaceId: string;
  path: string;
  requestedFilename?: string;
  kind: "list" | "find" | "read" | "download" | "upload";
  resolve: (data: any) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  releaseRoom?: () => void;
};

// Runner file downloads are capped at 100 MiB raw (≈ 133.4 MiB base64).
const MAX_FILE_RESULT_CHARS = 140 * 1024 * 1024;

function makeId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `file-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizedPath(path: string): string {
  return path || "/workspace";
}

function isSafeWorkspacePath(path: unknown): path is string {
  if (
    typeof path !== "string" ||
    !(path === "/workspace" || path.startsWith("/workspace/"))
  )
    return false;
  if (path.includes("\\") || /[\u0000-\u001f\u007f]/.test(path)) return false;
  return path.split("/").every((part) => part !== "." && part !== "..");
}

function errorFromPayload(
  payload: { error?: string; code?: string },
  fallback: string,
): Error | null {
  if (!payload.error) return null;
  return new FileTransferError(
    payload.error || fallback,
    payload.code || "file_transfer_error",
  );
}

/**
 * Framework-neutral Socket.IO event client and request-correlated workspace file transport.
 * Logical event listeners and workspace room ownership survive transport reconnects.
 */
export class RealtimeClient {
  private socket: RealtimeSocket | null = null;
  private active = true;
  private disposed = false;
  private everConnected = false;
  private detachContextListener: (() => void) | undefined;
  private listeners = new Map<string, Set<AnyCallback>>();
  private listenerBindings = new WeakMap<
    RealtimeSocket,
    Map<string, Map<AnyCallback, AnyCallback>>
  >();
  private listenerSubscriptions = new Map<
    string,
    Map<AnyCallback, () => void>
  >();
  private workspaceOwners = new Map<string, number>();
  private workspaceEpoch = 0;
  private reconnectListeners = new Set<() => void>();
  private pendingFiles = new Map<string, PendingFile>();
  private contextKey: string;
  private readonly timeoutMs: number;
  private readonly chunks = createChunkedTransferStore({
    maxChars: MAX_FILE_RESULT_CHARS,
    maxEntries: 16,
    maxChunks: MAX_READ_CHUNKS_PER_TRANSFER,
    onTimeout: (id) =>
      this.failPending(
        id,
        new FileTransferError("File transfer timed out", "timeout"),
      ),
  });

  constructor(private readonly options: RealtimeOptions) {
    this.timeoutMs = Math.max(
      1,
      options.timeoutMs ?? CHUNK_TRANSFER_TIMEOUT_MS,
    );
    this.contextKey = options.getContextKey();
  }

  connect(): void {
    this.assertNotDisposed();
    if (!this.detachContextListener && this.options.onContextChange) {
      this.detachContextListener = this.options.onContextChange(() =>
        this.resetForContextChange(),
      );
    }
    this.resetForContextChange(false);
    if (!this.active) return;
    if (this.socket) {
      if (!this.socket.connected && !this.socket.active) this.socket.connect();
      return;
    }
    const factory =
      this.options.socketFactory ??
      ((uri, opts) => io(uri, opts as any) as unknown as Socket);
    const base = this.options.wsBaseUrl.replace(/\/+$/, "");
    const server = base ? new URL(base) : null;
    const prefix = server?.pathname.replace(/\/+$/, "") ?? "";
    const socket = factory(server ? `${server.origin}/frontend` : "/frontend", {
      path: `${prefix}/ws/runner`,
      transports: ["websocket", "polling"],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 10000,
      auth: (callback) => {
        const token = this.options.getAccessToken();
        callback(token ? { token } : {});
      },
    });
    this.socket = socket;
    this.bindSocket(socket);
  }

  disconnect(): void {
    if (this.disposed) return;
    this.rejectAllFiles(
      new FileTransferError("Socket disconnected", "disconnected"),
    );
    this.socket?.disconnect();
  }

  /** Permanently release listeners, rooms, socket, and pending file requests. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.detachContextListener?.();
    this.detachContextListener = undefined;
    this.rejectAllFiles(
      new FileTransferError("Realtime client disposed", "disposed"),
    );
    this.detachSocket();
    this.listeners.clear();
    this.listenerSubscriptions.clear();
    this.workspaceOwners.clear();
    this.reconnectListeners.clear();
  }

  /** Pause or resume native foreground transport without losing logical subscriptions. */
  setActive(active: boolean): void {
    this.assertNotDisposed();
    this.active = active;
    if (!active) {
      this.rejectAllFiles(
        new FileTransferError("Socket suspended", "disconnected"),
      );
      this.socket?.disconnect();
    } else {
      this.connect();
    }
  }

  on<E extends keyof RealtimeEventMap>(
    event: E,
    callback: (data: RealtimeEventMap[E]) => void,
  ): () => void;
  on<E extends string>(
    event: E,
    callback: (
      data: E extends keyof RealtimeEventMap ? RealtimeEventMap[E] : unknown,
    ) => void,
  ): () => void;
  on(event: string, callback: (data: unknown) => void): () => void {
    this.assertNotDisposed();
    const handlers = this.listeners.get(event) ?? new Set<AnyCallback>();
    const handler = callback as AnyCallback;
    handlers.add(handler);
    this.listeners.set(event, handlers);
    const unsubscribe = this.bindListener(this.socket, event, handler);
    const subscriptions =
      this.listenerSubscriptions.get(event) ??
      new Map<AnyCallback, () => void>();
    if (unsubscribe) subscriptions.set(handler, unsubscribe);
    this.listenerSubscriptions.set(event, subscriptions);
    return () => {
      const current = this.listeners.get(event);
      current?.delete(handler);
      this.listenerSubscriptions.get(event)?.get(handler)?.();
      this.listenerSubscriptions.get(event)?.delete(handler);
      if (current?.size === 0) this.listeners.delete(event);
      if (this.listenerSubscriptions.get(event)?.size === 0)
        this.listenerSubscriptions.delete(event);
    };
  }

  subscribeWorkspace(workspaceId: string): () => void {
    this.assertNotDisposed();
    this.resetForContextChange();
    const epoch = this.workspaceEpoch;
    const releaseOwner = this.addWorkspaceOwner(workspaceId);
    let released = false;
    return () => {
      if (released || this.disposed) return;
      released = true;
      if (epoch === this.workspaceEpoch) releaseOwner();
    };
  }

  onReconnect(callback: () => void): () => void {
    this.assertNotDisposed();
    this.reconnectListeners.add(callback);
    return () => this.reconnectListeners.delete(callback);
  }

  /** Releasing a context-changed subscription is intentionally a no-op. Obtain
   * fresh workspace handles after organization, auth, or API-base changes.
   */
  get isConnected(): boolean {
    return Boolean(this.socket?.connected);
  }

  get status(): "connected" | "connecting" | "disconnected" | "disposed" {
    if (this.disposed) return "disposed";
    if (this.socket?.connected) return "connected";
    if (this.active && this.socket) return "connecting";
    return "disconnected";
  }

  /** Search workspace-relative names. Wire `{path,name}` entries become full paths at the client boundary. */
  async findFiles(workspaceId: string, query: string): Promise<FileFindResult> {
    const result = await this.requestFile<FileFindResultEvent>(
      "find",
      workspaceId,
      query,
      "files:find_result",
      "frontend:files_find",
      { query, limit: 50 },
    );
    return {
      ...result,
      paths: result.paths.flatMap((entry) => {
        const path = typeof entry === "string" ? entry : entry?.path;
        // Require absolute paths in /workspace. Do not synthesize from a name;
        // this avoids turning malicious/out-of-root names into trusted targets.
        return isSafeWorkspacePath(path) ? [path] : [];
      }),
    };
  }

  listFiles(workspaceId: string, path: string): Promise<FileListResult> {
    const wantedPath = normalizedPath(path);
    return this.requestFile(
      "list",
      workspaceId,
      wantedPath,
      "files:list_result",
      "frontend:files_list",
      { path: wantedPath },
    );
  }

  readFile(
    workspaceId: string,
    path: string,
    maxSize?: number,
  ): Promise<FileContentResult> {
    const wantedPath = normalizedPath(path);
    return this.requestFile(
      "read",
      workspaceId,
      wantedPath,
      "files:content_result",
      "frontend:files_read",
      {
        path: wantedPath,
        ...(maxSize === undefined ? {} : { max_size: maxSize }),
      },
    );
  }

  downloadFile(workspaceId: string, path: string): Promise<FileDownloadResult> {
    const wantedPath = normalizedPath(path);
    return this.requestFile(
      "download",
      workspaceId,
      wantedPath,
      "files:download_result",
      "frontend:files_download",
      {
        path: wantedPath,
      },
    );
  }

  uploadFile(
    workspaceId: string,
    input: { directory: string; filename: string; base64: string },
  ): Promise<FileUploadResult> {
    const path = normalizedPath(input.directory);
    const content = stripBase64Whitespace(input.base64);
    let size: number;
    try {
      size = decodedBase64Size(content);
    } catch {
      return Promise.reject(
        new FileTransferError("Invalid base64 file payload", "invalid_payload"),
      );
    }
    if (size > UPLOAD_MAX_BYTES) {
      return Promise.reject(
        new FileTransferError(
          `Upload exceeds the ${UPLOAD_MAX_BYTES / (1024 * 1024)} MB limit`,
          "size_limit",
        ),
      );
    }
    if (
      !input.filename ||
      input.filename.includes("/") ||
      input.filename.includes("\\")
    ) {
      return Promise.reject(
        new FileTransferError("Invalid filename", "validation_error"),
      );
    }
    if (!this.socket?.connected || !this.active) {
      return Promise.reject(
        new FileTransferError("Socket is not connected", "disconnected"),
      );
    }
    const requestId = this.newRequestId();
    if (!requestId || this.pendingFiles.has(requestId)) {
      return Promise.reject(
        new FileTransferError(
          "Duplicate file request id",
          "duplicate_request_id",
        ),
      );
    }
    const promise = this.registerFile("upload", workspaceId, path, requestId);
    const pending = this.pendingFiles.get(requestId);
    if (pending) pending.requestedFilename = input.filename;
    const base = { workspace_id: workspaceId, request_id: requestId };
    if (content.length <= FILE_CHUNK_B64_SIZE) {
      this.socket.emit("frontend:files_upload", {
        ...base,
        path,
        filename: input.filename,
        content,
        is_directory: false,
      });
      return promise;
    }

    const pieces = splitBase64Chunks(content);
    if (!pieces.length || pieces.length > 64) {
      this.failPending(
        requestId,
        new FileTransferError("Upload exceeds the chunk limit", "size_limit"),
      );
      return promise;
    }
    // Recheck immediately before every event. Never buffer a partial upload across disconnects.
    const emit = (event: string, payload: Record<string, unknown>): boolean => {
      if (!this.socket?.connected || !this.active || this.disposed) {
        this.failPending(
          requestId,
          new FileTransferError(
            "Socket disconnected during upload",
            "disconnected",
          ),
        );
        return false;
      }
      this.socket.emit(event, payload);
      return true;
    };
    if (
      !emit("frontend:files_upload_start", {
        ...base,
        path,
        filename: input.filename,
        is_directory: false,
        chunked: true,
        total_chunks: pieces.length,
      })
    )
      return promise;
    for (let index = 0; index < pieces.length; index += 1) {
      if (
        !emit("frontend:files_upload_chunk", {
          ...base,
          path,
          index,
          total_chunks: pieces.length,
          content: pieces[index],
        })
      )
        return promise;
    }
    emit("frontend:files_upload_finish", {
      ...base,
      path,
      total_chunks: pieces.length,
    });
    return promise;
  }

  private bindSocket(socket: RealtimeSocket): void {
    socket.on("connect", () => {
      if (socket !== this.socket || this.disposed) return;
      const reconnect = this.everConnected;
      this.everConnected = true;
      for (const workspaceId of this.workspaceOwners.keys())
        this.emitRoom("frontend:subscribe_workspace", workspaceId);
      if (reconnect)
        for (const callback of [...this.reconnectListeners]) callback();
    });
    socket.on("disconnect", () => {
      if (socket === this.socket)
        this.rejectAllFiles(
          new FileTransferError("Socket disconnected", "disconnected"),
        );
    });
    socket.on("connect_error", (error: Error) => {
      if (socket !== this.socket || !this.isAuthError(error?.message ?? ""))
        return;
      // Namespace authentication failures do not participate in Socket.IO's
      // automatic transport retry loop; explicitly reconnect after refresh.
      void this.options
        .refreshAccessToken()
        .then((refreshed) => {
          if (
            refreshed &&
            socket === this.socket &&
            this.active &&
            !this.disposed
          ) {
            socket.connect();
          }
        })
        .catch(() => undefined);
    });
    socket.on("files:content_chunk", (event: FileChunkEvent) => {
      if (socket === this.socket) this.onChunk(event, "read");
    });
    socket.on("files:download_chunk", (event: FileChunkEvent) => {
      if (socket === this.socket) this.onChunk(event, "download");
    });
    for (const event of [
      "files:list_result",
      "files:find_result",
      "files:content_result",
      "files:upload_result",
      "files:download_result",
    ])
      socket.on(event, (data: any) => {
        if (socket === this.socket) this.onFileResult(event, data);
      });
    for (const [event, callbacks] of this.listeners) {
      const subscriptions =
        this.listenerSubscriptions.get(event) ??
        new Map<AnyCallback, () => void>();
      for (const callback of callbacks) {
        const unsubscribe = this.bindListener(socket, event, callback);
        if (unsubscribe) subscriptions.set(callback, unsubscribe);
      }
      this.listenerSubscriptions.set(event, subscriptions);
    }
  }

  private bindListener(
    socket: RealtimeSocket | null,
    event: string,
    callback: AnyCallback,
  ): (() => void) | undefined {
    if (!socket) return undefined;
    const prior = this.listenerBindings.get(socket)?.get(event)?.get(callback);
    if (prior) return () => socket.off(event, prior);
    const binding = (data: unknown): void => {
      if (socket === this.socket) callback(data);
    };
    const events =
      this.listenerBindings.get(socket) ??
      new Map<string, Map<AnyCallback, AnyCallback>>();
    const bindings = events.get(event) ?? new Map<AnyCallback, AnyCallback>();
    bindings.set(callback, binding);
    events.set(event, bindings);
    this.listenerBindings.set(socket, events);
    socket.on(event, binding);
    return () => {
      socket.off(event, binding);
      bindings.delete(callback);
    };
  }

  private detachSocket(): void {
    if (!this.socket) return;
    this.socket.disconnect();
    this.socket = null;
    this.everConnected = false;
  }

  private resetForContextChange(reconnect = true): void {
    if (this.disposed) return;
    const next = this.options.getContextKey();
    if (next === this.contextKey) return;
    this.contextKey = next;
    this.rejectAllFiles(
      new FileTransferError(
        "Authentication or organization context changed",
        "context_changed",
      ),
    );
    for (const subscriptions of this.listenerSubscriptions.values()) {
      for (const unsubscribe of subscriptions.values()) unsubscribe();
    }
    this.listenerSubscriptions.clear();
    this.workspaceOwners.clear();
    this.workspaceEpoch += 1;
    this.detachSocket();
    if (reconnect && this.active) this.connect();
  }

  private addWorkspaceOwner(workspaceId: string): () => void {
    const count = this.workspaceOwners.get(workspaceId) ?? 0;
    this.workspaceOwners.set(workspaceId, count + 1);
    if (count === 0) this.emitRoom("frontend:subscribe_workspace", workspaceId);
    let released = false;
    return () => {
      if (released || this.disposed) return;
      released = true;
      const current = this.workspaceOwners.get(workspaceId) ?? 0;
      if (current <= 1) {
        this.workspaceOwners.delete(workspaceId);
        this.emitRoom("frontend:unsubscribe_workspace", workspaceId);
      } else {
        this.workspaceOwners.set(workspaceId, current - 1);
      }
    };
  }

  private emitRoom(event: string, workspaceId: string): void {
    if (this.socket?.connected)
      this.socket.emit(event, { workspace_id: workspaceId });
  }

  private requestFile<T>(
    kind: PendingFile["kind"],
    workspaceId: string,
    path: string,
    resultEvent: string,
    requestEvent: string,
    extra: Record<string, unknown>,
  ): Promise<T> {
    this.assertNotDisposed();
    this.resetForContextChange();
    if (!this.socket?.connected || !this.active) {
      return Promise.reject(
        new FileTransferError("Socket is not connected", "disconnected"),
      );
    }
    const requestId = this.newRequestId();
    if (!requestId || this.pendingFiles.has(requestId)) {
      return Promise.reject(
        new FileTransferError(
          "Duplicate file request id",
          "duplicate_request_id",
        ),
      );
    }
    const promise = this.registerFile(kind, workspaceId, path, requestId);
    // addWorkspaceOwner emits a join before the correlated request.
    this.socket.emit(requestEvent, {
      workspace_id: workspaceId,
      request_id: requestId,
      ...extra,
    });
    void resultEvent;
    return promise;
  }

  private registerFile(
    kind: PendingFile["kind"],
    workspaceId: string,
    path: string,
    requestId: string,
  ): Promise<any> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () =>
          this.failPending(
            requestId,
            new FileTransferError("File transfer timed out", "timeout"),
          ),
        this.timeoutMs,
      );
      // Transfer requests use their own ephemeral room reference. It is released
      // at settlement and is never retained across an auth/org context change.
      const epoch = this.workspaceEpoch;
      const releaseRoom = this.addWorkspaceOwner(workspaceId);
      this.pendingFiles.set(requestId, {
        requestId,
        workspaceId,
        path,
        kind,
        resolve,
        reject,
        timer,
        releaseRoom: () => {
          if (epoch === this.workspaceEpoch) releaseRoom();
        },
      });
    });
  }

  private onChunk(event: FileChunkEvent, kind: "read" | "download"): void {
    if (!event || typeof event.request_id !== "string") return;
    const pending = this.pendingFiles.get(event.request_id);
    if (!pending || pending.kind !== kind) return;
    if (
      pending.workspaceId !== event.workspace_id ||
      pending.path !== event.path
    ) {
      this.failPending(
        event.request_id,
        new FileTransferError(
          "File chunk context mismatch",
          "correlation_error",
        ),
      );
      return;
    }
    if (
      !Number.isInteger(event.total_chunks) ||
      event.total_chunks < 1 ||
      event.total_chunks > MAX_READ_CHUNKS_PER_TRANSFER
    ) {
      this.failPending(
        event.request_id,
        new FileTransferError("Invalid chunk count", "invalid_chunk"),
      );
      return;
    }
    try {
      if (!this.chunks.has(event.request_id)) {
        this.chunks.start(
          event.request_id,
          event.total_chunks,
          { totalChunks: event.total_chunks },
          event.path,
        );
      }
      this.chunks.addChunk(event.request_id, event);
    } catch (error) {
      this.failPending(
        event.request_id,
        new FileTransferError(
          error instanceof Error ? error.message : "Invalid file chunk",
          "invalid_chunk",
        ),
      );
    }
  }

  private onFileResult(eventName: string, data: any): void {
    if (!data || typeof data.request_id !== "string") return;
    const pending = this.pendingFiles.get(data.request_id);
    if (!pending) return;
    const uploadResultPathMatches =
      pending.kind === "upload" && data.path === pending.path;
    if (
      pending.workspaceId !== data.workspace_id ||
      (pending.kind !== "find" &&
        pending.path !== data.path &&
        !uploadResultPathMatches)
    ) {
      this.failPending(
        data.request_id,
        new FileTransferError(
          "File result context mismatch",
          "correlation_error",
        ),
      );
      return;
    }
    const expectedEvent: Record<PendingFile["kind"], string> = {
      list: "files:list_result",
      find: "files:find_result",
      read: "files:content_result",
      download: "files:download_result",
      upload: "files:upload_result",
    };
    if (expectedEvent[pending.kind] !== eventName) return;
    const payloadError = errorFromPayload(data, "File request failed");
    if (payloadError) {
      this.failPending(data.request_id, payloadError);
      return;
    }
    if (pending.kind === "read" || pending.kind === "download") {
      if (data.chunked) {
        if (
          !Number.isInteger(data.total_chunks) ||
          data.total_chunks < 1 ||
          data.total_chunks > MAX_READ_CHUNKS_PER_TRANSFER
        ) {
          this.failPending(
            data.request_id,
            new FileTransferError("Invalid chunk count", "invalid_chunk"),
          );
          return;
        }
        if (!this.chunks.has(data.request_id)) {
          this.failPending(
            data.request_id,
            new FileTransferError(
              "Chunked result arrived without chunks",
              "incomplete_transfer",
            ),
          );
          return;
        }
        try {
          const complete = this.chunks.finish(
            data.request_id,
            data.total_chunks,
          );
          const rawSize = decodedBase64Size(complete.content);
          if (data.size !== rawSize)
            throw new Error("file content size mismatch");
          pending.resolve({ ...data, content: complete.content });
          this.clearPending(data.request_id);
        } catch (error) {
          this.failPending(
            data.request_id,
            new FileTransferError(
              error instanceof Error
                ? error.message
                : "Incomplete file transfer",
              "incomplete_transfer",
            ),
          );
        }
        return;
      }
      if (this.chunks.has(data.request_id)) {
        this.failPending(
          data.request_id,
          new FileTransferError(
            "Unexpected partial file chunks",
            "incomplete_transfer",
          ),
        );
        return;
      }
      try {
        const rawSize = decodedBase64Size(
          stripBase64Whitespace(data.content ?? ""),
        );
        if (data.size !== rawSize)
          throw new Error("file content size mismatch");
      } catch {
        this.failPending(
          data.request_id,
          new FileTransferError(
            "Invalid base64 file payload or size mismatch",
            "invalid_payload",
          ),
        );
        return;
      }
    }
    if (pending.kind === "upload" && data.status !== "success") {
      this.failPending(
        data.request_id,
        new FileTransferError(data.error || "Upload failed", "upload_failed"),
      );
      return;
    }
    if (
      pending.kind === "upload" &&
      data.path === pending.path &&
      pending.requestedFilename
    ) {
      data = {
        ...data,
        path: `${pending.path.replace(/\/$/, "")}/${pending.requestedFilename}`,
      };
    }
    pending.resolve(data);
    this.clearPending(data.request_id);
  }

  private clearPending(requestId: string): void {
    const pending = this.pendingFiles.get(requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.releaseRoom?.();
    this.pendingFiles.delete(requestId);
    this.chunks.cancel(requestId);
  }

  private failPending(requestId: string, error: Error): void {
    const pending = this.pendingFiles.get(requestId);
    if (!pending) return;
    this.clearPending(requestId);
    pending.reject(error);
  }

  private rejectAllFiles(error: Error): void {
    for (const requestId of [...this.pendingFiles.keys()])
      this.failPending(requestId, error);
    this.chunks.clear();
  }

  private newRequestId(): string {
    return this.options.createRequestId?.() ?? makeId();
  }

  private isAuthError(message: string): boolean {
    const value = message.toLowerCase();
    return ["auth", "401", "unauthorized", "forbidden", "expired token"].some(
      (part) => value.includes(part),
    );
  }

  private assertNotDisposed(): void {
    if (this.disposed)
      throw new FileTransferError("Realtime client disposed", "disposed");
  }
}
