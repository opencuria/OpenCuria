import { Directory, File, Paths } from "expo-file-system";
import * as Sharing from "expo-sharing";
import { attachmentByteSize } from "@opencuria/chat-core/harnessAttachments";
import {
  classifyWorkspaceFile,
  getWorkspaceFileExtension,
  resolveWorkspaceFilePath,
  type WorkspaceFileKind,
} from "@opencuria/chat-core/workspaceFileRefs";
import {
  canTransferFile,
  FILE_TRANSFER_MAX_BYTES,
  fileTransferLimitMessage,
  type WorkspaceFileLink,
} from "@opencuria/chat-core/workspaceFiles";
import type { OpenCuriaClient } from "@opencuria/client";

const MEBIBYTE = 1024 * 1024;
/** LRU budget for media that is not on screen. */
export const MEDIA_CACHE_MAX_BYTES = 32 * MEBIBYTE;
/**
 * Absolute cap, including media on screen and the open document: one file
 * of the largest transferable size fits beside the LRU budget.
 */
export const MEDIA_CACHE_HARD_MAX_BYTES =
  FILE_TRANSFER_MAX_BYTES + MEDIA_CACHE_MAX_BYTES;
const MAX_CONCURRENT_TRANSFERS = 2;
const ROOT_NAME = "opencuria";

/** Failures that loading the same file again cannot fix. */
const PERMANENT_ERROR_CODES = new Set([
  "invalid_path",
  "unsupported_preview",
  "unsupported_file",
  "is_directory",
  "size_limit",
  "disposed",
]);

export class NativeMediaError extends Error {
  readonly code: string;

  constructor(message: string, code = "media_error") {
    super(message);
    this.name = "NativeMediaError";
    this.code = code;
  }

  /** Whether an automatic retry may succeed (e.g. after a reconnect). */
  get retryable(): boolean {
    return !PERMANENT_ERROR_CODES.has(this.code);
  }
}

export interface MediaCacheLimits {
  /** LRU budget for unpinned files; defaults to MEDIA_CACHE_MAX_BYTES. */
  lruBytes?: number;
  /** Cap including pinned files; defaults to MEDIA_CACHE_HARD_MAX_BYTES. */
  maxBytes?: number;
}

export interface OpenedWorkspaceFile {
  uri: string;
  mimeType: string;
  name: string;
  kind: WorkspaceFileKind;
}

/** A workspace media URI retained against LRU eviction until release(). */
export interface PinnedWorkspaceMedia {
  uri: string;
  release(): void;
}

type CacheKind = "image" | "document" | "attachment";
type CacheEntry = {
  uri: string;
  mimeType: string;
  bytes: number;
  lastUsed: number;
  pins: number;
  kind: CacheKind;
  /** Size and modification time of the workspace file it holds. */
  version?: string;
};
type TransferWaiter = {
  generation: number;
  resolve: () => void;
  reject: (error: Error) => void;
};

/**
 * Workspace files for one workspace and server/account scope. Images and
 * opened documents download over signed links into an app-private, random
 * cache directory that clear()/dispose() remove; videos stream from their
 * link instead. Files on screen are pinned; others share an LRU budget.
 */
export class MediaCache {
  private readonly directory: Directory;
  private readonly cache = new Map<string, CacheEntry>();
  private readonly pendingFiles = new Map<string, Promise<CacheEntry>>();
  private readonly waiters: TransferWaiter[] = [];
  private readonly activeTransfers = new Set<Promise<void>>();
  private readonly savedAttachments = new Map<
    string,
    { key: string; mimeType: string; name: string }
  >();
  private readonly temporaryFiles = new Set<string>();
  /** Cancels the downloads of the current generation. */
  private downloads = new AbortController();
  /** The document openFile() returned last; it stays pinned. */
  private openDocument: { key: string; uri: string } | null = null;
  private generation = 0;
  private activeCount = 0;
  private useCounter = 0;
  private clearing: Promise<void> | null = null;
  private disposal: Promise<void> | null = null;
  private disposed = false;

  private readonly lruBytes: number;
  private readonly maxBytes: number;

