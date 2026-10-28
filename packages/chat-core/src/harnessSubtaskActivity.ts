/**
 * Subagent timeline helpers: type labels, child-tool activity, and
 * whether a message still has live model steps or tool/subtask work.
 */

import type { HarnessMessage, HarnessPart, HarnessSession } from "./harness";

const SUBAGENT_TYPE_LABELS: Record<string, string> = {
  explore: "Explorer",
  general: "General",
  computeruse: "Computer use",
};

/** Display label for a subagent type (`explore` → `Explorer`). */
export function formatSubagentType(
  agent: string | null | undefined,
): string | null {
  if (!agent) return null;
  const key = agent.trim().toLowerCase();
  if (!key) return null;
  const mapped = SUBAGENT_TYPE_LABELS[key];
  if (mapped) return mapped;
  return key.charAt(0).toUpperCase() + key.slice(1);
}

/** Display label for the known subagent types that own child sessions. */
export function gateSourceLabel(
  agentName: string | null | undefined,
): string | null {
  const key = (agentName || "").trim().toLowerCase();
  if (key === "explore" || key === "general" || key === "computeruse") {
    return formatSubagentType(key);
  }
  return null;
}

/**
 * Ancestor chain from root to *sessionId* (inclusive).
 * Cycle-safe; stops when a parent is missing from *sessions*.
 */
export function collectAncestorSessions(
  sessionId: string,
  sessions: HarnessSession[],
): HarnessSession[] {
  const byId = new Map(sessions.map((session) => [session.id, session]));
  const current = byId.get(sessionId);
  if (!current) return [];
  const chain: HarnessSession[] = [];
  const seen = new Set<string>();
  let cursor: HarnessSession | undefined = current;
  while (cursor) {
    if (seen.has(cursor.id)) break;
    seen.add(cursor.id);
    chain.push(cursor);
    const parentId = cursor.parent_id;
    if (!parentId) break;
    cursor = byId.get(parentId);
  }
  chain.reverse();
  return chain;
}

/**
 * *rootId* plus every descendant session id (parent_id chain), breadth-first.
 */
export function collectDescendantSessionIds(
  rootId: string,
  sessions: HarnessSession[],
): string[] {
  const ids: string[] = [rootId];
  const seen = new Set<string>([rootId]);
  let added = true;
  while (added) {
    added = false;
    for (const session of sessions) {
      if (
        !session.parent_id ||
        seen.has(session.id) ||
        !seen.has(session.parent_id)
      ) {
        continue;
      }
      seen.add(session.id);
      ids.push(session.id);
      added = true;
    }
  }
  return ids;
}

/**
 * True while a model step has started but its finish marker has not arrived.
 */
export function hasRunningStep(parts: HarnessPart[]): boolean {
  const active = new Set<number>();
  const finished = new Set<number>();
  for (const part of parts) {
    const step = part.meta?.["step"] ?? part.display?.step;
    if (typeof step !== "number" || !Number.isFinite(step)) continue;
    if (part.type === "step-finish") finished.add(step);
    else if (
      part.type === "step-start" ||
      (part.type === "reasoning" && part.state === "running")
    ) {
      active.add(step);
    }
  }
  for (const step of active) {
    if (!finished.has(step)) return true;
  }
  return false;
}

/**
 * True when this tool part is the parent `task` invocation that duplicates
 * a sibling subtask card (`tool === 'task'` or title `Subagent: …`).
 */
export function isTaskToolPart(part: HarnessPart): boolean {
  if (part.type !== "tool") return false;
  const named = (part.tool || "").trim().toLowerCase();
  if (named === "task") return true;
  const fromInput = part.input?.["tool"];
  if (
    typeof fromInput === "string" &&
    fromInput.trim().toLowerCase() === "task"
  ) {
    return true;
  }
  return (part.title || "").startsWith("Subagent:");
}

/** Latest running tool part in child-session messages (newest last). */
export function latestRunningChildTool(
  messages: HarnessMessage[],
): HarnessPart | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!message) continue;
    for (let j = message.parts.length - 1; j >= 0; j -= 1) {
      const part = message.parts[j];
      if (
        part?.type === "tool" &&
        (part.state === "running" || part.state === "pending")
      ) {
        return part;
      }
    }
  }
  return null;
}

/** Child session ids for still-running subtask parts. */
export function collectRunningChildSessionIds(
  messages: HarnessMessage[],
): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type !== "subtask" || part.state !== "running") continue;
      const childId = resolveChildSessionId(part);
      if (!childId || seen.has(childId)) continue;
      seen.add(childId);
      ids.push(childId);
    }
  }
  return ids;
}

/** Return the child chat explicitly linked by the backend, or null. */
export function resolveChildSessionId(part: HarnessPart): string | null {
  const childId =
    part.meta?.["child_session_id"] ?? part.display?.child_session_id;
  return typeof childId === "string" && childId.trim() ? childId.trim() : null;
}

/**
 * Subtitle under the subagent title: current child tool, or a terminal status.
 * Running with no child tool yet returns null.
 */
export function subtaskActivityLabel(
  part: HarnessPart,
  childMessages: HarnessMessage[],
): string | null {
  if (part.state === "completed") return "Completed";
  if (part.state === "error") return "Failed";
  if (part.state !== "running") return null;
  const tool = latestRunningChildTool(childMessages);
  const label = (tool?.title || tool?.tool || "").trim();
  return label || null;
}

/** True when a tool or subtask part is still running (blocks Thinking). */
export function hasRunningToolOrSubtask(parts: HarnessPart[]): boolean {
  return parts.some(
    (part) =>
      (part.type === "tool" || part.type === "subtask") &&
      (part.state === "running" || part.state === "pending"),
  );
}
