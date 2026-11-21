import { marked } from "marked";
import {
  parseComposerSegments,
  type ComposerFileToken,
  type ComposerToken,
} from "@opencuria/chat-core/composerTokens";
import { classifyWorkspaceFile } from "@opencuria/chat-core/workspaceFileRefs";

/** Inline styles a prompt run inherits from its Markdown. */
export interface PromptRunStyle {
  strong?: boolean;
  em?: boolean;
  strike?: boolean;
  code?: boolean;
  /** Link target; the run is pressable. */
  href?: string;
}

/** A run of a prompt paragraph: styled text, or a mention drawn as a chip. */
export type PromptRun =
  | ({ kind: "text"; text: string } & PromptRunStyle)
  | { kind: "mention"; token: ComposerToken };

/**
 * A prompt block. Paragraphs with mentions are laid out natively so their
 * chips flow with the text, as in the webapp; everything else stays native
 * Markdown, where file mentions become links.
 */
export type PromptBlock =
  | { kind: "paragraph"; runs: PromptRun[] }
  | { kind: "markdown"; markdown: string };

export interface PromptLayout {
  blocks: PromptBlock[];
  /** Image mentions outside code, shown as thumbnails below the bubble. */
  images: ComposerFileToken[];
}

interface LexToken {
  type?: string;
  raw?: string;
  text?: string;
  href?: string;
  tokens?: LexToken[];
}

/** Unterminated or fenced code keeps its mentions as typed. */
const INLINE_CODE = /(`+)[\s\S]*?\1/g;

/** Lay out a sent prompt: mention chips in paragraphs, Markdown elsewhere. */
export function promptLayout(markdown: string): PromptLayout {
  if (!markdown.includes("@")) return { blocks: plain(markdown), images: [] };
  const blocks: PromptBlock[] = [];
  const images: ComposerFileToken[] = [];
  let pending = "";
  const flush = () => {
    if (pending.trim()) blocks.push({ kind: "markdown", markdown: pending });
    pending = "";
  };
  let tokens: LexToken[];
  try {
    tokens = marked.lexer(markdown) as LexToken[];
  } catch {
    return { blocks: plain(markdown), images: [] };
  }
  for (const token of tokens) {
    if (token.type === "paragraph" && !hasImage(token.tokens ?? [])) {
      const runs = inlineRuns(token.tokens ?? [], {});
      if (runs.some((run) => run.kind === "mention")) {
        flush();
        blocks.push({ kind: "paragraph", runs });
        for (const run of runs)
          if (run.kind === "mention") addImage(images, run.token);
        continue;
      }
    }
    pending +=
      token.type === "code" || token.type === "html"
        ? (token.raw ?? "")
        : linkMentions(token.raw ?? "", images);
  }
  flush();
  return { blocks, images };
}

function plain(markdown: string): PromptBlock[] {
  return markdown.trim() ? [{ kind: "markdown", markdown }] : [];
}

function hasImage(tokens: LexToken[]): boolean {
  return tokens.some(
    (token) => token.type === "image" || hasImage(token.tokens ?? []),
  );
}

function addImage(images: ComposerFileToken[], token: ComposerToken): void {
  if (
    token.kind === "file" &&
    classifyWorkspaceFile(token.path) === "image" &&
    !images.some((image) => image.path === token.path)
  )
    images.push(token);
}

function inlineRuns(tokens: LexToken[], style: PromptRunStyle): PromptRun[] {
  const runs: PromptRun[] = [];
  for (const token of tokens) {
    switch (token.type) {
      case "text":
      case "escape":
        if (token.tokens?.length) {
          runs.push(...inlineRuns(token.tokens, style));
          break;
        }
        // A soft break reads as a space, as in native Markdown.
        for (const segment of parseComposerSegments(
          (token.text ?? "").replace(/\n/g, " "),
        )) {
          runs.push(
            segment.kind === "text"
              ? { kind: "text", text: segment.text, ...style }
              : { kind: "mention", token: segment },
          );
        }
        break;
      case "strong":
        runs.push(
          ...inlineRuns(token.tokens ?? [], { ...style, strong: true }),
        );
        break;
      case "em":
        runs.push(...inlineRuns(token.tokens ?? [], { ...style, em: true }));
        break;
      case "del":
        runs.push(
          ...inlineRuns(token.tokens ?? [], { ...style, strike: true }),
        );
        break;
      case "link":
        runs.push(
          ...inlineRuns(token.tokens ?? [], { ...style, href: token.href }),
        );
        break;
      case "codespan":
        runs.push({
          kind: "text",
          text: token.text ?? "",
          ...style,
          code: true,
        });
        break;
      case "br":
        runs.push({ kind: "text", text: "\n", ...style });
        break;
      default:
        runs.push({ kind: "text", text: token.raw ?? "", ...style });
    }
  }
  return runs;
}

/** File mentions in native Markdown become links that open the file. */
function linkMentions(raw: string, images: ComposerFileToken[]): string {
  if (!raw.includes("@file:")) return raw;
  let result = "";
  let last = 0;
  for (const code of raw.matchAll(INLINE_CODE)) {
    result += linkProse(raw.slice(last, code.index), images) + code[0];
    last = code.index + code[0].length;
  }
  return result + linkProse(raw.slice(last), images);
}

function linkProse(text: string, images: ComposerFileToken[]): string {
  return parseComposerSegments(text)
    .map((segment) => {
      if (segment.kind !== "file")
        return segment.kind === "text" ? segment.text : segment.raw;
      addImage(images, segment);
      return `[${segment.name.replace(/[[\]\\]/g, "")}](<${segment.path}>)`;
    })
    .join("");
}