  constructor(
    private readonly client: Pick<OpenCuriaClient, "files">,
    private readonly workspaceId: string,
    scopeKey: string,
    limits: MediaCacheLimits = {},
  ) {
    this.lruBytes = limits.lruBytes ?? MEDIA_CACHE_MAX_BYTES;
    this.maxBytes = Math.max(
      this.lruBytes,
      limits.maxBytes ?? MEDIA_CACHE_HARD_MAX_BYTES,
    );
    // The context is deliberately random: workspace, server, organization, and
    // account identifiers never appear in cache paths or filenames.
    // scopeKey signals ownership to callers; only this instance lifetime is
    // represented on disk, so a stale scope can never reopen prior cache data.
    void scopeKey;
    this.directory = new Directory(Paths.cache, ROOT_NAME, randomId());
  }

  /** Size, kind and signed URLs of a workspace file, without its bytes. */
  async describe(path: string): Promise<WorkspaceFileLink> {
    return this.link(validateWorkspaceMediaPath(path));
  }

  /**
   * Resolve a workspace image or video for display. Images are downloaded
   * and protected from LRU eviction until release(); videos return their
   * streaming URL. Any size up to the transfer limit loads: callers decide
   * beforehand whether a large file should (see `describe`).
   */
  async resolvePinned(path: string): Promise<PinnedWorkspaceMedia> {
    const safePath = validateWorkspaceMediaPath(path);
    const kind = classifyWorkspaceFile(safePath);
    if (kind !== "image" && kind !== "video") {
      throw new NativeMediaError(
        "Only workspace images and videos can be previewed.",
        "unsupported_preview",
      );
    }
    const generation = this.assertUsable();
    const link = await this.link(safePath);
    this.assertGeneration(generation);
    assertTransferable(link);
    // expo-video streams and seeks with range requests.
    if (kind === "video")
      return { uri: link.contentUrl, release: () => undefined };
    return this.pinImage(safePath, safePath, link, generation);
  }

  /**
   * Pin an image behind a link the caller signed, such as a Git index or
   * commit version (cached by its object id), like resolvePinned.
   */
  async pinLink(link: WorkspaceFileLink): Promise<PinnedWorkspaceMedia> {
    const safePath = validateWorkspaceMediaPath(link.path);
    if (classifyWorkspaceFile(safePath) !== "image") {
      throw new NativeMediaError(
        "Only workspace images can be previewed.",
        "unsupported_preview",
      );
    }
    const generation = this.assertUsable();
    assertTransferable(link);
    const key = link.objectId ? `git-blob:${link.objectId}` : safePath;
    return this.pinImage(key, safePath, link, generation);
  }

  /** Download `link` into the cache under `key` and pin it until release(). */
  private async pinImage(
    key: string,
    path: string,
    link: WorkspaceFileLink,
    generation: number,
  ): Promise<PinnedWorkspaceMedia> {
    let current: CacheEntry | undefined;
    // Fresh downloads are reserved until awaiting callers pin them; load
    // again if the file still went away (e.g. it was deleted from the cache).
    for (let attempt = 0; attempt < 2 && !current; attempt += 1) {
      const entry = await this.getFile(key, path, link, "image");
      this.assertGeneration(generation);
      const cached = this.cache.get(key);
      if (cached?.uri === entry.uri) current = cached;
    }
    if (!current) {
      throw new NativeMediaError(
        "The workspace media preview was evicted.",
        "file_unavailable",
      );
    }
    const pinned = current;
    pinned.pins += 1;
    pinned.lastUsed = ++this.useCounter;
    let released = false;
    return {
      uri: pinned.uri,
      release: () => {
        if (released) return;
        released = true;
        this.unpin(key, pinned.uri);
      },
    };
  }

  /**
   * Download a workspace file the user opened (PDF view, sharing). Images
   * share the media cache; any other file stays until the next document is
   * opened or the scope ends.
   */
  async openFile(path: string): Promise<OpenedWorkspaceFile> {
    const safePath = validateWorkspaceMediaPath(path);
    const generation = this.assertUsable();
    const link = await this.link(safePath);
    this.assertGeneration(generation);
    assertTransferable(link);
    const name = safeFilename(link.name || safePath);
    const mimeType = safeMime(link.mimeType) ?? "application/octet-stream";
    const kind = classifyWorkspaceFile(name, mimeType);
    if (classifyWorkspaceFile(safePath) === "image") {
      const entry = await this.getFile(safePath, safePath, link, "image");
      return { uri: entry.uri, mimeType: entry.mimeType, name, kind };
    }
    const key = `document:${safePath}`;
    // Only one document is kept: let the previous one go before loading.
    if (this.openDocument?.key !== key) this.releaseDocument();
    const entry = await this.getFile(key, safePath, link, "document");
    this.assertGeneration(generation);
    if (this.openDocument?.uri !== entry.uri) {
      this.releaseDocument();
      entry.pins += 1;
      this.openDocument = { key, uri: entry.uri };
    }
    return { uri: entry.uri, mimeType: entry.mimeType, name, kind };
  }

