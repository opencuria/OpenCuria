/**
 * Subagent timeline helpers: type labels, child-tool activity, and
 * whether a parent message still has live tool/subtask work.
 */

import type { HarnessMessage, HarnessPart, HarnessSession } from '@/types/harness'

const SUBAGENT_TYPE_LABELS: Record<string, string> = {
  explore: 'Explorer',
  general: 'General',
  computeruse: 'Computer use',
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

/** Display label for the known subagent types that own child sessions. */
export function gateSourceLabel(agentName: string | null | undefined): string | null {
  const key = (agentName || '').trim().toLowerCase()
  if (key === 'explore' || key === 'general' || key === 'computeruse') {
    return formatSubagentType(key)
  }
  return null
}

/**
 * *rootId* plus every descendant session id (parent_id chain), breadth-first.
 */
export function collectDescendantSessionIds(
  rootId: string,
  sessions: HarnessSession[],
): string[] {
  const ids: string[] = [rootId]
  const seen = new Set<string>([rootId])
  let added = true
  while (added) {
    added = false
    for (const session of sessions) {
      if (!session.parent_id || seen.has(session.id) || !seen.has(session.parent_id)) {
        continue
      }
      seen.add(session.id)
      ids.push(session.id)
      added = true
    }
  }
  return ids
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
 * Resolve a subtask part to its child session id.
 *
 * Prefers `meta.child_session_id`, then a known map (by subtask id or part
 * id), then the only child of the parent, then a unique title match.
 * Child session titles are often rewritten by title generation, so a
 * single child of the parent is enough even when titles differ.
 */
export function resolveChildSessionId(
  part: HarnessPart,
  sessions: HarnessSession[],
  knownIds: Record<string, string> = {},
): string | null {
  const fromMeta = part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta.trim()) return fromMeta.trim()
  const subtaskId = String(part.meta?.['subtask_id'] ?? '')
  if (subtaskId && knownIds[subtaskId]) return knownIds[subtaskId]!
  if (knownIds[part.id]) return knownIds[part.id]!
  const parentId = part.session_id
  if (!parentId) return null
  const children = sessions.filter((session) => session.parent_id === parentId)
  if (children.length === 1) return children[0]!.id
  const title = (part.title || '').trim()
  if (!title) return null
  const matches = children.filter((session) => (session.title || '').trim() === title)
  if (matches.length === 1) return matches[0]!.id
  return null
}

function sessionCreatedAt(session: HarnessSession): number {
  return session.created_at ? new Date(session.created_at).getTime() : 0
}

function rememberChildId(
  map: Record<string, string>,
  part: HarnessPart,
  childId: string,
): void {
  const subtaskId = part.meta?.['subtask_id']
  if (typeof subtaskId === 'string' && subtaskId) map[subtaskId] = childId
  map[part.id] = childId
}

/** Map subtask id / part id → child session id for parent timeline cards. */
export function buildChildSessionIdMap(
  sessions: HarnessSession[],
  messagesBySession: Record<string, HarnessMessage[]>,
): Record<string, string> {
  const map: Record<string, string> = {}
  const usedChildIds = new Set<string>()
  const partsByParent: Record<string, HarnessPart[]> = {}

  for (const session of sessions) {
    for (const message of messagesBySession[session.id] ?? []) {
      for (const part of message.parts) {
        if (part.type !== 'subtask') continue
        const parentId = part.session_id || session.id
        if (!partsByParent[parentId]) partsByParent[parentId] = []
        partsByParent[parentId]!.push(part)
        const fromMeta = part.meta?.['child_session_id']
        if (typeof fromMeta === 'string' && fromMeta.trim()) {
          rememberChildId(map, part, fromMeta.trim())
          usedChildIds.add(fromMeta.trim())
        }
      }
    }
  }

  const childrenByParent: Record<string, HarnessSession[]> = {}
  for (const session of sessions) {
    if (!session.parent_id) continue
    if (!childrenByParent[session.parent_id]) childrenByParent[session.parent_id] = []
    childrenByParent[session.parent_id]!.push(session)
  }
  for (const children of Object.values(childrenByParent)) {
    children.sort((a, b) => {
      const delta = sessionCreatedAt(a) - sessionCreatedAt(b)
      if (delta !== 0) return delta
      return a.id.localeCompare(b.id)
    })
  }

  for (const [parentId, parts] of Object.entries(partsByParent)) {
    const unmatchedParts = parts.filter((part) => !map[part.id])
    const unmatchedChildren = (childrenByParent[parentId] ?? []).filter(
      (child) => !usedChildIds.has(child.id),
    )
    const leftoverParts: HarnessPart[] = []
    for (const part of unmatchedParts) {
      const title = (part.title || '').trim()
      const matches = title
        ? unmatchedChildren.filter(
            (child) => !usedChildIds.has(child.id) && (child.title || '').trim() === title,
          )
        : []
      if (matches.length === 1) {
        rememberChildId(map, part, matches[0]!.id)
        usedChildIds.add(matches[0]!.id)
        continue
      }
      leftoverParts.push(part)
    }
    const leftoverChildren = unmatchedChildren.filter((child) => !usedChildIds.has(child.id))
    const paired = Math.min(leftoverParts.length, leftoverChildren.length)
    for (let index = 0; index < paired; index += 1) {
      rememberChildId(map, leftoverParts[index]!, leftoverChildren[index]!.id)
      usedChildIds.add(leftoverChildren[index]!.id)
    }
  }

  return map
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
