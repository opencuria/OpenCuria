import type { OpenCuriaClient } from "@opencuria/client";
import { base64ToBytes, createUtf8StreamDecoder } from "../utf8";

/** Source preview limit; larger files show their beginning (like t3code). */
export const FILE_PREVIEW_MAX_BYTES = 1024 * 1024;

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

/** Read a workspace file as UTF-8 text, at most the preview limit. */
export async function readWorkspaceText(
  client: OpenCuriaClient,
  workspaceId: string,
  path: string,
): Promise<WorkspaceText> {
  const result = await client.files.readFile(
    workspaceId,
    path,
    FILE_PREVIEW_MAX_BYTES,
  );
  if (result.error) throw new Error(result.error);
  const bytes = base64ToBytes(result.content ?? "");
  if (looksBinary(bytes)) throw new BinaryFileError();
  const text = createUtf8StreamDecoder().decode(bytes);
  return {
    text: text.replace(/^﻿/, ""),
    truncated: result.truncated === true || result.size > bytes.length,
    size: result.size,
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

export function isSvgPath(path: string): boolean {
  return /\.svg$/i.test(path);
}
