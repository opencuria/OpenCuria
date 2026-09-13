/**
 * Reducers for harness streaming events (M7, pure functions).
 *
 * The harness pinia store (`stores/harness.ts`) delegates all
 * `harness.part_updated` / `todo_updated` / `subtask_started/finished`
 * handling to these functions so the block-model transitions are unit
 * testable without sockets or REST.
 */

import type {
  HarnessMessage,
  HarnessPart,
  HarnessPartDelta,
  HarnessPartState,
  HarnessPartType,
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodo,
  HarnessTodoUpdatedEvent,
} from '@/types/harness'
import { deltaAttachments } from './harnessAttachments'

let partCounter = 0

/** Reset the local part-id counter (tests only). */
export function resetHarnessPartCounter(): void {
  partCounter = 0
}

function nextLocalPartId(sessionId: string): string {
  partCounter += 1
  return `local-${sessionId}-${partCounter}`
}

/** Ensure the session has a running assistant message; create one if missing. */
export function ensureAssistantMessage(
  messages: HarnessMessage[],
  sessionId: string,
): HarnessMessage {
  const last = messages[messages.length - 1]
  if (last?.role === 'assistant' && last.completed_at == null) {
    return last
  }
  const created: HarnessMessage = {
    id: `local-msg-${sessionId}-${messages.length}`,
    session_id: sessionId,
    role: 'assistant',
    content: '',
    parts: [],
    created_at: new Date().toISOString(),
  }
  messages.push(created)
  return created
}

/** Find a part by part_id, call_id, or subtask id (in `meta.subtask_id`). */
export function findPart(
  message: HarnessMessage,
  opts: { partId?: string; callId?: string; subtaskId?: string },
): HarnessPart | undefined {
  if (opts.partId) {
    const byId = message.parts.find((p) => p.id === opts.partId)
    if (byId) return byId
  }
  if (opts.callId) {
    const byCall = message.parts.find((p) => p.call_id === opts.callId)
    if (byCall) return byCall
  }
  if (opts.subtaskId) {
    return message.parts.find(
      (p) => p.type === 'subtask' && (p.meta?.['subtask_id'] as string) === opts.subtaskId,
    )
  }
  return undefined
}

function ensureTextPart(message: HarnessMessage, sessionId: string): HarnessPart {
  let part = message.parts.find((p) => p.type === 'text' && p.state === 'running')
  if (!part) {
    part = {
      id: nextLocalPartId(sessionId),
      message_id: message.id,
      session_id: sessionId,
      type: 'text',
      state: 'running',
      title: '',
      output: '',
    }
    message.parts.push(part)
  }
  return part
}

function ensureReasoningPart(
  message: HarnessMessage,
  sessionId: string,
  opts: { step?: number } = {},
): HarnessPart {
  let part = message.parts.find(
    (p) => p.type === 'reasoning' && p.state === 'running',
  )
  if (!part) {
    part = {
      id: nextLocalPartId(sessionId),
      message_id: message.id,
      session_id: sessionId,
      type: 'reasoning',
      state: 'running',
      title: '',
      output: '',
      ...(opts.step !== undefined ? { meta: { step: opts.step } } : {}),
    }
    message.parts.push(part)
  }
  return part
}

/** Read a finite step number from a part id or payload step. */
function toStepNumber(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return value
}

/** Parts that identify an Agent-S step and must survive busy reconciliation. */
const STEP_IDENTITY_TYPES: ReadonlySet<HarnessPartType> = new Set([
  'agent',
  'reasoning',
  'step-start',
  'step-finish',
])

/** Step number carried by a part (`meta.step` or `delta.step_*` markers). */
function partStepNumber(part: HarnessPart): number | undefined {
  const metaStep = toStepNumber(part.meta?.['step'])
  if (metaStep !== undefined) return metaStep
  if (part.type === 'step-start' || part.type === 'step-finish') {
    const titleStep = /step\s+(\d+)/i.exec(part.title ?? '')
    if (titleStep) return Number(titleStep[1])
  }
  return undefined
}

/** Normalize a defensive agent_meta map (unknown keys are dropped). */
function sanitizeAgentMeta(value: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  if (!value || typeof value !== 'object') return out
  const source = value as Record<string, unknown>
  for (const key of ['verification', 'analysis', 'next_action', 'action', 'action_kind']) {
    const entry = source[key]
    if (typeof entry === 'string' && entry) out[key] = entry
  }
  return out
}

/** Cost/tokens meta keys forwarded on step-finish parts. */
const STEP_FINISH_META_KEYS = ['cost', 'tokens', 'step'] as const