  /**
   * Persist a legacy inline image/PDF data URL (tool parts stored before
   * workspace media) into this session's private cache. URLs are rejected.
   */
  async saveAttachment(data: string): Promise<string> {
    const parsed = parseAttachmentDataUrl(data);
    const generation = this.assertUsable();
    return this.withTransfer(generation, async () => {
      this.ensureDirectory();
      const extension = extensionForMime(parsed.mimeType);
      const file = new File(this.directory, `${randomId()}.${extension}`);
      file.create({ intermediates: true });
      this.temporaryFiles.add(file.uri);
      const key = `attachment:${file.uri}`;
      try {
        file.write(parsed.base64, { encoding: "base64" });
        if (file.size !== parsed.bytes) {
          throw new NativeMediaError(
            "The attachment could not be saved completely.",
            "invalid_payload",
          );
        }
        this.assertGeneration(generation);
        this.cache.set(key, {
          uri: file.uri,
          mimeType: parsed.mimeType,
          bytes: parsed.bytes,
          lastUsed: ++this.useCounter,
          pins: 0,
          kind: "attachment",
        });
        this.temporaryFiles.delete(file.uri);
        this.savedAttachments.set(file.uri, {
          key,
          mimeType: parsed.mimeType,
          name: `attachment.${extension}`,
        });
        this.trimCache();
        return file.uri;
      } catch (error) {
        this.deleteTemporary(file.uri);
        this.cache.delete(key);
        this.savedAttachments.delete(file.uri);
        throw error;
      }
    });
  }

  /** Save and share a legacy inline image/PDF through the native share sheet. */
  async shareAttachment(data: string): Promise<void> {
    if (!(await Sharing.isAvailableAsync())) {
      throw new NativeMediaError(
        "Native file sharing is not available on this device.",
        "sharing_unavailable",
      );
    }
    const uri = await this.saveAttachment(data);
    const saved = this.savedAttachments.get(uri);
    const entry = saved ? this.cache.get(saved.key) : undefined;
    if (!saved || !entry || !new File(uri).exists) {
      throw new NativeMediaError(
        "The attachment is no longer available. Save it again to share.",
        "file_unavailable",
      );
    }
    entry.pins += 1;
    entry.lastUsed = ++this.useCounter;
    try {
      await Sharing.shareAsync(uri, {
        mimeType: saved.mimeType,
        dialogTitle: saved.name,
      });
    } finally {
      entry.pins = Math.max(0, entry.pins - 1);
      this.savedAttachments.delete(uri);
      this.cache.delete(saved.key);
      this.deleteTemporary(uri);
      this.trimCache();
    }
  }

  /** Remove this scope's cache and pending ownership while keeping the adapter usable. */
  async clear(): Promise<void> {
    if (this.disposed)
      throw new NativeMediaError("Media cache has been disposed.", "disposed");
    if (this.clearing) return this.clearing;
    const generation = ++this.generation;
    this.downloads.abort();
    this.downloads = new AbortController();
    this.clearStaleWaiters(generation);
    for (const uri of [...this.cache.values()].map((entry) => entry.uri))
      this.deleteTemporary(uri);
    this.cache.clear();
    this.savedAttachments.clear();
    this.openDocument = null;
    const active = [...this.activeTransfers];
    this.clearing = (async () => {
      await Promise.allSettled(active);
      for (const uri of [...this.temporaryFiles]) this.deleteTemporary(uri);
      this.temporaryFiles.clear();
      if (this.directory.exists) this.directory.delete();
    })();
    try {
      await this.clearing;
    } finally {
      this.clearing = null;
      this.pumpWaiters();
    }
  }

