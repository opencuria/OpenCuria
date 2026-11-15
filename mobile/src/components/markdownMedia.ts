import { marked } from "marked";
import {
  classifyWorkspaceFile,
  resolveWorkspaceMediaPath,
} from "@opencuria/chat-core/workspaceFileRefs";

/** A picture or video referenced from chat Markdown. */
export interface MarkdownMedia {
  kind: "image" | "video";
  /** Sandboxed `/workspace/...` path, loaded through the authenticated cache. */
  path?: string;
  /** Safe remote URL, rendered directly. */
  uri?: string;
  label: string;
}

/** Markdown for the native renderer, interleaved with natively drawn media. */
export type MarkdownPiece =
  | { kind: "markdown"; text: string }
  | { kind: "media"; media: MarkdownMedia };

interface LexToken {
  type?: string;
  raw?: string;
  href?: string;
  text?: string;
  [key: string]: unknown;
}

/** An image reference: shown as media, or as its alt text when unsupported. */
interface Reference {
  /** Exact source syntax, e.g. `![alt](path)` or `<img src=…>`. */
  raw: string;
  media: MarkdownMedia | null;
  alt: string;
}

/**
 * A located source range to replace. `keepText` units are inline wrappers
 * (link, emphasis) with real text: the text stays, its media follows it.
 */
type Operation =
  | { kind: "skip"; raw: string }
  | { kind: "unit"; raw: string; references: Reference[]; keepText: boolean };

interface Replacement {
  start: number;
  end: number;
  pieces: MarkdownPiece[];
}

const INLINE_WRAPPERS = new Set(["link", "strong", "em", "del"]);
const HTML_IMAGE = /<img\b[^>]*>/i;
/** Longer multi-line tokens are matched literally only. */
const MAX_FUZZY_RAW = 10_000;

/** HTTPS URLs without credentials or secret-looking query parameters. */
export function isSafeExternalUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const sensitiveParameter =
      /(?:^|[_-])(token|auth|key|secret|signature|credential|password)(?:$|[_-])/i;
    const development = typeof __DEV__ !== "undefined" && __DEV__;
    return (
      (url.protocol === "https:" ||
        (development && url.protocol === "http:")) &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password &&
      ![...url.searchParams.keys()].some((key) => sensitiveParameter.test(key))
    );
  } catch {
    return false;
  }
}

/**
 * Split chat Markdown into native Markdown and media pieces.
 *
 * Images are found wherever GFM allows them, including list items,
 * blockquotes, tables, linked images and HTML `<img>` tags. Each image is
 * located in the original source, so the surrounding Markdown stays intact.
 * Images inside table cells keep their alt text in the table and follow it.
 */
export function splitMarkdownMedia(markdown: string): MarkdownPiece[] {
  const source = markdown.replace(/\r\n?/g, "\n");
  if (!source.includes("![") && !/<img\b/i.test(source))
    return source ? [{ kind: "markdown", text: source }] : [];
  let tokens: unknown[];
  try {
    tokens = marked.lexer(source) as unknown[];
  } catch {
    return [{ kind: "markdown", text: source }];
  }

  const replacements: Replacement[] = [];
  let offset = 0;
  for (const value of tokens) {
    const token = asToken(value);
    if (!token?.raw) continue;
    // Top-level raws tile the source; search only if a token ever diverges.
    const start = source.startsWith(token.raw, offset)
      ? offset
      : source.indexOf(token.raw, offset);
    if (start < 0) continue;
    const end = start + token.raw.length;
    offset = end;
    replacements.push(
      ...(token.type === "table"
        ? tableReplacements(source, token, start, end)
        : blockReplacements(source, token, start, end)),
    );
  }
  if (!replacements.length) return [{ kind: "markdown", text: source }];

  const pieces: MarkdownPiece[] = [];
  let cursor = 0;
  for (const replacement of replacements) {
    if (replacement.start < cursor) continue;
    pushMarkdown(pieces, source.slice(cursor, replacement.start));
    for (const piece of replacement.pieces)
      if (piece.kind === "media") pieces.push(piece);
      else pushMarkdown(pieces, piece.text);
    cursor = replacement.end;
  }
  pushMarkdown(pieces, source.slice(cursor));
  return tidyPieces(pieces);
}

