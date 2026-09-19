/**
 * Agent-S step/timeline helpers for the computer-use chat UI.
 *
 * The backend persists Agent-S plans as `agent` parts (full plan in
 * `output`, safe summaries in `meta.agent_meta`) with `meta.step`, plus
 * hidden `step-start`/`step-finish` markers and step-attributed `reasoning`
 * parts (`meta.step`). These pure helpers derive a compact per-step view
 * model without inventing facts missing from older/partial payloads.
 */

import type { HarnessAgentMeta, HarnessPart } from '@/types/harness'

/** Step status derived from markers and streaming state. */
export type AgentStepStatus = 'in_progress' | 'completed' | 'error'

/** Compact view model for one Agent-S plan step. */
export interface AgentStepView {
  part: HarnessPart
  step: number | null
  status: AgentStepStatus
  /** True while this step is the latest and the turn is still running. */
  live: boolean
  /** True for legacy/partial payloads without usable structured meta. */
  legacy: boolean
}

const AGENT_META_KEYS: ReadonlyArray<keyof HarnessAgentMeta> = [
  'verification',
  'analysis',
  'next_action',
  'action',
  'action_kind',
]

function textOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** Defensive read of `part.meta.agent_meta` (nested object, strings only). */
export function readAgentMeta(part: HarnessPart): HarnessAgentMeta {
  const raw = part.meta?.['agent_meta']
  const empty: HarnessAgentMeta = {
    verification: '',
    analysis: '',
    next_action: '',
    action: '',
    action_kind: '',
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return empty
  const source = raw as Record<string, unknown>
  return {
    verification: textOf(source['verification']),
    analysis: textOf(source['analysis']),
    next_action: textOf(source['next_action']),
    action: textOf(source['action']),
    action_kind: textOf(source['action_kind']),
  }
}

/** Step number from `meta.step` (finite numbers only). */
export function agentStepNumber(part: HarnessPart): number | null {
  const step = part.meta?.['step']
  if (typeof step === 'number' && Number.isFinite(step)) return step
  return null
}

/** True when at least one structured meta field carries content. */
export function hasStructuredAgentMeta(part: HarnessPart): boolean {
  const meta = readAgentMeta(part)
  return AGENT_META_KEYS.some((key) => meta[key] !== '')
}

/** True for error tool parts that belong to an Agent-S step. */
function isStepErrorTool(part: HarnessPart, step: number | null): boolean {
  if (part.type !== 'tool' || part.state !== 'error') return false
  if (step === null) return false
  const toolStep = part.meta?.['step']
  return typeof toolStep === 'number' && toolStep === step
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
  if (step !== null) {
    if (parts.some((candidate) => isStepErrorTool(candidate, step))) return 'error'
    const hasFinish = parts.some(
      (candidate) => candidate.type === 'step-finish' && candidate.meta?.['step'] === step,
    )
    const hasStart = parts.some(
      (candidate) => candidate.type === 'step-start' && candidate.meta?.['step'] === step,
    )
    const agentSteps = parts
      .filter((candidate) => candidate.type === 'agent')
      .map((candidate) => agentStepNumber(candidate))
      .filter((value): value is number => value !== null)
    const maxStep = agentSteps.length > 0 ? Math.max(...agentSteps) : null
    // A newer plan for a higher step supersedes this one: the older step is
    // done even when its explicit finish marker has not arrived yet. When no
    // explicit markers exist at all (legacy payloads), fall back to the
    // persisted part state so a lone running plan still reads as active.
    if (maxStep !== null && maxStep > step && (hasStart || hasFinish)) return 'completed'
    if (hasFinish) return 'completed'
    if (hasStart) return 'in_progress'
    if (part.state === 'running' || part.state === 'pending') return 'in_progress'
    return 'completed'
  }
  if (part.state === 'error') return 'error'
  if (part.state === 'running' || part.state === 'pending') return 'in_progress'
  return 'completed'
}

function formatStepKind(actionKind: string, fallback: string): string {
  const kind = actionKind.trim().toLowerCase()
  if (!kind) return fallback
  if (kind === 'done') return 'Finish task'
  if (kind === 'fail') return 'Report failure'
  const pretty = kind.replace(/_/g, ' ').trim()
  if (!pretty) return fallback
  return pretty.charAt(0).toUpperCase() + pretty.slice(1)
}

/**
 * Primary one-line action for a step header (e.g. `Click "Submit"`).
 *
 * Prefers the safe structured `action` summary; falls back to the first
 * non-empty line of `next_action`, then to a defensive single-line slice
 * of the raw plan (marked as legacy), without inventing details.
 */
export function agentPrimaryAction(part: HarnessPart): { text: string; legacy: boolean } {
  const meta = readAgentMeta(part)
  if (meta.action) {
    const kind = formatStepKind(meta.action_kind, meta.action)
    const trimmed = meta.action.trim()
    const kindKey = meta.action_kind.trim().toLowerCase()
    if (kindKey && trimmed.toLowerCase() === kindKey) {
      return { text: kind, legacy: false }
    }
    if (kindKey && trimmed.toLowerCase().startsWith(kindKey)) {
      // Capitalize the leading verb ("click ..." -> "Click ...") while
      // keeping the backend-provided remainder verbatim.
      const rest = trimmed.slice(kindKey.length)
      if (rest === '' || /^\s/.test(rest)) {
        return { text: `${kind}${rest}`, legacy: false }
      }
    }
    return { text: trimmed, legacy: false }
  }
  if (meta.next_action) {
    const firstLine = meta.next_action
      .split('\n')
      .map((line) => line.trim())
      .find(Boolean)
    if (firstLine) return { text: firstLine, legacy: false }
  }
  const rawLines = (part.output || '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('```'))
  if (rawLines.length > 0) {
    const first = rawLines[0] as string
    const sliced = first.length > 120 ? `${first.slice(0, 117)}…` : first
    return { text: sliced, legacy: true }
  }
  return { text: 'Agent plan', legacy: true }
}

/**
 * View-model options: streaming state plus the final assistant-message
 * outcome. `finish`/`error` come straight from `HarnessMessage`
 * (`HarnessService` sets `finish='error'|'aborted'|'interrupted'` with
 * `error=...` on a failed run, after a best-effort `step_finish` already
 * closed the markers). While streaming the message is not final yet, so
 * no stale error may leak into the timeline: callers pass the message
 * finish/error only when the run is no longer streaming.
 */
export interface AgentStepViewOptions {
  streaming?: boolean
  /** Final `HarnessMessage.finish` (e.g. `'error'` / `'aborted'` / `'interrupted'`). */
  finish?: string
  /** Final `HarnessMessage.error` text. */
  messageError?: string
}

/** True for a final run-level failure surfaced on the assistant message. */
export function isFinalMessageError(opts: AgentStepViewOptions): boolean {
  if (opts.streaming) return false
  const finish = (opts.finish ?? '').trim().toLowerCase()
  if (finish === 'error' || finish === 'aborted' || finish === 'interrupted') return true
  return (opts.messageError ?? '').trim() !== ''
}

/**
 * Build one view model per `agent` part, in chronological order.
 *
 * Reasoning parts stay separate chronological rows (they are not consumed
 * here); only their step state flows into `live` via the caller. `live`
 * marks the newest non-completed step while the turn streams. On a final
 * run-level failure (`finish='error'|'aborted'|'interrupted'` or a message error once the
 * turn is no longer streaming) the last agent step reads as `error`
 * regardless of its marker status (even when its `step-finish` marker is
 * still missing); earlier completed steps stay completed.
 */
export function buildAgentStepViews(
  parts: HarnessPart[],
  opts: AgentStepViewOptions = {},
): AgentStepView[] {
  const agents = parts.filter((part) => part.type === 'agent')
  const finalError = isFinalMessageError(opts)
  return agents.map((part, index) => {
    const step = agentStepNumber(part)
    const markerStatus = agentStepStatus(part, parts, step)
    const { legacy } = agentPrimaryAction(part)
    const isLast = index === agents.length - 1
    const structured = hasStructuredAgentMeta(part)
    const status: AgentStepStatus = finalError && isLast ? 'error' : markerStatus
    return {
      part,
      step,
      status,
      live: Boolean(opts.streaming) && isLast && status !== 'completed',
      legacy: legacy || !structured,
    }
  })
}

/**
 * True while an Agent-S step sequence is still active: a step marker
 * without its finish, or a step-attributed reasoning part still running.
 * Used to suppress the generic `Thinking` gap indicator directly after a
 * completed plan card (the step is still working).
 */
export function hasRunningAgentSequence(parts: HarnessPart[]): boolean {
  const steps = new Set<number>()
  for (const part of parts) {
    const step = part.meta?.['step']
    if (typeof step !== 'number' || !Number.isFinite(step)) continue
    if (part.type === 'step-start' || part.type === 'agent' || part.type === 'reasoning') {
      steps.add(step)
    }
  }
  for (const step of steps) {
    const finished = parts.some(
      (part) => part.type === 'step-finish' && part.meta?.['step'] === step,
    )
    if (finished) continue
    const started = parts.some(
      (part) =>
        (part.type === 'step-start' || part.type === 'agent') && part.meta?.['step'] === step,
    )
    const liveReasoning = parts.some(
      (part) =>
        part.type === 'reasoning' && part.state === 'running' && part.meta?.['step'] === step,
    )
    if (started || liveReasoning) return true
  }
  return false
}
