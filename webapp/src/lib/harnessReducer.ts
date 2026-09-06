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
  HarnessSubtaskFinishedEvent,
  HarnessSubtaskStartedEvent,
  HarnessTodo,
  HarnessTodoUpdatedEvent,
} from '@/types/harness'

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

function ensureReasoningPart(message: HarnessMessage, sessionId: string): HarnessPart {
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
    }
    message.parts.push(part)
  }
  return part
}

/** Cost/tokens meta keys forwarded on step-finish parts. */
const STEP_FINISH_META_KEYS = ['cost', 'tokens', 'step'] as const

/**
 * Apply a `harness.part_updated` delta to the running assistant message.
 *
 * Text/reasoning deltas append to the running part; tool_started creates a
 * running tool part; tool_completed/tool_error transition the matching part
 * to completed/error; step_start/step_finish create step marker parts.
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
    const part = ensureReasoningPart(message, sessionId)
    part.output += delta.reasoning
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
    const part = findPart(message, {
      partId: opts.partId,
      callId: delta.call_id,
    })
    if (part) {
      part.state = 'completed'
      if (delta.output) part.output = delta.output
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
        meta: opts.step !== undefined ? { step: opts.step } : {},
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
 */
export function mergeBusyFetchedMessages(
  previous: HarnessMessage[],
  incoming: HarnessMessage[],
): HarnessMessage[] {
  const prevLast = [...previous].reverse().find((m) => m.role === 'assistant')
  const nextLast = [...incoming].reverse().find((m) => m.role === 'assistant')
  if (
    prevLast &&
    nextLast &&
    prevLast.completed_at == null &&
    streamLength(prevLast) > streamLength(nextLast)
  ) {
    nextLast.parts = prevLast.parts
    nextLast.content = prevLast.content
  }
  return incoming
}
