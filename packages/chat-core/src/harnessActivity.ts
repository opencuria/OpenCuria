/**
 * Compact activity timeline for narrow chat clients (mobile).
 *
 * Unlike `buildMessageBlocks`, every contiguous run of agent work — tool
 * calls (failed ones included), patches, reasoning and subtasks — becomes one
 * activity block summarized as e.g. "Explored 9 files, 2 searches". Only
 * content meant for the reader splits a run: assistant text, delivered
 * steers, answered questions and the to-do card. Nothing is wrapped once the
 * turn finishes, so the layout stays the same while and after it streams.
 */

import type { HarnessMessage, HarnessPart } from "./harness";
import { isQuestionToolPart, visibleTimelineParts } from "./harnessBlocks";
import { isTaskToolPart } from "./harnessSubtaskActivity";
import { resolveToolName } from "./toolDisplay";

export type ActivityTimelineBlock =
  | { kind: "text"; part: HarnessPart }
  | { kind: "steer"; part: HarnessPart }
  | { kind: "question"; part: HarnessPart }
  | { kind: "todos"; part: HarnessPart }
  | { kind: "activity"; parts: HarnessPart[] };

/** Lower-case tool id, including the projection of detail-less parts. */
function toolKey(part: HarnessPart): string {
  return (resolveToolName(part) || String(part.display?.tool ?? ""))
    .trim()
    .toLowerCase();
}

/** A `todowrite` call: the place where the session to-do list is shown. */
export function isTodoToolPart(part: HarnessPart): boolean {
  return part.type === "tool" && toolKey(part) === "todowrite";
}

/**
 * Group visible parts into reader content and activity runs.
 * `todoPartId` marks the call that renders the to-do card in place.
 */
export function buildActivityBlocks(
  parts: HarnessPart[],
  options: { todoPartId?: string | null } = {},
): ActivityTimelineBlock[] {
  const blocks: ActivityTimelineBlock[] = [];
  let run: HarnessPart[] = [];
  function flushRun(): void {
    if (run.length) blocks.push({ kind: "activity", parts: run });
    run = [];
  }
  for (const part of visibleTimelineParts(parts)) {
    if (part.type === "text") {
      flushRun();
      blocks.push({ kind: "text", part });
    } else if (part.type === "steer") {
      flushRun();
      if (part.output) blocks.push({ kind: "steer", part });
    } else if (isQuestionToolPart(part)) {
      flushRun();
      blocks.push({ kind: "question", part });
    } else if (options.todoPartId && part.id === options.todoPartId) {
      flushRun();
      blocks.push({ kind: "todos", part });
    } else if (part.type === "compaction") {
      flushRun();
      blocks.push({ kind: "activity", parts: [part] });
    } else {
      run.push(part);
    }
  }
  flushRun();
  return blocks;
}

/** Where the session to-do card renders inside an ordered transcript. */
export interface TodoAnchor {
  messageId: string;
  /** The hosting `todowrite` part, or null to append after the message. */
  partId: string | null;
}

/**
 * The latest non-failed `todowrite` call hosts the live to-do list. Engines
 * that publish plans without a tool call (or a call outside the loaded
 * history) fall back to the end of the latest assistant message.
 */
export function findTodoAnchor(messages: HarnessMessage[]): TodoAnchor | null {
  let latestAssistant: string | null = null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    if (message.role !== "assistant") continue;
    latestAssistant ??= message.id;
    let anchor: HarnessPart | null = null;
    for (const part of message.parts ?? []) {
      if (!isTodoToolPart(part) || part.state === "error") continue;
      if (!anchor || (part.position ?? 0) >= (anchor.position ?? 0)) {
        anchor = part;
      }
    }
    if (anchor) return { messageId: message.id, partId: anchor.id };
  }
  return latestAssistant ? { messageId: latestAssistant, partId: null } : null;
}

type ActivityCategory =
  | "read"
  | "search"
  | "edit"
  | "command"
  | "task"
  | "todo"
  | "other";

const SEARCH_TOOLS = new Set(["grep", "glob", "list", "websearch"]);
const EDIT_TOOLS = new Set(["edit", "write"]);

function activityCategory(part: HarnessPart): ActivityCategory | null {
  if (part.type === "reasoning" || part.type === "compaction") return null;
  if (part.type === "patch") return "edit";
  if (part.type === "subtask" || isTaskToolPart(part)) return "task";
  if (part.type !== "tool") return "other";
  const tool = toolKey(part);
  if (tool === "read") return "read";
  if (SEARCH_TOOLS.has(tool)) return "search";
  if (EDIT_TOOLS.has(tool)) return "edit";
  if (tool === "bash") return "command";
  if (tool === "todowrite") return "todo";
  return "other";
}

/** Past and progressive verb per phrase; the first one titles the summary. */
const VERBS = {
  explore: ["Explored", "Exploring"],
  edit: ["Edited", "Editing"],
  command: ["Ran", "Running"],
  task: ["Delegated", "Delegating"],
  todo: ["Updated", "Updating"],
  other: ["Used", "Using"],
} as const;

function count(value: number, singular: string, plural = `${singular}s`) {
  return `${value} ${value === 1 ? singular : plural}`;
}

export interface ActivitySummary {
  /** Leading verb or label, e.g. "Explored" or "Thought". */
  title: string;
  /** Counts after the title, e.g. "9 files, 2 searches, 2 other tools". */
  detail: string;
  /** True while any part of the run is still pending or running. */
  running: boolean;
}

/** One-line summary of an activity run, e.g. "Explored 9 files, 2 searches". */
export function summarizeActivity(parts: HarnessPart[]): ActivitySummary {
  const running = parts.some(
    (part) => part.state === "running" || part.state === "pending",
  );
  const counts: Record<ActivityCategory, number> = {
    read: 0,
    search: 0,
    edit: 0,
    command: 0,
    task: 0,
    todo: 0,
    other: 0,
  };
  let failed = 0;
  for (const part of parts) {
    const category = activityCategory(part);
    if (category) counts[category] += 1;
    if (part.state === "error") failed += 1;
  }
  const explored = [
    counts.read ? count(counts.read, "file") : "",
    counts.search ? count(counts.search, "search", "searches") : "",
  ]
    .filter(Boolean)
    .join(", ");
  const phrases: Array<[keyof typeof VERBS, string]> = [];
  if (explored) phrases.push(["explore", explored]);
  if (counts.edit) phrases.push(["edit", count(counts.edit, "file")]);
  if (counts.command)
    phrases.push(["command", count(counts.command, "command")]);
  if (counts.task) phrases.push(["task", count(counts.task, "task")]);
  if (counts.todo) phrases.push(["todo", "to-dos"]);
  if (counts.other) {
    phrases.push([
      "other",
      count(counts.other, phrases.length ? "other tool" : "tool"),
    ]);
  }
  const tense = running ? 1 : 0;
  const [first, ...rest] = phrases;
  if (!first) {
    const thought = parts.some((part) => part.type === "reasoning");
    const compacted = parts.some((part) => part.type === "compaction");
    const title = thought
      ? ["Thought", "Thinking"][tense]!
      : compacted
        ? ["Compacted context", "Compacting context"][tense]!
        : ["Worked", "Working"][tense]!;
    return { title, detail: "", running };
  }
  const detail = [
    first[1],
    ...rest.map(([key, text]) =>
      key === "other" ? text : `${VERBS[key][0].toLowerCase()} ${text}`,
    ),
    ...(failed ? [`${failed} failed`] : []),
  ].join(", ");
  return { title: VERBS[first[0]][tense], detail, running };
}
