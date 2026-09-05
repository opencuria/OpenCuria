/**
 * Subagent timeline helpers: type labels, child-tool activity, and
 * whether a parent message still has live tool/subtask work.
 */

import type { HarnessMessage, HarnessPart } from '@/types/harness'

const SUBAGENT_TYPE_LABELS: Record<string, string> = {
  explore: 'Explorer',
  general: 'General',
}

/** Display label for a subagent type (`explore` → `Explorer`). */
export function formatSubagentType(agent: string | null | undefined): string | null {
  if (!agent) return null
  const key = agent.trim().toLowerCase()
  if (!key) return null
  const mapped = SUBAGENT_TYPE_LABELS[key]
  if (mapped) return mapped
  return key.charAt(0).toUpperCase() + key.slice(1)
}

/**
 * True when this tool part is the parent `task` invocation that duplicates
 * a sibling subtask card (`tool === 'task'` or title `Subagent: …`).
 */
export function isTaskToolPart(part: HarnessPart): boolean {
  if (part.type !== 'tool') return false
  const named = (part.tool || '').trim().toLowerCase()
  if (named === 'task') return true
  const fromInput = part.input?.['tool']
  if (typeof fromInput === 'string' && fromInput.trim().toLowerCase() === 'task') {
    return true
  }
  return (part.title || '').startsWith('Subagent:')
}

/** Latest running tool part in child-session messages (newest last). */
export function latestRunningChildTool(messages: HarnessMessage[]): HarnessPart | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (!message) continue
    for (let j = message.parts.length - 1; j >= 0; j -= 1) {
      const part = message.parts[j]
      if (part?.type === 'tool' && part.state === 'running') {
        return part
      }
    }
  }
  return null
}

/** Child session ids for still-running subtask parts. */
export function collectRunningChildSessionIds(messages: HarnessMessage[]): string[] {
  const ids: string[] = []
  const seen = new Set<string>()
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type !== 'subtask' || part.state !== 'running') continue
      const childId = part.meta?.['child_session_id']
      if (typeof childId !== 'string' || !childId || seen.has(childId)) continue
      seen.add(childId)
      ids.push(childId)
    }
  }
  return ids
}

/**
 * Subtitle under the subagent title: current child tool, or a terminal status.
 * Running with no child tool yet returns null.
 */
export function subtaskActivityLabel(
  part: HarnessPart,
  childMessages: HarnessMessage[],
): string | null {
  if (part.state === 'completed') return 'Completed'
  if (part.state === 'error') return 'Failed'
  if (part.state !== 'running') return null
  const tool = latestRunningChildTool(childMessages)
  const label = (tool?.title || tool?.tool || '').trim()
  return label || null
}

/** True when a tool or subtask part is still running (blocks Thinking). */
export function hasRunningToolOrSubtask(parts: HarnessPart[]): boolean {
  return parts.some(
    (part) => (part.type === 'tool' || part.type === 'subtask') && part.state === 'running',
  )
}
