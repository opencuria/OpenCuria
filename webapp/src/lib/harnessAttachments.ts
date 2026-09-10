/**
 * Tool image/PDF attachments persisted in `HarnessPart.meta.attachments`.
 *
 * Mirrors `backend/apps/harness/images.py::select_persisted_tool_attachments`:
 * the backend stores at most `TOOL_ATTACHMENT_MAX_COUNT` entries shaped as
 * `{type: "file", mime, url: "data:...;base64,..."}` (images and PDFs alike).
 * All readers here are defensive: broken `meta` never throws, invalid entries
 * are dropped, and only `data:` URLs are returned.
 */

import type { HarnessPart } from '@/types/harness'

/** File attachment persisted in `HarnessPart.meta.attachments`. */
export interface ToolAttachment {
  type: string
  mime: string
  url: string
  /** Original filename (backend `images.py` persists it, may be `""`). */
  filename?: string
}

/** Backend cap (`TOOL_ATTACHMENT_MAX_COUNT` in `images.py`): max 2 entries. */
export const TOOL_ATTACHMENT_MAX_COUNT = 2

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Read sanitized attachments from `part.meta.attachments`.
 *
 * Keeps only `{type: "file", mime: non-empty, url: "data:..."}` entries and
 * returns defensive copies capped at `TOOL_ATTACHMENT_MAX_COUNT`.
 * A non-empty string `filename` is carried over defensively (omitted
 * otherwise so downstream code can fall back to the tool path).
 */
export function toolAttachments(part: HarnessPart): ToolAttachment[] {
  if (!isRecord(part) || !isRecord(part.meta)) return []
  return sanitizeAttachmentList(part.meta['attachments'])
}

/**
 * Read sanitized attachments from a live `harness.part_updated` delta
 * (`delta.attachments` on `tool_completed`, same shape as `meta`).
 * Defensive: any broken payload yields `[]`, entries are sanitized and
 * capped exactly like `toolAttachments`.
 */
export function deltaAttachments(delta: unknown): ToolAttachment[] {
  if (!isRecord(delta)) return []
  return sanitizeAttachmentList(delta['attachments'])
}

function sanitizeAttachmentEntry(entry: Record<string, unknown>): ToolAttachment | null {
  if (entry['type'] !== 'file') return null
  const mime = entry['mime']
  if (typeof mime !== 'string' || !mime.trim()) return null
  const url = entry['url']
  if (typeof url !== 'string' || !url.startsWith('data:')) return null
  const clean: ToolAttachment = { type: 'file', mime: mime.trim(), url }
  const filename = entry['filename']
  if (typeof filename === 'string' && filename.trim()) {
    clean.filename = filename.trim()
  }
  return clean
}

function sanitizeAttachmentList(raw: unknown): ToolAttachment[] {
  if (!Array.isArray(raw)) return []
  const kept: ToolAttachment[] = []
  for (const entry of raw) {
    if (kept.length >= TOOL_ATTACHMENT_MAX_COUNT) break
    if (!isRecord(entry)) continue
    const clean = sanitizeAttachmentEntry(entry)
    if (clean) kept.push(clean)
  }
  return kept
}

/** True when the attachment is an image (`image/*` MIME, case-insensitive). */
export function isImageAttachment(
  attachment: Pick<ToolAttachment, 'mime'> | null | undefined,
): boolean {
  if (!attachment || typeof attachment.mime !== 'string') return false
  return attachment.mime.trim().toLowerCase().startsWith('image/')
}

/** True when the attachment is a PDF (`application/pdf`, case-insensitive). */
export function isPdfAttachment(
  attachment: Pick<ToolAttachment, 'mime'> | null | undefined,
): boolean {
  if (!attachment || typeof attachment.mime !== 'string') return false
  return attachment.mime.trim().toLowerCase() === 'application/pdf'
}

/**
 * Estimate the binary byte size of a `data:` URL (base64 payload after `,`).
 * Returns `null` when the size cannot be determined defensively.
 */
export function attachmentByteSize(url: string): number | null {
  if (typeof url !== 'string' || !url.startsWith('data:')) return null
  const comma = url.indexOf(',')
  if (comma < 0 || comma + 1 >= url.length) return null
  const clean = url.slice(comma + 1).replace(/\s/g, '')
  if (!clean) return 0
  let padding = 0
  if (clean.endsWith('==')) padding = 2
  else if (clean.endsWith('=')) padding = 1
  return Math.max(0, Math.floor((clean.length * 3) / 4) - padding)
}

/** Format bytes like the file viewer (`B` / `KB` / `MB`). */
export function formatAttachmentSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Human-readable size label for a `data:` URL, or `null` when unknown. */
export function attachmentSizeLabel(url: string): string | null {
  const bytes = attachmentByteSize(url)
  if (bytes == null) return null
  const label = formatAttachmentSize(bytes)
  return label || null
}
