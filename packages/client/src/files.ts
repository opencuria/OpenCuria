import type { ApiClient, FetchLike } from "./api";
import { ApiRequestError } from "./api";
import {
  FILE_TRANSFER_MAX_BYTES,
  fileTransferLimitMessage,
  type WorkspaceFileKind,
  type WorkspaceFileLink,
} from "@opencuria/chat-core/workspaceFiles";

/** A refused or failed workspace file transfer with a machine-readable code. */
export class FileTransferError extends Error {
  readonly code: string;
  constructor(message: string, code = "file_transfer_error") {
    super(message);
    this.name = "FileTransferError";
    this.code = code;
  }
}

/** Paths per link request (backend `MAX_LINK_PATHS`). */
const LINK_BATCH_MAX = 100;
/** Links are renewed this long before they expire. */
const LINK_RENEW_MARGIN_SECONDS = 5 * 60;

const LINK_ERRORS: Record<string, string> = {
  file_not_found: "The file does not exist.",
  invalid_path: "The path is invalid.",
  file_unreadable: "The file cannot be read.",
  unsupported_file: "This file type cannot be opened.",
};

/** Signed links as `files/links/` and `git/file-link/` return them. */
export interface RawWorkspaceFileLinks {
  files: Array<{
    path: string;
    name: string;
    type: "file" | "directory" | "";
    size: number;
    modified_at: number;
    kind: string;
    mime_type: string;
    content_url: string;
    download_url: string;
    thumbnail_url: string;
    object_id?: string;
    error: string;
  }>;
  expires_at: number;
}

/** The refusal behind a link's `error` code. */
export function fileLinkError(code: string): FileTransferError {
  return new FileTransferError(LINK_ERRORS[code] ?? code, code);
}

/** One server link with absolute URLs (`baseUrl` is the API base URL). */
export function workspaceFileLink(
  entry: RawWorkspaceFileLinks["files"][number],
  expiresAt: number,
  baseUrl: string,
): WorkspaceFileLink {
  const absolute = (url: string) => (url ? `${baseUrl}${url}` : "");
  return {
    path: entry.path,
    name: entry.name,
    type: entry.type === "directory" ? "directory" : "file",
    size: entry.size,
    modifiedAt: entry.modified_at,
    kind: (entry.kind || "binary") as WorkspaceFileKind,
    mimeType: entry.mime_type,
    contentUrl: absolute(entry.content_url),
    downloadUrl: absolute(entry.download_url),
    thumbnailUrl: absolute(entry.thumbnail_url),
    ...(entry.object_id ? { objectId: entry.object_id } : {}),
    expiresAt,
  };
}

interface UploadResponse {
  upload_url: string;
  expires_at: number;
  max_bytes: number;
}

/** Where one upload goes; `uploadUrl` is absolute and accepts one `PUT`. */
export interface WorkspaceFileUploadTarget {
  uploadUrl: string;
  expiresAt: number;
  maxBytes: number;
}

/** The workspace file an upload created. */
export interface WorkspaceFileUploadResult {
  path: string;
  size: number;
}

export interface WorkspaceFileUploadInput {
  directory: string;
  filename: string;
  size: number;
  /** `rename` (default) keeps existing files as `name_1.ext`; `replace` overwrites. */
  conflict?: "rename" | "replace";
}

/** Bytes from the start of a file (a `Range` request). */
export interface WorkspaceFileBytes {
  link: WorkspaceFileLink;
  bytes: Uint8Array;
  /** True when the file is longer than the bytes returned. */
  truncated: boolean;
}

type Pending = {
  path: string;
  resolve: (link: WorkspaceFileLink) => void;
  reject: (error: Error) => void;
};

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function toError(error: unknown, fallback: string): Error {
  if (error instanceof FileTransferError) return error;
  if (error instanceof ApiRequestError)
    return new FileTransferError(error.message || fallback, error.code || "error");
  if (error instanceof Error && error.name === "AbortError") return error;
  return new FileTransferError(
    error instanceof Error && error.message ? error.message : fallback,
  );
}

function abortError(signal: AbortSignal): Error {
  const reason = signal.reason;
  if (reason instanceof Error) return reason;
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

/** Settle with the caller's abort without cancelling a shared request. */
function withSignal<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError(signal));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

/**
 * Workspace file links, uploads and partial reads over HTTP.
 *
 * Links are signed URLs that `<img>`, `<video>`, native players and
 * downloaders use directly. Requests made in the same tick are batched into
 * one call per workspace, and links are cached until shortly before they
 * expire (cleared when server, organization or session change).
 */
