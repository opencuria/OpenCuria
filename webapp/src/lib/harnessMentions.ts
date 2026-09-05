import type { FileNode } from '@/types'

/**
 * Static agent names for `@agent:` mentions (mirrors the backend harness
 * agent definitions: `build`/`plan` primary, `general`/`explore` subagents).
 */
export const HARNESS_AGENT_NAMES = ['build', 'plan', 'general', 'explore'] as const

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
        .filter((path) => path.toLowerCase().includes(fileQuery))
        .slice(0, 8)
        .map((path) => ({ kind: 'file', label: path, insert: `file:${path}` }))
    : []
  const agents: MentionCandidate[] = wantsAgents
    ? HARNESS_AGENT_NAMES.filter((name) => name.toLowerCase().includes(agentQuery))
        .slice(0, 8)
        .map((name) => ({ kind: 'agent', label: `@agent:${name}`, insert: `agent:${name}` }))
    : []
  return [...agents, ...files].slice(0, 10)
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
