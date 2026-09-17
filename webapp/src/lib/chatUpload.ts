/**
 * Chat upload helpers — drag & drop / paperclip uploads into the workspace.
 *
 * Files dropped into the chat (or picked via the paperclip) are uploaded to
 * `CHAT_UPLOAD_DIR` and referenced with an `@file:` mention token, exactly
 * like a mention-picker selection (`@file:<path> ` with trailing space).
 *
 * Transport reuses the existing `files:upload` socket channel
 * (`trackAndUpload` + `sendFilesUpload`); this module only holds pure,
 * unit-testable pieces: filename sanitizing, collision-free naming,
 * request ids, base64 encoding, drop-event extraction and prompt insertion.
 */

import { UPLOAD_MAX_BYTES } from '@/lib/fileChunks'
import type { FileNode } from '@/types'

/** Workspace directory chat uploads are stored in (created via `mkdir -p`). */
export const CHAT_UPLOAD_DIR = '/workspace/.opencuria/user-uploaded'

/** Re-exported so callers do not need a second import for the size guard. */
export { UPLOAD_MAX_BYTES }

let chatUploadFallbackCounter = 0

/**
 * Sanitize a browser file name for a workspace upload.
 *
 * Mirrors the historical chat-upload behaviour (`[^a-zA-Z0-9._-] → _`) so
 * the runner's `_sanitize_filename` (rejects empty, path separators,
 * `"."`/`".."`) never fails. Falls back to `"upload"` for empty /
 * `"."` / `".."` results.
 */
export function sanitizeUploadFilename(name: string): string {
  const raw = (name ?? '').trim()
  const safe = raw.replace(/[^a-zA-Z0-9._-]/g, '_')
  if (!safe || safe === '.' || safe === '..') return 'upload'
  return safe
}

/**
 * Return a collision-free file name for `existing` names.
 *
 * Appends `_1`, `_2`, … before the extension (no extension and dotfiles
 * like `.gitignore` get the suffix appended) so uploads never overwrite.
 * `name` is expected to be already sanitized; `existing` is mutated to
 * include the returned name so batch uploads with equal names stay unique.
 */
export function resolveUniqueFilename(existing: Set<string>, name: string): string {
  if (!existing.has(name)) {
    existing.add(name)
    return name
  }
  const dot = name.lastIndexOf('.')
  const hasExtension = dot > 0
  const base = hasExtension ? name.slice(0, dot) : name
  const ext = hasExtension ? name.slice(dot) : ''
  let counter = 1
  let candidate = `${base}_${counter}${ext}`
  while (existing.has(candidate)) {
    counter += 1
    candidate = `${base}_${counter}${ext}`
  }
  existing.add(candidate)
  return candidate
}

/** Non-colliding upload request id: crypto.randomUUID when available. */
export function nextChatUploadRequestId(fileName: string): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.()
    if (typeof uuid === 'string' && uuid.length > 0) return `upload-${uuid}`
  } catch {
    // fall through to the timestamp+counter fallback below
  }
  chatUploadFallbackCounter += 1
  return `upload-${Date.now()}-${chatUploadFallbackCounter}-${fileName}`
}

/** Encode a browser File as base64 (same arrayBuffer → btoa path as FileUploadZone). */
export async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]!)
  }
  return btoa(binary)
}

/** True when the drag carries files (used to ignore text selections). */
export function isFileDrag(event: DragEvent): boolean {
  const types = event.dataTransfer?.types
  if (!types) return false
  return Array.from(types).includes('Files')
}

/** Extract dropped files; empty for folder drops and non-file drags. */
export function getDroppedFiles(event: DragEvent): File[] {
  const files = event.dataTransfer?.files
  if (!files || files.length === 0) return []
  return Array.from(files)
}

/** Single `@file:` mention token (without trailing space). */
export function buildUploadMentionToken(path: string): string {
  return `@file:${path}`
}

/**
 * Append `@file:` tokens for uploaded paths to the composer prompt.
 *
 * Tokens are space-separated with one trailing space (exactly like
 * `applyMentionCandidate`), separated from existing text by `\n` as the
 * previous markdown reference insertion did. Paths that are already
 * referenced are skipped so a doubly-handled drop can never insert the
 * same reference twice.
 */
export function appendUploadMentions(prompt: string, paths: string[]): string {
  const fresh = paths.filter((path) => !hasUploadMention(prompt, path))
  if (fresh.length === 0) return prompt
  const block = `${fresh.map((path) => buildUploadMentionToken(path)).join(' ')} `
  return prompt ? `${prompt}\n${block}` : block
}

/** True when `prompt` already references `path` via an `@file:` token. */
export function hasUploadMention(prompt: string, path: string): boolean {
  const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?:^|\\s)@file:${escaped}(?=\\s|$)`).test(prompt)
}

/** Full workspace path for an uploaded file name. */
export function uploadTargetPath(filename: string): string {
  return `${CHAT_UPLOAD_DIR}/${filename}`
}

/** Find a node by exact workspace path anywhere in the explorer tree. */
export function findWorkspaceTreeNode(
  nodes: FileNode[],
  targetPath: string,
): FileNode | null {
  for (const node of nodes) {
    if (node.path === targetPath) return node
    if (node.children) {
      const found = findWorkspaceTreeNode(node.children, targetPath)
      if (found) return found
    }
  }
  return null
}

function findTreeNode(nodes: FileNode[], targetPath: string): FileNode | null {
  return findWorkspaceTreeNode(nodes, targetPath)
}

/** True when `targetPath` exists anywhere in the explorer tree. */
export function hasTreePath(tree: FileNode[], targetPath: string): boolean {
  return findTreeNode(tree, targetPath) !== null
}

/**
 * Collect known file/dir names inside the chat upload directory.
 *
 * Returns an empty set when the directory (or its parents) is not loaded
 * yet so callers fall back to the sanitized name and let the runner
 * `mkdir -p` create the directory on upload.
 */
export function collectUploadDirFilenames(tree: FileNode[]): Set<string> {
  const node = findTreeNode(tree, CHAT_UPLOAD_DIR)
  const names = new Set<string>()
  for (const child of node?.children ?? []) {
    if (child.name) names.add(child.name)
  }
  return names
}
