/**
 * Reducers for harness streaming events (M7, pure functions).
 *
 * The harness pinia store (`stores/harness.ts`) delegates all
 * `harness.part_updated` / `todo_updated` / `subtask_started/finished`
 * handling to these functions so the block-model transitions are unit
 * testable without sockets or REST.
 *
 * Backend contract (coordinated, see `harness_service.py`):
 * - every `harness.part_updated` / `subtask_started/finished` payload
 *   carries `message_id` (the current assistant shell) so live deltas
 *   route to the right turn;
 * - text/reasoning deltas additionally carry the persisted `part_id` of
 *   the running stream part;
 * - queued tools emit an initial `delta.tool_started` with `pending`
 *   state (or `queued: true`) followed by the actual `delta.tool_started`
 *   with `running` state for the *same* `part_id`;
 * - `harness.session_status` busy/idle carries `message_id` of the
 *   current assistant (plus `user_message_id` for follow-up correlation);
 * - REST snapshots carry per-session `position` on messages and
 *   per-message `position` on parts.
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

/** True for optimistic local ids (`local-*`) with no backend row. */
export function isLocalId(id: string | undefined): boolean {
  return typeof id === 'string' && id.startsWith('local-')
}

/**
 * Ensure the session has a running assistant message; create one if missing.
 *
 * Legacy path for events without a backend `message_id`. Prefer
 * {@link routeAssistantMessage} when the event carries `message_id` so a
 * fresh turn never attaches to the previous answer.
 */
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

/**
 * Deterministic per-turn routing keyed by the backend `message_id`.
 *
 * - When `messageId` matches an existing message, that turn owns the
 *   event — even when it is not the latest (late duplicates route to the
 *   correct old turn instead of the newest).
 * - When `messageId` is new, a fresh assistant shell is created with the
 *   *server* id (never reusing the previous answer), so the streaming
 *   cursor and Thinking indicator move to the fresh turn.
 * - When `messageId` is absent (legacy backend/tests), falls back to
 *   {@link ensureAssistantMessage}.
 */
export function routeAssistantMessage(
  messages: HarnessMessage[],
  sessionId: string,
  messageId?: string,
): HarnessMessage {
  if (messageId) {
    const existing = messages.find((m) => m.id === messageId)
    if (existing) {
      if (existing.role === 'assistant') return existing
      // A user row colliding with the assistant id should not happen;
      // create the assistant right after it instead of hijacking it.
      const created: HarnessMessage = {
        id: messageId,
        session_id: sessionId,
        role: 'assistant',
        content: '',
        parts: [],
        created_at: new Date().toISOString(),
      }
      const index = messages.indexOf(existing)
      messages.splice(index + 1, 0, created)
      return created
    }
    const created: HarnessMessage = {
      id: messageId,
      session_id: sessionId,
      role: 'assistant',
      content: '',
      parts: [],
      created_at: new Date().toISOString(),
    }
    messages.push(created)
    return created
  }
  return ensureAssistantMessage(messages, sessionId)
}

/**
 * Ensure the busy-anchored assistant shell exists for a
 * `harness.session_status: busy` event.
 *
 * Creates the server-id shell positioned after the anchoring user message
 * (optimistic follow-up) or at the end when the anchor is unknown, so the
 * fresh empty turn exists before the first delta arrives (Thinking shows
 * on the fresh turn, never a cursor on the previous answer).
 */
export function ensureBusyAssistant(
  messages: HarnessMessage[],
  sessionId: string,
  messageId: string,
  userMessageId?: string,
): HarnessMessage {
  const existing = messages.find((m) => m.id === messageId)
  if (existing && existing.role === 'assistant') return existing
  const created: HarnessMessage = {
    id: messageId,
    session_id: sessionId,
    role: 'assistant',
    content: '',
    parts: [],
    created_at: new Date().toISOString(),
  }
  if (userMessageId) {
    const anchor = messages.findIndex((m) => m.id === userMessageId)
    if (anchor !== -1) {
      messages.splice(anchor + 1, 0, created)
      return created
    }
    // Optimistic follow-up user still local: anchor after the last user.
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i]?.role === 'user') {
        messages.splice(i + 1, 0, created)
        return created
      }
    }
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

