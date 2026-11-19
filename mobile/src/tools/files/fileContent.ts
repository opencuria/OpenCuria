import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";
import { TEXT_PREVIEW_MAX_BYTES } from "@opencuria/chat-core/workspaceFiles";
import type { OpenCuriaClient } from "@opencuria/client";
import { createUtf8StreamDecoder } from "../utf8";

export interface WorkspaceText {
  text: string;
  truncated: boolean;
  size: number;
}

export class BinaryFileError extends Error {
  constructor() {
    super("This file is not text.");
    this.name = "BinaryFileError";
  }
}

/** True when the leading bytes look binary (NUL byte, as git decides). */
export function looksBinary(bytes: Uint8Array): boolean {
  const end = Math.min(bytes.length, 8000);
  for (let index = 0; index < end; index += 1)
    if (bytes[index] === 0) return true;
  return false;
}

/** Read the beginning of a workspace file as UTF-8 text (like t3code). */
export async function readWorkspaceText(
  client: Pick<OpenCuriaClient, "files">,
  workspaceId: string,
  path: string,
): Promise<WorkspaceText> {
  const { link, bytes, truncated } = await client.files.readBytes(
    workspaceId,
    path,
    { maxBytes: TEXT_PREVIEW_MAX_BYTES },
  );
  if (looksBinary(bytes)) throw new BinaryFileError();
  const text = createUtf8StreamDecoder().decode(bytes);
  return {
    text: text.replace(/^﻿/, ""),
    truncated,
    size: link.size,
  };
}

/** Minimal RFC 4180 parser for the table preview (quotes, escaped quotes, CRLF). */
export function parseDelimited(
  text: string,
  delimiter: string,
  maxRows = 2000,
): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]!;
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else quoted = false;
      } else cell += char;
      continue;
    }
    if (char === '"' && cell === "") quoted = true;
    else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      if (rows.length >= maxRows) return rows;
    } else cell += char;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export function delimiterFor(path: string): string | null {
  const lower = path.toLowerCase();
  if (lower.endsWith(".csv")) return ",";
  if (lower.endsWith(".tsv")) return "\t";
  return null;
}

export function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown|mdx|mkd)$/i.test(path);
}

export function isImagePath(path: string): boolean {
  return /\.(png|jpe?g|gif|webp|bmp|heic|heif|avif|ico)$/i.test(path);
}

export function isPdfPath(path: string): boolean {
  return /\.pdf$/i.test(path);
}

export function isSvgPath(path: string): boolean {
  return /\.svg$/i.test(path);
}

/** Prefer TypeScript source over the ambiguous MPEG transport-stream suffix. */
export function isVideoPath(path: string): boolean {
  return !/\.ts$/i.test(path) && classifyWorkspaceFile(path) === "video";
}
