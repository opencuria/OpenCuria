import { Directory, File, Paths } from "expo-file-system";
import * as Sharing from "expo-sharing";
import {
  classifyWorkspaceFile,
  getWorkspaceFileExtension,
  resolveWorkspaceLinkPath,
  type WorkspaceFileKind,
} from "@opencuria/chat-core/workspaceFileRefs";
import {
  decodedBase64Size,
  stripBase64Whitespace,
} from "@opencuria/chat-core/fileChunks";
import type { OpenCuriaClient } from "@opencuria/client";

const MEBIBYTE = 1024 * 1024;
export const MEDIA_PREVIEW_MAX_BYTES = 20 * MEBIBYTE;
export const MEDIA_CACHE_MAX_BYTES = 32 * MEBIBYTE;
const DOCUMENT_DOWNLOAD_MAX_BYTES = 100 * MEBIBYTE;
const MAX_CONCURRENT_TRANSFERS = 2;
const ROOT_NAME = "opencuria";

export class NativeMediaError extends Error {
  readonly code: string;

  constructor(message: string, code = "media_error") {
    super(message);
    this.name = "NativeMediaError";
    this.code = code;
  }
}

export interface OpenedWorkspaceFile {
  uri: string;
  mimeType: string;
  name: string;
  kind: WorkspaceFileKind;
  text?: string;
}

/** A workspace media URI retained against LRU eviction until release(). */
export interface PinnedWorkspaceMedia {
  uri: string;
  release(): void;
}

type CacheKind = "image" | "video" | "attachment";
type CacheEntry = {
  uri: string;
  mimeType: string;
  bytes: number;
  lastUsed: number;
  pins: number;
  kind: CacheKind;
};
type TransferWaiter = {
  generation: number;
  resolve: () => void;
  reject: (error: Error) => void;
};

/**
 * Authenticated native workspace-media adapter. Files are held only in an
 * app-private, random cache context and are removed by clear()/dispose().
 */
export class MediaCache {
  private readonly directory: Directory;
  private readonly cache = new Map<string, CacheEntry>();
  private readonly pendingMedia = new Map<string, Promise<CacheEntry>>();
  private readonly waiters: TransferWaiter[] = [];
  private readonly activeTransfers = new Set<Promise<void>>();
  private readonly savedAttachments = new Map<
    string,
    { key: string; mimeType: string; name: string }
  >();
  private readonly temporaryFiles = new Set<string>();
  private generation = 0;
  private activeCount = 0;
  private useCounter = 0;
  private clearing: Promise<void> | null = null;
  private disposal: Promise<void> | null = null;
  private disposed = false;

  constructor(
    private readonly client: Pick<OpenCuriaClient, "files">,
    private readonly workspaceId: string,
    scopeKey: string,
  ) {
    // The context is deliberately random: workspace, server, organization, and
    // account identifiers never appear in cache paths or filenames.
    // scopeKey signals ownership to callers; only this instance lifetime is
    // represented on disk, so a stale scope can never reopen prior cache data.
    void scopeKey;
    this.directory = new Directory(Paths.cache, ROOT_NAME, randomId());
  }

  /** Resolve a workspace image/video reference to a local native file URI. */
  async resolve(path: string): Promise<string> {
    const safePath = validateWorkspaceMediaPath(path);
    const kind = classifyWorkspaceFile(safePath);
    if (kind !== "image" && kind !== "video") {
      throw new NativeMediaError(
        "Only workspace images and videos can be previewed.",
        "unsupported_preview",
      );
    }
    return (await this.getMedia(safePath)).uri;
  }