/**
 * Apply a `harness.part_updated` delta to the running assistant message.
 *
 * Text/reasoning deltas append to the running part; tool_started creates a
 * running tool part; tool_completed/tool_error transition the matching part
 * to completed/error; step_start/step_finish create step marker parts;
 * agent deltas create (or idempotently update) the Agent-S plan part.
 */
export function applyPartDelta(
  messages: HarnessMessage[],
  sessionId: string,
  delta: HarnessPartDelta,
  opts: { step?: number; partId?: string } = {},
): HarnessMessage {
  const message = ensureAssistantMessage(messages, sessionId)

  if (delta.text) {
    const part = ensureTextPart(message, sessionId)
    part.output += delta.text
    message.content += delta.text
  }

  if (delta.reasoning) {
    const part = ensureReasoningPart(message, sessionId, { step: opts.step })
    part.output += delta.reasoning
    if (opts.step !== undefined) {
      part.meta = { ...part.meta, step: opts.step }
    }
  }

  if (delta.tool_started) {
    const part: HarnessPart = {
      id: opts.partId ?? nextLocalPartId(sessionId),
      message_id: message.id,
      session_id: sessionId,
      type: 'tool',
      state: 'running',
      call_id: delta.call_id ?? opts.partId,
      tool: delta.tool_started,
      title: delta.title ?? delta.tool_started,
      input: {
        tool: delta.tool_started,
        arguments: delta.arguments ?? '',
      },
      output: '',
      meta: opts.step !== undefined ? { step: opts.step } : {},
    }
    message.parts.push(part)
  }

  if (delta.tool_completed) {
    const liveAttachments = deltaAttachments(delta)
    const part = findPart(message, {
      partId: opts.partId,
      callId: delta.call_id,
    })
    if (part) {
      part.state = 'completed'
      if (delta.output) part.output = delta.output
      if (liveAttachments.length > 0) {
        part.meta = { ...part.meta, attachments: liveAttachments }
      }
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'tool',
        state: 'completed',
        call_id: delta.call_id,
        tool: delta.tool_completed,
        title: delta.title ?? delta.tool_completed,
        output: delta.output ?? '',
        meta: {
          ...(opts.step !== undefined ? { step: opts.step } : {}),
          ...(liveAttachments.length > 0 ? { attachments: liveAttachments } : {}),
        },
      })
    }
  }

  if (delta.tool_error) {
    const part = findPart(message, {
      partId: opts.partId,
      callId: delta.call_id,
    })
    if (part) {
      part.state = 'error'
      part.output = delta.tool_error
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'tool',
        state: 'error',
        call_id: delta.call_id,
        title: delta.title ?? 'Tool failed',
        output: delta.tool_error,
        meta: opts.step !== undefined ? { step: opts.step } : {},
      })
    }
  }

  if (delta.step_start !== undefined) {
    const existing = opts.partId ? findPart(message, { partId: opts.partId }) : undefined
    if (existing) {
      existing.type = 'step-start'
      existing.state = 'running'
      existing.title = `Step ${delta.step_start}`
      existing.meta = { ...existing.meta, step: delta.step_start }
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'step-start',
        state: 'running',
        title: `Step ${delta.step_start}`,
        output: '',
        meta: { step: delta.step_start },
      })
    }
  }

  if (delta.step_finish !== undefined) {
    const meta: Record<string, unknown> = { step: delta.step_finish }
    for (const key of STEP_FINISH_META_KEYS) {
      const value = (delta as Record<string, unknown>)[key]
      if (value !== undefined) meta[key] = value
    }
    message.parts.push({
      id: opts.partId ?? nextLocalPartId(sessionId),
      message_id: message.id,
      session_id: sessionId,
      type: 'step-finish',
      state: 'completed',
      title: `Step ${delta.step_finish} finished`,
      output: '',
      meta,
    })
    // A finished step closes the running text/reasoning parts.
    for (const part of message.parts) {
      if (
        (part.type === 'text' || part.type === 'reasoning') &&
        part.state === 'running'
      ) {
        part.state = 'completed'
      }
    }
  }

  if (delta.agent !== undefined) {
    // Agent-S plan event: create (or idempotently update) the completed
    // plan part. Never touches the assistant `content` (plans are cards,
    // not the final answer). The idle REST fetch reconciles afterwards;
    // no per-event refetch is scheduled here.
    const step = toStepNumber(opts.step)
    const agentMeta = sanitizeAgentMeta(delta.agent_meta)
    const existing = opts.partId ? findPart(message, { partId: opts.partId }) : undefined
    if (existing) {
      existing.type = 'agent'
      existing.state = 'completed'
      existing.title = 'Agent plan'
      existing.output = delta.agent
      existing.meta = {
        ...existing.meta,
        ...(step !== undefined ? { step } : {}),
        ...(Object.keys(agentMeta).length > 0 ? { agent_meta: agentMeta } : {}),
      }
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'agent',
        state: 'completed',
        title: 'Agent plan',
        output: delta.agent,
        meta: {
          ...(step !== undefined ? { step } : {}),
          ...(Object.keys(agentMeta).length > 0 ? { agent_meta: agentMeta } : {}),
        },
      })
    }
  }

  return message
}