  /** Permanently invalidate requests and delete all session-scoped temporary files. */
  async dispose(): Promise<void> {
    if (this.disposal) return this.disposal;
    this.disposed = true;
    this.generation += 1;
    this.downloads.abort();
    for (const waiter of this.waiters.splice(0)) {
      waiter.reject(
        new NativeMediaError("Media cache has been disposed.", "disposed"),
      );
    }
    this.cache.clear();
    this.savedAttachments.clear();
    this.openDocument = null;
    const active = [...this.activeTransfers];
    this.disposal = (async () => {
      if (this.clearing) await this.clearing;
      await Promise.allSettled(active);
      for (const uri of [...this.temporaryFiles]) this.deleteTemporary(uri);
      this.temporaryFiles.clear();
      if (this.directory.exists) this.directory.delete();
    })();
    return this.disposal;
  }

  private async link(safePath: string): Promise<WorkspaceFileLink> {
    this.assertUsable();
    try {
      return await this.client.files.link(this.workspaceId, safePath);
    } catch (error) {
      throw toMediaError(error, "The workspace file is unavailable.");
    }
  }

  /** The cached copy of `link` under `key`, downloading it when missing or outdated. */
  private getFile(
    key: string,
    path: string,
    link: WorkspaceFileLink,
    kind: "image" | "document",
  ): Promise<CacheEntry> {
    const generation = this.assertUsable();
    const version = `${link.size}:${link.modifiedAt}`;
    const cachedCopy = () => {
      const cached = this.cache.get(key);
      if (cached?.version !== version || !new File(cached.uri).exists)
        return undefined;
      cached.lastUsed = ++this.useCounter;
      this.reserve(cached);
      return cached;
    };
    const cached = cachedCopy();
    if (cached) return Promise.resolve(cached);

    const requestKey = `${generation}\0${key}\0${version}`;
    const pending = this.pendingFiles.get(requestKey);
    if (pending) return pending;

    const request = this.withTransfer(generation, async () => {
      const again = cachedCopy();
      if (again) return again;
      this.dropEntry(key);
      this.trimCache();
      if (this.cachedBytes() + link.size > this.maxBytes) {
        throw new NativeMediaError(
          "Too much media is open to load this file now.",
          "cache_full",
        );
      }
      const mimeType = safeMime(link.mimeType) ?? "application/octet-stream";
      const file = await this.download(link, path, mimeType, generation);
      const entry: CacheEntry = {
        uri: file.uri,
        mimeType,
        bytes: link.size,
        lastUsed: ++this.useCounter,
        pins: 0,
        kind,
        version,
      };
      this.cache.set(key, entry);
      this.temporaryFiles.delete(file.uri);
      this.reserve(entry);
      this.trimCache();
      // Two transfers may have passed the check above at once.
      if (this.cachedBytes() > this.maxBytes) {
        this.cache.delete(key);
        this.deleteTemporary(entry.uri);
        throw new NativeMediaError(
          "Too much media is open to load this file now.",
          "cache_full",
        );
      }
      return entry;
    });
    const tracked = request.finally(() => this.pendingFiles.delete(requestKey));
    this.pendingFiles.set(requestKey, tracked);
    return tracked;
  }

  /**
   * Download into `<name>.part` and give the file its name only once it is
   * complete: Android streams into the destination and may leave a partial
   * file behind when the transfer fails.
   */
  private async download(
    link: WorkspaceFileLink,
    path: string,
    mimeType: string,
    generation: number,
  ): Promise<File> {
    this.ensureDirectory();
    const extension =
      safeExtension(link.name) ||
      safeExtension(path) ||
      extensionForMime(mimeType);
    const name = `${randomId()}.${extension}`;
    const target = new File(this.directory, name);
    const part = new File(this.directory, `${name}.part`);
    this.temporaryFiles.add(part.uri);
    this.temporaryFiles.add(target.uri);
    try {
      await File.downloadFileAsync(link.contentUrl || link.downloadUrl, part, {
        idempotent: true,
        signal: this.downloads.signal,
      });
      this.assertGeneration(generation);
      if (part.size !== link.size) {
        throw new NativeMediaError(
          "The download was incomplete.",
          "download_incomplete",
        );
      }
      part.moveSync(target, { overwrite: true });
      return target;
    } catch (error) {
      this.deleteTemporary(part.uri);
      this.deleteTemporary(target.uri);
      this.assertGeneration(generation);
      // The link may have expired or changed: sign it again next time.
      this.client.files.forget(this.workspaceId, path);
      throw toMediaError(error, "The download failed.");
    }
  }