/** Find a tool part by call id (placeholder-aware). */
function findToolPart(message: HarnessMessage, callId?: string): HarnessPart | undefined {
  if (!callId) return undefined
  return message.parts.find((p) => p.type === 'tool' && p.call_id === callId)
}

function ensureTextPart(message: HarnessMessage, sessionId: string, partId?: string): HarnessPart {
  if (partId) {
    const byId = message.parts.find((p) => p.id === partId)
    if (byId && byId.type === 'text') return byId
  }
  const part = message.parts.find((p) => p.type === 'text' && p.state === 'running')
  if (part) {
    // Adopt the server part_id for a local placeholder (same stream).
    if (partId && isLocalId(part.id)) part.id = partId
    return part
  }
  const created: HarnessPart = {
    id: partId ?? nextLocalPartId(sessionId),
    message_id: message.id,
    session_id: sessionId,
    type: 'text',
    state: 'running',
    title: '',
    output: '',
  }
  message.parts.push(created)
  return created
}

function ensureReasoningPart(
  message: HarnessMessage,
  sessionId: string,
  opts: { step?: number; partId?: string } = {},
): HarnessPart {
  if (opts.partId) {
    const byId = message.parts.find((p) => p.id === opts.partId)
    if (byId && byId.type === 'reasoning') {
      if (opts.step !== undefined) byId.meta = { ...byId.meta, step: opts.step }
      return byId
    }
  }
  const part = message.parts.find((p) => p.type === 'reasoning' && p.state === 'running')
  if (part) {
    if (opts.partId && isLocalId(part.id)) part.id = opts.partId
    if (opts.step !== undefined) part.meta = { ...part.meta, step: opts.step }
    return part
  }
  const created: HarnessPart = {
    id: opts.partId ?? nextLocalPartId(sessionId),
    message_id: message.id,
    session_id: sessionId,
    type: 'reasoning',
    state: 'running',
    title: '',
    output: '',
    ...(opts.step !== undefined ? { meta: { step: opts.step } } : {}),
  }
  message.parts.push(created)
  return created
}

/** Complete running text/reasoning streams so nothing merges across a boundary. */
function closeRunningStreams(message: HarnessMessage, except?: 'text' | 'reasoning'): void {
  for (const part of message.parts) {
    if (part.state !== 'running' && part.state !== 'pending') continue
    if (part.type === 'text' && except !== 'text') part.state = 'completed'
    if (part.type === 'reasoning' && except !== 'reasoning') part.state = 'completed'
  }
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
  const metaStep = toStepNumber(part.meta?.['step'] ?? part.display?.step)
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
    if (typeof entry === 'string' && entry) out[key] = entry.slice(0, 240)
  }
  return out
}

/** Cost/tokens meta keys forwarded on step-finish parts. */
const STEP_FINISH_META_KEYS = ['cost', 'tokens', 'step'] as const

/**
 * Resolve the lifecycle state for an incoming `tool_started` delta.
 *
 * Prefers an explicit `delta.state` (`pending`/`running`) when the backend
 * sends it; an explicit `queued: true` flag also forces pending. Otherwise
 * legacy single-emit callers (one `tool_started` per execution, no queued
 * pre-emit) default to `running` so old tests/payloads keep working. The
 * two-emit queued flow is detected by the caller: an initial `tool_started`
 * for an unknown part creates the queued (`pending`) row (see
 * `applyPartDelta`), and the repeat for the same `part_id` flips it to
 * `running` via `resolveQueuedRepeatState`.
 */
export function resolveQueuedToolState(
  delta: HarnessPartDelta,
  _opts: { known: boolean },
): HarnessPartState {
  const raw = typeof delta.state === 'string' ? delta.state.trim().toLowerCase() : ''
  if (raw === 'pending' || raw === 'queued') return 'pending'
  if (raw === 'running') return 'running'
  if (raw === 'completed' || raw === 'error') return raw
  if (delta.queued === true) return 'pending'
  if (delta.queued === false) return 'running'
  return 'running'
}

/** True when an initial `tool_started` is only the queued pre-emit. */
function isQueuedToolStarted(delta: HarnessPartDelta): boolean {
  const raw = typeof delta.state === 'string' ? delta.state.trim().toLowerCase() : ''
  if (raw === 'pending' || raw === 'queued') return true
  if (raw === 'running' || raw === 'completed' || raw === 'error') return false
  return delta.queued === true
}

