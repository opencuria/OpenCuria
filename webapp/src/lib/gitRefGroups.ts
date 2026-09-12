/**
 * Grouping of local + remote branch ref tags, mirroring vscode-git-graph's
 * `getBranchLabels` (web/main.ts): a remote ref `origin/main` is grouped
 * with the local branch `main` when `remote.name.substring(remote.length+1)
 * === localName`. The result renders as ONE badge — local name plus one
 * italic chip per remote carrying only the remote name.
 */

import type { GitRefTag } from '@/types/git'

/** Parsed remote ref: remote name + branch name without the remote prefix. */
export interface ParsedRemoteRef {
  remote: string
  branch: string
}

/** One inner remote chip of a group badge. `full` is the full ref name. */
export interface GitRefRemoteChip {
  full: string
  remote: string
}

/**
 * One rendered badge. `local` is null for remote-only badges. HEAD
 * pseudo-tags always form their own group (`isHead: true`, no remotes).
 */
export interface GitRefGroup {
  key: string
  local: { name: string; current: boolean } | null
  remotes: GitRefRemoteChip[]
  isHead: boolean
}

/**
 * Split a remote ref `full` (e.g. `origin/feature/foo`) into remote +
 * branch. The longest known-remote prefix wins (protects
 * `origin/feature/foo` → `feature/foo`); otherwise the first path segment
 * is used as a fallback so unknown remotes still parse.
 */
export function parseRemoteRef(
  full: string,
  knownRemotes: string[],
): ParsedRemoteRef | null {
  const trimmed = full.trim()
  if (!trimmed) return null
  const sorted = [...knownRemotes]
    .filter((r) => r.trim() !== '')
    .sort((a, b) => b.length - a.length)
  for (const remote of sorted) {
    if (trimmed === remote) return { remote, branch: '' }
    if (trimmed.startsWith(`${remote}/`)) {
      return { remote, branch: trimmed.slice(remote.length + 1) }
    }
  }
  const slash = trimmed.indexOf('/')
  if (slash <= 0 || slash === trimmed.length - 1) return null
  return { remote: trimmed.slice(0, slash), branch: trimmed.slice(slash + 1) }
}

/**
 * Group ref tags into badges. A remote tag joins a local group only when
 * its remote is known AND the remaining branch name matches the local
 * branch name exactly (no suffix guessing — `fork/main` never matches
 * `main` unless `fork` is a known remote). Unknown / non-matching remotes
 * stay as standalone remote-only badges. HEAD pseudo-tags always stay
 * separate. The current (checked-out) branch sorts first.
 */
export function groupRefTags(
  tags: GitRefTag[],
  knownRemotes: string[],
): GitRefGroup[] {
  const known = new Set(knownRemotes)
  const groups: GitRefGroup[] = []
  const locals = new Map<string, { name: string; current: boolean }>()

  for (const tag of tags) {
    if (tag.name === 'HEAD') {
      groups.push({
        key: `head:${tag.name}`,
        local: { name: tag.name, current: true },
        remotes: [],
        isHead: true,
      })
      continue
    }
    if (!tag.remote) {
      const existing = locals.get(tag.name)
      if (existing) {
        existing.current = existing.current || tag.current
      } else {
        locals.set(tag.name, { name: tag.name, current: tag.current })
      }
    }
  }

  const grouped = new Map<string, GitRefRemoteChip[]>()
  const standalone: GitRefRemoteChip[] = []
  for (const tag of tags) {
    if (tag.name === 'HEAD' || !tag.remote) continue
    const parsed = parseRemoteRef(tag.name, knownRemotes)
    if (
      parsed &&
      parsed.branch !== '' &&
      known.has(parsed.remote) &&
      locals.has(parsed.branch)
    ) {
      const list = grouped.get(parsed.branch) ?? []
      if (!list.some((c) => c.full === tag.name)) {
        list.push({ full: tag.name, remote: parsed.remote })
      }
      grouped.set(parsed.branch, list)
    } else {
      if (!standalone.some((c) => c.full === tag.name)) {
        standalone.push({
          full: tag.name,
          remote: parsed?.remote ?? '',
        })
      }
    }
  }

  const localGroups: GitRefGroup[] = [...locals.values()].map((local) => ({
    key: `local:${local.name}`,
    local,
    remotes: (grouped.get(local.name) ?? []).sort((a, b) =>
      a.remote.localeCompare(b.remote),
    ),
    isHead: false,
  }))
  const remoteGroups: GitRefGroup[] = standalone
    .sort((a, b) => a.full.localeCompare(b.full))
    .map((chip) => ({
      key: `remote:${chip.full}`,
      local: null,
      remotes: [chip],
      isHead: false,
    }))

  const all = [...groups, ...localGroups, ...remoteGroups]
  // Active branch first (vscode-git-graph prepends the checked-out label),
  // then alphabetically for stable rendering.
  const headGroups = all.filter((g) => g.isHead)
  const rest = all
    .filter((g) => !g.isHead)
    .sort((a, b) => {
      const aCurrent = a.local?.current === true ? 0 : 1
      const bCurrent = b.local?.current === true ? 0 : 1
      if (aCurrent !== bCurrent) return aCurrent - bCurrent
      const aName = a.local?.name ?? a.remotes[0]?.full ?? a.key
      const bName = b.local?.name ?? b.remotes[0]?.full ?? b.key
      return aName.localeCompare(bName)
    })
  return [...headGroups, ...rest]
}