  /** Resolve media while protecting its file from LRU eviction until release. */
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
    const entry = await this.getMedia(safePath);
    this.assertGeneration(generation);
    const current = this.cache.get(safePath);
    if (!current || current.uri !== entry.uri) {
      throw new NativeMediaError(
        "The workspace media preview was evicted.",
        "file_unavailable",
      );
    }
    current.pins += 1;
    current.lastUsed = ++this.useCounter;
    let released = false;
    return {
      uri: current.uri,
      release: () => {
        if (released) return;
        released = true;
        const stillCached = this.cache.get(safePath);
        if (stillCached?.uri === current.uri) {
          stillCached.pins = Math.max(0, stillCached.pins - 1);
          stillCached.lastUsed = ++this.useCounter;
          void this.trimCache();
        }
      },
    };
  }

  /** Alias for resolvePinned: keep the referenced URI alive until release(). */
  async pin(path: string): Promise<PinnedWorkspaceMedia> {
    return this.resolvePinned(path);
  }

  /** Load a workspace file on explicit user request. */
  async load(path: string): Promise<OpenedWorkspaceFile> {
    return this.openFile(path);
  }

  /**
   * Read a previewable workspace file or explicitly download a document.
   * Media/text reads are limited to 20 MiB; documents use the runner's
   * explicit-download transport (maximum 100 MiB).
   */
  async openFile(path: string): Promise<OpenedWorkspaceFile> {
    const safePath = validateWorkspaceMediaPath(path);
    const kind = classifyWorkspaceFile(safePath);
    const name = safePath.slice(safePath.lastIndexOf("/") + 1);
    const generation = this.assertUsable();

    if (kind === "image" || kind === "video") {
      const entry = await this.getMedia(safePath);
      return { uri: entry.uri, mimeType: entry.mimeType, name, kind };
    }

    return this.withTransfer(generation, async () => {
      if (kind === "text") {
        const result = await this.client.files.readFile(
          this.workspaceId,
          safePath,
          MEDIA_PREVIEW_MAX_BYTES + 1,
        );
        this.assertGeneration(generation);
        const base64 = checkedReadPayload(result, MEDIA_PREVIEW_MAX_BYTES);
        const mimeType =
          safeMime(result.mime_type) ?? mimeForPath(safePath) ?? "text/plain";
        if (classifyWorkspaceFile(safePath, mimeType) !== "text") {
          throw new NativeMediaError(
            "This file is not safe to display as text.",
            "unsupported_preview",
          );
        }
        const temporary = await this.writeTemporary(
          base64,
          `${randomId()}.${safeExtension(safePath) || "txt"}`,
          generation,
        );
        try {
          const text = await temporary.text();
          this.assertGeneration(generation);
          const textKey = `text:${temporary.uri}`;
          const textEntry = this.rememberFile(
            temporary,
            mimeType,
            result.size,
            "text",
            textKey,
          );
          await this.trimCache();
          this.assertGeneration(generation);
          if (!this.cache.has(textKey)) {
            throw new NativeMediaError(
              "This file exceeds the available cache limit.",
              "size_limit",
            );
          }
          textEntry.lastUsed = ++this.useCounter;
          return { uri: temporary.uri, mimeType, name, kind, text };
        } catch (error) {
          this.deleteTemporary(temporary.uri);
          this.cache.delete(`file:${temporary.uri}`);
          this.cache.delete(`text:${temporary.uri}`);
          throw error;
        }
      }

      // Binary documents are downloaded only through this explicit API. This
      // deliberately does not power inline previews or markdown image loads.
      const result = await this.client.files.downloadFile(
        this.workspaceId,
        safePath,
      );
      this.assertGeneration(generation);
      if (result.error)
        throw new NativeMediaError(result.error, "download_failed");
      const base64 = checkedDownloadPayload(result.content, result.size);
      this.assertGeneration(generation);
      const downloadedName = safeFilename(result.filename || name);
      const mimeType =
        mimeForPath(downloadedName) ??
        mimeForPath(safePath) ??
        "application/octet-stream";
      const extension =
        safeExtension(downloadedName) || extensionForMime(mimeType) || "bin";
      const temporary = await this.writeTemporary(
        base64,
        `${randomId()}.${extension}`,
        generation,
      );
      this.assertGeneration(generation);
      if (temporary.size !== result.size) {
        this.deleteTemporary(temporary.uri);
        throw new NativeMediaError(
          "The saved workspace file was incomplete.",
          "invalid_payload",
        );
      }
      // Explicit downloads are not inline/LRU previews: keep the caller's file
      // available until its owning scope is cleared, up to the runner's 100 MiB cap.
      return {
        uri: temporary.uri,
        mimeType,
        name: downloadedName,
        kind: classifyWorkspaceFile(downloadedName, mimeType),
      };
    });
  }

  /**
   * Persist a generated image/PDF data URL into this session's private cache.
   * Only bounded inline base64 image/PDF data is accepted; URLs are rejected.
   */
  async saveAttachment(data: string): Promise<string> {
    const parsed = parseAttachmentDataUrl(data);
    const generation = this.assertUsable();
    return this.withTransfer(generation, async () => {
      await this.ensureDirectory();
      this.assertGeneration(generation);
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
        await this.trimCache();
        this.assertGeneration(generation);
        return file.uri;
      } catch (error) {
        this.deleteTemporary(file.uri);
        this.cache.delete(key);
        this.savedAttachments.delete(file.uri);
        throw error;
      }
    });
  }

  /** Save and share a tool-produced image/PDF through the native share sheet. */
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
      await this.trimCache();
    }
  }

  /** Remove this scope's cache and pending ownership while keeping the adapter usable. */
  async clear(): Promise<void> {
    if (this.disposed)
      throw new NativeMediaError("Media cache has been disposed.", "disposed");
    if (this.clearing) return this.clearing;
    const generation = ++this.generation;
    this.clearStaleWaiters(generation);
    for (const uri of [...this.cache.values()].map((entry) => entry.uri))
      this.deleteTemporary(uri);
    this.cache.clear();
    this.savedAttachments.clear();
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
    for (const waiter of this.waiters.splice(0)) {
      waiter.reject(
        new NativeMediaError("Media cache has been disposed.", "disposed"),
      );
    }
    this.cache.clear();
    this.savedAttachments.clear();
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

  private getMedia(path: string): Promise<CacheEntry> {
    const generation = this.assertUsable();
    const cached = this.cache.get(path);
    if (cached && cached.kind !== "attachment" && new File(cached.uri).exists) {
      cached.lastUsed = ++this.useCounter;
      return Promise.resolve(cached);
    }
    if (cached && cached.kind !== "attachment") this.cache.delete(path);

    const requestKey = `${generation}\0${path}`;
    const pending = this.pendingMedia.get(requestKey);
    if (pending) return pending;

    const request = this.withTransfer(generation, async () => {
      const again = this.cache.get(path);
      if (again && again.kind !== "attachment" && new File(again.uri).exists) {
        again.lastUsed = ++this.useCounter;
        return again;
      }
      const result = await this.client.files.readFile(
        this.workspaceId,
        path,
        MEDIA_PREVIEW_MAX_BYTES + 1,
      );
      this.assertGeneration(generation);
      const base64 = checkedReadPayload(result, MEDIA_PREVIEW_MAX_BYTES);
      if (!base64)
        throw new NativeMediaError(
          "The workspace file is empty.",
          "empty_file",
        );
      const responseMime = safeMime(result.mime_type);
      const responseKind = responseMime
        ? classifyWorkspaceFile(path, responseMime)
        : "binary";
      const pathKind = classifyWorkspaceFile(path);
      if (
        responseMime &&
        responseMime !== "application/octet-stream" &&
        (responseKind !== pathKind ||
          (responseKind !== "image" && responseKind !== "video"))
      ) {
        throw new NativeMediaError(
          "The workspace file MIME type does not match its media extension.",
          "invalid_mime",
        );
      }
      const kind = pathKind;
      if (kind !== "image" && kind !== "video") {
        throw new NativeMediaError(
          "This workspace file is not a supported media preview.",
          "unsupported_preview",
        );
      }
      const mimeType =
        mimeForPath(path) ?? (kind === "image" ? "image/png" : "video/mp4");
      await this.ensureDirectory();
      this.assertGeneration(generation);
      const extension = safeExtension(path) || extensionForMime(mimeType);
      const file = new File(
        this.directory,
        `${randomId()}.${extension || "bin"}`,
      );
      file.create({ intermediates: true });
      this.temporaryFiles.add(file.uri);
      try {
        file.write(base64, { encoding: "base64" });
        const bytes = file.size;
        if (bytes > MEDIA_PREVIEW_MAX_BYTES || bytes !== result.size) {
          throw new NativeMediaError(
            "The workspace media preview exceeded its size limit.",
            "size_limit",
          );
        }
        this.assertGeneration(generation);
        const entry: CacheEntry = this.rememberFile(
          file,
          mimeType,
          bytes,
          kind,
          path,
        );
        await this.trimCache();
        this.assertGeneration(generation);
        if (!this.cache.has(path)) {
          throw new NativeMediaError(
            "This media preview exceeds the available cache limit.",
            "size_limit",
          );
        }
        return entry;
      } catch (error) {
        this.deleteTemporary(file.uri);
        throw error;
      }
    });
    const tracked = request.then(
      (value) => {
        this.pendingMedia.delete(requestKey);
        return value;
      },
      (error: unknown) => {
        this.pendingMedia.delete(requestKey);
        throw error;
      },
    );
    this.pendingMedia.set(requestKey, tracked);
    return tracked;
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

  private async ensureDirectory(): Promise<void> {
    if (!this.directory.exists) {
      this.directory.create({ intermediates: true, idempotent: true });
    }
  }

  private async writeTemporary(
    base64: string,
    filename: string,
    generation: number,
  ): Promise<File> {
    await this.ensureDirectory();
    this.assertGeneration(generation);
    const file = new File(this.directory, filename);
    file.create({ intermediates: true });
    this.temporaryFiles.add(file.uri);
    try {
      file.write(base64, { encoding: "base64" });
      this.assertGeneration(generation);
      return file;
    } catch (error) {
      this.deleteTemporary(file.uri);
      throw error;
    }
  }

  private rememberFile(
    file: File,
    mimeType: string,
    bytes: number,
    kind: CacheKind | "text",
    key = `file:${file.uri}`,
  ): CacheEntry {
    const entry: CacheEntry = {
      uri: file.uri,
      mimeType,
      bytes,
      lastUsed: ++this.useCounter,
      pins: 0,
      kind: kind === "text" ? "attachment" : kind,
    };
    // Cache entries are keyed by workspace path only in this in-memory instance.
    this.cache.set(key, entry);
    this.temporaryFiles.delete(file.uri);
    return entry;
  }

  private deleteTemporary(uri: string): void {
    this.temporaryFiles.delete(uri);
    const file = new File(uri);
    if (file.exists) file.delete();
  }

  private async trimCache(): Promise<void> {
    let total = [...this.cache.values()].reduce(
      (sum, entry) => sum + entry.bytes,
      0,
    );
    if (total <= MEDIA_CACHE_MAX_BYTES) return;
    const lru = [...this.cache.entries()]
      .filter(([, entry]) => entry.pins === 0)
      .sort(([, left], [, right]) => left.lastUsed - right.lastUsed);
    for (const [key, entry] of lru) {
      if (total <= MEDIA_CACHE_MAX_BYTES) break;
      this.cache.delete(key);
      this.savedAttachments.delete(entry.uri);
      this.temporaryFiles.delete(entry.uri);
      const file = new File(entry.uri);
      if (file.exists) file.delete();
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

export function validateWorkspaceMediaPath(path: string): string {
  if (typeof path !== "string" || !path.trim()) {
    throw new NativeMediaError("Invalid workspace file path.", "invalid_path");
  }
  const input = path.trim().replace(/[?#].*$/, "");
  if (/[\\\p{Cc}]/u.test(input)) {
    throw new NativeMediaError(
      "Workspace file paths cannot contain control characters.",
      "invalid_path",
    );
  }
  let decoded: string;
  try {
    decoded = decodeURIComponent(input);
  } catch {
    throw new NativeMediaError("Invalid workspace file path.", "invalid_path");
  }
  if (
    /[\\\p{Cc}]/u.test(decoded) ||
    decoded.split("/").some((segment) => segment === "." || segment === "..")
  ) {
    throw new NativeMediaError(
      "Workspace file paths cannot traverse directories.",
      "invalid_path",
    );
  }
  const safe = resolveWorkspaceLinkPath(path);
  if (!safe || safe === "/workspace") {
    throw new NativeMediaError(
      "Only files inside the workspace can be opened.",
      "invalid_path",
    );
  }
  return safe;
}

function checkedReadPayload(
  result: Awaited<ReturnType<OpenCuriaClient["files"]["readFile"]>>,
  maxBytes: number,
): string {
  if (result.error) throw new NativeMediaError(result.error, "read_failed");
  if (!Number.isFinite(result.size) || result.size < 0) {
    throw new NativeMediaError(
      "The workspace returned invalid file metadata.",
      "invalid_payload",
    );
  }
  if (result.truncated || result.size > maxBytes) {
    throw new NativeMediaError(
      `This file is too large to preview (maximum ${maxBytes / MEBIBYTE} MiB).`,
      "size_limit",
    );
  }
  const rawBase64 = result.content ?? "";
  const encodedLimit = Math.ceil(maxBytes / 3) * 4;
  if (rawBase64.length > encodedLimit + Math.ceil(encodedLimit / 32)) {
    throw new NativeMediaError(
      "The workspace file exceeds its preview transfer limit.",
      "size_limit",
    );
  }
  const base64 = stripBase64Whitespace(rawBase64);
  let bytes: number;
  try {
    bytes = decodedBase64Size(base64);
  } catch {
    throw new NativeMediaError(
      "The workspace returned invalid file data.",
      "invalid_payload",
    );
  }
  if (bytes !== result.size || bytes > maxBytes) {
    throw new NativeMediaError(
      "The workspace file was incomplete or exceeded its preview limit.",
      "invalid_payload",
    );
  }
  return base64;
}

function checkedDownloadPayload(
  content: string,
  announcedSize: number,
): string {
  if (
    !Number.isFinite(announcedSize) ||
    announcedSize < 0 ||
    announcedSize > DOCUMENT_DOWNLOAD_MAX_BYTES
  ) {
    throw new NativeMediaError(
      "This file exceeds the 100 MiB download limit.",
      "size_limit",
    );
  }
  if (
    typeof content !== "string" ||
    content.length > Math.ceil(DOCUMENT_DOWNLOAD_MAX_BYTES / 3) * 4 + 4
  ) {
    throw new NativeMediaError(
      "This file exceeds the 100 MiB download limit.",
      "size_limit",
    );
  }
  const clean = stripBase64Whitespace(content);
  if (clean.length > Math.ceil(DOCUMENT_DOWNLOAD_MAX_BYTES / 3) * 4 + 4) {
    throw new NativeMediaError(
      "This file exceeds the 100 MiB download limit.",
      "size_limit",
    );
  }
  let actualSize: number;
  try {
    actualSize = decodedBase64Size(clean);
  } catch {
    throw new NativeMediaError(
      "The workspace returned invalid file data.",
      "invalid_payload",
    );
  }
  if (actualSize !== announcedSize) {
    throw new NativeMediaError(
      "The workspace file was incomplete or exceeded its download limit.",
      "invalid_payload",
    );
  }
  return clean;
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
  const rawBase64 = match[2]!;
  const encodedLimit = Math.ceil(MEDIA_PREVIEW_MAX_BYTES / 3) * 4;
  if (rawBase64.length > encodedLimit + Math.ceil(encodedLimit / 32)) {
    throw new NativeMediaError(
      "The attachment exceeds the 20 MiB sharing limit.",
      "size_limit",
    );
  }
  const base64 = stripBase64Whitespace(rawBase64);
  if (base64.length > encodedLimit + 4) {
    throw new NativeMediaError(
      "The attachment exceeds the 20 MiB sharing limit.",
      "size_limit",
    );
  }
  let bytes: number;
  try {
    bytes = decodedBase64Size(base64);
  } catch {
    throw new NativeMediaError(
      "The attachment contains invalid data.",
      "invalid_payload",
    );
  }
  if (bytes > MEDIA_PREVIEW_MAX_BYTES) {
    throw new NativeMediaError(
      "The attachment exceeds the 20 MiB sharing limit.",
      "size_limit",
    );
  }
  return { base64, bytes, mimeType };
}

function safeMime(value?: string): string | null {
  const mime = (value ?? "").split(";", 1)[0]!.trim().toLowerCase();
  return /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(mime)
    ? mime
    : null;
}

function mimeForPath(path: string): string | null {
  const ext = getWorkspaceFileExtension(path);
  const types: Record<string, string> = {
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
    svg: "image/svg+xml",
    bmp: "image/bmp",
    avif: "image/avif",
    mp4: "video/mp4",
    webm: "video/webm",
    mov: "video/quicktime",
    m4v: "video/x-m4v",
    ogg: "video/ogg",
    ogv: "video/ogg",
    avi: "video/x-msvideo",
    mkv: "video/x-matroska",
    pdf: "application/pdf",
    txt: "text/plain",
    md: "text/markdown",
    csv: "text/csv",
    json: "application/json",
    xml: "application/xml",
    yaml: "application/yaml",
    yml: "application/yaml",
    html: "text/html",
    htm: "text/html",
    css: "text/css",
    js: "application/javascript",
    ts: "application/typescript",
    py: "text/x-python",
    sh: "text/x-shellscript",
  };
  return types[ext] ?? null;
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