/** Transition a tool part to completed/error (direct state updates). */
export function applyToolState(
  message: HarnessMessage,
  callId: string,
  state: HarnessPartState,
  output?: string,
): HarnessPart | undefined {
  const part = findPart(message, { callId })
  if (!part) return undefined
  part.state = state
  if (output !== undefined) part.output = output
  return part
}

/** Replace the todo list for a session (`harness.todo_updated`). */
export function applyTodoUpdate(
  current: HarnessTodo[],
  event: Pick<HarnessTodoUpdatedEvent, 'todos'>,
): HarnessTodo[] {
  return [...event.todos]
}

/** Create a running subtask part (`harness.subtask_started`). */
export function applySubtaskStarted(
  message: HarnessMessage,
  sessionId: string,
  event: HarnessSubtaskStartedEvent,
): HarnessPart {
  const existing = findPart(message, { subtaskId: event.subtask_id })
  if (existing) {
    existing.state = 'running'
    return existing
  }
  const part: HarnessPart = {
    id: event.part_id ?? nextLocalPartId(sessionId),
    message_id: message.id,
    session_id: sessionId,
    type: 'subtask',
    state: 'running',
    title: event.description || `Subagent ${event.agent}`,
    output: '',
    meta: {
      subtask_id: event.subtask_id,
      agent: event.agent,
      ...(event.child_session_id ? { child_session_id: event.child_session_id } : {}),
      ...(event.model ? { model: event.model } : {}),
      ...(event.reasoning_effort ? { reasoning_effort: event.reasoning_effort } : {}),
    },
  }
  message.parts.push(part)
  return part
}

/** Transition a subtask part to completed/error (`harness.subtask_finished`). */
export function applySubtaskFinished(
  message: HarnessMessage,
  event: HarnessSubtaskFinishedEvent,
): HarnessPart | undefined {
  const part = findPart(message, { subtaskId: event.subtask_id })
  if (!part) return undefined
  part.state = event.status === 'completed' ? 'completed' : 'error'
  part.meta = {
    ...part.meta,
    subtask_id: event.subtask_id,
    status: event.status,
    ...(event.child_session_id ? { child_session_id: event.child_session_id } : {}),
  }
  if (event.summary) part.output = event.summary
  return part
}

function streamLength(message: HarnessMessage): number {
  return message.content.length + message.parts.reduce((n, p) => n + p.output.length, 0)
}

/**
 * Keep a locally streamed assistant turn when a mid-run `fetchParts` snapshot
 * is behind the live deltas. Idle fetches should skip this and replace fully.
 *
 * The server snapshot may be older than the live socket state, so live-only
 * Agent-S step parts (`agent` / `reasoning` / `step-start` / `step-finish`)
 * are carried over by id instead of being dropped when the total stream
 * length is equal (or the server is ahead). Text streaming keeps its
 * existing behavior: the longer running text output wins, including across
 * unstable local/server part ids.
 */
