/**
 * Harness block-model types for the M7 frontend chat (M6 backend contracts).
 *
 * Mirrors `backend/apps/harness/models.py` (HarnessSession, HarnessMessage,
 * HarnessPart, Todo, PermissionRequest) and the frontend socket payloads
 * emitted by `HarnessService` (`harness.part_updated`,
 * `harness.permission_required`, `harness.session_status`,
 * `harness.todo_updated`, `harness.subtask_started/finished`).
 *
 * Additive only: the legacy `Session` type in `types/index.ts` stays
 * untouched (M8 removes it).
 */

// --- Block model -----------------------------------------------------------

/** Persisted part types (`HarnessPartType` in the backend). */
export type HarnessPartType =
  | 'text'
  | 'reasoning'
  | 'tool'
  | 'step-start'
  | 'step-finish'
  | 'subtask'
  | 'patch'
  | 'agent'

/** Lifecycle states of a part (`HarnessPartState` in the backend). */
export type HarnessPartState = 'pending' | 'running' | 'completed' | 'error'

/** One streamed block (text/tool/step/subtask) of an assistant message. */
export interface HarnessPart {
  id: string
  message_id?: string
  session_id: string
  type: HarnessPartType
  state: HarnessPartState
  call_id?: string
  /** Tool name for tool parts (kept in `input.tool` by the backend). */
  tool?: string
  title: string
  input?: Record<string, unknown>
  output: string
  meta?: Record<string, unknown>
}

/** One user prompt or assistant answer inside a harness session. */
export interface HarnessMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  model?: string
  cost?: number
  tokens?: Record<string, number>
  finish?: string
  error?: string
  parts: HarnessPart[]
  created_at?: string
  completed_at?: string | null
}

/** Execution mode of a harness session. */
export type HarnessSessionMode = 'plan' | 'build'

/** Lifecycle state of a harness session. */
export type HarnessSessionStatus = 'busy' | 'idle'

/** One persistent agent conversation bound to a workspace. */
export interface HarnessSession {
  id: string
  workspace_id: string
  parent_id?: string | null
  title: string
  mode: HarnessSessionMode
  agent_name: string
  model: string
  status: HarnessSessionStatus
  cost: number
  tokens: Record<string, number>
  created_at?: string
  updated_at?: string
}

/** One persistent todo entry of a harness session. */
export interface HarnessTodo {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
  priority: string
  order: number
}

/** A pending tool-permission gate surfaced to the user. */
export interface HarnessPermissionRequest {
  request_id: string
  session_id: string
  workspace_id: string
  tool: string
  /** The matched action/pattern preview (e.g. the shell command). */
  pattern: string
  title: string
  call_id?: string
  status?: 'pending' | 'approved' | 'rejected'
}

/** Permission resolution choice (M6 `HarnessPermissionResolveIn`). */
export type HarnessPermissionResponse = 'once' | 'always' | 'reject'

// --- REST payloads (M6 `backend/apps/harness/api.py`) ----------------------

export interface HarnessSessionCreateIn {
  prompt: string
  agent_name?: string
  mode?: HarnessSessionMode
  model?: string
}

export interface HarnessPartsOut {
  session: HarnessSession
  messages: HarnessMessage[]
}

// --- Socket event payloads (M6 `HarnessService` emit shapes) ----------------

/** Delta map inside `harness.part_updated` (exactly one key per emit). */
export interface HarnessPartDelta {
  text?: string
  reasoning?: string
  step_start?: number
  step_finish?: number
  tool_started?: string
  tool_completed?: string
  tool_error?: string
  title?: string
  call_id?: string
  output?: string
}

export interface HarnessPartUpdatedEvent {
  workspace_id: string
  session_id: string
  delta: HarnessPartDelta
  step?: number
  part_id?: string
}

export interface HarnessPermissionRequiredEvent {
  workspace_id: string
  session_id: string
  request_id?: string
  tool: string
  pattern: string
  title: string
  call_id?: string
}

export interface HarnessPermissionResolvedEvent {
  workspace_id: string
  session_id: string
  request_id: string
  decision: string
  remember: string
}

export interface HarnessSessionStatusEvent {
  workspace_id: string
  session_id: string
  status: HarnessSessionStatus
}

export interface HarnessTodoUpdatedEvent {
  workspace_id: string
  session_id: string
  todos: HarnessTodo[]
  step?: number
}

export interface HarnessSubtaskStartedEvent {
  workspace_id: string
  session_id: string
  subtask_id: string
  agent: string
  description: string
  part_id?: string
}

export interface HarnessSubtaskFinishedEvent {
  workspace_id: string
  session_id: string
  subtask_id: string
  agent?: string
  status: string
  summary: string
}
