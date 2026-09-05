import type { FileNode } from '@/types'

/**
 * Static agent names for `@agent:` mentions (mirrors the backend harness
 * agent definitions: `build`/`plan` primary, `general`/`explore` subagents).
 */
export const HARNESS_AGENT_NAMES = ['build', 'plan', 'general', 'explore'] as const

/** Max file rows shown in the `@` picker. */
export const MENTION_FILE_LIMIT = 8
/** Max combined agent+file rows shown in the `@` picker. */
export const MENTION_TOTAL_LIMIT = 10
/** Runner search cap requested when typing `@`. */
export const MENTION_FIND_LIMIT = 50

const WORKSPACE_ROOT = '/workspace'

export interface MentionCandidate {
  kind: 'file' | 'agent' | 'skill'
  /** Display label (rendered in the suggestion list). */
  label: string
  /** Token inserted after `@`, or skill id for `kind: 'skill'`. */
  insert: string
}

/** Collect all file paths (excluding directories) from a file-explorer tree. */
export function flattenFilePaths(nodes: FileNode[]): string[] {
  const out: string[] = []
  const walk = (list: FileNode[]): void => {
    for (const node of list) {
      if (node.type === 'directory') {
        if (node.children) walk(node.children)
        continue
      }
      out.push(node.path)
    }
  }
  walk(nodes)
  return out
}

/** Strip `/workspace/` so mention labels stay readable. */
export function workspaceRelativePath(path: string): string {
  if (path === WORKSPACE_ROOT) return path
  const prefix = `${WORKSPACE_ROOT}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}

/**
 * File search string sent to `files:find`, or `null` when the query is
 * agent-only (`agent:` prefix).
 */
export function mentionFileSearchQuery(query: string): string | null {
  const lower = query.toLowerCase()
  if (lower.startsWith('agent:')) return null
  if (lower.startsWith('file:')) return query.slice('file:'.length)
  return query
}

/** Deduplicate search hits and already-loaded explorer paths. */
export function mergeMentionFilePaths(
  searchPaths: string[],
  treePaths: string[],
): string[] {
  const merged: string[] = []
  const seen = new Set<string>()
  for (const path of [...searchPaths, ...treePaths]) {
    if (seen.has(path)) continue
    seen.add(path)
    merged.push(path)
  }
  return merged
}

function fileMatchScore(path: string, query: string): number {
  if (!query) return 3
  const q = query.toLowerCase()
  const name = (path.split('/').pop() ?? '').toLowerCase()
  const relative = workspaceRelativePath(path).toLowerCase()
  if (name.startsWith(q)) return 0
  if (name.includes(q)) return 1
  if (relative.includes(q) || path.toLowerCase().includes(q)) return 2
  return 99
}

/**
 * Filter mention candidates for the current `@` query.
 *
 * `query` is the raw text after `@` (may be empty). `file:`-prefixed queries
 * only match files, `agent:`-prefixed queries only match agents; otherwise
 * both kinds are offered.
 */
export function filterMentionCandidates(
  query: string,
  filePaths: string[],
): MentionCandidate[] {
  const q = query.toLowerCase()
  const wantsFiles = !q.startsWith('agent:')
  const wantsAgents = !q.startsWith('file:')
  const fileQuery = q.startsWith('file:') ? q.slice('file:'.length) : q
  const agentQuery = q.startsWith('agent:') ? q.slice('agent:'.length) : q

  const files: MentionCandidate[] = wantsFiles
    ? filePaths
        .filter((path) => {
          if (!fileQuery) return true
          const relative = workspaceRelativePath(path).toLowerCase()
          const name = (path.split('/').pop() ?? '').toLowerCase()
          return (
            name.includes(fileQuery) ||
            relative.includes(fileQuery) ||
            path.toLowerCase().includes(fileQuery)
          )
        })
        .sort((left, right) => {
          const scoreDelta = fileMatchScore(left, fileQuery) - fileMatchScore(right, fileQuery)
          if (scoreDelta !== 0) return scoreDelta
          return workspaceRelativePath(left).localeCompare(workspaceRelativePath(right))
        })
        .slice(0, MENTION_FILE_LIMIT)
        .map((path) => ({
          kind: 'file' as const,
          label: workspaceRelativePath(path),
          insert: `file:${path}`,
        }))
    : []
  const agents: MentionCandidate[] = wantsAgents
    ? HARNESS_AGENT_NAMES.filter((name) => name.toLowerCase().includes(agentQuery))
        .slice(0, 8)
        .map((name) => ({ kind: 'agent', label: `@agent:${name}`, insert: `agent:${name}` }))
    : []
  return [...agents, ...files].slice(0, MENTION_TOTAL_LIMIT)
}

/** Extract the `@` query directly before `cursor`, or null when absent. */
export function detectMentionQuery(text: string, cursor: number): string | null {
  const match = /(^|\s)@([a-zA-Z0-9_/:.+-]*)$/.exec(text.slice(0, cursor))
  return match ? (match[2] ?? '') : null
}

/** Replace the active `@query` before `cursor` with the chosen candidate. */
export function applyMentionCandidate(
  text: string,
  cursor: number,
  candidate: MentionCandidate,
): { text: string; cursor: number } {
  const rewritten = text
    .slice(0, cursor)
    .replace(/(^|\s)@[a-zA-Z0-9_/:.+-]*$/, `$1@${candidate.insert} `)
  return { text: `${rewritten}${text.slice(cursor)}`, cursor: rewritten.length }
}

/** Extract the `/` skill query directly before *cursor*, or null when absent. */
export function detectSlashQuery(text: string, cursor: number): string | null {
  const match = /(^|\s)\/([a-zA-Z0-9_.+-]*)$/.exec(text.slice(0, cursor))
  return match ? (match[2] ?? '') : null
}

/**
 * Filter skill candidates for the current `/` query.
 *
 * `query` is the raw text after `/` (may be empty).
 */
export function filterSkillCandidates(
  query: string,
  skills: Array<{ id: string; name: string }>,
): MentionCandidate[] {
  const q = query.toLowerCase()
  return skills
    .filter(
      (skill) =>
        skill.name.toLowerCase().includes(q) || skill.id.toLowerCase().includes(q),
    )
    .slice(0, 10)
    .map((skill) => ({ kind: 'skill' as const, label: skill.name, insert: skill.id }))
}

/** Remove the active `/query` before *cursor* without inserting a token. */
export function consumeSlashQuery(
  text: string,
  cursor: number,
): { text: string; cursor: number } {
  const rewritten = text.slice(0, cursor).replace(/(^|\s)\/[a-zA-Z0-9_.+-]*$/, '$1')
  return { text: `${rewritten}${text.slice(cursor)}`, cursor: rewritten.length }
}

/**
 * Ignore pointer hover until the cursor actually moves.
 *
 * Keyboard navigation (and the scroll it causes) can fire `mousemove` on the
 * item under a stationary cursor; those events reuse the last client position.
 */
export function createPointerHoverGate(): {
  moved: (event: { clientX: number; clientY: number }) => boolean
  reset: () => void
} {
  let x = Number.NaN
  let y = Number.NaN
  return {
    moved(event) {
      if (event.clientX === x && event.clientY === y) return false
      x = event.clientX
      y = event.clientY
      return true
    },
    reset() {
      x = Number.NaN
      y = Number.NaN
    },
  }
}