/** State for a repeated `tool_started` on an already-known part. */
function resolveQueuedRepeatState(
  delta: HarnessPartDelta,
  current: HarnessPartState,
): HarnessPartState {
  const raw = typeof delta.state === 'string' ? delta.state.trim().toLowerCase() : ''
  if (raw === 'pending' || raw === 'queued') return 'pending'
  if (raw === 'running') return 'running'
  if (raw === 'completed' || raw === 'error') return raw
  if (delta.queued === true) return current === 'running' ? 'running' : 'pending'
  // Repeat of the same part_id without an explicit state is the actual
  // start following the queued pre-emit: flip pending → running.
  if (current === 'pending') return 'running'
  return current
}

/**
 * Apply a `harness.part_updated` delta to its per-turn assistant message.
 *
 * Routing is deterministic on `opts.messageId` (backend `message_id`):
 * a fresh id creates a new server-id shell instead of appending to the
 * previous answer, and late events for an older id route to that old turn
 * (completed turns ignore further deltas so stale events can never attach
 * to the latest turn). Text/reasoning deltas append to the running part
 * (adopting the server `part_id` for local placeholders); the first
 * `tool_started` for an unknown part creates the queued (`pending`) row
 * and the repeat for the same `part_id` flips it to `running` without
 * duplicating; `tool_completed`/`tool_error` transition the matching part;
 * `step_start`/`step_finish` create step marker parts; `agent` deltas
 * create (or idempotently update) the Agent-S plan part.
 */
