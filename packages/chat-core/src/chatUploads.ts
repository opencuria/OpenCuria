/** Pure naming, token, and file-tree helpers shared by chat upload clients. */

import { UPLOAD_MAX_BYTES } from "./fileChunks";
import type { FileNode } from "./types";

/** Workspace directory chat uploads are stored in (created via `mkdir -p`). */
export const CHAT_UPLOAD_DIR = "/workspace/.opencuria/user-uploaded";
export { UPLOAD_MAX_BYTES };

let chatUploadFallbackCounter = 0;

/** Sanitize browser filename input to the runner-compatible safe name. */
export function sanitizeUploadFilename(name: string): string {
  const raw = (name ?? "").trim();
  const safe = raw.replace(/[^a-zA-Z0-9._-]/g, "_");
  if (!safe || safe === "." || safe === "..") return "upload";
  return safe;
}

/** Resolve a unique name and add the result to `existing`. */
export function resolveUniqueFilename(
  existing: Set<string>,
  name: string,
): string {
  if (!existing.has(name)) {
    existing.add(name);
    return name;
  }
  const dot = name.lastIndexOf(".");
  const hasExtension = dot > 0;
  const base = hasExtension ? name.slice(0, dot) : name;
  const ext = hasExtension ? name.slice(dot) : "";
  let counter = 1;
  let candidate = `${base}_${counter}${ext}`;
  while (existing.has(candidate)) {
    counter += 1;
    candidate = `${base}_${counter}${ext}`;
  }
  existing.add(candidate);
  return candidate;
}

/** Non-colliding upload request id: crypto.randomUUID when available. */
export function nextChatUploadRequestId(fileName: string): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (typeof uuid === "string" && uuid.length > 0) return `upload-${uuid}`;
  } catch {
    // Fall through to timestamp+counter fallback below.
  }
  chatUploadFallbackCounter += 1;
  return `upload-${Date.now()}-${chatUploadFallbackCounter}-${fileName}`;
}

/** Single `@file:` mention token (without trailing space). */
export function buildUploadMentionToken(path: string): string {
  return `@file:${path}`;
}

/** Append unseen `@file:` references, separated from existing text by newline. */
export function appendUploadMentions(prompt: string, paths: string[]): string {
  const fresh = paths.filter((path) => !hasUploadMention(prompt, path));
  if (fresh.length === 0) return prompt;
  const block = `${fresh.map((path) => buildUploadMentionToken(path)).join(" ")} `;
  return prompt ? `${prompt}\n${block}` : block;
}

/** True when `prompt` already references `path` via an exact `@file:` token. */
export function hasUploadMention(prompt: string, path: string): boolean {
  const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|\\s)@file:${escaped}(?=\\s|$)`).test(prompt);
}

/** Full workspace path for an uploaded file name. */
export function uploadTargetPath(filename: string): string {
  return `${CHAT_UPLOAD_DIR}/${filename}`;
}

/** Find a node by exact workspace path anywhere in the explorer tree. */
export function findWorkspaceTreeNode(
  nodes: FileNode[],
  targetPath: string,
): FileNode | null {
  for (const node of nodes) {
    if (node.path === targetPath) return node;
    if (node.children) {
      const found = findWorkspaceTreeNode(node.children, targetPath);
      if (found) return found;
    }
  }
  return null;
}

/** True when `targetPath` exists anywhere in the explorer tree. */
export function hasTreePath(tree: FileNode[], targetPath: string): boolean {
  return findWorkspaceTreeNode(tree, targetPath) !== null;
}

/** Collect known file/dir names inside the chat upload directory. */
export function collectUploadDirFilenames(tree: FileNode[]): Set<string> {
  const node = findWorkspaceTreeNode(tree, CHAT_UPLOAD_DIR);
  const names = new Set<string>();
  for (const child of node?.children ?? []) {
    if (child.name) names.add(child.name);
  }
  return names;
}