  private async withTransfer<T>(
    generation: number,
    operation: () => Promise<T>,
  ): Promise<T> {
    await this.acquireTransfer(generation);
    let finish!: () => void;
    const finished = new Promise<void>((resolve) => {
      finish = resolve;
    });
    this.activeTransfers.add(finished);
    try {
      this.assertGeneration(generation);
      return await operation();
    } finally {
      this.activeTransfers.delete(finished);
      finish();
      this.activeCount = Math.max(0, this.activeCount - 1);
      this.pumpWaiters();
    }
  }

  private acquireTransfer(generation: number): Promise<void> {
    this.assertGeneration(generation);
    if (
      !this.clearing &&
      this.activeCount < MAX_CONCURRENT_TRANSFERS &&
      this.waiters.length === 0
    ) {
      this.activeCount += 1;
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      this.waiters.push({ generation, resolve, reject });
    });
  }

  private pumpWaiters(): void {
    if (this.disposed || this.clearing) return;
    while (
      this.activeCount < MAX_CONCURRENT_TRANSFERS &&
      this.waiters.length > 0
    ) {
      const waiter = this.waiters.shift()!;
      if (waiter.generation !== this.generation) {
        waiter.reject(
          new NativeMediaError("Media cache scope changed.", "scope_changed"),
        );
        continue;
      }
      this.activeCount += 1;
      waiter.resolve();
    }
  }

  private clearStaleWaiters(generation: number): void {
    const stillWaiting: TransferWaiter[] = [];
    for (const waiter of this.waiters.splice(0)) {
      if (waiter.generation < generation) {
        waiter.reject(
          new NativeMediaError("Media cache was cleared.", "cleared"),
        );
      } else {
        stillWaiting.push(waiter);
      }
    }
    this.waiters.push(...stillWaiting);
  }

  private ensureDirectory(): void {
    if (!this.directory.exists) {
      this.directory.create({ intermediates: true, idempotent: true });
    }
  }

  private deleteTemporary(uri: string): void {
    this.temporaryFiles.delete(uri);
    const file = new File(uri);
    if (file.exists) file.delete();
  }

  /** Forget an outdated entry; a pinned one keeps its file until the scope ends. */
  private dropEntry(key: string): void {
    const entry = this.cache.get(key);
    if (!entry) return;
    this.cache.delete(key);
    if (entry.pins === 0) this.deleteTemporary(entry.uri);
    else this.temporaryFiles.add(entry.uri);
  }

  private unpin(key: string, uri: string): void {
    const entry = this.cache.get(key);
    if (entry?.uri !== uri) return;
    entry.pins = Math.max(0, entry.pins - 1);
    entry.lastUsed = ++this.useCounter;
    this.trimCache();
  }

  private releaseDocument(): void {
    const open = this.openDocument;
    this.openDocument = null;
    if (open) this.unpin(open.key, open.uri);
  }

  /**
   * Hold an entry for the callers awaiting it: their continuations (which pin
   * it) run as microtasks, before this timer releases the reservation.
   */
  private reserve(entry: CacheEntry): void {
    entry.pins += 1;
    setTimeout(() => {
      entry.pins = Math.max(0, entry.pins - 1);
      this.trimCache();
    }, 0);
  }

  private cachedBytes(): number {
    return [...this.cache.values()].reduce(
      (sum, entry) => sum + entry.bytes,
      0,
    );
  }

  /** Evict unpinned files beyond the LRU budget; files on screen stay. */
  private trimCache(): void {
    let total = this.cachedBytes();
    if (total <= this.lruBytes) return;
    const lru = [...this.cache.entries()]
      .filter(([, entry]) => entry.pins === 0)
      .sort(([, left], [, right]) => left.lastUsed - right.lastUsed);
    for (const [key, entry] of lru) {
      if (total <= this.lruBytes) break;
      this.cache.delete(key);
      this.savedAttachments.delete(entry.uri);
      this.deleteTemporary(entry.uri);
      total -= entry.bytes;
    }
  }

  private assertUsable(): number {
    if (this.disposed)
      throw new NativeMediaError("Media cache has been disposed.", "disposed");
    return this.generation;
  }

  private assertGeneration(generation: number): void {
    if (this.disposed)
      throw new NativeMediaError("Media cache has been disposed.", "disposed");
    if (generation !== this.generation) {
      throw new NativeMediaError(
        "Media cache scope changed during the file request.",
        "scope_changed",
      );
    }
  }
}

