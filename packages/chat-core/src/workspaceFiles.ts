/**
 * How clients load, open and upload workspace files.
 *
 * Bytes stream over signed HTTP links (`@opencuria/client` `files`), so the
 * limits below protect the user's data plan and the server, not memory.
 * They mirror `backend/apps/workspace_files/services.py`.
 */

export const MEBIBYTE = 1024 * 1024;

/**
 * Images and videos embedded in a chat load by themselves up to this size.
 * Larger ones show a blurred preview and load when the user taps it.
 */
export const MEDIA_AUTOLOAD_MAX_BYTES = 5 * MEBIBYTE;

/** Largest file a client opens, downloads or uploads. */
export const FILE_TRANSFER_MAX_BYTES = 500 * MEBIBYTE;

/** Text previews read at most this many bytes (a `Range` request). */
export const TEXT_PREVIEW_MAX_BYTES = MEBIBYTE;

/** What the server knows about a file: type and size, plus signed URLs. */
export type WorkspaceFileKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "text"
  | "binary";

export interface WorkspaceFileLink {
  /** The path as requested. */
  path: string;
  name: string;
  type: "file" | "directory";
  /** Bytes (0 for directories). */
  size: number;
  /** Unix seconds. */
  modifiedAt: number;
  kind: WorkspaceFileKind;
  mimeType: string;
  /** Absolute URL streaming the file; supports `Range`. Empty for folders. */
  contentUrl: string;
  /** Absolute URL forcing a download; folders download as `.tar.gz`. */
  downloadUrl: string;
  /** Absolute URL of a tiny blurred JPEG (images and videos), else empty. */
  thumbnailUrl: string;
  /** The Git blob a link to an index or commit version streams. */
  objectId?: string;
  /** Unix seconds after which the URLs stop working. */
  expiresAt: number;
}

/**
 * How a media element in a chat behaves:
 * - `auto`: load and show it.
 * - `tap`: show the blurred preview and size; load on tap.
 * - `too_large`: above {@link FILE_TRANSFER_MAX_BYTES}; preview only.
 */
export type MediaLoadMode = "auto" | "tap" | "too_large";

/**
 * Decide how an embedded image or video loads. `requested` is true once the
 * user tapped it or opened it directly (viewer, gallery, file explorer).
 */
export function mediaLoadMode(
  link: Pick<WorkspaceFileLink, "size">,
  requested = false,
): MediaLoadMode {
  if (link.size > FILE_TRANSFER_MAX_BYTES) return "too_large";
  if (requested || link.size <= MEDIA_AUTOLOAD_MAX_BYTES) return "auto";
  return "tap";
}

/** Whether a file of this size may be opened, downloaded or uploaded. */
export function canTransferFile(size: number): boolean {
  return Number.isFinite(size) && size >= 0 && size <= FILE_TRANSFER_MAX_BYTES;
}

/** "Files up to 500 MB can be transferred." */
export function fileTransferLimitMessage(): string {
  return `Files up to ${FILE_TRANSFER_MAX_BYTES / MEBIBYTE} MB can be transferred.`;
}
