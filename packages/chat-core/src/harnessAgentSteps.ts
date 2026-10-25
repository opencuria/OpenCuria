/**
 * Agent-S step/timeline helpers for the computer-use chat UI.
 *
 * The backend persists Agent-S plans as `agent` parts (full plan in
 * `output`, safe summaries in `meta.agent_meta`) with `meta.step`, plus
 * hidden `step-start`/`step-finish` markers and step-attributed `reasoning`
 * parts (`meta.step`). These pure helpers derive a compact per-step view
 * model without inventing facts missing from older/partial payloads.
 */

import type { HarnessAgentMeta, HarnessPart } from "./harness";

/** Step status derived from markers and streaming state. */
export type AgentStepStatus = "in_progress" | "completed" | "error";

/** Compact view model for one Agent-S plan step. */
export interface AgentStepView {
  part: HarnessPart;
  step: number | null;
  status: AgentStepStatus;
  /** True while this step is the latest and the turn is still running. */
  live: boolean;
  /** True for legacy/partial payloads without usable structured meta. */
  legacy: boolean;
}

const AGENT_META_KEYS: ReadonlyArray<keyof HarnessAgentMeta> = [
  "verification",
  "analysis",
  "next_action",
  "action",
  "action_kind",
];

function textOf(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** Defensive read of `part.meta.agent_meta` (nested object, strings only). */
export function readAgentMeta(part: HarnessPart): HarnessAgentMeta {
  const raw = part.meta?.["agent_meta"] ?? part.display?.agent_meta;
  const empty: HarnessAgentMeta = {
    verification: "",
    analysis: "",
    next_action: "",
    action: "",
    action_kind: "",
  };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return empty;
  const source = raw as Record<string, unknown>;
  return {
    verification: textOf(source["verification"]),
    analysis: textOf(source["analysis"]),
    next_action: textOf(source["next_action"]),
    action: textOf(source["action"]),
    action_kind: textOf(source["action_kind"]),
  };
}

/** Step number from `meta.step` (finite numbers only). */
export function agentStepNumber(part: HarnessPart): number | null {
  const step = part.meta?.["step"] ?? part.display?.step;
  if (typeof step === "number" && Number.isFinite(step)) return step;
  return null;
}

/** True when at least one structured meta field carries content. */
export function hasStructuredAgentMeta(part: HarnessPart): boolean {
  const meta = readAgentMeta(part);
  return AGENT_META_KEYS.some((key) => meta[key] !== "");
}

interface AgentStepFacts {
  starts: Set<number>;
  finishes: Set<number>;
  errors: Set<number>;
  active: Set<number>;
  maxStep: number | null;
}

/** Collect message-wide facts once instead of rescanning parts for each plan. */
function collectAgentStepFacts(parts: HarnessPart[]): AgentStepFacts {
  const facts: AgentStepFacts = {
    starts: new Set(),
    finishes: new Set(),
    errors: new Set(),
    active: new Set(),
    maxStep: null,
  };
  for (const part of parts) {
    const step = part.meta?.["step"] ?? part.display?.step;
    // Set treats NaN as equal to itself; the original marker comparisons use ===.
    if (typeof step !== "number" || Number.isNaN(step)) continue;
    switch (part.type) {
      case "agent":
        if (Number.isFinite(step)) {
          facts.maxStep =
            facts.maxStep === null ? step : Math.max(facts.maxStep, step);
          facts.active.add(step);
        }
        break;
      case "step-start":
        facts.starts.add(step);
        if (Number.isFinite(step)) facts.active.add(step);
        break;
      case "step-finish":
        facts.finishes.add(step);
        break;
      case "tool":
        if (part.state === "error") facts.errors.add(step);
        break;
      case "reasoning":
        if (part.state === "running" && Number.isFinite(step))
          facts.active.add(step);
        break;
    }
  }
  return facts;
}

/**
 * Derive the status of an `agent` part from its step markers.
 *
 * - `error` when a same-step tool failed (the step did not finish cleanly).
 * - `completed` when a matching `step-finish` marker exists, or when a newer
 *   `agent` plan superseded this one. A finished step still counts when the
 *   finish marker was superseded by a newer step-start (a newer `agent` plan
 *   for a higher step means the older step is done, even when its explicit
 *   `step-finish` marker has not arrived yet).
 * - `in_progress` otherwise (matching `step-start` seen, or streaming with
 *   no markers yet). Parts without a step number fall back to their own
 *   persisted state.
 */
export function agentStepStatus(
  part: HarnessPart,
  parts: HarnessPart[],
  step: number | null,
): AgentStepStatus {
  return statusFromFacts(part, step, collectAgentStepFacts(parts));
}

function statusFromFacts(
  part: HarnessPart,
  step: number | null,
  facts: AgentStepFacts,
): AgentStepStatus {
  if (step !== null) {
    if (facts.errors.has(step)) return "error";
    const hasFinish = facts.finishes.has(step);
    const hasStart = facts.starts.has(step);
    // A newer plan for a higher step supersedes this one: the older step is
    // done even when its explicit finish marker has not arrived yet. When no
    // explicit markers exist at all (legacy payloads), fall back to the
    // persisted part state so a lone running plan still reads as active.
    if (
      facts.maxStep !== null &&
      facts.maxStep > step &&
      (hasStart || hasFinish)
    )
      return "completed";
    if (hasFinish) return "completed";
    if (hasStart) return "in_progress";
    if (part.state === "running" || part.state === "pending")
      return "in_progress";
    return "completed";
  }
  if (part.state === "error") return "error";
  if (part.state === "running" || part.state === "pending")
    return "in_progress";
  return "completed";
}

function formatStepKind(actionKind: string, fallback: string): string {
  const kind = actionKind.trim().toLowerCase();
  if (!kind) return fallback;
  if (kind === "done") return "Finish task";
  if (kind === "fail") return "Report failure";
  const pretty = kind.replace(/_/g, " ").trim();
  if (!pretty) return fallback;
  return pretty.charAt(0).toUpperCase() + pretty.slice(1);
}

/**
 * Primary one-line action for a step header (e.g. `Click "Submit"`).
 *
 * Prefers the safe structured `action` summary; falls back to the first
 * non-empty line of `next_action`, then to a defensive single-line slice
 * of the raw plan (marked as legacy), without inventing details.
 */
export function agentPrimaryAction(part: HarnessPart): {
  text: string;
  legacy: boolean;
} {
  const meta = readAgentMeta(part);
  if (meta.action) {
    const kind = formatStepKind(meta.action_kind, meta.action);
    const trimmed = meta.action.trim();
    const kindKey = meta.action_kind.trim().toLowerCase();
    if (kindKey && trimmed.toLowerCase() === kindKey) {
      return { text: kind, legacy: false };
    }
    if (kindKey && trimmed.toLowerCase().startsWith(kindKey)) {
      // Capitalize the leading verb ("click ..." -> "Click ...") while
      // keeping the backend-provided remainder verbatim.
      const rest = trimmed.slice(kindKey.length);
      if (rest === "" || /^\s/.test(rest)) {
        return { text: `${kind}${rest}`, legacy: false };
      }
    }
    return { text: trimmed, legacy: false };
  }
  const displaySummary = textOf(part.display?.summary);
  if (displaySummary) return { text: displaySummary, legacy: false };
  if (meta.next_action) {
    const firstLine = meta.next_action
      .split("\n")
      .map((line) => line.trim())
      .find(Boolean);
    if (firstLine) return { text: firstLine, legacy: false };
  }
  const rawLines = (part.output || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("```"));
  if (rawLines.length > 0) {
    const first = rawLines[0] as string;
    const sliced = first.length > 120 ? `${first.slice(0, 117)}…` : first;
    return { text: sliced, legacy: true };
  }
  return { text: "Agent plan", legacy: true };
}

/**
 * View-model options: streaming state plus the final assistant-message
 * outcome. `finish`/`error` come straight from `HarnessMessage`
 * (`HarnessService` sets `finish='error'|'aborted'` with `error=...` on a
 * failed run, after a best-effort `step_finish` already closed the markers).
 * While streaming the message is not final yet, so no stale error may leak
 * into the timeline: callers pass the message finish/error only when the
 * run is no longer streaming.
 */
export interface AgentStepViewOptions {
  streaming?: boolean;
  /** Final `HarnessMessage.finish` (e.g. `'error'` / `'aborted'`). */
  finish?: string;
  /** Final `HarnessMessage.error` text. */
  messageError?: string;
}

/** True for a final run-level failure surfaced on the assistant message. */
export function isFinalMessageError(opts: AgentStepViewOptions): boolean {
  if (opts.streaming) return false;
  const finish = (opts.finish ?? "").trim().toLowerCase();
  if (finish === "error" || finish === "aborted") return true;
  return (opts.messageError ?? "").trim() !== "";
}

/**
 * Build one view model per `agent` part, in chronological order.
 *
 * Reasoning parts stay separate chronological rows (they are not consumed
 * here); only their step state flows into `live` via the caller. `live`
 * marks the newest non-completed step while the turn streams. On a final
 * run-level failure (`finish='error'|'aborted'` or a message error once the
 * turn is no longer streaming) the last agent step reads as `error`
 * regardless of its marker status (even when its `step-finish` marker is
 * still missing); earlier completed steps stay completed.
 */
export function buildAgentStepViews(
  parts: HarnessPart[],
  opts: AgentStepViewOptions = {},
): AgentStepView[] {
  const agents = parts.filter((part) => part.type === "agent");
  if (agents.length === 0) return [];
  const facts = collectAgentStepFacts(parts);
  const finalError = isFinalMessageError(opts);
  return agents.map((part, index) => {
    const step = agentStepNumber(part);
    const markerStatus = statusFromFacts(part, step, facts);
    const { legacy } = agentPrimaryAction(part);
    const isLast = index === agents.length - 1;
    const structured = hasStructuredAgentMeta(part);
    const status: AgentStepStatus =
      finalError && isLast ? "error" : markerStatus;
    return {
      part,
      step,
      status,
      live: Boolean(opts.streaming) && isLast && status !== "completed",
      legacy: legacy || !structured,
    };
  });
}

/**
 * True while an Agent-S step sequence is still active: a step marker
 * without its finish, or a step-attributed reasoning part still running.
 * Used to suppress the generic `Thinking` gap indicator directly after a
 * completed plan card (the step is still working).
 */
export function hasRunningAgentSequence(parts: HarnessPart[]): boolean {
  const { active, finishes } = collectAgentStepFacts(parts);
  for (const step of active) {
    if (!finishes.has(step)) return true;
  }
  return false;
}