export function createWorkspaceFilesApi(
  api: () => ApiClient,
  fetchImpl?: FetchLike,
) {
  const cache = new Map<string, WorkspaceFileLink>();
  const queued = new Map<string, Pending[]>();
  let flushTimer: ReturnType<typeof setTimeout> | null = null;
  let generation = 0;
  let subscribed: { client: ApiClient; unsubscribe: () => void } | undefined;

  function reset(): void {
    generation += 1;
    cache.clear();
  }

  /** Follow the current REST client; a new client means a new context. */
  function subscribe(): void {
    const client = api();
    if (subscribed?.client === client) return;
    if (subscribed) {
      subscribed.unsubscribe();
      reset();
    }
    subscribed = { client, unsubscribe: client.onContextChange(reset) };
  }

  const key = (workspaceId: string, path: string) => `${workspaceId}\0${path}`;
  const absolute = (url: string) => (url ? `${api().getContext().baseUrl}${url}` : "");

  function fresh(link: WorkspaceFileLink | undefined): link is WorkspaceFileLink {
    return !!link && link.expiresAt - nowSeconds() > LINK_RENEW_MARGIN_SECONDS;
  }

  async function request(workspaceId: string, batch: Pending[]): Promise<void> {
    const started = generation;
    try {
      const response = await api().post<RawWorkspaceFileLinks>(
        `/workspaces/${encodeURIComponent(workspaceId)}/files/links/`,
        { paths: batch.map((item) => item.path) },
      );
      const byPath = new Map(response.files.map((entry) => [entry.path, entry]));
      for (const item of batch) {
        const entry = byPath.get(item.path);
        if (!entry || entry.error) {
          item.reject(fileLinkError(entry?.error || "file_not_found"));
          continue;
        }
        const link = workspaceFileLink(
          entry,
          response.expires_at,
          api().getContext().baseUrl,
        );
        if (started === generation) cache.set(key(workspaceId, item.path), link);
        item.resolve(link);
      }
    } catch (error) {
      const failure = toError(error, "Could not reach the workspace.");
      for (const item of batch) item.reject(failure);
    }
  }

  function flush(): void {
    flushTimer = null;
    const groups = [...queued.entries()];
    queued.clear();
    for (const [workspaceId, items] of groups) {
      const byPath = new Map<string, Pending[]>();
      for (const item of items) {
        const list = byPath.get(item.path) ?? [];
        list.push(item);
        byPath.set(item.path, list);
      }
      const paths = [...byPath.keys()];
      for (let start = 0; start < paths.length; start += LINK_BATCH_MAX) {
        const slice = paths.slice(start, start + LINK_BATCH_MAX);
        void request(
          workspaceId,
          slice.map((path) => ({
            path,
            resolve: (link) => byPath.get(path)!.forEach((item) => item.resolve(link)),
            reject: (error) => byPath.get(path)!.forEach((item) => item.reject(error)),
          })),
        );
      }
    }
  }

  /** Signed link and metadata for one workspace path (batched, cached). */
  function link(
    workspaceId: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<WorkspaceFileLink> {
    subscribe();
    const cached = cache.get(key(workspaceId, path));
    if (fresh(cached)) return Promise.resolve(cached);
    const promise = new Promise<WorkspaceFileLink>((resolve, reject) => {
      const list = queued.get(workspaceId) ?? [];
      list.push({ path, resolve, reject });
      queued.set(workspaceId, list);
      flushTimer ??= setTimeout(flush, 0);
    });
    return withSignal(promise, signal);
  }

  /** The cached link, if still fresh (for synchronous rendering). */
  function peekLink(workspaceId: string, path: string): WorkspaceFileLink | undefined {
    subscribe();
    const cached = cache.get(key(workspaceId, path));
    return fresh(cached) ? cached : undefined;
  }

  /** Drop cached links after a file changed (one path or the workspace). */
  function forget(workspaceId: string, path?: string): void {
    if (path !== undefined) {
      cache.delete(key(workspaceId, path));
      return;
    }
    for (const cached of [...cache.keys()])
      if (cached.startsWith(`${workspaceId}\0`)) cache.delete(cached);
  }

  /** Sign the upload of one file; send its bytes with one `PUT uploadUrl`. */
  async function createUpload(
    workspaceId: string,
    input: WorkspaceFileUploadInput,
    signal?: AbortSignal,
  ): Promise<WorkspaceFileUploadTarget> {
    if (input.size > FILE_TRANSFER_MAX_BYTES)
      throw new FileTransferError(fileTransferLimitMessage(), "file_too_large");
    try {
      const response = await api().post<UploadResponse>(
        `/workspaces/${encodeURIComponent(workspaceId)}/files/uploads/`,
        {
          directory: input.directory,
          filename: input.filename,
          size: input.size,
          conflict: input.conflict ?? "rename",
        },
        signal,
      );
      return {
        uploadUrl: absolute(response.upload_url),
        expiresAt: response.expires_at,
        maxBytes: response.max_bytes,
      };
    } catch (error) {
      throw toError(error, "The upload could not start.");
    }
  }

  /**
   * Upload a Blob (browser `File`) as one workspace file. Uses
   * `XMLHttpRequest` where available for progress; native apps upload the
   * file from disk with {@link createUpload} instead.
   */
  async function upload(
    workspaceId: string,
    input: Omit<WorkspaceFileUploadInput, "size"> & {
      body: Blob;
      onProgress?: (sent: number, total: number) => void;
      signal?: AbortSignal;
    },
  ): Promise<WorkspaceFileUploadResult> {
    const target = await createUpload(
      workspaceId,
      { ...input, size: input.body.size },
      input.signal,
    );
    const result = await putBlob(target.uploadUrl, input.body, input);
    forget(workspaceId, result.path);
    return result;
  }

  function putBlob(
    url: string,
    body: Blob,
    options: { onProgress?: (sent: number, total: number) => void; signal?: AbortSignal },
  ): Promise<WorkspaceFileUploadResult> {
    const { signal, onProgress } = options;
    if (signal?.aborted) return Promise.reject(abortError(signal));
    if (typeof XMLHttpRequest === "undefined") {
      return (fetchImpl ?? fetch)(url, {
        method: "PUT",
        body,
        headers: { "Content-Type": "application/octet-stream" },
        signal,
      }).then((response) => uploadResult(response.status, response.text()));
    }
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const onAbort = () => xhr.abort();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (event) => onProgress?.(event.loaded, event.total || body.size);
      xhr.onload = () => {
        signal?.removeEventListener("abort", onAbort);
        uploadResult(xhr.status, Promise.resolve(String(xhr.responseText ?? ""))).then(
          resolve,
          reject,
        );
      };
      xhr.onerror = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new FileTransferError("The upload failed. Check your connection.", "network"));
      };
      xhr.onabort = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(signal ? abortError(signal) : new FileTransferError("Upload cancelled.", "cancelled"));
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      xhr.send(body);
    });
  }

  /** The first `maxBytes` of a file, for text previews. */
  async function readBytes(
    workspaceId: string,
    path: string,
    options: { maxBytes: number; signal?: AbortSignal },
  ): Promise<WorkspaceFileBytes> {
    const fileLink = await link(workspaceId, path, options.signal);
    if (fileLink.type !== "file")
      throw new FileTransferError("The path is a directory.", "is_directory");
    const maxBytes = Math.max(1, Math.floor(options.maxBytes));
    if (fileLink.size === 0)
      return { link: fileLink, bytes: new Uint8Array(), truncated: false };
    let response: Response;
    try {
      response = await (fetchImpl ?? fetch)(fileLink.contentUrl, {
        headers: { Range: `bytes=0-${maxBytes - 1}` },
        signal: options.signal,
      });
    } catch (error) {
      throw toError(error, "Could not reach the workspace.");
    }
    if (response.status !== 200 && response.status !== 206) {
      forget(workspaceId, path);
      throw await responseError(response.status, response.text());
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    return {
      link: fileLink,
      bytes: bytes.length > maxBytes ? bytes.slice(0, maxBytes) : bytes,
      truncated: fileLink.size > maxBytes,
    };
  }

  return { link, peekLink, forget, createUpload, upload, readBytes };
}

async function responseError(status: number, text: Promise<string>): Promise<FileTransferError> {
  let detail = "";
  let code = `http_${status}`;
  try {
    const payload = JSON.parse(await text) as { detail?: string; code?: string };
    detail = payload.detail ?? "";
    code = payload.code ?? code;
  } catch {
    // Not JSON: keep the status code.
  }
  return new FileTransferError(detail || `The server answered ${status}.`, code);
}

async function uploadResult(
  status: number,
  text: Promise<string>,
): Promise<WorkspaceFileUploadResult> {
  if (status !== 201) throw await responseError(status, text);
  const payload = JSON.parse(await text) as WorkspaceFileUploadResult;
  return { path: payload.path, size: payload.size };
}

export type WorkspaceFilesApi = ReturnType<typeof createWorkspaceFilesApi>;
