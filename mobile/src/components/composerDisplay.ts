import {
  parseComposerSegments,
  type ComposerSegment,
} from "@opencuria/chat-core/composerTokens";

/**
 * The composer edits a draft in wire format (`@file:/workspace/src/a.ts`)
 * through a native text field that shows file mentions by name, like the
 * webapp's chips. These pure helpers translate between the two texts.
 */

/** A piece of the shown text and the draft range it stands for. */
export interface DisplayPiece {
  segment: ComposerSegment;
  /** What the field shows for the segment. */
  text: string;
  start: number;
  end: number;
}

export interface ComposerDisplay {
  /** The draft it shows. */
  source: string;
  /** The text the native field shows. */
  text: string;
  pieces: DisplayPiece[];
}

export interface DisplayEdit {
  draft: string;
  /** Caret in the draft after the edit. */
  cursor: number;
}

/** Longest file name a mention shows; longer ones keep head and extension. */
const NAME_LIMIT = 24;

/**
 * A file name short enough for a chip, like the webapp's truncated badge:
 * `page-2026-09-25…617Z.png`.
 */
export function shortFileName(name: string, limit = NAME_LIMIT): string {
  if (name.length <= limit) return name;
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 && name.length - dot <= 8 ? name.slice(dot) : "";
  const tail = Math.min(name.length - 1, extension.length + 4);
  return `${name.slice(0, limit - 1 - tail)}…${name.slice(name.length - tail)}`;
}

/** Shown label of a mention; agents read as typed. */
export function mentionLabel(segment: ComposerSegment): string {
  if (segment.kind === "file") return shortFileName(segment.name);
  return segment.kind === "agent" ? segment.raw : segment.text;
}

/** The shown text of a draft, piece by piece. */
export function composerDisplay(draft: string): ComposerDisplay {
  const pieces: DisplayPiece[] = [];
  let text = "";
  for (const segment of parseComposerSegments(draft)) {
    const shown = mentionLabel(segment);
    pieces.push({
      segment,
      text: shown,
      start: text.length,
      end: text.length + shown.length,
    });
    text += shown;
  }
  return { source: draft, text, pieces };
}

function pieceAt(
  display: ComposerDisplay,
  offset: number,
): DisplayPiece | undefined {
  return display.pieces.find(
    (piece) => offset >= piece.start && offset <= piece.end,
  );
}

/**
 * A shown offset in the draft. Offsets inside a mention snap to the nearer
 * edge, or to `bias` when given: the caret never sits inside a chip.
 */
export function draftOffset(
  display: ComposerDisplay,
  offset: number,
  bias?: "start" | "end",
): number {
  const piece = pieceAt(display, offset);
  if (!piece) return display.pieces.at(-1)?.segment.end ?? 0;
  const { segment } = piece;
  if (segment.kind === "text") return segment.start + (offset - piece.start);
  if (offset === piece.start) return segment.start;
  if (offset === piece.end) return segment.end;
  const end = bias ? bias === "end" : offset - piece.start > piece.end - offset;
  return end ? segment.end : segment.start;
}

/** A shown selection in the draft; a caret inside a chip moves to an edge. */
export function draftSelection(
  display: ComposerDisplay,
  selection: { start: number; end: number },
): { start: number; end: number } {
  if (selection.start === selection.end) {
    const caret = draftOffset(display, selection.start);
    return { start: caret, end: caret };
  }
  return {
    start: draftOffset(display, selection.start, "start"),
    end: draftOffset(display, selection.end, "end"),
  };
}

/** A draft offset in the shown text; inside a mention it moves to its end. */
export function displayOffset(
  display: ComposerDisplay,
  offset: number,
): number {
  for (const piece of display.pieces) {
    const { segment } = piece;
    if (offset > segment.end) continue;
    if (segment.kind === "text") return piece.start + (offset - segment.start);
    return offset === segment.start ? piece.start : piece.end;
  }
  return display.text.length;
}

/**
 * Apply an edit of the shown text to the draft. Deleting into a mention
 * deletes all of it, as backspacing a chip does; text typed against a mention
 * never merges with its path. `caret`, the shown caret before the edit,
 * places an edit of repeated characters where it happened.
 */
export function applyDisplayEdit(
  draft: string,
  shown: string,
  caret?: number,
): DisplayEdit {
  const display = composerDisplay(draft);
  const before = display.text;
  if (shown === before) return { draft, cursor: draft.length };
  let prefix = 0;
  const limit = Math.min(before.length, shown.length);
  while (prefix < limit && before[prefix] === shown[prefix]) prefix += 1;
  if (caret !== undefined) prefix = Math.min(prefix, Math.max(0, caret));
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    before[before.length - 1 - suffix] === shown[shown.length - 1 - suffix]
  )
    suffix += 1;
  const removedEnd = before.length - suffix;
  const inserted = shown.slice(prefix, shown.length - suffix);
  const from =
    removedEnd === prefix
      ? draftOffset(display, prefix)
      : draftOffset(display, prefix, "start");
  const to =
    removedEnd === prefix ? from : draftOffset(display, removedEnd, "end");
  let next = draft.slice(0, from) + inserted + draft.slice(to);
  let cursor = from + inserted.length;
  if (inserted) {
    // A mention touching the inserted text keeps its exact token.
    const kept = parseComposerSegments(next);
    const intact = (segment: ComposerSegment, shift: number) =>
      kept.some(
        (other) =>
          other.kind === segment.kind &&
          other.start === segment.start + shift &&
          other.end === segment.end + shift,
      );
    for (const { segment } of display.pieces) {
      if (segment.kind === "text") continue;
      if (segment.end === from && !intact(segment, 0)) {
        next = `${draft.slice(0, from)} ${inserted}${draft.slice(to)}`;
        cursor += 1;
        break;
      }
      if (
        segment.start === to &&
        !intact(segment, inserted.length - (to - from))
      ) {
        next = `${draft.slice(0, from)}${inserted} ${draft.slice(to)}`;
        break;
      }
    }
  }
  return { draft: next, cursor };
}