export function mergeBusyFetchedMessages(
  previous: HarnessMessage[],
  incoming: HarnessMessage[],
): HarnessMessage[] {
  const prevLast = [...previous].reverse().find((m) => m.role === 'assistant')
  const nextLast = [...incoming].reverse().find((m) => m.role === 'assistant')
  if (!prevLast || !nextLast || prevLast.completed_at != null) {
    return incoming
  }
  const liveLonger = streamLength(prevLast) > streamLength(nextLast)

  const liveById = new Map(prevLast.parts.map((part) => [part.id, part]))
  const merged = nextLast.parts.map((serverPart) => {
    const live = liveById.get(serverPart.id)
    if (!live) return serverPart
    if (
      (serverPart.type === 'text' ||
        serverPart.type === 'reasoning' ||
        serverPart.type === 'agent') &&
      live.output.length > serverPart.output.length
    ) {
      return live
    }
    if (
      serverPart.meta?.['agent_meta'] == null &&
      live.meta?.['agent_meta'] != null
    ) {
      return { ...serverPart, meta: { ...serverPart.meta, agent_meta: live.meta['agent_meta'] } }
    }
    return serverPart
  })
  const mergedIds = new Set(merged.map((part) => part.id))

  for (const live of prevLast.parts) {
    if (mergedIds.has(live.id)) continue
    if (live.type === 'text') {
      // Local and server text ids differ; fold the longer running output
      // into the server text slot instead of duplicating the row.
      const slot = merged.find((part) => part.type === 'text' && part.state === 'running')
      if (slot && live.state === 'running' && live.output.length > slot.output.length) {
        slot.output = live.output
      } else if (!slot && liveLonger && live.output) {
        merged.push(live)
        mergedIds.add(live.id)
      }
      continue
    }
    if (STEP_IDENTITY_TYPES.has(live.type)) {
      // Never drop a live step part just because an older server snapshot
      // does not know it yet; skip it only when the server already holds
      // the same step content under a different id (e.g. reconciled row).
      // Reasoning uses a prefix fold (local vs server ids diverge): same
      // step + prefix-related outputs are one stream, the longer wins.
      if (live.type === 'reasoning' && foldLiveReasoningIntoServerSlot(merged, live)) {
        continue
      }
      if (isSameStepContent(merged, live)) continue
      merged.push(live)
      mergedIds.add(live.id)
      continue
    }
    if (liveLonger) {
      merged.push(live)
      mergedIds.add(live.id)
    }
  }

  nextLast.parts = merged
  if (liveLonger) {
    nextLast.content = prevLast.content
  }
  return incoming
}

/**
 * Fold a live reasoning part into the matching server reasoning slot.
 *
 * Live reasoning carries a local id while the server row uses a UUID; when
 * both share the same step and one output is a prefix of the other they are
 * the same stream observed at different times (not two rows). The longer /
 * more advanced output wins on the server id; state/meta merge sensibly
 * (exact matches dedupe). Same-step but genuinely different (non-prefix)
 * content is kept as its own row (returns false).
 */
function foldLiveReasoningIntoServerSlot(
  merged: HarnessPart[],
  live: HarnessPart,
): boolean {
  const step = partStepNumber(live)
  const liveOutput = live.output ?? ''
  const slot = merged.find((part) => {
    if (part.type !== 'reasoning') return false
    if (partStepNumber(part) !== step) return false
    const serverOutput = part.output ?? ''
    return (
      serverOutput === liveOutput ||
      serverOutput.startsWith(liveOutput) ||
      liveOutput.startsWith(serverOutput)
    )
  })
  if (!slot) return false
  const serverOutput = slot.output ?? ''
  if (liveOutput.length > serverOutput.length) {
    slot.output = liveOutput
    slot.state = live.state
    if (live.title) slot.title = live.title
  } else if (serverOutput.length === liveOutput.length) {
    // Exact match: dedupe on the server id, keep the furthest state.
    if (slot.state !== 'completed' && live.state === 'completed') {
      slot.state = 'completed'
    }
  }
  // Merge live meta (e.g. step attribution) without losing server keys.
  slot.meta = { ...slot.meta, ...live.meta }
  if (step !== undefined) slot.meta = { ...slot.meta, step }
  return true
}

/**
 * True when the merged server parts already hold this live step part under
 * a different id (same step number and same output for agent/reasoning,
 * same step number for step markers).
 */
function isSameStepContent(merged: HarnessPart[], live: HarnessPart): boolean {
  const step = partStepNumber(live)
  return merged.some((part) => {
    if (part.type !== live.type) return false
    if (part.type === 'step-start' || part.type === 'step-finish') {
      return step !== undefined && partStepNumber(part) === step
    }
    if (step !== undefined && partStepNumber(part) !== step) return false
    return part.output === live.output
  })
}

const STREAM_PART_TYPES = new Set(['text', 'reasoning'])

/**
 * Coerce leftover running/pending text and reasoning parts to completed.
 *
 * Idle fetches of older sessions can still have those parts stuck in
 * `running` because the backend used to leave them open after a turn.
 */
export function settleOpenStreamParts(messages: HarnessMessage[]): HarnessMessage[] {
  for (const message of messages) {
    for (const part of message.parts) {
      if (
        STREAM_PART_TYPES.has(part.type) &&
        (part.state === 'running' || part.state === 'pending')
      ) {
        part.state = 'completed'
      }
    }
  }
  return messages
}