/**
 * Validate a literal file path. Markdown URLs are decoded once where they are
 * parsed, so `%`, `#` and `?` here are part of the file name.
 */
export function validateWorkspaceMediaPath(path: string): string {
  if (typeof path !== "string" || !path.trim()) {
    throw new NativeMediaError("Invalid workspace file path.", "invalid_path");
  }
  if (/[\\\p{Cc}]/u.test(path)) {
    throw new NativeMediaError(
      "Workspace file paths cannot contain control characters.",
      "invalid_path",
    );
  }
  if (path.split("/").some((segment) => segment === "." || segment === "..")) {
    throw new NativeMediaError(
      "Workspace file paths cannot traverse directories.",
      "invalid_path",
    );
  }
  const safe = resolveWorkspaceFilePath(path);
  if (!safe || safe === "/workspace") {
    throw new NativeMediaError(
      "Only files inside the workspace can be opened.",
      "invalid_path",
    );
  }
  return safe;
}

function assertTransferable(link: WorkspaceFileLink): void {
  if (link.type !== "file")
    throw new NativeMediaError("Folders can't be opened.", "is_directory");
  if (!canTransferFile(link.size))
    throw new NativeMediaError(
      `This file is too large. ${fileTransferLimitMessage()}`,
      "size_limit",
    );
}

/** Keep the code of client and media errors; cancelled downloads are retryable. */
function toMediaError(error: unknown, fallback: string): NativeMediaError {
  if (error instanceof NativeMediaError) return error;
  const code =
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string"
      ? (error as { code: string }).code
      : "download_failed";
  const message =
    error instanceof Error && error.message ? error.message : fallback;
  return new NativeMediaError(message, code);
}

function parseAttachmentDataUrl(data: string): {
  base64: string;
  bytes: number;
  mimeType: string;
} {
  const match = /^data:([a-z0-9.+-]+\/[a-z0-9.+-]+);base64,([\s\S]*)$/i.exec(
    data,
  );
  if (!match)
    throw new NativeMediaError(
      "Only inline image and PDF attachments can be shared.",
      "invalid_payload",
    );
  const rawMime = match[1]!.toLowerCase();
  const mimeType = rawMime === "image/jpg" ? "image/jpeg" : rawMime;
  if (
    !/^image\/(?:png|jpeg|gif|webp|avif)$/.test(mimeType) &&
    mimeType !== "application/pdf"
  ) {
    throw new NativeMediaError(
      "Only supported images and PDF attachments can be shared.",
      "unsupported_attachment",
    );
  }
  const bytes = attachmentByteSize(data);
  if (bytes === null)
    throw new NativeMediaError(
      "The attachment contains invalid data.",
      "invalid_payload",
    );
  if (!canTransferFile(bytes))
    throw new NativeMediaError(
      `The attachment is too large. ${fileTransferLimitMessage()}`,
      "size_limit",
    );
  return { base64: match[2]!.replace(/\s+/g, ""), bytes, mimeType };
}

function safeMime(value?: string): string | null {
  const mime = (value ?? "").split(";", 1)[0]!.trim().toLowerCase();
  return /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(mime)
    ? mime
    : null;
}

function safeExtension(nameOrPath: string): string {
  const ext = getWorkspaceFileExtension(nameOrPath);
  return /^[a-z0-9]{1,12}$/.test(ext) ? ext : "";
}

function extensionForMime(mimeType: string): string {
  const extensions: Record<string, string> = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "application/pdf": "pdf",
  };
  return extensions[mimeType] ?? "bin";
}

function safeFilename(filename: string): string {
  const basename = filename.replace(/\\/g, "/").split("/").pop() ?? "";
  const sanitized = basename.replace(/[^a-zA-Z0-9._-]/g, "_");
  return sanitized && sanitized !== "." && sanitized !== ".."
    ? sanitized
    : "download";
}

function randomId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return uuid;
  } catch {
    // Use a non-identifying random fallback on runtimes without randomUUID.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}