function blockReplacements(
  source: string,
  block: LexToken,
  start: number,
  end: number,
): Replacement[] {
  const operations: Operation[] = [];
  collect(block, operations);
  if (!operations.some((operation) => operation.kind === "unit")) return [];
  const replacements: Replacement[] = [];
  const orphans: MarkdownPiece[] = [];
  let cursor = start;
  for (const operation of operations) {
    const range = locate(source, operation.raw, cursor, end);
    if (!range) {
      // Unmatched syntax stays in the Markdown; its media still appears.
      if (operation.kind === "unit")
        orphans.push(...mediaPieces(operation.references));
      continue;
    }
    cursor = range[1];
    if (operation.kind === "skip") continue;
    const located = source.slice(range[0], range[1]);
    replacements.push({
      start: range[0],
      end: range[1],
      pieces: operation.keepText
        ? [
            {
              kind: "markdown",
              text: replaceWithAlt(located, operation.references, false),
            },
            ...mediaPieces(operation.references),
          ]
        : operation.references.map((reference) =>
            reference.media
              ? { kind: "media", media: reference.media }
              : { kind: "markdown", text: escapeMarkdown(reference.alt) },
          ),
    });
  }
  if (orphans.length) replacements.push({ start: end, end, pieces: orphans });
  return replacements;
}

/** Tables keep their shape: cells show the alt text, media follows below. */
function tableReplacements(
  source: string,
  table: LexToken,
  start: number,
  end: number,
): Replacement[] {
  const references: Reference[] = [];
  collectReferences(table, references);
  if (!references.length) return [];
  return [
    {
      start,
      end,
      pieces: [
        {
          kind: "markdown",
          text: replaceWithAlt(source.slice(start, end), references, true),
        },
        ...mediaPieces(references),
      ],
    },
  ];
}

/** Document-order image units plus code anchors that guard the search. */
function collect(node: unknown, out: Operation[]): void {
  const token = asToken(node);
  if (!token) return;
  const raw = typeof token.raw === "string" ? token.raw : "";
  if (token.type === "image") {
    out.push({
      kind: "unit",
      raw,
      references: [imageReference(token)],
      keepText: false,
    });
    return;
  }
  if (token.type === "html") {
    const references = htmlReferences(raw);
    out.push(
      references.length
        ? { kind: "unit", raw, references, keepText: false }
        : { kind: "skip", raw },
    );
    return;
  }
  if (token.type === "code" || token.type === "codespan") {
    out.push({ kind: "skip", raw });
    return;
  }
  if (token.type && INLINE_WRAPPERS.has(token.type) && raw) {
    const references: Reference[] = [];
    collectReferences(token, references);
    if (references.length) {
      out.push({ kind: "unit", raw, references, keepText: !onlyMedia(token) });
      return;
    }
  }
  forEachChild(token, (child) => collect(child, out));
}

function collectReferences(node: unknown, out: Reference[]): void {
  const token = asToken(node);
  if (!token) return;
  if (token.type === "image") {
    out.push(imageReference(token));
    return;
  }
  if (token.type === "html") {
    out.push(...htmlReferences(typeof token.raw === "string" ? token.raw : ""));
    return;
  }
  if (token.type === "code" || token.type === "codespan") return;
  forEachChild(token, (child) => collectReferences(child, out));
}

/** Children live in `tokens`, `items`, table `header` cells and `rows`. */
function forEachChild(token: LexToken, visit: (child: unknown) => void): void {
  const walk = (value: unknown[]) => {
    for (const child of value)
      if (Array.isArray(child)) walk(child);
      else visit(child);
  };
  for (const value of Object.values(token))
    if (Array.isArray(value)) walk(value);
}

/** True when a wrapper holds media and whitespace only, e.g. a linked image. */
function onlyMedia(token: LexToken): boolean {
  if (token.type === "image") return true;
  if (token.type === "html") return htmlReferences(token.raw ?? "").length > 0;
  if (token.type === "text" || token.type === "br")
    return !(token.text ?? "").trim();
  if (!token.type || !INLINE_WRAPPERS.has(token.type)) return false;
  const children = Array.isArray(token.tokens) ? token.tokens : [];
  return (
    children.length > 0 &&
    children.every((child) => {
      const nested = asToken(child);
      return nested ? onlyMedia(nested) : false;
    })
  );
}

function imageReference(token: LexToken): Reference {
  // marked has already parsed the destination (brackets, title, spaces).
  const destination = typeof token.href === "string" ? token.href.trim() : "";
  return reference(
    typeof token.raw === "string" ? token.raw : "",
    destination,
    typeof token.text === "string" ? token.text : "",
  );
}

function htmlReferences(html: string): Reference[] {
  const references: Reference[] = [];
  const pattern = new RegExp(HTML_IMAGE.source, "gi");
  for (let match = pattern.exec(html); match; match = pattern.exec(html)) {
    const tag = match[0];
    const source = htmlAttribute(tag, "src");
    if (source)
      references.push(reference(tag, source, htmlAttribute(tag, "alt") ?? ""));
  }
  return references;
}

function reference(raw: string, destination: string, alt: string): Reference {
  const path = workspaceMediaPath(destination);
  const label = alt.trim() || (path ? filename(path) : "") || "Media";
  if (path) {
    const kind = classifyWorkspaceFile(path);
    return {
      raw,
      alt: label,
      media:
        kind === "image" || kind === "video" ? { kind, path, label } : null,
    };
  }
  return {
    raw,
    alt: label,
    media: isSafeExternalUrl(destination)
      ? { kind: "image", uri: destination, label }
      : null,
  };
}