export function applyPartDelta(
  messages: HarnessMessage[],
  sessionId: string,
  delta: HarnessPartDelta,
  opts: { step?: number; partId?: string; partPosition?: number; messageId?: string } = {},
): HarnessMessage {
  const message = routeAssistantMessage(messages, sessionId, opts.messageId)
  // Completed turns are frozen: late/stale duplicates for an older
  // message_id must not resurrect them nor leak into the latest turn.
  if (message.completed_at != null) return message

  if (delta.text) {
    closeRunningStreams(message, 'text')
    const part = ensureTextPart(message, sessionId, opts.partId)
    part.state = 'running'
    part.output += delta.text
    message.content += delta.text
  }

  if (delta.reasoning) {
    closeRunningStreams(message, 'reasoning')
    const part = ensureReasoningPart(message, sessionId, { step: opts.step, partId: opts.partId })
    part.state = 'running'
    part.output += delta.reasoning
    if (opts.step !== undefined) {
      part.meta = { ...part.meta, step: opts.step }
    }
  }

  if (delta.tool_started) {
    closeRunningStreams(message)
    const existing =
      (opts.partId ? findPart(message, { partId: opts.partId }) : undefined) ??
      findToolPart(message, delta.call_id)
    if (existing && existing.type === 'tool') {
      // Queued → running for the same part_id: adopt the server id when
      // the first sighting used a local placeholder, then flip state
      // without duplicating the row.
      if (opts.partId && isLocalId(existing.id)) existing.id = opts.partId
      existing.tool = delta.tool_started || existing.tool
      existing.title = delta.title ?? existing.title ?? delta.tool_started
      existing.call_id = delta.call_id ?? existing.call_id
      // Live socket start/queued payloads intentionally omit arguments.
      // Preserve legacy arguments when a legacy event supplied them, but
      // leave server-backed modern parts explicitly detail-incomplete.
      existing.input = {
        ...existing.input,
        tool: delta.tool_started,
        ...(delta.arguments !== undefined ? { arguments: delta.arguments } : {}),
      }
      if (opts.step !== undefined) existing.meta = { ...existing.meta, step: opts.step }
      existing.state = resolveQueuedRepeatState(delta, existing.state)
      existing.detail_loaded = false
    } else {
      // No known row for this tool: distinguish the initial queued
      // pre-emit (explicit pending/queued signal) from a legacy/actual
      // single start (defaults to running).
      const queued = isQueuedToolStarted(delta)
      const part: HarnessPart = {
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'tool',
        state: queued ? 'pending' : 'running',
        call_id: delta.call_id ?? opts.partId,
        tool: delta.tool_started,
        title: delta.title ?? delta.tool_started,
        input: {
          tool: delta.tool_started,
          ...(delta.arguments !== undefined ? { arguments: delta.arguments } : {}),
        },
        output: '',
        meta: opts.step !== undefined ? { step: opts.step } : {},
        detail_loaded: false,
      }
      message.parts.push(part)
    }
  }

  if (delta.tool_completed) {
    closeRunningStreams(message)
    const liveAttachments = deltaAttachments(delta)
    const part = findPart(message, {
      partId: opts.partId,
      callId: delta.call_id,
    })
    if (part) {
      if (opts.partId && isLocalId(part.id)) part.id = opts.partId
      part.state = 'completed'
      part.tool = delta.tool_completed || part.tool
      part.title = delta.title ?? part.title
      part.call_id = delta.call_id ?? part.call_id
      if (delta.output !== undefined) part.output = delta.output
      if (delta.display) part.display = delta.display
      if (liveAttachments.length > 0) {
        part.meta = { ...part.meta, attachments: liveAttachments }
      }
      if (delta.display) {
        part.detail_loaded = delta.output !== undefined || delta.attachments !== undefined
      } else if (delta.output !== undefined || liveAttachments.length > 0) {
        // Preserve compatibility with legacy deltas carrying full fields.
        part.detail_loaded = true
      }
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'tool',
        state: 'completed',
        call_id: delta.call_id,
        tool: delta.tool_completed || delta.display?.tool,
        title: delta.title ?? delta.display?.summary ?? delta.tool_completed,
        input: delta.display?.tool ? { tool: delta.display.tool } : {},
        output: delta.output ?? '',
        ...(delta.display ? { display: delta.display } : {}),
        meta: {
          ...(opts.step !== undefined ? { step: opts.step } : {}),
          ...(liveAttachments.length > 0 ? { attachments: liveAttachments } : {}),
        },
        detail_loaded:
          delta.display && delta.output === undefined && delta.attachments === undefined
            ? false
            : true,
      })
    }
  }

  if (delta.tool_error) {
    closeRunningStreams(message)
    const part = findPart(message, {
      partId: opts.partId,
      callId: delta.call_id,
    })
    if (part) {
      if (opts.partId && isLocalId(part.id)) part.id = opts.partId
      part.state = 'error'
      part.call_id = delta.call_id ?? part.call_id
      part.tool = delta.display?.tool ?? part.tool
      part.title = delta.title ?? part.title
      part.output = delta.tool_error
      if (delta.display) {
        part.display = delta.display
        part.detail_loaded = false
      }
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'tool',
        state: 'error',
        call_id: delta.call_id,
        tool: delta.display?.tool,
        title: delta.title ?? delta.display?.summary ?? 'Tool failed',
        input: delta.display?.tool ? { tool: delta.display.tool } : {},
        output: delta.tool_error,
        ...(delta.display ? { display: delta.display } : {}),
        meta: opts.step !== undefined ? { step: opts.step } : {},
        detail_loaded: delta.display ? false : undefined,
      })
    }
  }

  if (delta.step_start !== undefined) {
    closeRunningStreams(message)
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
    closeRunningStreams(message)
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
  }

  if (delta.agent !== undefined) {
    closeRunningStreams(message)
    // Agent-S plan event: create (or idempotently update) the completed
    // plan part. Never touches the assistant `content` (plans are cards,
    // not the final answer). The idle REST fetch reconciles afterwards;
    // no per-event refetch is scheduled here.
    const step = toStepNumber(opts.step)
    const agentMeta = sanitizeAgentMeta(delta.agent_meta)
    // The explicit `display` field is the protocol marker for a compact
    // Agent-S projection. Never infer projection status from text equality:
    // legacy full plan payloads can legitimately equal a parsed summary.
    const safeSummary = String(
      delta.display?.summary ?? agentMeta.analysis ?? agentMeta.next_action ?? 'Agent plan',
    ).slice(0, 240)
    const compactAgent = delta.display !== undefined
    const display = compactAgent
      ? {
          ...delta.display,
          summary: safeSummary,
          ...(step !== undefined ? { step } : {}),
          agent_meta: agentMeta,
        }
      : undefined
    const existing = opts.partId ? findPart(message, { partId: opts.partId }) : undefined
    if (existing) {
      existing.type = 'agent'
      existing.state = 'completed'
      existing.title = 'Agent plan'
      existing.output = delta.agent
      existing.display = display
      existing.detail_loaded = compactAgent ? false : true
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
        ...(display ? { display } : {}),
        detail_loaded: compactAgent ? false : true,
        meta: {
          ...(step !== undefined ? { step } : {}),
          ...(Object.keys(agentMeta).length > 0 ? { agent_meta: agentMeta } : {}),
        },
      })
    }
  }

  if (delta.patch !== undefined) {
    closeRunningStreams(message)
    const existing = opts.partId ? findPart(message, { partId: opts.partId }) : undefined
    if (existing) {
      existing.type = 'patch'
      existing.state = 'completed'
      if (delta.patch) existing.title = delta.patch
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'patch',
        state: 'completed',
        title: delta.patch || 'Patch',
        output: '',
        meta: opts.step !== undefined ? { step: opts.step } : {},
      })
    }
  }

  if (delta.compaction !== undefined) {
    closeRunningStreams(message)
    const existing = opts.partId ? findPart(message, { partId: opts.partId }) : undefined
    if (existing) {
      existing.type = 'compaction'
      existing.state = 'completed'
    } else {
      message.parts.push({
        id: opts.partId ?? nextLocalPartId(sessionId),
        message_id: message.id,
        session_id: sessionId,
        type: 'compaction',
        state: 'completed',
        title: 'Session compacted',
        output: '',
        meta: {},
      })
    }
  }

  // The server assigns a position when the part is created. REST fetches
  // may race later socket events; applying that position to the live row
  // keeps newly merged snapshots in the same chronological order.
  if (opts.partId && opts.partPosition !== undefined) {
    const positioned = message.parts.find((part) => part.id === opts.partId)
    if (positioned) positioned.position = opts.partPosition
  }
  message.parts = sortHarnessParts(message.parts)
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
  const byPartId = event.part_id ? findPart(message, { partId: event.part_id }) : undefined
  if (byPartId && byPartId.type === 'subtask') {
    byPartId.state = 'running'
    if (event.part_position !== undefined) byPartId.position = event.part_position
    return byPartId
  }
  const existing = findPart(message, { subtaskId: event.subtask_id })
  if (existing) {
    if (event.part_id && isLocalId(existing.id)) existing.id = event.part_id
    if (event.part_position !== undefined) existing.position = event.part_position
    existing.state = 'running'
    return existing
  }
  closeRunningStreams(message)
  const part: HarnessPart = {
    id: event.part_id ?? nextLocalPartId(sessionId),
    message_id: message.id,
    session_id: sessionId,
    position: event.part_position,
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

// --- Ordering ---------------------------------------------------------------

function messagePosition(message: HarnessMessage): number | undefined {
  const raw = message.position ?? message.message_position
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : undefined
}

function messageTime(message: HarnessMessage): number {
  if (!message.created_at) return Number.POSITIVE_INFINITY
  const parsed = Date.parse(message.created_at)
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY
}

/**
 * Sort messages by backend `position`, falling back to creation order
 * (`created_at`, then id). Local optimistic rows without a position sort
 * after positioned rows but keep creation order among themselves.
 */
export function sortHarnessMessages<T extends HarnessMessage>(messages: T[]): T[] {
  return [...messages].sort((a, b) => {
    const ap = messagePosition(a)
    const bp = messagePosition(b)
    if (ap !== undefined && bp !== undefined && ap !== bp) return ap - bp
    if (ap !== undefined && bp === undefined) return -1
    if (ap === undefined && bp !== undefined) return 1
    const at = messageTime(a)
    const bt = messageTime(b)
    if (at !== bt) return at - bt
    // A local/server live shell with no position is appended after its
    // user anchor; timestamp ties must not invert that observed order.
    return 0
  })
}

function partPosition(part: HarnessPart): number | undefined {
  const raw = part.position
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : undefined
}

/**
 * Sort parts by backend `position`, preserving observed order when
 * positions are missing or tied (stable sort, no id comparison — local
 * `local-*` ids must never interleave server UUID order).
 */
export function sortHarnessParts<T extends HarnessPart>(parts: T[]): T[] {
  return [...parts].sort((a, b) => {
    const ap = partPosition(a)
    const bp = partPosition(b)
    if (ap !== undefined && bp !== undefined && ap !== bp) return ap - bp
    if (ap !== undefined && bp === undefined) return -1
    if (ap === undefined && bp !== undefined) return 1
    return 0
  })
}

// --- Busy reconciliation -----------------------------------------------------

/**
 * Merge one live assistant turn into its server snapshot row.
 *
 * Matched by message id (same turn). Parts merge by part id; local
 * placeholder rows (`local-*`) fold into the server row for the same
 * logical stream (same call/subtask id, or prefix-related text/reasoning
 * output) so already-received data survives a stale snapshot. The merged
 * row keeps server ids/positions with the longest known output and the
 * furthest lifecycle state. Live-only rows the server does not know yet
 * are appended in observed order.
 */
function mergeAssistantParts(live: HarnessMessage, server: HarnessMessage): void {
  const liveById = new Map(live.parts.map((part) => [part.id, part]))
  const merged = server.parts.map((serverPart) => {
    const liveSame = liveById.get(serverPart.id)
    if (liveSame) {
      // Same row observed at two times: the longer output wins (stale
      // snapshot must not clobber already-streamed data), state moves
      // forward (pending → running → completed/error, never backwards).
      if (liveSame.output.length > serverPart.output.length) {
        serverPart.output = liveSame.output
      }
      serverPart.state = furthestPartState(serverPart.state, liveSame.state)
      if (liveSame.display && !serverPart.display) serverPart.display = liveSame.display
      if (liveSame.detail_loaded === false && !serverPart.detail_loaded) {
        serverPart.detail_loaded = false
        if (liveSame.input?.['tool'] && !serverPart.input?.['tool']) {
          serverPart.input = { ...serverPart.input, tool: liveSame.input['tool'] }
        }
      }
      if (serverPart.meta?.['agent_meta'] == null && liveSame.meta?.['agent_meta'] != null) {
        serverPart.meta = { ...serverPart.meta, agent_meta: liveSame.meta['agent_meta'] }
      }
      // A lightweight timeline refresh must not discard details fetched while
      // the same session was idle (or attachments received over the socket).
      if (liveSame.detail_loaded && !serverPart.detail_loaded) {
        serverPart.input = liveSame.input
        serverPart.output = liveSame.output
        serverPart.meta = { ...serverPart.meta, ...liveSame.meta }
        serverPart.detail_loaded = true
      }
      // Keep server position/id; merge any live-only meta keys.
      for (const [key, value] of Object.entries(liveSame.meta ?? {})) {
        if (serverPart.meta?.[key] === undefined && value !== undefined) {
          serverPart.meta = { ...serverPart.meta, [key]: value }
        }
      }
      return serverPart
    }
    // Placeholder (local id) for the same logical part under a server id:
    // fold the live output/state into the server row instead of duplicating.
    const placeholder = findPlaceholderForServerPart(live.parts, serverPart)
    if (placeholder) {
      if (placeholder.output.length > serverPart.output.length) {
        serverPart.output = placeholder.output
      }
      serverPart.state = furthestPartState(serverPart.state, placeholder.state)
      if (placeholder.display && !serverPart.display) serverPart.display = placeholder.display
      if (placeholder.detail_loaded === false && !serverPart.detail_loaded) {
        serverPart.detail_loaded = false
      }
      if (placeholder.detail_loaded && !serverPart.detail_loaded) {
        serverPart.input = placeholder.input
        serverPart.output = placeholder.output
        serverPart.meta = { ...serverPart.meta, ...placeholder.meta }
        serverPart.detail_loaded = true
      }
      if (serverPart.meta?.['agent_meta'] == null && placeholder.meta?.['agent_meta'] != null) {
        serverPart.meta = { ...serverPart.meta, agent_meta: placeholder.meta['agent_meta'] }
      }
      return serverPart
    }
    return serverPart
  })
  const coveredLiveIds = new Set<string>()
  for (const livePart of live.parts) {
    if (liveById.has(livePart.id) && merged.some((p) => p.id === livePart.id)) {
      coveredLiveIds.add(livePart.id)
      continue
    }
    // Already folded as a placeholder into a server row.
    if (
      merged.some((serverPart) => findPlaceholderForServerPart([livePart], serverPart) === livePart)
    ) {
      coveredLiveIds.add(livePart.id)
      continue
    }
  }

  for (const livePart of live.parts) {
    if (coveredLiveIds.has(livePart.id)) continue
    if (liveById.has(livePart.id) && merged.some((p) => p.id === livePart.id)) continue
    // Text placeholders fold into the running server text slot.
    if (livePart.type === 'text' && foldLiveTextIntoServerSlot(merged, livePart)) continue
    // Reasoning placeholders fold by step + prefix.
    if (livePart.type === 'reasoning' && foldLiveReasoningIntoServerSlot(merged, livePart)) continue
    // Same step content under a new server id: dedupe.
    if (STEP_IDENTITY_TYPES.has(livePart.type) && isSameStepContent(merged, livePart)) continue
    // Tool placeholders matched by call_id were already folded above when
    // the server row existed; a live-only tool (server behind) is kept.
    merged.push(livePart)
  }

  server.parts = sortHarnessParts(merged)
  // Content mirrors the text stream: keep the longest known answer for
  // the same turn (stale snapshot must not truncate it).
  if (live.content.length > server.content.length) {
    server.content = live.content
  }
}

function furthestPartState(a: HarnessPartState, b: HarnessPartState): HarnessPartState {
  const rank: Record<HarnessPartState, number> = {
    pending: 0,
    running: 1,
    completed: 2,
    error: 2,
  }
  // Errors are terminal: either side error wins over completed.
  if (a === 'error' || b === 'error') return 'error'
  return rank[b] > rank[a] ? b : a
}

/**
 * Find a local placeholder part for a server row (same logical stream,
 * different id). Tools/subtasks match by call/subtask id; text/reasoning
 * match by prefix-related output (local vs server ids diverge).
 */
function findPlaceholderForServerPart(
  liveParts: HarnessPart[],
  serverPart: HarnessPart,
): HarnessPart | undefined {
  for (const live of liveParts) {
    if (!isLocalId(live.id)) continue
    if (live.type !== serverPart.type) continue
    if (live.type === 'tool') {
      if (live.call_id && serverPart.call_id && live.call_id === serverPart.call_id) return live
      continue
    }
    if (live.type === 'subtask') {
      const liveSub = live.meta?.['subtask_id']
      const serverSub = serverPart.meta?.['subtask_id']
      if (typeof liveSub === 'string' && liveSub && liveSub === serverSub) return live
      continue
    }
    if (live.type === 'text') {
      const liveOut = live.output ?? ''
      const serverOut = serverPart.output ?? ''
      if (!liveOut || !serverOut) continue
      if (serverOut.startsWith(liveOut) || liveOut.startsWith(serverOut)) return live
      continue
    }
    if (live.type === 'reasoning') {
      if (partStepNumber(live) !== partStepNumber(serverPart)) continue
      const liveOut = live.output ?? ''
      const serverOut = serverPart.output ?? ''
      if (serverOut === liveOut || serverOut.startsWith(liveOut) || liveOut.startsWith(serverOut)) {
        return live
      }
      continue
    }
  }
  return undefined
}

function foldLiveTextIntoServerSlot(merged: HarnessPart[], live: HarnessPart): boolean {
  const liveOutput = live.output ?? ''
  if (!liveOutput) return false
  const slot = merged.find((part) => part.type === 'text' && part.state === 'running')
  if (!slot) return false
  const serverOutput = slot.output ?? ''
  if (serverOutput === liveOutput) return true
  if (liveOutput.startsWith(serverOutput) || serverOutput.startsWith(liveOutput)) {
    if (liveOutput.length > serverOutput.length) {
      slot.output = liveOutput
      slot.state = furthestPartState(slot.state, live.state)
    }
    return true
  }
  return false
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
function foldLiveReasoningIntoServerSlot(merged: HarnessPart[], live: HarnessPart): boolean {
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
    slot.state = furthestPartState(slot.state, live.state)
    if (live.title) slot.title = live.title
  } else if (serverOutput.length === liveOutput.length) {
    slot.state = furthestPartState(slot.state, live.state)
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

/**
 * Keep locally streamed turns when a mid-run `fetchParts` snapshot races
 * the live socket deltas. Idle fetches should skip this and replace fully.
 *
 * Reconciliation is by message/part ID and position order — never by
 * comparing content lengths across turns. Same-turn rows merge (longest
 * known output wins per row so stale snapshots preserve already-received
 * data); optimistic local rows (`local-*`) the server has not echoed yet
 * are carried over; server-echoed locals are dropped in favor of the
 * server ids. Messages and parts sort by `position` with creation-order
 * fallback.
 */
export function mergeBusyFetchedMessages(
  previous: HarnessMessage[],
  incoming: HarnessMessage[],
): HarnessMessage[] {
  if (previous.length === 0) return sortHarnessMessages(incoming)
  if (incoming.length === 0) return previous

  const prevById = new Map(previous.map((m) => [m.id, m]))
  const incomingIds = new Set(incoming.map((m) => m.id))

  // Same-turn merges (ids match): preserve already-streamed data when the
  // snapshot is stale.
  for (const serverMsg of incoming) {
    const live = prevById.get(serverMsg.id)
    if (!live) continue
    if (live.role !== 'assistant' || serverMsg.role !== 'assistant') {
      // User rows: keep the server echo (positions/ids authoritative).
      continue
    }
    if (live.completed_at != null && serverMsg.completed_at != null) continue
    mergeAssistantParts(live, serverMsg)
  }

  // Live-only rows the server has not echoed yet (optimistic follow-up
  // user, local assistant shell, or live-only streamed parts on a new
  // server id). Server-echoed locals (same content, server id present)
  // are dropped to avoid duplicates.
  const extras: HarnessMessage[] = []
  for (const live of previous) {
    if (incomingIds.has(live.id)) continue
    if (!isLocalId(live.id)) {
      // A server-id row missing from the snapshot is a stale-snapshot
      // hole (or a deleted suffix after edit/fork): keep completed rows
      // so already-received data is not lost mid-run.
      if (live.role === 'assistant' || live.role === 'user') extras.push(live)
      continue
    }
    if (live.role === 'user') {
      // Legacy optimistic user: only pair with a newly echoed user row
      // *after* the last user known before this snapshot. Comparing against
      // any identical prompt would erase a valid repeated follow-up.
      const lastKnownUserPosition = Math.max(
        -1,
        ...previous
          .filter((m) => m.role === 'user' && !isLocalId(m.id))
          .map((m) => messagePosition(m) ?? -1),
      )
      const echoed = incoming.some(
        (m) =>
          m.role === 'user' &&
          !isLocalId(m.id) &&
          m.content === live.content &&
          (messagePosition(m) ?? -1) > lastKnownUserPosition,
      )
      if (!echoed) extras.push(live)
      continue
    }
    // A legacy local assistant shell can be folded into a matching
    // running server turn below.
    extras.push(live)
  }

  // Same-turn id mismatch (local shell vs fresh server id): the trailing
  // local assistant and the trailing server assistant are one turn — fold
  // instead of duplicating.
  const localAssistants = extras.filter((m) => m.role === 'assistant' && isLocalId(m.id))
  const serverAssistants = incoming.filter((m) => m.role === 'assistant' && !isLocalId(m.id))
  if (localAssistants.length === 1 && serverAssistants.length > 0) {
    const local = localAssistants[0]!
    const serverLast = serverAssistants[serverAssistants.length - 1]!
    // Only fold an uncompleted legacy placeholder into the uncompleted
    // server turn; even an empty local shell must not appear twice.
    if (local.completed_at == null && serverLast.completed_at == null) {
      mergeAssistantParts(local, serverLast)
      const idx = extras.indexOf(local)
      if (idx !== -1) extras.splice(idx, 1)
    }
  }

  const combined = sortHarnessMessages([...incoming, ...extras])
  // Keep part order deterministic for every message.
  for (const message of combined) {
    message.parts = sortHarnessParts(message.parts)
  }
  // Also normalize incoming-only part order when no extras existed.
  if (extras.length === 0) {
    for (const message of incoming) {
      message.parts = sortHarnessParts(message.parts)
    }
  }
  return combined
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
    message.parts = sortHarnessParts(message.parts)
  }
  return sortHarnessMessages(messages)
}