/**
 * Percent-encode a parsed destination like marked's own URL cleaning (and the
 * webapp's rendered `src`), so spaces survive and the media cache decodes once.
 */
function workspaceMediaPath(destination: string): string | null {
  if (!destination) return null;
  try {
    return resolveWorkspaceMediaPath(
      encodeURI(destination).replace(/%25/g, "%"),
    );
  } catch {
    return null;
  }
}

function htmlAttribute(tag: string, name: string): string | null {
  const match = new RegExp(
    `\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\`]+))`,
    "i",
  ).exec(tag);
  if (!match) return null;
  return (match[1] ?? match[2] ?? match[3] ?? "")
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .trim();
}

/**
 * Find a token in the source. Container blocks strip line prefixes
 * (indentation, `>`) from their children, so multi-line tokens may carry
 * such prefixes in the source after each line break.
 */
function locate(
  source: string,
  raw: string,
  from: number,
  limit: number,
): [number, number] | null {
  if (!raw) return null;
  const literal = source.indexOf(raw, from);
  if (literal >= 0 && literal + raw.length <= limit)
    return [literal, literal + raw.length];
  if (!raw.includes("\n") || raw.length > MAX_FUZZY_RAW) return null;
  const pattern = new RegExp(
    raw.split("\n").map(escapeRegExp).join("\\n[ \\t>]*"),
    "g",
  );
  pattern.lastIndex = from;
  const match = pattern.exec(source);
  return match && match.index + match[0].length <= limit
    ? [match.index, match.index + match[0].length]
    : null;
}

function replaceWithAlt(
  text: string,
  references: Reference[],
  tableCell: boolean,
): string {
  let output = "";
  let cursor = 0;
  for (const reference of references) {
    const at = reference.raw ? text.indexOf(reference.raw, cursor) : -1;
    if (at < 0) continue;
    const alt = escapeMarkdown(reference.alt);
    output +=
      text.slice(cursor, at) + (tableCell ? alt.replace(/\|/g, "\\|") : alt);
    cursor = at + reference.raw.length;
  }
  return output + text.slice(cursor);
}

function mediaPieces(references: Reference[]): MarkdownPiece[] {
  return references.flatMap((reference) =>
    reference.media ? [{ kind: "media" as const, media: reference.media }] : [],
  );
}

function pushMarkdown(pieces: MarkdownPiece[], text: string): void {
  if (!text) return;
  const last = pieces[pieces.length - 1];
  if (last?.kind === "markdown") last.text += text;
  else pieces.push({ kind: "markdown", text });
}

/**
 * Markdown that resumes after media continues the interrupted block. Its
 * container indentation is dropped (deep indentation would render as code),
 * and fragments made of bare list or quote markers are omitted.
 */
function tidyPieces(pieces: MarkdownPiece[]): MarkdownPiece[] {
  const tidy: MarkdownPiece[] = [];
  pieces.forEach((piece, index) => {
    if (piece.kind === "media") {
      tidy.push(piece);
      return;
    }
    const text =
      pieces[index - 1]?.kind === "media"
        ? dedentContinuation(piece.text)
        : piece.text;
    if (hasVisibleContent(text)) tidy.push({ kind: "markdown", text });
  });
  return tidy;
}

function dedentContinuation(text: string): string {
  const lines = text.split("\n");
  lines[0] = lines[0]!.replace(/^[ \t]+/, "");
  let end = 1;
  while (
    end < lines.length &&
    (!lines[end]!.trim() || /^[ \t]/.test(lines[end]!))
  )
    end += 1;
  const indents = lines
    .slice(1, end)
    .filter((line) => line.trim())
    .map((line) => /^[ \t]*/.exec(line)![0].length);
  if (indents.length) {
    const indent = Math.min(...indents);
    for (let index = 1; index < end; index += 1)
      lines[index] = lines[index]!.slice(
        Math.min(indent, lines[index]!.length),
      );
  }
  return lines.join("\n");
}

function hasVisibleContent(text: string): boolean {
  return text.split("\n").some((line) => {
    let rest = line.trim();
    for (;;) {
      const next = rest.replace(
        /^(?:>|[-*+](?=\s|$)|\d{1,9}[.)](?=\s|$)|#{1,6}(?=\s|$))\s*/,
        "",
      );
      if (next === rest) return rest.length > 0;
      rest = next;
    }
  });
}

function escapeMarkdown(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!|<>~]/g, "\\$&");
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function filename(path: string): string {
  const name = path.split("/").filter(Boolean).pop() ?? "";
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

function asToken(value: unknown): LexToken | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as LexToken)
    : null;
}
